"""Preflight checks before a lesion network mapping run, and post-run output assertions (numpy only).

Before any Map, cohort or pipeline run, :func:`run_preflight` shows exactly what will be analysed and
what is wrong, with a fix for each problem (SPEC_LNM_ADDENDUM §3.4, §3.10, F10 / RW-09). After every
run, :func:`assert_run_outputs` makes "done" mean finite, valid, correctly headed outputs of the
requested N.

**Header-only.** Lesion metrics come from participants.tsv (volume_ml, fractional_sum,
outside_brain_pct, qc_flags), trusted while the row's lesion_mtime and lesion_size match the file
(the metric cache of the L-01 registry, written by the Studio session). Each lesion costs one
``os.stat`` and one NIfTI header read. Only a row whose cache key is stale or missing (a warn
"metrics stale" with the fix "re-measure") has its voxel data read, through :func:`measure_lesion`.
Files are hashed only to find identical lesions (only within groups of files of equal size) and to
see whether a native lesion changed after normalization. Hashes are kept per (path, size, mtime) in
the process and in ``<study>/.studio/cache/lesion_sha256.json``; a preflight hashes at most
:data:`HASH_BUDGET_BYTES` of files that are not in that cache and defers the rest to later preflights
(an info "hash_deferred"), so uncompressed lesions of one grid (all of equal size) keep it fast.

**Checks** (:class:`Check`: id, level "block" | "warn" | "info", subjects, message, fix
{id, label, params}, scope "subject" | "run", excludes):

- Subject-level blocks exclude only their subjects, with the reason (``strict=True`` makes them
  block the run): lesion file missing, unreadable or not 3D; empty or sub-voxel lesion
  (fractional_sum < 1 connectome voxel).
- Run-level blocks stop the run (CLI exit code :data:`EXIT_BLOCKED`): data use terms not accepted;
  mixed space families without a transform; fewer than 3 subjects in a contrast group; not enough
  disk; blocking design checks (``design.build_design``, shared, not duplicated); no eligible lesion.
- Warnings: metrics stale; space family or tracing template other than the connectome's (fix:
  transform); sform/qform disagreement > 0.1 mm; non-binary masks without --probabilistic; > 20%
  outside the brain or connectome mask; duplicate source ids or identical lesion files; unreviewed or
  auto-segmented unreviewed lesions, native-space lesions and normalized lesions with norm_qc != pass
  (all excluded unless overridden with ``overrides``); a normalized lesion whose native lesion
  changed since normalization (stale); flipped lesions; a 2D-share difference > 20 points between
  groups; missing score values; case reports without record_id and doi; an N-dependent threshold
  rule mismatch; the GSR banner of symmetric |T| rules; warn-level design checks.
- Information: radiological (left-handed) storage; ambiguous grids (2009c/2009a); lesions of unknown
  space against a known connectome; lesions stored as floating point (possibly non-binary); non-ASCII
  paths (official Slicer 5.12.4 needs a UTF-8 locale to read them); rows that are not included
  (include = 0, false or no, as ``Study.analysis_rows`` reads it); hashes deferred to a later
  preflight; info-level design checks.

The report is a JSON-compatible dict; :func:`save_report` writes it to
``<study>/derivatives/qc/preflight-<UTC>.json`` and registers it in the run registry (kind
"preflight"). It holds no absolute local path (SPEC 9.7).

Command line (JSON events, see ``jobs``; exit codes 0 nothing blocks, 1 error, 2 usage, 5 blocked)::

    python -m LesionNetworkMappingLib.preflight --study DIR [--bundle B] [--rule JSON] [--design design.json]
        [--accepted-terms] [--probabilistic] [--strict] [--include-anyway CHECK ...] [--json OUT] [--save]
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import dataclasses
import datetime
import hashlib
import json
import math
import shutil
import time

import numpy as np

from . import geometry, jobs, lesionops, nifti, runs, schema, thresholds
from . import design as designlib
from . import study as studylib

FORMAT_NAME = "slicer-lnm-preflight"
FORMAT_VERSION = 1
LEVELS = ("block", "warn", "info")
SCOPES = ("subject", "run")
#: Exit code of the command line when a check blocks the run (the pipeline's "blocked by preflight")
EXIT_BLOCKED = 5
QC_RELPATH = ("derivatives", "qc")
MIN_PER_CONTRAST_GROUP = 3
OUTSIDE_PCT_WARN = 20.0  # % of the lesion outside the brain or connectome mask
XFORM_MM_MAX = nifti.XFORM_WARNING_MM
MTIME_TOLERANCE_S = 0.0015  # lesion_mtime is stored rounded to 1 ms
#: Bytes per lesion and connectome voxel of one run's outputs: AvgR, AvgFz and T as float32
BYTES_PER_LESION_VOXEL = 4 * 3
#: The per-lesion map cache holds a second copy of the maps
CACHE_COPIES = 1
DISK_HEADROOM_BYTES = 256 * 1024**2
NORMALIZATION_JSON = "normalization.json"
MAX_LISTED = 5
#: sha256 cache of lesion files in a study: {"version", "files": {path relative to the study: [size, mtime_ns, sha256]}}
HASH_CACHE_RELPATH = (studylib.PRIVATE_DIR, "cache", "lesion_sha256.json")
HASH_CACHE_VERSION = 1
#: Bytes of uncached files one preflight may hash (about 0.2 s); the rest waits for the next preflight
HASH_BUDGET_BYTES = 64 * 1024**2
#: NIfTI datatypes of floating-point voxels (float32, float64, float128)
FLOAT_DATATYPES = (16, 64, 1536)

# Fix actions (Check.fix["id"]); the Map Preflight card (L-30) implements them
FIX_LIBRARY = "open-library"  # open the Library filtered to the subjects
FIX_REMEASURE = "re-measure"  # measure the lesion files again (remeasure())
FIX_TRANSFORM = "transform"  # offer the template-family transform (transforms CLI)
FIX_RULE_FWE = "rule-fwe"  # switch the threshold rule to FWE
FIX_RULE_EFFECT = "rule-effect-size"  # an asymmetric or effect-size rule (top-x%, mean Fz)
FIX_ACCEPT_TERMS = "accept-terms"  # scroll to the Connectome card's terms
FIX_INCLUDE = "include-anyway"  # override a warn-level exclusion (recorded in provenance)
FIX_PROBABILISTIC = "probabilistic"  # map fractional lesions with --probabilistic
FIX_FREE_DISK = "free-disk"

# Assertion failure codes (assert_run_outputs)
SIDECAR_FIELDS = {
    "map": ("N", "df", "rule", "disclaimer"),
    "glm": ("N", "df", "rule", "disclaimer"),
    "slnm": ("N", "df", "rule", "disclaimer"),
    "cohort": ("N", "rule", "disclaimer"),
}
DEFAULT_SIDECAR_FIELDS = ("N", "disclaimer")
GROUP_RECORDS = ("glm.json", "slnm.json", "cohort.json")
#: Output keys of group records whose maps are unthresholded statistics (checked for non-zero variance);
#: p, overlap, sensitivity and conjunction maps may legitimately be constant (a null result)
GROUP_STAT_KEYS = ("stat", "effect", "r", "circuit", "legacyWelchT")
HEADER_FIELDS = ("qform_code", "sform_code", "quatern_b", "quatern_c", "quatern_d", "qoffset_x", "qoffset_y", "qoffset_z", "srow_x", "srow_y", "srow_z")
HEADER_ATOL = 1e-4


@dataclasses.dataclass
class Check:
    """One preflight finding.

    :ivar id: check id (e.g. "lesion_missing", "design.vif").
    :ivar level: "block", "warn" or "info".
    :ivar subjects: participant ids concerned (empty for run-level findings).
    :ivar message: what is wrong, in the user's words.
    :ivar fix: {"id", "label", "params"} of the action that fixes it, or None.
    :ivar scope: "subject" (a block excludes only these subjects) or "run".
    :ivar excludes: the subjects are excluded from the run (subject-level blocks, and warn-level
      exclusions that were not overridden).
    :ivar detail: JSON-compatible values behind the message.
    """

    id: str
    level: str
    subjects: list
    message: str
    fix: dict | None = None
    scope: str = "run"
    excludes: bool = False
    detail: object = None

    def to_dict(self):
        return {
            "id": self.id,
            "level": self.level,
            "scope": self.scope,
            "subjects": list(self.subjects),
            "message": self.message,
            "fix": self.fix,
            "excludes": bool(self.excludes),
            "detail": _jsonable(self.detail),
        }


def _fix(fix_id, label, **params):
    return {"id": fix_id, "label": label, "params": _jsonable(params)}


def _jsonable(value):
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _listed(sids, limit=MAX_LISTED):
    sids = list(sids)
    text = ", ".join(sids[:limit])
    return text + (f" and {len(sids) - limit} more" if len(sids) > limit else "")


def _count(n, word, plural=None):
    return f"{n} {word if n == 1 else plural or word + 's'}"


def _number(text):
    return schema._number(text)


def _utc_stamp(moment=None):
    moment = moment or datetime.datetime.now(datetime.UTC)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _is_ascii(text):
    try:
        os.fspath(text).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


# Lesion metrics (the only voxel reads of a preflight)


def metrics_fresh(row, stat):
    """True when the row's cached metrics describe this file (lesion_mtime and lesion_size match ``os.stat``)."""
    mtime, size = _number(row.get("lesion_mtime")), _number(row.get("lesion_size"))
    if mtime is None or size is None or schema.is_missing(row.get("volume_ml")):
        return False
    return int(size) == int(stat.st_size) and abs(mtime - stat.st_mtime) <= MTIME_TOLERANCE_S


def measure_lesion(path, connectome_grid=None, connectome_mask=None, header=None, multifocal=False):
    """Measure a lesion file (reads its voxels): the participants.tsv metric values and the hygiene result.

    :param connectome_grid: the connectome grid (fractional_sum = connectome voxels the lesion fills).
    :param connectome_mask: optional (mask array, affine) for the share of the lesion outside it.
    :return: (values, hygiene): values holds volume_ml, n_parts, fractional_sum (the lesion's sum on the
      connectome grid; None without ``connectome_grid``, since a sum on another grid would later be
      trusted against the connectome), qc_flags codes (list), lesion_mtime and lesion_size; hygiene is
      ``lesionops.hygiene``'s result (outside_pct relative to ``connectome_mask``).
    """
    stat = os.stat(path)
    data, affine, file_header = nifti.read_nifti(path)
    if data.ndim > 3:
        if int(np.prod(data.shape[3:])) != 1:
            raise ValueError(f"The lesion is not a 3D image (shape {data.shape})")
        data = data.reshape(data.shape[:3])
    result = lesionops.hygiene(data, affine, header=header or file_header, connectome_grid=connectome_grid, brain_mask=connectome_mask, multifocal=multifocal)
    fractional_sum = None
    if connectome_grid is not None:
        fractional_sum = result["connectome_sum"] if result["connectome_sum"] is not None else 0.0  # None: an empty lesion
    values = {
        "volume_ml": round(result["volume_ml"], 6),
        "n_parts": result["n_parts"],
        "fractional_sum": None if fractional_sum is None else round(float(fractional_sum), 6),
        "qc_flags": lesionops.qc_flags({"hygiene": result}),
        "lesion_mtime": round(stat.st_mtime, 3),
        "lesion_size": stat.st_size,
    }
    return values, result


def remeasure(study, sids, bundle=None, post=False):
    """The "re-measure" fix: measure the lesions of ``sids`` again (voxel reads).

    :param bundle: the connectome; without it fractional_sum is None (posted as n/a, which clears a
      value measured on another grid; a preflight then estimates it from volume_ml).
    :param post: post the values to the study inbox (``Study.post_results``, source "preflight"); the
      session merges them (single-writer rule: this never writes participants.tsv).
    :return: {sid: values} (qc_flags joined with ";", keeping the row's flags that the hygiene checks
      do not compute, such as side_mismatch, csf and duplicate); unreadable files are left out.
    """
    study = _as_study(study)
    bundle = _as_bundle(bundle)
    grid = bundle.grid if bundle is not None else None
    hygiene_codes = ("nonbinary", "empty", "subvoxel", "outside", "xform", "grid", "multipart")
    out = {}
    for sid in sids:
        row = study.subject(sid)
        path = study.abspath(row.get("lesion"))
        if not path or not os.path.isfile(path):
            continue
        try:
            values, _ = measure_lesion(path, connectome_grid=grid, multifocal=row.get("multifocal") == "1")
        except (OSError, EOFError, ValueError, ArithmeticError, IndexError):
            continue
        kept = [code for code in _flags(row) if code not in hygiene_codes]
        codes = set(kept) | set(values["qc_flags"])
        values["qc_flags"] = ";".join(code for code in lesionops.QC_FLAGS if code in codes) or None
        out[sid] = values
    if post and out:
        studylib.Study.post_results(study.path, out, "preflight")
    return out


def _flags(row):
    text = row.get("qc_flags")
    return [] if schema.is_missing(text) else [code for code in str(text).split(";") if code]


# Inputs


def _as_study(study):
    if isinstance(study, studylib.Study):
        return study
    return studylib.Study.open(study)


def _as_bundle(bundle):
    if bundle is None:
        return None
    if isinstance(bundle, (str, os.PathLike)):
        from .connectome import ConnectomeBundle

        return ConnectomeBundle.open(bundle)
    return bundle


def connectome_family(bundle):
    """Space family of a connectome bundle: its connectome.json "space", else its grid's family, else "unknown"."""
    space = (getattr(bundle, "metadata", None) or {}).get("space")
    if space:
        return geometry.space_family(space)
    match = geometry.detect_grid_candidates(bundle.shape, bundle.affine)
    return match.family if match is not None else "unknown"


def _transform_pairs(transforms):
    """{(image family from, image family to)} of transform files, TransformDirection objects or (from, to) pairs."""
    from . import transforms as transformlib

    pairs = set()
    for item in transforms or ():
        direction = None
        if isinstance(item, (tuple, list)) and len(item) == 2 and not isinstance(item, transformlib.TransformDirection):
            direction = (item[0], item[1])
        elif isinstance(item, transformlib.TransformDirection):
            direction = (item.image_from, item.image_to)
        else:
            try:
                parsed = transformlib.parse_transform_name(item)
            except ValueError:
                parsed = None
            if parsed is not None:
                direction = (parsed.image_from, parsed.image_to)
        if direction is not None:
            pairs.add((geometry.space_family(direction[0]), geometry.space_family(direction[1])))
    return pairs


def cached_transforms():
    """Template-family transforms of the shipped catalogs that are in the download cache (file names)."""
    try:
        from . import fetch
    except ImportError:
        return []
    names = []
    for catalog_name in fetch.CATALOG_NAMES:
        try:
            catalog = fetch.load_catalog(catalog_name)
        except Exception:
            continue
        for resource_id in catalog.ids():
            resource = catalog.get(resource_id)
            if resource.get("kind") != "transform":
                continue
            try:
                if fetch.is_cached(resource):
                    names.append(resource.get("filename") or resource_id)
            except OSError:
                continue
    return names


def _free_bytes(path):
    """Free bytes of the file system holding ``path`` (or its nearest existing parent)."""
    path = os.path.abspath(os.fspath(path))
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return shutil.disk_usage(path).free


def disk_estimate(n_lesions, n_voxels):
    """Bytes a run of ``n_lesions`` lesions needs: L·V·4·3 for the maps, once more for the map cache, plus headroom."""
    return int(n_lesions) * int(n_voxels) * BYTES_PER_LESION_VOXEL * (1 + CACHE_COPIES) + DISK_HEADROOM_BYTES


def _sha256(path, block=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(block):
            digest.update(chunk)
    return digest.hexdigest()


_SHA_CACHE = {}  # (real path, size, mtime_ns) -> sha256, within the process


class _Hasher:
    """sha256 of lesion files through the process and study caches, within a byte budget for new hashes."""

    def __init__(self, study_path, budget=HASH_BUDGET_BYTES):
        self.root = os.path.realpath(study_path)
        self.path = os.path.join(study_path, *HASH_CACHE_RELPATH)
        self.budget = budget
        self.spent = 0
        self.changed = False
        self.deferred = set()  # real paths not hashed because the budget was spent
        self.files = {}
        data = _read_json(self.path)
        if data is not None and data.get("version") == HASH_CACHE_VERSION and isinstance(data.get("files"), dict):
            self.files = data["files"]

    def _relpath(self, real):
        rel = os.path.relpath(real, self.root)
        return None if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel) else rel.replace(os.sep, "/")

    def get(self, path, stat=None):
        """sha256 of a file, or None when hashing it would exceed the budget (the file is then deferred)."""
        real = os.path.realpath(path)
        stat = stat or os.stat(real)
        key = (real, int(stat.st_size), int(stat.st_mtime_ns))
        if key in _SHA_CACHE:
            return _SHA_CACHE[key]
        rel = self._relpath(real)
        entry = self.files.get(rel) if rel else None
        if isinstance(entry, list) and len(entry) == 3 and entry[0] == key[1] and entry[1] == key[2] and isinstance(entry[2], str):
            _SHA_CACHE[key] = entry[2]
            return entry[2]
        if self.budget is not None and self.spent + key[1] > self.budget:
            self.deferred.add(real)
            return None
        sha = _sha256(real)
        self.spent += key[1]
        _SHA_CACHE[key] = sha
        if rel:
            self.files[rel] = [key[1], key[2], sha]
            self.changed = True
        return sha

    def save(self):
        """Write the study cache when new hashes were computed (a read-only study keeps the process cache only)."""
        if not self.changed:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            jobs.write_json_atomic(self.path, {"version": HASH_CACHE_VERSION, "files": self.files})
        except OSError:
            return
        self.changed = False


def _subset_study(study, sids):
    """An in-memory copy of a study that holds only these rows (for the shared design checks; never saved)."""
    keep = set(sids)
    rows = [row for row in study.subjects if row["participant_id"] in keep]
    return studylib.Study(study.path, dict(study.metadata), study.columns, rows, study.dictionary)


# Preflight


class _Collector:
    """Aggregates per-subject findings into one Check per id."""

    def __init__(self):
        self.order = []
        self.items = {}

    def add(self, check_id, level, sid, message_fn, fix=None, scope="subject", excludes=False, detail=None):
        """Add a subject to the check ``check_id`` (created on first use; ``message_fn(subjects, details)`` writes its message)."""
        entry = self.items.get(check_id)
        if entry is None:
            entry = {"level": level, "subjects": [], "message_fn": message_fn, "fix": fix, "scope": scope, "excludes": excludes, "details": {}}
            self.items[check_id] = entry
            self.order.append(check_id)
        if sid is not None and sid not in entry["subjects"]:
            entry["subjects"].append(sid)
        if detail is not None and sid is not None:
            entry["details"][sid] = detail

    def checks(self):
        out = []
        for check_id in self.order:
            entry = self.items[check_id]
            fix = entry["fix"]
            if callable(fix):
                fix = fix(entry["subjects"])
            out.append(Check(check_id, entry["level"], entry["subjects"], entry["message_fn"](entry["subjects"], entry["details"]), fix, entry["scope"], entry["excludes"], entry["details"] or None))
        return out


def _library_fix(label="Show in Library"):
    return lambda sids: _fix(FIX_LIBRARY, label, subjects=sids)


def run_preflight(
    study,
    bundle=None,
    recipe_rule=None,
    design_spec=None,
    accepted_terms=None,
    probabilistic=False,
    *,
    strict=False,
    overrides=(),
    n_subjects=None,
    transforms=None,
    out_dir=None,
    require_review=None,
    hash_budget=HASH_BUDGET_BYTES,
):
    """Check a study (and connectome, rule and design) before a run; header-only for rows with fresh metrics.

    :param study: ``study.Study`` or a study folder.
    :param bundle: ``connectome.ConnectomeBundle`` or a bundle folder (None: the connectome checks are skipped).
    :param recipe_rule: the primary threshold rule (dict or JSON) for the N-dependent and GSR checks.
    :param design_spec: ``design.DesignSpec``, design.json dict or path: its checks are run on the
      subjects that pass this preflight (``design.build_design``).
    :param accepted_terms: True when the bundle's data use terms were accepted, or the manifest
      sha256(s) whose terms were accepted; anything else blocks a run with a bundle.
    :param probabilistic: the run maps fractional lesions (--probabilistic): no non-binary warning.
    :param strict: subject-level blocks (missing, unreadable, not 3D, empty) block the run.
    :param overrides: ids of warn-level exclusions to override ("unreviewed", "auto_unreviewed",
      "native_space", "norm_qc"): the subjects are included and the check says so.
    :param n_subjects: connectome subjects of the run (default: all of the bundle's) for the rule checks.
    :param transforms: template-family transforms available (file names, ``transforms.TransformDirection``
      or (from space, to space) pairs); None looks for cached catalog transforms when needed.
    :param out_dir: where the run writes (default: the study's derivatives folder) for the disk and path checks.
    :param require_review: require review approval (None: ``Study.review_required``).
    :param hash_budget: bytes of files not yet in the hash cache that this preflight may hash (None: no
      limit); files beyond it are listed in the info "hash_deferred" and hashed by later preflights.
    :return: report dict: format, version, created, study (name), bundle {name, manifest_sha256},
      checks (dicts), counts {block, warn, info, subjects, included, excluded}, blocked, included
      [sids], excluded {sid: reason}, measured {sid: values} (rows whose metrics were stale),
      settings and elapsed (s).
    """
    started = time.perf_counter()
    overrides = set(overrides or ())
    run_checks = []
    try:
        study = _as_study(study)
    except (OSError, ValueError) as exc:
        run_checks.append(Check("study_unreadable", "block", [], f"The study cannot be read: {exc}", None, "run"))
        return _report(None, None, run_checks, [], {}, {}, {"strict": strict}, started)
    try:
        bundle = _as_bundle(bundle)
    except (OSError, ValueError) as exc:
        run_checks.append(Check("bundle_unreadable", "block", [], f"The connectome cannot be read: {exc}", None, "run"))
        bundle = None
    review = study.review_required if require_review is None else bool(require_review)
    conn_family = connectome_family(bundle) if bundle is not None else None
    conn_grid = bundle.grid if bundle is not None else None
    conn_voxel_ml = abs(float(np.linalg.det(conn_grid.affine[:3, :3]))) / 1000.0 if conn_grid is not None else None
    grid_answers = study.grid_answers
    collector = _Collector()
    excluded = {}
    measured = {}
    families = {}  # sid -> family of included rows
    lesion_files = {}  # sid -> (path, os.stat result)
    connectome_mask = []  # loaded once, only when a stale row is measured
    outside_against = {}  # sid -> "brain" or "connectome": the mask of the row's outside share
    hasher = _Hasher(study.path, hash_budget)
    deferred = {}  # sid -> what waits for a hash ("duplicates", "normalization")

    def exclude(sid, reason):
        excluded.setdefault(sid, reason)

    def subject_block(check_id, sid, message_fn, reason, fix=None):
        collector.add(check_id, "block", sid, message_fn, fix or _library_fix(), "subject", True)
        exclude(sid, reason)

    def warn_exclusion(check_id, sid, message_fn, reason, fix_label):
        overridden = check_id in overrides

        def message(sids, details, base=message_fn):
            text = base(sids, details)
            return text + (" Included anyway (override recorded)." if overridden else " They are excluded unless you include them anyway.")

        collector.add(check_id, "warn", sid, message, lambda sids: _fix(FIX_INCLUDE, fix_label, check=check_id, subjects=sids), "subject", not overridden)
        if not overridden:
            exclude(sid, reason)

    for row in study.subjects:
        sid = row["participant_id"]
        if not studylib._included(row):  # the analyses' own test (Study.analysis_rows): 0, false, no
            collector.add("include_off", "info", sid, lambda sids, _d: f"{_count(len(sids), 'subject')} with include = 0 {'is' if len(sids) == 1 else 'are'} not analysed: {_listed(sids)}.")
            exclude(sid, studylib.EXCLUDED_INCLUDE)
            continue
        path = study.abspath(row.get("lesion"))
        if not path or not os.path.isfile(path):
            subject_block("lesion_missing", sid, lambda sids, _d: f"{_count(len(sids), 'lesion file')} {'is' if len(sids) == 1 else 'are'} missing: {_listed(sids)}.", "lesion file missing")
            continue
        try:
            stat = os.stat(path)
            header = nifti.read_nifti_header(path)
        except (OSError, EOFError, ValueError, IndexError) as exc:
            subject_block("lesion_unreadable", sid, lambda sids, d: f"{_count(len(sids), 'lesion file')} cannot be read: {_listed(sids)} ({next(iter(d.values()))}).", "lesion file unreadable")
            collector.items["lesion_unreadable"]["details"][sid] = f"{type(exc).__name__}: {exc}"
            continue
        shape = tuple(int(n) for n in header["shape"])
        if len(shape) != 3 or min(shape) < 1:
            subject_block("lesion_not_3d", sid, lambda sids, _d: f"{_count(len(sids), 'lesion file')} {'is' if len(sids) == 1 else 'are'} not a 3D image: {_listed(sids)}.", "lesion file not 3D")
            continue
        lesion_files[sid] = (path, stat)
        affine = header["affine"]

        # Cached metrics, or a measurement when the cache key is stale or missing
        flags = _flags(row)
        volume_ml = _number(row.get("volume_ml"))
        fractional_sum = _number(row.get("fractional_sum"))
        outside_pct = _number(row.get("outside_brain_pct"))
        if not metrics_fresh(row, stat):
            collector.add(
                "metrics_stale",
                "warn",
                sid,
                lambda sids, _d: f"The lesion metrics of {_count(len(sids), 'subject')} are missing or older than the file ({_listed(sids)}); they were measured now.",
                lambda sids: _fix(FIX_REMEASURE, "Re-measure", subjects=sids),
            )
            try:
                if bundle is not None and not connectome_mask:
                    try:
                        connectome_mask.append((bundle.mask, bundle.affine))
                    except (OSError, ValueError):
                        connectome_mask.append(None)
                values, result = measure_lesion(path, conn_grid, connectome_mask[0] if connectome_mask else None, header, row.get("multifocal") == "1")
            except (OSError, EOFError, ValueError, ArithmeticError, IndexError) as exc:
                subject_block("lesion_unreadable", sid, lambda sids, d: f"{_count(len(sids), 'lesion file')} cannot be read: {_listed(sids)} ({next(iter(d.values()))}).", "lesion file unreadable")
                collector.items["lesion_unreadable"]["details"][sid] = f"{type(exc).__name__}: {exc}"
                continue
            measured[sid] = {**values, "qc_flags": ";".join(values["qc_flags"]) or None}
            volume_ml, fractional_sum = values["volume_ml"], values["fractional_sum"]
            flags = sorted(set(flags) - {"nonbinary", "empty", "subvoxel", "outside", "xform", "grid", "multipart"} | set(values["qc_flags"]))
            if result["outside_pct"] is not None:
                outside_pct = result["outside_pct"]
                outside_against[sid] = "connectome"
        elif volume_ml is not None and conn_voxel_ml:
            # Fraction resampling keeps the volume, so a lesion fills at most volume_ml / voxel connectome
            # voxels (exactly that inside the grid). A larger stored sum was measured on a finer grid than
            # this connectome's (the column does not record the grid) and would hide a sub-voxel lesion.
            estimate = volume_ml / conn_voxel_ml
            fractional_sum = estimate if fractional_sum is None else min(fractional_sum, estimate)

        # Empty and sub-voxel lesions
        if (volume_ml is not None and volume_ml <= 0) or (fractional_sum is not None and fractional_sum <= 0) or "empty" in flags:
            subject_block("lesion_empty", sid, lambda sids, _d: f"{_count(len(sids), 'lesion')} {'is' if len(sids) == 1 else 'are'} empty: {_listed(sids)}.", "empty lesion")
            continue
        if (fractional_sum is not None and fractional_sum < 1.0) or "subvoxel" in flags:
            subject_block(
                "lesion_subvoxel",
                sid,
                lambda sids, _d: f"{_count(len(sids), 'lesion')} {'fills' if len(sids) == 1 else 'fill'} less than one connectome voxel: {_listed(sids)}.",
                "lesion smaller than one connectome voxel",
            )
            collector.items["lesion_subvoxel"]["details"][sid] = None if fractional_sum is None else round(float(fractional_sum), 4)
            continue

        # File-level warnings (header)
        xform_mm = nifti.xform_disagreement(header)
        if xform_mm > XFORM_MM_MAX:
            collector.add(
                "xform",
                "warn",
                sid,
                lambda sids, d: f"The sform and qform of {_count(len(sids), 'lesion file')} disagree by up to {max(d.values()):.2f} mm ({_listed(sids)}): programs may place the lesion differently.",
                _library_fix(),
                detail=round(xform_mm, 3),
            )
        if not probabilistic and ("nonbinary" in flags or "desc-lesionProb" in os.path.basename(path)):
            collector.add(
                "nonbinary",
                "warn",
                sid,
                lambda sids, _d: f"{_count(len(sids), 'lesion')} {'has' if len(sids) == 1 else 'have'} values other than 0 and 1 ({_listed(sids)}) but the run binarizes lesions.",
                _fix(FIX_PROBABILISTIC, "Map fractional lesions (--probabilistic)"),
            )
        elif not probabilistic and sid not in measured and (header["datatype"] in FLOAT_DATATYPES or header["scl_slope"] not in (0.0, 1.0) or header["scl_inter"] != 0.0):
            collector.add(
                "float_storage",
                "info",
                sid,
                lambda sids, _d: f"{_count(len(sids), 'lesion file')} {'is' if len(sids) == 1 else 'are'} stored as floating point or scaled values ({_listed(sids)}): {'it' if len(sids) == 1 else 'they'} may hold values other than 0 and 1, which the run binarizes.",
                lambda sids: _fix(FIX_REMEASURE, "Re-measure", subjects=sids),
            )
        if outside_pct is not None and outside_pct > OUTSIDE_PCT_WARN:
            collector.add(
                "outside_mask",
                "warn",
                sid,
                lambda sids, d: f"More than {OUTSIDE_PCT_WARN:g}% of {_count(len(sids), 'lesion')} {'lies' if len(sids) == 1 else 'lie'} outside {_mask_text(sids, outside_against)} ({_listed(sids)}; up to {max(d.values()):.0f}%).",
                _library_fix(),
                detail=round(float(outside_pct), 2),
            )
            outside_against.setdefault(sid, "brain")
        if np.linalg.det(affine[:3, :3]) < 0:
            collector.add(
                "radiological",
                "info",
                sid,
                lambda sids, _d: f"{_count(len(sids), 'lesion file')} {'is' if len(sids) == 1 else 'are'} stored in radiological (left-handed) voxel order ({_listed(sids)}); {'it is' if len(sids) == 1 else 'they are'} read through the affine.",
            )
        match = geometry.detect_grid_candidates(shape, affine)

        # Space family
        space = None if schema.is_missing(row.get("space")) else row["space"]
        if match is not None and match.ambiguous and match.grid_id not in grid_answers and (space is None or space == match.space):
            collector.add(
                "ambiguous_grid",
                "info",
                sid,
                lambda sids, d: f"{_count(len(sids), 'lesion')} {'is' if len(sids) == 1 else 'are'} on a grid shared by several templates ({', '.join(sorted({c for v in d.values() for c in v}))}); answer once which template it is ({_listed(sids)}).",
                _library_fix("Choose the template"),
                detail=list(match.candidates),
            )
        family = geometry.space_family(space) if space else (match.family if match is not None else "unknown")
        if family == "unknown" and conn_family not in (None, "unknown"):
            collector.add(
                "space_unknown",
                "info",
                sid,
                lambda sids, _d: f"The space of {_count(len(sids), 'lesion')} is not recorded and {'its' if len(sids) == 1 else 'their'} grid is not a known template grid ({_listed(sids)}): {'it is' if len(sids) == 1 else 'they are'} assumed to be in the connectome's space ({conn_family}).",
                _library_fix("Set the space"),
            )
        normalized = not schema.is_missing(row.get("norm_method"))
        if family == "native" and not normalized:
            warn_exclusion(
                "native_space",
                sid,
                lambda sids, _d: f"{_count(len(sids), 'lesion')} {'is' if len(sids) == 1 else 'are'} in native space and not yet normalized ({_listed(sids)}): normalize to MNI in the Library.",
                "native space: not normalized",
                "Include native lesions anyway",
            )
        if normalized and row.get("norm_qc") != "pass":
            warn_exclusion(
                "norm_qc",
                sid,
                lambda sids, _d: f"The normalization of {_count(len(sids), 'lesion')} {'has' if len(sids) == 1 else 'have'} not passed review (norm_qc ≠ pass: {_listed(sids)}).",
                studylib.EXCLUDED_NORM_QC,
                "Include unreviewed normalizations anyway",
            )
        if normalized:
            stale = _normalization_stale(study, sid, row, hasher)
            if stale is None:
                deferred.setdefault(sid, set()).add("normalization")
            elif stale:
                collector.add(
                    "norm_stale",
                    "warn",
                    sid,
                    lambda sids, _d: f"The native lesion of {_count(len(sids), 'subject')} changed after normalization ({_listed(sids)}): normalize again.",
                    _library_fix("Normalize again"),
                )
        template = None if schema.is_missing(row.get("trace_template")) else row["trace_template"]
        if template and conn_family not in (None, "unknown") and geometry.space_family(template) not in ("unknown", conn_family):
            collector.add(
                "template_family",
                "warn",
                sid,
                lambda sids, d: f"{_count(len(sids), 'lesion')} {'was' if len(sids) == 1 else 'were'} traced on a template of another family than the connectome ({', '.join(sorted(set(d.values())))}; {_listed(sids)}).",
                _library_fix(),
                detail=template,
            )

        # Review gate
        if review and row.get("review_status") != "approved":
            warn_exclusion(
                "unreviewed",
                sid,
                lambda sids, _d: f"{_count(len(sids), 'lesion')} {'is' if len(sids) == 1 else 'are'} not approved by review ({_listed(sids)}).",
                studylib.EXCLUDED_REVIEW,
                "Include unreviewed lesions anyway",
            )
        elif any(token.startswith("auto:") for token in str(row.get("trace_method") or "").split("+")) and row.get("review_status") != "approved":
            warn_exclusion(
                "auto_unreviewed",
                sid,
                lambda sids, _d: f"{_count(len(sids), 'lesion')} {'was' if len(sids) == 1 else 'were'} segmented automatically and {'is' if len(sids) == 1 else 'are'} not approved by a person ({_listed(sids)}).",
                "automatic segmentation not approved",
                "Include automatic segmentations anyway",
            )
        if sid not in excluded:
            families[sid] = family

    included = [sid for sid in study.subject_ids if sid not in excluded]
    rows_by_sid = {row["participant_id"]: row for row in study.subjects}
    included_rows = [rows_by_sid[sid] for sid in included]

    # Duplicates: source identity, lesion path, identical files, and the duplicate index's findings
    _duplicate_checks(study, included_rows, lesion_files, collector, hasher, deferred)
    hasher.save()
    for sid, waiting in deferred.items():
        if sid not in excluded:
            collector.add("hash_deferred", "info", sid, _deferred_message(hash_budget), scope="run", detail=sorted(waiting))

    # Flipped lesions, missing scores, case reports without a source
    flipped = [row["participant_id"] for row in included_rows if row.get("flipped") == "1"]
    for sid in flipped:
        collector.add("flipped", "warn", sid, lambda sids, _d: f"{_count(len(sids), 'lesion')} {'was' if len(sids) == 1 else 'were'} mirrored left-right ({_listed(sids)}); report the count in the methods.", scope="run")
    for column, confirmed in study.score_columns():
        if not confirmed:
            continue
        for row in included_rows:
            if schema.is_missing(row.get(column)):
                collector.add(
                    "missing_scores",
                    "warn",
                    row["participant_id"],
                    lambda sids, d: f"{_count(len(sids), 'subject')} {'has' if len(sids) == 1 else 'have'} no value of {', '.join(sorted(set(d.values())))} ({_listed(sids)}); score analyses exclude them (never imputed).",
                    _library_fix(),
                    "run",
                    detail=column,
                )
    for row in included_rows:
        if row.get("source_type") == "case_report" and schema.is_missing(row.get("record_id")) and schema.is_missing(row.get("doi")):
            collector.add(
                "case_report_source",
                "warn",
                row["participant_id"],
                lambda sids, _d: f"{_count(len(sids), 'case report')} {'has' if len(sids) == 1 else 'have'} neither a record_id nor a DOI ({_listed(sids)}): the source cannot be cited.",
                _library_fix(),
                "run",
            )

    checks = collector.checks()
    blocked_subject_checks = [check for check in checks if check.level == "block"]

    # Run-level checks
    if bundle is not None:
        manifest_sha256 = _manifest_sha256(bundle)
        if not _terms_accepted(accepted_terms, manifest_sha256):
            run_checks.append(Check("terms_not_accepted", "block", [], f"The data use terms of the connectome {bundle.name} are not accepted.", _fix(FIX_ACCEPT_TERMS, "Accept the data use terms"), "run"))
    run_checks.extend(_family_checks(families, conn_family, transforms))
    if not included:
        message = "No lesion can be analysed: every subject is excluded." if len(study) else "No lesion can be analysed: the study has no subjects."
        run_checks.append(Check("no_eligible", "block", [], message, _library_fix("Show the excluded subjects")(sorted(excluded)), "run"))
    if bundle is not None and included:
        target = out_dir or os.path.join(study.path, studylib.DERIVATIVES_DIR)
        need = disk_estimate(len(included), bundle.n_voxels)
        try:
            free = _free_bytes(target)
        except OSError:
            free = None
        if free is not None and free < need:
            run_checks.append(
                Check(
                    "disk_space",
                    "block",
                    [],
                    f"Not enough disk space: the run needs about {need / 1024**3:.1f} GB (3 maps of {bundle.n_voxels:,} voxels for each of {len(included)} lesions, plus the map cache); {free / 1024**3:.1f} GB are free.",
                    _fix(FIX_FREE_DISK, "Free disk space or choose another output folder"),
                    "run",
                    detail={"need_bytes": need, "free_bytes": free},
                ),
            )
    for label, path in (("study folder", study.path), ("output folder", out_dir)):
        if path and not _is_ascii(path):
            run_checks.append(Check("non_ascii_path", "info", [], f"The {label} path has non-ASCII characters: official 3D Slicer 5.12 reads it only with a UTF-8 locale.", None, "run", detail={"which": label}))
    run_checks.extend(_rule_checks(recipe_rule, bundle, n_subjects))
    design_checks = _design_checks(study, included, design_spec)
    if design_spec is None:
        run_checks.extend(_dimensionality_share(study, included_rows))
    run_checks.extend(design_checks)

    all_checks = checks + run_checks
    settings = {
        "probabilistic": bool(probabilistic),
        "strict": bool(strict),
        "overrides": sorted(overrides),
        "review_required": review,
        "rule": None if recipe_rule is None else thresholds.normalize_rule(recipe_rule),
        "design": None if design_spec is None else _design_dict(design_spec),
        "connectome_family": conn_family,
        "subject_blocks": len(blocked_subject_checks),
    }
    return _report(study, bundle, all_checks, included, excluded, measured, settings, started)


def _manifest_sha256(bundle):
    from .export import file_sha256

    path = os.path.join(bundle.path, "connectome.json")
    return file_sha256(path) if os.path.isfile(path) else None


def _terms_accepted(accepted, manifest_sha256):
    if accepted is True:
        return True
    if not accepted:
        return False
    if isinstance(accepted, str):
        return accepted == manifest_sha256
    try:
        return manifest_sha256 in set(accepted)
    except TypeError:
        return False


def _normalization_stale(study, sid, row, hasher):
    """True when the native lesion's sha256 differs from the one recorded in normalization.json (None: not hashed yet)."""
    native = study.abspath(row.get("lesion_native"))
    record_path = os.path.join(study.path, studylib.DERIVATIVES_DIR, "normalization", sid, NORMALIZATION_JSON)
    if not native or not os.path.isfile(native) or not os.path.isfile(record_path):
        return False
    try:
        with open(record_path, encoding="utf-8") as f:
            record = json.load(f)
        recorded = ((record.get("inputs") or {}).get("lesion") or {}).get("sha256")
    except (OSError, ValueError, AttributeError):
        return False
    if not recorded:
        return False
    try:
        sha = hasher.get(native)
    except OSError:
        return False
    return None if sha is None else recorded != sha


def _duplicate_checks(study, rows, lesion_files, collector, hasher, deferred):
    def same_source(sids, d):
        return f"{_count(len(sids), 'subject')} share a source identity or a lesion file with another subject ({_listed(sids)}): the same case may be counted twice."

    def same_lesion(sids, d):
        return f"{_count(len(sids), 'lesion')} {'is' if len(sids) == 1 else 'are'} identical or nearly identical to another lesion ({_listed(sids)}): the same case may be counted twice."

    groups = {}
    for row in rows:
        sid = row["participant_id"]
        keys = []
        if not schema.is_missing(row.get("source_id")):
            keys.append(("source", row.get("source_dataset") or "", row["source_id"]))
        if not schema.is_missing(row.get("case_label")) and not (schema.is_missing(row.get("record_id")) and schema.is_missing(row.get("doi"))):
            keys.append(("case", row.get("record_id") or "", row.get("doi") or "", row["case_label"]))
        if sid in lesion_files:
            keys.append(("file", os.path.normcase(os.path.realpath(lesion_files[sid][0]))))
        for key in keys:
            groups.setdefault(key, []).append(sid)
    for sids in groups.values():
        if len(sids) > 1:
            for sid in sids:
                collector.add("duplicate_id", "warn", sid, same_source, _library_fix(), "run")

    # Identical files: hash only files whose size another included file shares (cached; within the budget)
    by_size = {}
    for row in rows:
        sid = row["participant_id"]
        if sid in lesion_files:
            by_size.setdefault(lesion_files[sid][1].st_size, []).append(sid)
    by_hash = {}
    for sids in by_size.values():
        if len(sids) < 2:
            continue
        for sid in sids:
            try:
                sha = hasher.get(*lesion_files[sid])
            except OSError:
                continue
            if sha is None:
                deferred.setdefault(sid, set()).add("duplicates")
            else:
                by_hash.setdefault(sha, []).append(sid)
    for sids in by_hash.values():
        if len(sids) > 1:
            for sid in sids:
                collector.add("duplicate_lesion", "warn", sid, same_lesion, _library_fix(), "run")
    for row in rows:
        if not schema.is_missing(row.get("duplicate_of")) or "duplicate" in _flags(row):
            collector.add("duplicate_lesion", "warn", row["participant_id"], same_lesion, _library_fix(), "run")


def _deferred_message(budget):
    def message(sids, details):
        what = {"duplicates": "identical-lesion", "normalization": "changed-native-lesion"}
        checks = " and ".join(what[item] for item in sorted({item for items in details.values() for item in items}))
        limit = f"up to {budget / 1024**2:.0f} MB of" if budget is not None else "all"
        return (
            f"{_count(len(sids), 'lesion file')} {'was' if len(sids) == 1 else 'were'} not hashed yet ({_listed(sids)}): the {checks} check of "
            f"{'it' if len(sids) == 1 else 'them'} waits for a later preflight. Each preflight hashes {limit} new files and remembers the hashes."
        )

    return message


def _mask_text(sids, against):
    """Which mask the outside shares of these subjects were measured against."""
    kinds = {against.get(sid, "brain") for sid in sids}
    return "the connectome mask" if kinds == {"connectome"} else "the brain mask" if kinds == {"brain"} else "the brain or connectome mask"


def _family_checks(families, conn_family, transforms):
    """Mixed space families without a transform (block) and families other than the connectome's (warn)."""
    present = sorted({family for family in families.values() if family not in ("unknown", "native")})
    if not present:
        return []
    target = conn_family if conn_family not in (None, "unknown") else None
    others = [family for family in present if family != target] if target else []
    needs = []
    available = None
    if target and others:
        available = _transform_pairs(cached_transforms() if transforms is None else transforms)
        needs = [family for family in others if (family, target) not in available]
    elif target is None and len(present) > 1:
        available = _transform_pairs(cached_transforms() if transforms is None else transforms)
        reachable = {(a, b) for a, b in available}
        needs = [family for family in present[1:] if (family, present[0]) not in reachable and (present[0], family) not in reachable]
    out = []
    if len(present) > 1 and needs:
        sids = sorted(sid for sid, family in families.items() if family in needs)
        out.append(
            Check(
                "mixed_families",
                "block",
                sids,
                f"The lesions are in several template families ({', '.join(present)}) and no transform to {target or present[0]} is available for {', '.join(needs)}.",
                _fix(FIX_TRANSFORM, "Get the template-family transform", source=needs, target=target or present[0]),
                "run",
                detail={"families": {family: sum(1 for value in families.values() if value == family) for family in present}},
            ),
        )
    for family in others:
        sids = sorted(sid for sid, value in families.items() if value == family)
        have = available is not None and (family, target) in available
        out.append(
            Check(
                "space_family",
                "warn",
                sids,
                f"{_count(len(sids), 'lesion')} {'is' if len(sids) == 1 else 'are'} in the {family} template family but the connectome is {target}"
                + (": a transform is available." if have else ": without a template-family transform the lesions are placed a few millimetres off in deep structures."),
                _fix(FIX_TRANSFORM, "Transform the lesions to the connectome space", source=family, target=target, available=have),
                "subject",
                detail={"family": family, "connectome": target},
            ),
        )
    return out


def _rule_checks(rule, bundle, n_subjects):
    if rule is None or bundle is None:
        return []
    out = []
    try:
        normalized = thresholds.normalize_rule(rule)
        n = int(n_subjects) if n_subjects else bundle.n_subjects
        if normalized["kind"] == "fwe_bonferroni" and n >= 2:
            # Valid at any N >= 2 without a banner: skip resolving it, which imports scipy (about 1 s cold)
            banner, allowed = None, True
        else:
            resolved = thresholds.resolve(normalized, n, bundle.n_voxels)
            banner, allowed = resolved.banner, resolved.allowed
    except (ValueError, TypeError) as exc:
        return [Check("rule_invalid", "block", [], f"The threshold rule is invalid: {exc}", _fix(FIX_RULE_FWE, "Use the FWE rule"), "run")]
    banner = banner if banner != thresholds.TOP_PCT_NEEDS_VALUES else None
    if banner or (not allowed and normalized["kind"] != "top_pct"):
        out.append(
            Check(
                "rule_n",
                "warn",
                [],
                banner or f"The rule {thresholds.rule_label(normalized)} is not meaningful at N={n}.",
                _fix(FIX_RULE_FWE, "Use the FWE rule"),
                "run",
                detail={"n": n, "allowed": allowed},
            ),
        )
    gsr = ((getattr(bundle, "provenance", None) or {}).get("preprocessing") or {}).get("gsr")
    if gsr is False and normalized["kind"] in thresholds.SYMMETRIC_T_KINDS:
        out.append(Check("gsr_rule", "warn", [], thresholds.GSR_BANNER, _fix(FIX_RULE_EFFECT, "Use an asymmetric or effect-size rule"), "run"))
    return out


def _design_dict(spec):
    try:
        return designlib.as_spec(spec).to_dict()
    except (OSError, ValueError):
        return None


def _design_checks(study, included, design_spec):
    """The shared design checks (``design.build_design``) on the subjects this preflight includes."""
    if design_spec is None:
        return []
    if not included:
        return []
    try:
        spec = designlib.as_spec(design_spec)
        built = designlib.build_design(_subset_study(study, included), spec)
    except (OSError, ValueError) as exc:  # DesignError is a ValueError
        return [Check("design.error", "block", [], f"The design cannot be built: {exc}", None, "run")]
    out = []
    for check in built.checks:
        subjects = []
        detail = check.detail
        if isinstance(detail, dict) and isinstance(detail.get("sids"), list):
            subjects = list(detail["sids"])
        out.append(Check(f"design.{check.code}", check.level, subjects, check.message, None, "run", detail=designlib._jsonable(detail)))
    if built.spec.test in designlib.GROUP_TESTS:
        small = {level: count for level, count in built.n_per_level.items() if count < MIN_PER_CONTRAST_GROUP}
        if small:
            text = ", ".join(f"{level} (n = {count})" for level, count in small.items())
            out.append(Check("small_group", "block", [], f"Fewer than {MIN_PER_CONTRAST_GROUP} subjects in a contrast group: {text}.", _library_fix()([]), "run", detail=small))
    return out


def _dimensionality_share(study, rows):
    """2D-share difference between case and control (only without a design: the design reports its own)."""
    if "dimensionality" not in study.columns:
        return []
    shares = {}
    for level in ("case", "control"):
        values = [row.get("dimensionality") for row in rows if row.get("group") == level and not schema.is_missing(row.get("dimensionality"))]
        if values:
            shares[level] = 100.0 * sum(1 for value in values if value in designlib.TWO_D_LEVELS) / len(values)
    if len(shares) == 2 and abs(shares["case"] - shares["control"]) > designlib.DIMENSIONALITY_SHARE_LIMIT:
        text = ", ".join(f"{level} {share:.0f}%" for level, share in shares.items())
        return [Check("dimensionality_share", "warn", [], f"The share of 2D tracings differs between groups by more than {designlib.DIMENSIONALITY_SHARE_LIMIT:.0f} points ({text}): consider dimensionality as a covariate.", None, "run", detail=shares)]
    return []


def _report(study, bundle, checks, included, excluded, measured, settings, started):
    strict = bool(settings.get("strict"))
    order = {level: index for index, level in enumerate(LEVELS)}
    checks = sorted(checks, key=lambda check: order[check.level])  # stable: blocks, warnings, information
    blocked = any(check.level == "block" and (check.scope == "run" or strict) for check in checks)
    counts = {level: sum(1 for check in checks if check.level == level) for level in LEVELS}
    counts.update({"subjects": len(study) if study is not None else 0, "included": len(included), "excluded": len(excluded)})
    bundle_info = None
    if bundle is not None:
        bundle_info = {"name": bundle.name, "manifest_sha256": _manifest_sha256(bundle), "n_subjects": bundle.n_subjects, "n_voxels": bundle.n_voxels}
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "created": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "study": None if study is None else study.name,
        "bundle": bundle_info,
        "checks": [check.to_dict() for check in checks],
        "counts": counts,
        "blocked": blocked,
        "included": list(included),
        "excluded": dict(excluded),
        "measured": _jsonable(measured),
        "settings": _jsonable(settings),
        "elapsed": round(time.perf_counter() - started, 4),
    }


def blocking_checks(report):
    """The checks that block the run: run-level blocks, and subject-level blocks of a strict report."""
    strict = bool((report.get("settings") or {}).get("strict"))
    return [check for check in report["checks"] if check["level"] == "block" and (check["scope"] == "run" or strict)]


def save_report(study, report):
    """Write the report to ``<study>/derivatives/qc/preflight-<UTC>.json`` and register it (kind "preflight").

    :return: path of the file; the report gets its "run_id".
    """
    study_path = study.path if isinstance(study, studylib.Study) else os.path.abspath(os.fspath(study))
    folder = os.path.join(study_path, *QC_RELPATH)
    os.makedirs(folder, exist_ok=True)
    stamp = _utc_stamp()
    path = os.path.join(folder, f"preflight-{stamp}.json")
    index = 1
    while os.path.exists(path):
        path = os.path.join(folder, f"preflight-{stamp}-{index}.json")
        index += 1
    run_id = runs.new_run_id("preflight")
    report["run_id"] = run_id
    jobs.write_json_atomic(path, report)
    summary = {**report["counts"], "blocked": report["blocked"], "file": os.path.basename(path)}
    runs.register(study_path, "preflight", folder, params=report.get("settings"), status="done", summary=summary, run_id=run_id)
    return path


# Post-run assertions


def _failure(code, message, path=None, **detail):
    out = {"code": code, "message": message, "path": None if path is None else os.fspath(path)}
    if detail:
        out["detail"] = _jsonable(detail)
    return out


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_record(path):
    """A run record (``_read_json``) with the paths that cohortmaps.json, glm.json and slnm.json store relative to their
    folder made absolute (``export.resolve_record_paths``).
    """
    from .export import GROUP_RECORD_PATH_FIELDS, resolve_record_paths

    record = _read_json(path)
    name = os.path.basename(path)
    if record is None or name not in ("cohortmaps.json", "glm.json", "slnm.json"):
        return record
    if name == "cohortmaps.json":
        from .cohortmaps import RECORD_PATH_FIELDS as fields
    else:
        fields = GROUP_RECORD_PATH_FIELDS
    return resolve_record_paths(record, os.path.dirname(os.path.abspath(path)), fields)


def _record_n(record):
    for key in ("N", "n", "n_subjects"):
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    subjects = record.get("subjects")
    if isinstance(subjects, dict) and isinstance(subjects.get("n"), int):
        return subjects["n"]
    return None


def _sidecar_field(record, field):
    """Whether a sidecar or run record states N, df, a threshold rule or the disclaimer."""
    if field == "N":
        return _record_n(record) is not None
    if field == "df":
        return any(isinstance(item, dict) and item.get("df") is not None for item in (record, record.get("inference"), record.get("threshold", {}).get("resolved") if isinstance(record.get("threshold"), dict) else None))
    if field == "rule":
        threshold = record.get("threshold")
        if isinstance(threshold, dict) and threshold.get("rule"):
            return True
        return any(record.get(key) for key in ("threshold_rule", "rule", "fwe", "inference"))
    return bool(record.get(field))


def _partial(record):
    """The record says that the run stopped early (engine run.json "cancelled", a cancelled status, or an export's subset.partial)."""
    if record.get("cancelled") or record.get("status") == "cancelled":
        return True
    subset = record.get("subset")
    return isinstance(subset, dict) and bool(subset.get("partial"))


def _collect_outputs(source, kind):
    """{kind, record, record_path, maps, sidecars, rows, folder, value_maps, group} of a run record, folder, maps.tsv or outputs dict.

    value_maps: the maps whose values must vary (None: all of them); group: a group record (glm, slnm,
    cohort), whose "n" is the number of lesions and whose connectome N is :func:`_group_connectome_n`.
    """
    out = {"kind": kind, "record": None, "record_path": None, "maps": [], "sidecars": [], "rows": None, "folder": None, "value_maps": None, "group": False}
    if isinstance(source, dict):
        maps = source.get("maps") or []
        if isinstance(maps, dict):
            maps = [path for item in maps.values() for path in (item.values() if isinstance(item, dict) else [item])]
        out["maps"] = list(maps)
        out["sidecars"] = list(source.get("sidecars") or [])
        provenance = source.get("provenance") or source.get("record")
        if isinstance(provenance, (str, os.PathLike)):
            out["record_path"] = os.fspath(provenance)
            out["record"] = _read_json(provenance)
        elif isinstance(provenance, dict):
            out["record"] = provenance
        else:
            out["record"] = {key: source[key] for key in ("N", "n", "status", "cancelled", "subset", "bundle", "connectome") if key in source}
        return out
    path = os.path.abspath(os.fspath(source))
    if os.path.isdir(path):
        for name in ("provenance.json", "cohortmaps.json", *GROUP_RECORDS, "run.json"):
            if os.path.isfile(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            out["folder"] = path
            return out
    folder = os.path.dirname(path)
    out["folder"] = folder
    name = os.path.basename(path)
    if name.endswith(".tsv"):
        from .cohortmaps import COHORTMAPS_JSON, MAP_KEYS, read_maps_tsv

        out["kind"] = "cohortmaps"
        try:
            rows = read_maps_tsv(path)
        except (OSError, ValueError):
            rows = None
        out["rows"] = rows
        out["maps"] = [row[key] for row in rows or () for key in MAP_KEYS if not schema.is_missing(row.get(key))]
        out["record_path"] = os.path.join(folder, COHORTMAPS_JSON)
        out["record"] = _read_record(out["record_path"])
        return out
    out["record_path"] = path
    record = _read_record(path)
    out["record"] = record
    if record is None:
        return out
    if name == "cohortmaps.json":
        from .cohortmaps import MAP_KEYS, MAPS_TSV, read_maps_tsv

        out["kind"] = "cohortmaps"
        tsv = os.path.join(folder, MAPS_TSV)
        try:
            out["rows"] = read_maps_tsv(tsv)
        except (OSError, ValueError):
            out["rows"] = None
        out["maps"] = [row[key] for row in out["rows"] or () for key in MAP_KEYS if not schema.is_missing(row.get(key))]
    elif name == "provenance.json":
        out["maps"] = [os.path.join(folder, map_name) for lesion in record.get("lesions") or () for map_name in (lesion.get("maps") or {}).values()]
        out["sidecars"] = [os.path.join(folder, lesion["sidecar"]) for lesion in record.get("lesions") or () if lesion.get("sidecar")]
    elif name == "run.json":
        maps = record.get("maps") or {}
        out["maps"] = [path for item in maps.values() for path in item.values()] if isinstance(maps, dict) else list(maps)
        provenance = os.path.join(folder, "provenance.json")
        if os.path.isfile(provenance):
            exported = _read_json(provenance) or {}
            out["sidecars"] = [os.path.join(folder, lesion["sidecar"]) for lesion in exported.get("lesions") or () if lesion.get("sidecar")]
    else:  # glm.json, slnm.json, cohort.json: outputs {key: file name}
        out["kind"] = os.path.splitext(name)[0] if name in GROUP_RECORDS else kind
        out["group"] = True
        outputs = record.get("outputs") or {}
        items = outputs.items() if isinstance(outputs, dict) else ((None, value) for value in outputs)
        out["value_maps"] = []
        for key, value in items:
            if not (isinstance(value, str) and value.endswith((".nii", ".nii.gz"))):
                continue
            map_path = os.path.join(folder, os.path.basename(value))
            out["maps"].append(map_path)
            if _group_stat_key(key):
                out["value_maps"].append(map_path)
        out["sidecars"] = [path]
    return out


def _group_stat_key(key):
    """Whether a group record's output key is an unthresholded statistic map (glm stat or effect, sLNM r maps)."""
    if not isinstance(key, str):
        return False
    return key in GROUP_STAT_KEYS or key.endswith(":r") or key.startswith("lodo:")


def _group_connectome_n(record, kind, folder):
    """Connectome N of a group record: cohort.json n_subjects, else the N of the cohortmaps run a glm or sLNM run read (None: unknown)."""
    if kind == "cohort":
        value = record.get("n_subjects")
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    path = (record.get("inputs") or {}).get("cohortmaps_json")
    if not isinstance(path, str) or not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(folder or "", path)
    cohortmaps = _read_json(path)
    return _record_n(cohortmaps) if cohortmaps else None


def _record_lesions(record, kind):
    """Number of lesions (design size) of a group record, or None."""
    for key in ("n_maps",) if kind == "cohort" else ("n",):
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return None


def _reference_record(reference_header, record):
    """Raw NIfTI header record of the reference (a path, raw header, bundle or bundle folder), or None."""
    if reference_header is None:
        info = (record or {}).get("bundle") or (record or {}).get("connectome") or {}
        bundle_path = info.get("path") if isinstance(info, dict) else None
        if bundle_path and os.path.isfile(os.path.join(bundle_path, "mask.nii.gz")):
            reference_header = os.path.join(bundle_path, "mask.nii.gz")
        else:
            return None
    if isinstance(reference_header, np.void):
        return reference_header
    if hasattr(reference_header, "path") and hasattr(reference_header, "affine"):  # a ConnectomeBundle
        reference_header = os.path.join(reference_header.path, "mask.nii.gz")
    path = os.fspath(reference_header)
    if os.path.isdir(path):
        path = os.path.join(path, "mask.nii.gz")
    return nifti.read_raw_header(path)


def header_differences(raw, reference):
    """Names of the geometry fields (dim 1-3, pixdim 0-3, codes, quaternion, sform rows) in which two raw headers differ."""
    differences = []
    if not np.array_equal(np.asarray(raw["dim"])[1:4], np.asarray(reference["dim"])[1:4]):
        differences.append("dim")
    if not np.allclose(np.asarray(raw["pixdim"], dtype=np.float64)[:4], np.asarray(reference["pixdim"], dtype=np.float64)[:4], atol=HEADER_ATOL, rtol=0):
        differences.append("pixdim")
    for field in HEADER_FIELDS:
        a, b = np.asarray(raw[field], dtype=np.float64), np.asarray(reference[field], dtype=np.float64)
        if not np.allclose(a, b, atol=HEADER_ATOL, rtol=0):
            differences.append(field)
    return differences


def assert_run_outputs(run_json_or_outputs, expected_n, kind="map", reference_header=None, expected_lesions=None, check_values=True):
    """Check that a finished run produced what "done" promises; return the failures (an empty list when none).

    :param run_json_or_outputs: an engine run.json, an export folder or its provenance.json, a cohortmaps
      maps.tsv or cohortmaps.json (or their folder), a group record (glm.json, slnm.json, cohort.json),
      or an outputs dict ({"maps": [paths] or {name: {key: path}}, "sidecars": [paths], "provenance":
      path or dict, or "N"/"n", "status", "cancelled"}; ``export.write_run``'s result is accepted).
    :param expected_n: the requested number of connectome subjects (N). A stopped run may have fewer if it
      says so (cancelled, or subset.partial in its record and sidecars). For a group record it is
      compared with the connectome N (cohort.json n_subjects, or the N of the cohortmaps run that
      glm.json or slnm.json names), not with its "n" (the number of lesions).
    :param kind: "map", "cohortmaps", "glm", "slnm", "cohort", ...: which sidecar fields are required (see
      :data:`SIDECAR_FIELDS`); a group record sets it from its file name.
    :param reference_header: the connectome header (a NIfTI path, ``nifti.read_raw_header`` record, bundle or
      bundle folder); default: the mask of the bundle the record names, else the maps must agree with each other.
    :param expected_lesions: the number of lesions: maps.tsv rows of cohort maps, the design n of glm.json
      and slnm.json, n_maps of cohort.json.
    :param check_values: read the maps and check that they are finite, and that statistic maps have
      non-zero variance (per-lesion maps; glm stat and effect, sLNM r maps; not p, overlap, sensitivity
      or conjunction maps, which a null result leaves constant).
    :return: list of {"code", "message", "path", ["detail"]}; codes: missing_record, not_done, no_outputs,
      missing_file, unreadable, header_mismatch, xform, non_finite, constant_map, wrong_n,
      partial_not_labelled, wrong_lesion_count, missing_sidecar, sidecar_incomplete.
    """
    found = _collect_outputs(run_json_or_outputs, kind)
    kind = found["kind"]
    record = found["record"]
    failures = []
    if record is None:
        failures.append(_failure("missing_record", "The run record (run.json, provenance.json, cohortmaps.json or the group record) is missing or unreadable.", found["record_path"] or found["folder"]))
        record = {}
    status = record.get("status")
    partial = _partial(record)
    if status not in (None, "done") and not (partial and status in ("cancelled", "done")):
        failures.append(_failure("not_done", f"The run did not finish (status {status}).", found["record_path"], status=status))

    # N
    if found["group"]:
        n = _group_connectome_n(record, kind, found["folder"])  # unknown when the maps did not come from a cohortmaps run
    else:
        n = _record_n(record) if record else None
        if record and n is None:
            failures.append(_failure("wrong_n", "The run record does not state its N.", found["record_path"]))
    if n is not None and expected_n is not None and n != int(expected_n):
        if not partial:
            failures.append(_failure("wrong_n", f"The run used N={n} connectome subjects; N={int(expected_n)} was requested.", found["record_path"], n=n, expected=int(expected_n)))

    # Lesion count of cohort maps and group records
    lesions = _record_lesions(record, kind) if found["group"] else None
    if lesions is not None and expected_lesions is not None and lesions != int(expected_lesions):
        failures.append(_failure("wrong_lesion_count", f"The analysis used {lesions} lesions; {int(expected_lesions)} were expected.", found["record_path"], used=lesions, expected=int(expected_lesions)))
    if found["rows"] is not None and expected_lesions is not None and len(found["rows"]) != int(expected_lesions):
        failures.append(_failure("wrong_lesion_count", f"{len(found['rows'])} lesions were mapped; {int(expected_lesions)} were expected.", found["record_path"], mapped=len(found["rows"]), expected=int(expected_lesions)))
    if found["rows"] is None and kind == "cohortmaps":
        failures.append(_failure("missing_file", "maps.tsv is missing or unreadable.", found["folder"]))

    # Maps: existence, header, values
    maps = [os.fspath(path) for path in found["maps"]]
    if not maps:
        failures.append(_failure("no_outputs", "The run lists no output maps.", found["record_path"] or found["folder"]))
    try:
        reference = _reference_record(reference_header, record)
    except (OSError, ValueError) as exc:
        failures.append(_failure("unreadable", f"The reference header cannot be read: {exc}", None if reference_header is None else os.fspath(reference_header) if isinstance(reference_header, (str, os.PathLike)) else None))
        reference = None
    first = None
    for path in maps:
        if not os.path.isfile(path):
            failures.append(_failure("missing_file", f"Output map not found: {os.path.basename(path)}", path))
            continue
        try:
            raw = nifti.read_raw_header(path)
            header = nifti.read_nifti_header(path)
        except (OSError, EOFError, ValueError) as exc:
            failures.append(_failure("unreadable", f"Output map cannot be read: {os.path.basename(path)} ({exc})", path))
            continue
        against = reference if reference is not None else first
        if against is not None:
            differences = header_differences(raw, against)
            if differences:
                what = "the connectome header" if reference is not None else "the other maps"
                failures.append(_failure("header_mismatch", f"{os.path.basename(path)}: the header differs from {what} ({', '.join(differences)}).", path, fields=differences))
        elif first is None:
            first = raw
        if nifti.xform_disagreement(header) > XFORM_MM_MAX:
            failures.append(_failure("xform", f"{os.path.basename(path)}: its sform and qform disagree.", path))
        if check_values:
            try:
                data = nifti.read_nifti(path)[0]
            except (OSError, EOFError, ValueError) as exc:
                failures.append(_failure("unreadable", f"Output map cannot be read: {os.path.basename(path)} ({exc})", path))
                continue
            values = np.asarray(data, dtype=np.float64)
            finite = np.isfinite(values)
            if not finite.all():
                failures.append(_failure("non_finite", f"{os.path.basename(path)} has {int(values.size - np.count_nonzero(finite))} non-finite values.", path, count=int(values.size - np.count_nonzero(finite))))
            elif (found["value_maps"] is None or path in found["value_maps"]) and (values.size == 0 or float(values.max()) == float(values.min())):
                failures.append(_failure("constant_map", f"{os.path.basename(path)} has the same value everywhere (zero variance).", path))

    # Sidecars
    required = SIDECAR_FIELDS.get(kind, DEFAULT_SIDECAR_FIELDS)
    sidecars = [os.fspath(path) for path in found["sidecars"]]
    expects_sidecars = kind in SIDECAR_FIELDS and found["record"] is not None and found["record_path"] is not None and os.path.basename(found["record_path"]) != "run.json"
    if kind != "cohortmaps" and (sidecars or expects_sidecars):
        if not sidecars:
            failures.append(_failure("missing_sidecar", "The run has no sidecar files.", found["record_path"] or found["folder"]))
        for path in sidecars:
            sidecar = _read_json(path)
            if sidecar is None:
                failures.append(_failure("missing_sidecar", f"Sidecar missing or unreadable: {os.path.basename(path)}", path))
                continue
            missing = [field for field in required if not _sidecar_field(sidecar, field)]
            if missing:
                failures.append(_failure("sidecar_incomplete", f"{os.path.basename(path)} does not state {', '.join(missing)}.", path, missing=missing))
            side_n = None if found["group"] else _record_n(sidecar)  # a group record's own n counts lesions
            if side_n is not None and n is not None and side_n != n:
                failures.append(_failure("wrong_n", f"{os.path.basename(path)} states N={side_n}; the run used N={n}.", path, n=side_n, run_n=n))
            if partial and not _partial(sidecar):
                failures.append(_failure("partial_not_labelled", f"{os.path.basename(path)} does not say that the run is partial.", path))
    return failures


# Command line


def _parser():
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.preflight", description="Preflight checks of a lesion network mapping run (exit code 5 when something blocks).")
    parser.add_argument("--study", required=True, help="study folder")
    parser.add_argument("--bundle", default=None, help="connectome bundle folder")
    parser.add_argument("--rule", default=None, help="primary threshold rule (JSON)")
    parser.add_argument("--design", default=None, help="design.json")
    parser.add_argument("--accepted-terms", action="store_true", help="the connectome's data use terms are accepted")
    parser.add_argument("--probabilistic", action="store_true", help="the run maps fractional lesions")
    parser.add_argument("--strict", action="store_true", help="missing, unreadable, not 3D or empty lesions block the run instead of being excluded")
    parser.add_argument("--include-anyway", action="append", default=[], metavar="CHECK", help="override a warn-level exclusion (unreviewed, auto_unreviewed, native_space, norm_qc); repeatable")
    parser.add_argument("--subjects", type=jobs.positive_int, default=None, help="connectome subjects of the run (default: all)")
    parser.add_argument("--out", default=None, help="output folder of the run (disk and path checks)")
    parser.add_argument("--json", default=None, help="write the report to this file")
    parser.add_argument("--save", action="store_true", help="save the report in <study>/derivatives/qc and register it")
    parser.add_argument("--threads", type=jobs.positive_int, default=1, help="BLAS threads (default: 1)")
    return parser


def main(argv):
    args = _parser().parse_args(argv)
    jobs.emit("start", job="preflight", argv=list(argv), threads=args.threads)
    rule = None
    if args.rule:
        try:
            rule = thresholds.rule_from_json(args.rule)
        except ValueError as exc:
            raise jobs.JobError(f"Invalid --rule: {exc}", code="usage", exit_code=jobs.EXIT_USAGE) from None
    if not studylib.Study.is_study(args.study):
        raise jobs.JobError(f"Not a study folder: {args.study}", code="usage", exit_code=jobs.EXIT_USAGE)
    report = run_preflight(
        args.study,
        bundle=args.bundle,
        recipe_rule=rule,
        design_spec=args.design,
        accepted_terms=bool(args.accepted_terms),
        probabilistic=args.probabilistic,
        strict=args.strict,
        overrides=args.include_anyway,
        n_subjects=args.subjects,
        out_dir=args.out,
    )
    for check in report["checks"]:
        if check["level"] in ("block", "warn"):
            jobs.emit("warning", code=check["id"], level=check["level"], scope=check["scope"], participants=check["subjects"], message=check["message"], fix=check["fix"])
    outputs = []
    if args.save:
        path = save_report(args.study, report)
        outputs.append(path)
        jobs.emit("result", kind="preflight", path=path)
    if args.json:
        jobs.write_json_atomic(args.json, report)
        outputs.append(os.path.abspath(args.json))
        jobs.emit("result", kind="preflight", path=os.path.abspath(args.json))
    jobs.emit("done", outputs=outputs, elapsed=jobs.elapsed(), run_id=report.get("run_id"), blocked=report["blocked"], counts=report["counts"])
    return EXIT_BLOCKED if report["blocked"] else jobs.EXIT_OK


if __name__ == "__main__":
    jobs.run_job(main, job="preflight")

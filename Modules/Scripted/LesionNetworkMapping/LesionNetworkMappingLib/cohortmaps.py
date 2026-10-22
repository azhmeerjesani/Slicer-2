"""Cohort network maps: batched engine passes under a memory budget and a per-lesion map cache.

A cohort (cases, controls or a control-lesion database of 50-1000 lesions) is mapped once per
connectome and subject list. The lesions that are not in the cache are computed in batches of at
most ``L_max = floor(budget / (68 * V))`` lesions (clamped to [1, 100]; addendum §3.13's 36 * V plus
the engine's measured temporaries): one streaming pass over the connectome subjects per batch, every
lesion a column of the same GEMM. A lesion file that cannot be read or parsed is excluded (reported
with its path and the error) and the cohort continues. Each lesion's
maps go to a cache entry keyed by everything they depend on::

    <cache root>/conn-<label>_subj-<sha8>/<key>/      default root: <study>/derivatives/lnm/cache
        lesion-<key12>_conn-<label>_desc-{AvgR,AvgRFz,T}_funcmap.nii.gz   (Lead-DBS names, exact connectome header)
        weights.npz     sparse lesion occupancy over the connectome mask (int32 indices, float32 values)
        entry.json      key fields, lesion path (relative to the entry folder), qc (volume_mm3, volume_ml,
                        voxels_in_mask, ...), qa, file sizes, created
        qa-<qa16>.json  per-lesion QA computed later for other QA settings (relocation nulls, degree map)

    key = sha256(canonical JSON of {lesion_sha256, manifest_sha256, subject_ids_sha256, supersample,
                                    probabilistic, engine: {library version, output keys, RMAX, MIN_WEIGHT}})

An entry is built in a temporary folder and published with one ``os.replace`` of the folder; its
files are never modified afterwards (QA for other settings is a new file). Consumers copy the map
files (D-A21). An entry whose files are missing or have another size is recomputed. The lesion
sha256 is computed from the bytes that are mapped: each lesion file is read once into memory,
hashed, and parsed from those bytes.

The run writes ``<out>/maps.tsv`` (one row per mapped lesion: participant_id, lesion (see below),
lesion_sha256, key, AvgR, AvgFz and T (the cache files), volume_ml,
voxels_in_mask, fractional_sum, the pooling compatibility fields of ``export.compatibility_key`` and
the per-lesion QA) and
``<out>/cohortmaps.json`` (run id, connectome info, subject list, excluded subjects, cache folder).
cohortmaps.json is also a run record that ``export.check_poolable`` and ``compare_runs`` accept.

Paths are never stored as absolute local paths (SPEC 9.7 rule 10): the lesion and map columns of maps.tsv,
and the paths of cohortmaps.json (excluded lesion files, connectome, cache folders, maps.tsv, inbox files, the
absolute paths of the command line) are relative to <out>, the cache folder of runs.jsonl is relative to the run
root, and the lesion of entry.json is relative to the entry folder ("/" separators; the file name alone when the
file is on another drive; the sha256 and the cache key identify it). :func:`resolve_lesion_path` turns a stored
path back into an absolute one, and also accepts the absolute paths of files written before this rule;
:func:`read_maps_tsv`, :func:`read_record` and :func:`read_entry` return absolute paths.

Inclusion: with a study, the rows come from ``Study.analysis_rows``: rows with include = 0, rows not
approved by review when the study requires review (unless ``--include-unreviewed``) and rows that
failed normalization QC are excluded, reported as warning events and in cohortmaps.json. The job
never writes participants.tsv: a lesion volume that the study does not have yet (volume_ml = n/a) is
posted to the study inbox (``Study.post_results``, source "cohortmaps").

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage, 3 cancelled, 75 locked)::

    python -m LesionNetworkMappingLib.cohortmaps (--study DIR | --lesions-tsv F [--study DIR]) --bundle B --out DIR
        [--subjects N [--random] [--seed S] | --subject-list SPEC] [--cache DIR] [--threads K] [--budget-gb G]
        [--include-unreviewed] [--probabilistic] [--supersample K]
        [--null-relocations K [--null-seed S] [--null-voxels N] [--brain-mask MASK]]
        [--dry-run] [--cancel-file F] [--resume] [--run-root DIR] [--parent-run ID]

``--lesions-tsv`` has the columns participant_id, lesion (a path, relative to the TSV folder) and
optionally lesion_sha256: a lesion whose bytes do not have that hash is excluded. ``--budget-gb``
is how the GUI passes the setting Studio/LNM/MemoryBudgetGB; the default budget is
min(25% of the physical memory, 4 GB). ``--dry-run`` only looks the lesions up in the cache (no
computation, nothing written) and reports how many are new or changed. With ``--resume`` the
progress of a stopped run (``<out>/.cohortmaps-resume.json``) is kept; the cache is used in any case,
so a rerun never recomputes a published entry.
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import functools
import hashlib
import json
import secrets
import shutil
import tempfile
import time
import zlib

import numpy as np

from . import engine, jobs, runs
from .connectome import ConnectomeBundle, _is_cancelled
from .export import LIBRARY_VERSION, bundle_info, compatibility_key, file_sha256, now_iso, subjects_sha256, versions
from .geometry import to_masked_vector
from .nifti import XFORM_WARNING_MM, read_nifti, read_nifti_header, xform_disagreement

FORMAT_NAME = "slicer-lnm-cohortmaps"
ENTRY_FORMAT = "slicer-lnm-cache-entry"
QA_FORMAT = "slicer-lnm-cache-qa"
FORMAT_VERSION = 1
MAPS_TSV = "maps.tsv"
COHORTMAPS_JSON = "cohortmaps.json"
#: Paths of cohortmaps.json (dotted keys; "inbox" is a list), stored relative to its folder (:func:`read_record`)
RECORD_PATH_FIELDS = ("bundle.path", "cache_root", "cache_dir", "maps_tsv", "inbox", "null.brain_mask")
ENTRY_JSON = "entry.json"
WEIGHTS_NAME = "weights.npz"
RESUME_NAME = ".cohortmaps-resume.json"
LOCK_NAME = ".cohortmaps.lock"
CACHE_RELPATH = ("derivatives", "lnm", "cache")
STATISTIC = "T"

# Peak bytes of one engine pass per voxel and lesion. The addendum's 36 (float64 Welford accumulators 24
# + float32 output maps 12) leaves out the float64 temporaries of engine._Accumulator.maps() (average r,
# T and the standard error: 24) and the lesion weights held for the pass (stacked and per lesion: 8);
# measured with tracemalloc: 62 for the engine alone (/tmp/claude-0/verify/L-08-fix/mem_slope.txt).
BYTES_PER_LESION_VOXEL = 36 + 24 + 8
BYTES_PER_NULL_VOXEL = 12  # float32 null accumulators without r (8) + float32 T maps (4), per voxel and relocation
MAX_BATCH = 100
DEFAULT_BUDGET_FRACTION = 0.25
DEFAULT_BUDGET_CAP = 4 * 1024**3
FALLBACK_BUDGET = 1024**3  # when the physical memory cannot be read
EXCLUDED_MISSING = "lesion file not found"
EXCLUDED_UNREADABLE = "lesion file unreadable"
EXCLUDED_SHA = "lesion_sha256 mismatch (the file changed)"
EXCLUDED_NO_STUDY_ROW = "not in the study (review state unknown)"
EXCLUDED_CHANGED = "lesion file changed during the run"
EXCLUDED_ENTRY_GONE = "cache entry removed during the run"
STALE_TMP_SECONDS = 24 * 3600  # temporary folders of a killed job are removed after a day
# What reading, parsing or resampling a broken lesion file raises (truncated or non-gzip data, not a
# NIfTI-1 image, a 4D image, a singular affine, ...): the lesion is excluded, the cohort continues.
LESION_ERRORS = (OSError, EOFError, ValueError, ArithmeticError, IndexError, zlib.error)

MAP_KEYS = tuple(key for key, _ in engine.OUTPUT_KEYS)  # "AvgR", "AvgFz", "T"
QA_COLUMNS = ("degree_r", "degree_rho", "degree_level", "null_k", "null_r_mean_map", "null_percentile", "null_level")
COMPATIBILITY_COLUMNS = ("manifest_sha256", "subject_ids_sha256", "statistic", "supersample", "probabilistic", "library_version")
MAPS_COLUMNS = (
    "participant_id",
    "lesion",
    "lesion_sha256",
    "key",
    *MAP_KEYS,
    "volume_ml",
    "voxels_in_mask",
    "fractional_sum",
    "weight_outside_fraction",
    "cache",
    *COMPATIBILITY_COLUMNS,
    *QA_COLUMNS,
)
NA = "n/a"


# Memory budget and batches


def physical_memory_bytes():
    """Physical memory of this computer in bytes (POSIX sysconf, Windows GlobalMemoryStatusEx), or None."""
    try:
        if sys.platform == "win32":
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [  # noqa: RUF012 (ctypes structure layout)
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(_MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
            return None
        pages, page_size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return int(pages) * int(page_size)
    except (AttributeError, OSError, ValueError):
        pass
    return None


def default_budget_bytes():
    """Default memory budget of one engine pass: min(25% of the physical memory, 4 GB) (1 GB when unknown)."""
    total = physical_memory_bytes()
    if not total:
        return FALLBACK_BUDGET
    return int(min(DEFAULT_BUDGET_FRACTION * total, DEFAULT_BUDGET_CAP))


def lesion_pass_bytes(n_voxels, null_k=0, null_voxels=None):
    """Bytes one lesion adds to an engine pass: 68 * V (``BYTES_PER_LESION_VOXEL``), plus 12 * K * V' for K
    relocations mapped on V' voxels.
    """
    null_voxels = n_voxels if null_voxels is None else null_voxels
    return BYTES_PER_LESION_VOXEL * int(n_voxels) + BYTES_PER_NULL_VOXEL * max(int(null_k), 0) * int(null_voxels)


def batch_size(n_voxels, budget_bytes=None, null_k=0, null_voxels=None):
    """Lesions per engine pass: floor(budget / lesion_pass_bytes), clamped to [1, 100]."""
    budget = default_budget_bytes() if budget_bytes is None else int(budget_bytes)
    return int(min(MAX_BATCH, max(1, budget // max(lesion_pass_bytes(n_voxels, null_k, null_voxels), 1))))


def plan_batches(n_lesions, n_voxels, budget_bytes=None, null_k=0, null_voxels=None):
    """Batches of an engine run over ``n_lesions`` lesions: a list of (start, stop) index ranges.

    Every batch but the last holds :func:`batch_size` lesions (L_max = floor(budget / (68 * V)) without
    relocation nulls, clamped to [1, 100]).
    """
    size = batch_size(n_voxels, budget_bytes, null_k, null_voxels)
    return [(start, min(start + size, int(n_lesions))) for start in range(0, int(n_lesions), size)]


# Cache keys and entries


def engine_version():
    """The engine identity that is part of every cache key (library version, output keys and numeric constants)."""
    return {"library": LIBRARY_VERSION, "outputs": [list(pair) for pair in engine.OUTPUT_KEYS], "rmax": engine.RMAX, "min_weight": engine.MIN_WEIGHT}


def cache_key_fields(lesion_sha256, manifest_sha256, subject_ids_sha256, supersample=2, probabilistic=False):
    """The inputs a lesion's maps depend on (the content of its cache key)."""
    return {
        "lesion_sha256": lesion_sha256,
        "manifest_sha256": manifest_sha256,
        "subject_ids_sha256": subject_ids_sha256,
        "supersample": int(supersample),
        "probabilistic": bool(probabilistic),
        "engine": engine_version(),
    }


def cache_key(lesion_sha256, manifest_sha256, subject_ids_sha256, supersample=2, probabilistic=False):
    """Hex sha256 of the canonical JSON of :func:`cache_key_fields`."""
    return runs.params_sha256(cache_key_fields(lesion_sha256, manifest_sha256, subject_ids_sha256, supersample, probabilistic))


def cache_folder_name(bundle_name, subject_ids_sha256):
    """conn-<label>_subj-<first 8 hex digits of the subject list sha256>."""
    return f"conn-{engine.connectome_label(bundle_name)}_subj-{subject_ids_sha256[:8]}"


def default_cache_root(study_path):
    """<study>/derivatives/lnm/cache."""
    return os.path.join(os.fspath(study_path), *CACHE_RELPATH)


def cache_dir_for(cache_root, bundle_name, subject_ids_sha256):
    """The cache folder of a connectome and subject list: <cache root>/conn-<label>_subj-<sha8>."""
    return os.path.join(os.fspath(cache_root), cache_folder_name(bundle_name, subject_ids_sha256))


def entry_dir(cache_dir, key):
    return os.path.join(os.fspath(cache_dir), key)


def entry_stem(key):
    """Stem of the map files of an entry: "lesion-<first 12 hex digits of the key>"."""
    return f"lesion-{key[:12]}"


def portable_lesion_path(path, folder):
    """How cohort-map files store a lesion path: relative to ``folder`` with "/" separators, or the file
    name alone when the lesion is on another drive (never an absolute local path, SPEC 9.7 rule 10); None for None.
    """
    from .export import _portable_file

    return _portable_file(path, folder) if path else None


def resolve_lesion_path(value, folder):
    """Absolute path of a lesion path stored by :func:`portable_lesion_path` (relative to ``folder``);
    an absolute path (files written before relative paths were stored) is returned as it is; None for missing values.
    """
    if value is None or value in ("", NA):
        return None
    value = os.fspath(value)
    if os.path.isabs(value):
        return os.path.normpath(value)
    return os.path.normpath(os.path.join(os.path.abspath(os.fspath(folder)), *value.split("/")))


def read_entry(cache_dir, key):
    """The validated entry.json of a cache entry (with "dir", absolute "maps" paths and an absolute "lesion" path), or None.

    None when the entry is missing, unreadable, of another format or key, or when one of its files is
    missing or has another size than recorded (the entry is then recomputed).
    """
    folder = entry_dir(cache_dir, key)
    entry = _read_json(os.path.join(folder, ENTRY_JSON))
    if not isinstance(entry, dict) or entry.get("format") != ENTRY_FORMAT or entry.get("key") != key:
        return None
    if not isinstance(entry.get("version"), int) or entry["version"] > FORMAT_VERSION:
        return None
    files = entry.get("files")
    if not isinstance(files, dict) or any(name not in files for name in (*MAP_KEYS, "weights")):
        return None
    for item in files.values():
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or os.path.basename(item["name"]) != item["name"]:
            return None
        try:
            if os.path.getsize(os.path.join(folder, item["name"])) != item.get("size"):
                return None
        except OSError:
            return None
    entry["dir"] = folder
    entry["maps"] = {name: os.path.join(folder, files[name]["name"]) for name in MAP_KEYS}
    entry["lesion"] = resolve_lesion_path(entry.get("lesion"), folder)
    return entry


def read_weights(entry):
    """(indices int32, values float32, n_voxels) of a cache entry's sparse lesion occupancy."""
    with np.load(os.path.join(entry["dir"], entry["files"]["weights"]["name"])) as data:
        return data["indices"].astype(np.int32), data["values"].astype(np.float32), int(data["n_voxels"])


def dense_weights(entry, n_voxels):
    """The (V,) float32 lesion weights of a cache entry."""
    indices, values, stored = read_weights(entry)
    if stored != n_voxels:
        raise ValueError(f"The cache entry {entry.get('key')} has {stored} voxels, the connectome has {n_voxels}")
    weights = np.zeros(n_voxels, dtype=np.float32)
    weights[indices] = values
    return weights


def qa_settings(null_k=0, null_seed=0, null_voxels=None, brain_mask_sha256=None, degree_sha256=None):
    """What a lesion's QA depends on besides its maps: the relocation null settings and the degree map."""
    from .qa import MIN_INSIDE

    null = {"k": int(null_k)}
    if null_k:
        null.update({"seed": int(null_seed), "voxels": None if null_voxels is None else int(null_voxels), "min_inside": MIN_INSIDE, "brain_mask_sha256": brain_mask_sha256})
    return {"null": null, "degree_sha256": degree_sha256}


def qa_key(settings):
    return runs.params_sha256(settings)


def _qa_file(folder, key):
    return os.path.join(folder, f"qa-{key[:16]}.json")


def read_entry_qa(entry, key):
    """QA values of a cache entry for the QA settings ``key`` (:func:`qa_key`), or None when not computed."""
    stored = entry.get("qa")
    if isinstance(stored, dict) and stored.get("key") == key and isinstance(stored.get("values"), dict):
        return stored["values"]
    extra = _read_json(_qa_file(entry["dir"], key))
    if isinstance(extra, dict) and extra.get("format") == QA_FORMAT and extra.get("key") == key and isinstance(extra.get("values"), dict):
        return extra["values"]
    return None


def _write_entry_qa(entry, key, settings, values):
    """Add the QA of other settings to a published entry as a new file (the entry's files are never modified)."""
    jobs.write_json_atomic(_qa_file(entry["dir"], key), {"format": QA_FORMAT, "version": FORMAT_VERSION, "key": key, "settings": settings, "values": values, "created": now_iso()})


def _store_entry_qa(entry, context, values, participant_id, emit):
    """:func:`_write_entry_qa` for a run: a cache that cannot be written (read-only, full) only costs the
    caching of the QA, which the run still reports.
    """
    try:
        _write_entry_qa(entry, context.qa_key, context.qa_settings, values)
    except OSError as exc:
        emit("warning", code="qa", participant_id=participant_id, message=f"The QA of this lesion could not be saved in the cache ({exc}); it is computed again next time.")


def _cached_t_vector(entry, bundle):
    """The masked (V,) float32 T map of a cache entry."""
    data = np.asarray(read_nifti(entry["maps"]["T"])[0], dtype=np.float32)
    return to_masked_vector(data.reshape(bundle.shape), bundle.mask_indices)


def sweep_stale_temporaries(cache_dir, max_age=STALE_TMP_SECONDS):
    """Remove the .tmp-* and .trash-* folders that a killed job left in a cache folder more than
    ``max_age`` seconds ago (best effort; an entry is built in its temporary folder within seconds).
    Consumers of the cache ignore folders whose names start with a dot.
    """
    removed = []
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return removed
    limit = time.time() - max_age
    for name in names:
        path = os.path.join(cache_dir, name)
        if not (name.startswith(".tmp-") or name.startswith(".trash-")):
            continue
        try:
            if os.path.isdir(path) and not os.path.islink(path) and os.path.getmtime(path) < limit:
                shutil.rmtree(path)
                removed.append(name)
        except OSError:
            pass
    return removed


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _publish_entry(cache_dir, key, build):
    """Build an entry in a temporary folder with ``build(folder)`` and publish it with one os.replace.

    A valid entry published meanwhile (by another job) wins and the new one is discarded; an invalid
    one is moved aside and replaced. Returns the validated entry.
    """
    os.makedirs(cache_dir, exist_ok=True)
    tag = f"{key[:16]}-{os.getpid()}-{secrets.token_hex(3)}"
    tmp = os.path.join(cache_dir, f".tmp-{tag}")
    os.makedirs(tmp)
    trash = []
    try:
        build(tmp)
        target = entry_dir(cache_dir, key)
        for attempt in range(3):
            if read_entry(cache_dir, key) is not None:
                break  # published meanwhile by another job: a valid entry is never replaced
            if os.path.isdir(target):
                trash.append(os.path.join(cache_dir, f".trash-{tag}-{attempt}"))
                try:
                    os.replace(target, trash[-1])  # an invalid entry (missing files) is moved aside, never modified
                except FileNotFoundError:
                    pass
            try:
                os.replace(tmp, target)
                break
            except OSError:
                continue  # created between the check and the rename: check again
        entry = read_entry(cache_dir, key)
        if entry is None:
            raise OSError(f"The cache entry {target} could not be published")
        return entry
    finally:
        for folder in (tmp, *trash):
            if os.path.isdir(folder):
                shutil.rmtree(folder, ignore_errors=True)


def _write_entry_files(folder, key, key_fields, maps, index, bundle, weights, qc, lesion_path, participant_id, qa_record):
    single = engine.NetworkMaps(maps.avg_r[index:index + 1], maps.avg_fz[index:index + 1], maps.t[index:index + 1], maps.subject_ids)
    paths = engine.output_paths(folder, entry_stem(key), bundle.name)
    engine.write_maps(single, [entry_stem(key)], bundle, folder)
    positions = np.flatnonzero(weights > 0)
    with open(os.path.join(folder, WEIGHTS_NAME), "wb") as f:
        np.savez_compressed(f, indices=positions.astype(np.int32), values=weights[positions].astype(np.float32), n_voxels=np.int64(weights.size))
    files = {name: {"name": os.path.basename(paths[name]), "size": os.path.getsize(paths[name])} for name in MAP_KEYS}
    files["weights"] = {"name": WEIGHTS_NAME, "size": os.path.getsize(os.path.join(folder, WEIGHTS_NAME))}
    entry = {
        "format": ENTRY_FORMAT,
        "version": FORMAT_VERSION,
        "key": key,
        "key_fields": key_fields,
        "bundle": bundle.name,
        "n": maps.n,
        # Relative to the published entry folder (a sibling of this temporary folder)
        "lesion": portable_lesion_path(lesion_path, os.path.join(os.path.dirname(os.path.abspath(folder)), key)),
        "participant_id": participant_id,
        "qc": qc,
        "qa": qa_record,
        "files": files,
        "created": now_iso(),
        "software": runs.software(),
    }
    with open(os.path.join(folder, ENTRY_JSON), "w", encoding="utf-8") as f:
        f.write(jobs.to_json(entry) + "\n")


# Lesion inputs


def _normalize_lesions(lesions):
    """[(participant_id, absolute path, expected sha256 or None)] from (sid, path[, sha256]) tuples or dicts."""
    out, seen = [], set()
    for item in lesions:
        if isinstance(item, dict):
            sid, path, sha = item.get("participant_id"), item.get("lesion"), item.get("lesion_sha256")
        else:
            sid, path, sha = (*tuple(item), None)[:3]
        if not sid or not path:
            raise ValueError(f"A lesion needs a participant_id and a path: {item!r}")
        if sid in seen:
            raise ValueError(f"Duplicate participant_id {sid!r} in the lesion list")
        seen.add(sid)
        sha = None if sha in (None, "", NA) else str(sha).lower()
        out.append((str(sid), os.path.abspath(os.fspath(path)), sha))
    return out


def read_lesions_tsv(path):
    """Rows of a lesions TSV: [(participant_id, absolute lesion path, lesion_sha256 or None)].

    Columns: participant_id, lesion (relative paths are relative to the TSV folder) and optionally
    lesion_sha256 ("n/a" or empty: not checked).
    """
    from .study import read_tsv

    columns, rows = read_tsv(path)
    for column in ("participant_id", "lesion"):
        if column not in columns:
            raise ValueError(f"{path} has no {column} column")
    folder = os.path.dirname(os.path.abspath(os.fspath(path)))
    out = []
    for row in rows:
        lesion = row.get("lesion")
        if lesion in (None, "", NA):
            raise ValueError(f"{path}: {row.get('participant_id')} has no lesion path")
        lesion = lesion if os.path.isabs(lesion) else os.path.join(folder, *lesion.split("/"))
        out.append((row["participant_id"], os.path.normpath(lesion), row.get("lesion_sha256")))
    return _normalize_lesions(out)


def study_lesions(study, include_unreviewed=False, lesions=None):
    """The lesions of a study that an analysis may use, and the excluded subjects.

    :param study: a :class:`study.Study` or its folder.
    :param include_unreviewed: also use lesions that review has not approved when the study requires review.
    :param lesions: optional rows (e.g. from :func:`read_lesions_tsv`) to gate instead of the study's own
      lesion paths; rows of subjects the study does not know are excluded when review is required.
    :return: ([(participant_id, path, sha256 or None)], {reason: [participant ids]}). Reasons are those of
      ``Study.analysis_rows`` plus "lesion file not found".
    """
    from .study import Study

    study = Study.open(study) if isinstance(study, (str, os.PathLike)) else study
    require_review = False if include_unreviewed else None
    excluded = {}
    if lesions is None:
        rows, excluded = study.analysis_rows(columns=("lesion",), require_review=require_review)
        out = []
        for row in rows:
            path = study.abspath(row["lesion"])
            if not os.path.isfile(path):
                excluded.setdefault(EXCLUDED_MISSING, []).append(row["participant_id"])
                continue
            out.append((row["participant_id"], path, None))
        return out, excluded
    rows, study_excluded = study.analysis_rows(columns=(), require_review=require_review)
    allowed = {row["participant_id"] for row in rows}
    reason_of = {sid: reason for reason, sids in study_excluded.items() for sid in sids}
    review = study.review_required and not include_unreviewed
    out = []
    for sid, path, sha in _normalize_lesions(lesions):
        if sid in allowed or (sid not in study and not review):
            out.append((sid, path, sha))
        else:
            excluded.setdefault(reason_of.get(sid, EXCLUDED_NO_STUDY_ROW), []).append(sid)
    return out, excluded


def _read_lesion_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _load_lesion(path, data):
    """(volume data, affine, header) parsed from the given bytes of a lesion file."""
    name = os.path.basename(path).lower()
    suffix = ".nii.gz" if name.endswith(".nii.gz") else ".nii"
    handle, tmp_path = tempfile.mkstemp(prefix="lnm-lesion-", suffix=suffix)
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(data)
        return read_nifti(tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


# Planning (no computation)


class _Context:
    """Everything a cohort run depends on, resolved once (subjects, hashes, cache folder, QA settings)."""

    def __init__(self, bundle, subjects=None, random_subset=False, seed=0, subject_list=None, cache_root=None, study_path=None, out_dir=None, supersample=2, probabilistic=False, null_relocations=0, null_seed=0, null_voxels=engine.NULL_MAX_VOXELS, brain_mask=None, budget_bytes=None):
        self.bundle = ConnectomeBundle.open(bundle) if isinstance(bundle, (str, os.PathLike)) else bundle
        if self.bundle.kind != "timeseries":
            raise ValueError(f"The connectome {self.bundle.name!r} is a {self.bundle.kind!r} bundle; network maps need a time series connectome")
        self.subject_list = None
        if subject_list is not None:
            if subjects is not None or random_subset:
                raise ValueError("A subject list cannot be combined with a number of subjects or a random selection")
            self.subject_list = self.bundle.resolve_subject_list(subject_list)
            self.ids = list(self.subject_list.ids)
        else:
            self.ids = engine.resolve_subjects(self.bundle, subjects, random_subset, seed)
        self.subjects = {"n": len(self.ids), "requested": subjects, "random": bool(random_subset), "seed": seed}
        if self.subject_list is not None:
            self.subjects.update({"list_name": self.subject_list.name, "list_source": self.subject_list.source, "list_file": self.subject_list.path, "list_sha256": self.subject_list.sha256})
        self.seed = seed
        self.subject_ids_sha256 = subjects_sha256(self.ids)
        self.manifest_sha256 = file_sha256(os.path.join(self.bundle.path, "connectome.json"))
        self.supersample = int(supersample)
        self.probabilistic = bool(probabilistic)
        if cache_root is None:
            cache_root = default_cache_root(study_path) if study_path else os.path.join(os.fspath(out_dir or "."), "cache")
        self.cache_root = os.path.abspath(os.fspath(cache_root))
        self.cache_dir = cache_dir_for(self.cache_root, self.bundle.name, self.subject_ids_sha256)
        self.null_k = int(null_relocations)
        self.null_seed = int(null_seed)
        self.brain_mask = os.path.abspath(brain_mask) if brain_mask else None
        self.null_voxels = engine.null_voxel_sample(self.bundle.n_voxels, null_voxels, seed=self.null_seed) if self.null_k > 0 else None
        degree_path = os.path.join(self.bundle.path, "derived", "degree.npy")
        degree_sha = file_sha256(degree_path) if os.path.isfile(degree_path) else None
        self.qa_settings = qa_settings(
            self.null_k,
            self.null_seed,
            self.bundle.n_voxels if self.null_voxels is None else len(self.null_voxels),
            file_sha256(self.brain_mask) if self.brain_mask and self.null_k else None,
            degree_sha,
        )
        self.qa_key = qa_key(self.qa_settings)
        self.budget_bytes = default_budget_bytes() if budget_bytes is None else int(budget_bytes)
        self.batch_size = batch_size(self.bundle.n_voxels, self.budget_bytes, self.null_k, None if self.null_voxels is None else len(self.null_voxels))

    def key(self, lesion_sha256):
        return cache_key(lesion_sha256, self.manifest_sha256, self.subject_ids_sha256, self.supersample, self.probabilistic)

    def key_fields(self, lesion_sha256):
        return cache_key_fields(lesion_sha256, self.manifest_sha256, self.subject_ids_sha256, self.supersample, self.probabilistic)


def _check_lesion_header(path):
    """Raise one of ``LESION_ERRORS`` when the header of a lesion file shows that it cannot be mapped."""
    header = read_nifti_header(path)
    if len(header["shape"]) > 3:
        raise ValueError(f"A lesion must be a 3D image, got shape {tuple(header['shape'])}")


def _unreadable_detail(path, exc):
    return {"reason": EXCLUDED_UNREADABLE, "path": path, "message": f"{type(exc).__name__}: {exc}"}


def _lookup_items(context, lesions):
    """Hash every lesion file and look its key up in the cache.

    The header of a lesion that is not in the cache is checked, so that a file that cannot be mapped
    (not a NIfTI-1 image, not gzip data, 4D) is excluded up front, also by a dry run.

    :return: (items, {reason: [sids]}, {sid: {reason, path, message}} for unreadable files).
    """
    items, excluded, details = [], {}, {}
    for sid, path, expected in _normalize_lesions(lesions):
        if not os.path.isfile(path):
            excluded.setdefault(EXCLUDED_MISSING, []).append(sid)
            continue
        try:
            sha = file_sha256(path)
        except OSError as exc:
            excluded.setdefault(EXCLUDED_UNREADABLE, []).append(sid)
            details[sid] = _unreadable_detail(path, exc)
            continue
        if expected and expected != sha:
            excluded.setdefault(EXCLUDED_SHA, []).append(sid)
            continue
        key = context.key(sha)
        entry = read_entry(context.cache_dir, key)
        if entry is None:  # mapped before when it is in the cache
            try:
                _check_lesion_header(path)
            except LESION_ERRORS as exc:
                excluded.setdefault(EXCLUDED_UNREADABLE, []).append(sid)
                details[sid] = _unreadable_detail(path, exc)
                continue
        qa = read_entry_qa(entry, context.qa_key) if entry is not None else None
        items.append({"participant_id": sid, "lesion": path, "lesion_sha256": sha, "key": key, "hit": entry is not None, "qa_hit": qa is not None, "entry": entry, "qa": qa})
    return items, excluded, details


def _emit_unreadable(emit, sid, detail):
    emit("warning", code="unreadable", participant_id=sid, path=detail["path"], message=f"The lesion file of {sid} cannot be mapped and is excluded: {detail['message']} ({detail['path']})")


def lookup(lesions, bundle, cache_root=None, subjects=None, random_subset=False, seed=0, subject_list=None, study_path=None, out_dir=None, supersample=2, probabilistic=False, null_relocations=0, null_seed=0, null_voxels=engine.NULL_MAX_VOXELS, brain_mask=None, budget_bytes=None):
    """Look the lesions up in the cache without computing anything (A-04's plan: "N new or changed lesions").

    :return: dict with cache_dir, subject_ids_sha256, qa_key, budget_bytes, batch_size, "lesions" (one
      {participant_id, lesion, lesion_sha256, key, hit, qa_hit} per usable lesion), "excluded"
      ({reason: [participant ids]}), "excluded_details" ({participant id: {reason, path, message}} of the
      files that cannot be read or parsed), and the counts n, hits, new (lesions without an entry),
      qa_missing and passes (engine passes needed).
    """
    context = _Context(bundle, subjects, random_subset, seed, subject_list, cache_root, study_path, out_dir, supersample, probabilistic, null_relocations, null_seed, null_voxels, brain_mask, budget_bytes)
    items, excluded, details = _lookup_items(context, lesions)
    new_keys = {item["key"] for item in items if not item["hit"]}
    qa_keys = {item["key"] for item in items if item["hit"] and not item["qa_hit"] and context.null_k > 0}
    work = len(new_keys | qa_keys)
    return {
        "cache_dir": context.cache_dir,
        "subject_ids_sha256": context.subject_ids_sha256,
        "manifest_sha256": context.manifest_sha256,
        "qa_key": context.qa_key,
        "budget_bytes": context.budget_bytes,
        "batch_size": context.batch_size,
        "n": len(items),
        "hits": sum(1 for item in items if item["hit"]),
        "new": sum(1 for item in items if not item["hit"]),
        "qa_missing": sum(1 for item in items if item["hit"] and not item["qa_hit"]),
        "passes": -(-work // context.batch_size) if work else 0,
        "lesions": [{key: item[key] for key in ("participant_id", "lesion", "lesion_sha256", "key", "hit", "qa_hit")} for item in items],
        "excluded": excluded,
        "excluded_details": details,
    }


# Running


def _format(value, digits=6):
    if value is None:
        return NA
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.{digits}g}" if np.isfinite(value) else NA
    return str(value)


def write_maps_tsv(path, rows):
    """Write maps.tsv (``MAPS_COLUMNS``; "n/a" for missing values) atomically.

    The lesion and map columns (``MAP_KEYS``) are written relative to the maps.tsv folder (:func:`portable_lesion_path`):
    the file is shared with the study, so it holds no absolute local path (SPEC 9.7 rule 10).
    """
    folder = os.path.dirname(os.path.abspath(os.fspath(path)))
    lines = ["\t".join(MAPS_COLUMNS)]
    for row in rows:
        row = dict(row)
        for column in ("lesion", *MAP_KEYS):
            value = row.get(column)
            if isinstance(value, str) and os.path.isabs(value):
                row[column] = portable_lesion_path(value, folder)
        lines.append("\t".join(_format(row.get(column)).replace("\t", " ") for column in MAPS_COLUMNS))
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return path


def read_maps_tsv(path, resolve_paths=True):
    """Rows of a maps.tsv as dicts of text ("n/a" kept), in file order.

    :param resolve_paths: return the lesion and map columns (``MAP_KEYS``) as absolute paths (they are stored
      relative to the maps.tsv folder; older files hold absolute paths, kept as they are).
    """
    from .study import read_tsv

    rows = read_tsv(path)[1]
    if resolve_paths:
        folder = os.path.dirname(os.path.abspath(os.fspath(path)))
        for row in rows:
            for column in ("lesion", *MAP_KEYS):
                if column in row:
                    row[column] = resolve_lesion_path(row[column], folder) or row[column]
    return rows


def read_record(path):
    """The cohortmaps.json record at ``path`` with its paths (``RECORD_PATH_FIELDS``) absolute, or None when it
    cannot be read. The file stores them relative to its folder; older files hold absolute paths, kept as they are.
    """
    from .export import resolve_record_paths

    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    return resolve_record_paths(record, os.path.dirname(os.path.abspath(os.fspath(path))), RECORD_PATH_FIELDS)


def _file_record(record, out_dir):
    """The cohortmaps.json content of a run record: the excluded lesion files and the paths of
    ``RECORD_PATH_FIELDS`` (connectome, cache, maps.tsv, inbox files, command line) relative to ``out_dir``.

    The record returned by :func:`run` keeps absolute paths for the caller of this process.
    """
    from .export import portable_record_paths

    details = {}
    for sid, detail in (record.get("excluded_details") or {}).items():
        detail = dict(detail)
        path = detail.get("path")
        if path:
            detail["path"] = portable_lesion_path(path, out_dir)
            if isinstance(detail.get("message"), str):
                detail["message"] = detail["message"].replace(os.path.abspath(path), detail["path"])
        details[sid] = detail
    return portable_record_paths({**record, "excluded_details": details}, out_dir, RECORD_PATH_FIELDS)


def _qa_row(qa):
    qa = qa or {}
    null = qa.get("null") or {}
    return {
        "degree_r": qa.get("degree_r"),
        "degree_rho": qa.get("degree_rho"),
        "degree_level": qa.get("degree_level"),
        "null_k": null.get("k"),
        "null_r_mean_map": null.get("r_mean_map"),
        "null_percentile": null.get("percentile"),
        "null_level": qa.get("null_level") if null else None,
    }


def _jsonable(value):
    """JSON-compatible copy (numpy values converted, NaN as None) of QA values."""
    return json.loads(jobs.to_json(value))


def run(
    lesions,
    bundle,
    out_dir,
    subjects=None,
    random_subset=False,
    seed=0,
    subject_list=None,
    cache_root=None,
    study=None,
    threads=None,
    cancel=None,
    resume=False,
    budget_bytes=None,
    supersample=2,
    probabilistic=False,
    null_relocations=0,
    null_seed=0,
    null_voxels=engine.NULL_MAX_VOXELS,
    brain_mask=None,
    excluded=None,
    review=None,
    run_root=None,
    parent_runs=(),
    argv=None,
    emit=None,
    register=True,
):
    """Map a cohort of lesions with batched engine passes and the per-lesion cache; write maps.tsv and cohortmaps.json.

    :param lesions: [(participant_id, path[, expected lesion_sha256])] (see :func:`study_lesions`, :func:`read_lesions_tsv`).
    :param bundle: a :class:`ConnectomeBundle` or its folder.
    :param subjects, random_subset, seed: the connectome subjects (as the engine's --subjects/--random/--seed),
      or ``subject_list`` (a subset name, a file of ids or ids; see ``ConnectomeBundle.resolve_subject_list``).
    :param cache_root: folder of the cache (the entries go to <cache_root>/conn-<label>_subj-<sha8>/);
      default <study>/derivatives/lnm/cache, or <out_dir>/cache without a study.
    :param study: optional ``Study`` or study folder: volumes missing from it are posted to its inbox.
    :param cancel: optional ``jobs.CancelToken`` (or any callable/Event); a stopped run raises ``jobs.Cancelled``
      after the batches already finished were published (a rerun or ``resume`` skips them).
    :param resume: keep the progress state of a previous stopped run with the same parameters.
    :param budget_bytes: memory budget of one engine pass (default :func:`default_budget_bytes`).
    :param null_relocations, null_seed, null_voxels, brain_mask: relocated-lesion null QA, as the engine's options.
    :param excluded: {reason: [participant ids]} already excluded by the caller (reported with the job's own).
    :param review: {"required", "include_unreviewed"} recorded in cohortmaps.json.
    :param run_root: folder whose derivatives/lnm/runs.jsonl records the run (default: the study, else out_dir).
    :param emit: event callback ``emit(event, **fields)`` (default ``jobs.emit``: JSON lines on standard output).
    :return: the cohortmaps.json record.
    """
    emit = emit or jobs.emit
    started = time.monotonic()
    out_dir = os.path.abspath(os.fspath(out_dir))
    os.makedirs(out_dir, exist_ok=True)
    study_obj = None
    if study is not None:
        from .study import Study

        study_obj = Study.open(study) if isinstance(study, (str, os.PathLike)) else study
    context = _Context(
        bundle,
        subjects,
        random_subset,
        seed,
        subject_list,
        cache_root,
        study_obj.path if study_obj is not None else None,
        out_dir,
        supersample,
        probabilistic,
        null_relocations,
        null_seed,
        null_voxels,
        brain_mask,
        budget_bytes,
    )
    bundle = context.bundle
    lock = jobs.OutputLock(os.path.join(out_dir, LOCK_NAME))
    lock.acquire()
    if lock.reclaimed is not None:
        emit("warning", code="stale_lock", message=f"A stale lock of process {lock.reclaimed.get('pid')} on {out_dir} was reclaimed")
    run_root = os.path.abspath(os.fspath(run_root or (study_obj.path if study_obj is not None else out_dir)))
    run_id = None
    record = None
    try:
        for name in (MAPS_TSV, COHORTMAPS_JSON):  # never leave the outputs of an earlier run next to this one's
            if os.path.exists(os.path.join(out_dir, name)):
                os.remove(os.path.join(out_dir, name))
        sweep_stale_temporaries(context.cache_dir)
        excluded = {reason: list(sids) for reason, sids in (excluded or {}).items()}
        items, lookup_excluded, details = _lookup_items(context, lesions)
        for reason, sids in lookup_excluded.items():
            excluded.setdefault(reason, []).extend(sids)
        n_requested = len(items) + sum(len(sids) for sids in excluded.values())
        info = bundle_info(bundle)
        params = {
            "bundle": info.get("manifest_sha256"),
            "subject_ids_sha256": context.subject_ids_sha256,
            "supersample": context.supersample,
            "probabilistic": context.probabilistic,
            "qa": context.qa_settings,
            "lesions_sha256": runs.params_sha256(sorted((item["participant_id"], item["key"]) for item in items)),
        }
        state = jobs.ResumeState(os.path.join(out_dir, RESUME_NAME))
        if not resume:
            state.clear()
        elif state.done and state.data.get("params") != params:
            emit("warning", code="resume_mismatch", message="The saved progress belongs to other parameters; the cache entries are reused and the rest is computed.")
            state.clear()
        resumed = len(state.done)
        state.data = {"params": params}
        if register:
            run_id = runs.register(
                run_root,
                "cohortmaps",
                out_dir,
                parents=list(parent_runs or ()),
                params={**params, "cache_dir": portable_lesion_path(context.cache_dir, run_root), "n_lesions": len(items)},
                seeds={"subjects": context.seed, "null": context.null_seed},
            )
        record = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "run_id": run_id,
            "status": "running",
            "argv": list(argv or []),
            "start": now_iso(),
            "end": None,
            "bundle": info,
            "subjects": context.subjects,
            "n": len(context.ids),
            "subject_ids": list(context.ids),
            "subject_ids_sha256": context.subject_ids_sha256,
            "statistic": STATISTIC,
            "supersample": context.supersample,
            "probabilistic": context.probabilistic,
            "versions": versions(),
            "threads": threads,
            "cache_root": context.cache_root,
            "cache_dir": context.cache_dir,
            "budget_bytes": context.budget_bytes,
            "batch_size": context.batch_size,
            "null": {"k": context.null_k, "seed": context.null_seed, "voxels": context.qa_settings["null"].get("voxels"), "brain_mask": context.brain_mask},
            "qa_key": context.qa_key,
            "qa_settings": context.qa_settings,
            "review": review,
            "excluded": excluded,
            "excluded_n": sum(len(sids) for sids in excluded.values()),
            "excluded_details": details,
            "parents": list(parent_runs or ()),
        }
        for sid, detail in details.items():
            _emit_unreadable(emit, sid, detail)
        for reason, sids in excluded.items():
            emit("warning", code="excluded", reason=reason, participants=sids, message=f"{len(sids)} lesion(s) excluded ({reason}): {', '.join(sids[:10])}{' ...' if len(sids) > 10 else ''}")

        # Work: lesions without a valid entry (computed), and entries without QA for these settings
        work, seen = [], set()
        for item in items:
            needs_maps = not item["hit"]
            needs_null_qa = item["hit"] and not item["qa_hit"] and context.null_k > 0
            if (needs_maps or needs_null_qa) and item["key"] not in seen:
                seen.add(item["key"])
                work.append(item)
        batches = plan_batches(len(work), bundle.n_voxels, context.budget_bytes, context.null_k, None if context.null_voxels is None else len(context.null_voxels))
        record["batches"] = len(batches)
        emit(
            "start",
            job="cohortmaps",
            argv=list(argv or []),
            threads=threads,
            run_id=run_id,
            bundle=bundle.name,
            lesions=len(items),
            subjects=len(context.ids),
            cache_dir=context.cache_dir,
            cache_hits=sum(1 for item in items if item["hit"]),
            to_compute=len(work),
            batches=len(batches),
            batch_size=context.batch_size,
            budget_bytes=context.budget_bytes,
            resumed=resumed,
        )
        dropped, fresh_qa = _compute_batches(context, work, batches, state, cancel, emit, started)

        # Rows: every usable lesion in input order, from its (now) published entry
        degree = None
        rows, late = [], {}
        computed_keys = {item["key"] for item in work if not item["hit"]}  # "new" for the first participant of a key only
        for item in items:
            entry = read_entry(context.cache_dir, item["key"])
            if entry is None:  # excluded while computing (the file changed or cannot be parsed), or the entry vanished
                reason, detail = dropped.get(item["key"], (EXCLUDED_ENTRY_GONE, None))
                late.setdefault(reason, []).append(item["participant_id"])
                if detail is not None:
                    details[item["participant_id"]] = detail
                continue
            qa = read_entry_qa(entry, context.qa_key) or fresh_qa.get(item["key"])
            if qa is None and context.null_k > 0:  # the relocation pass failed for this entry: never report other settings' QA
                emit("warning", code="qa", participant_id=item["participant_id"], message="The relocation QA of this lesion is not available.")
            elif qa is None:  # degree QA only (no relocations): computed from the cached T map
                if degree is None:
                    degree = _read_degree(bundle, emit)
                from .qa import lesion_qa

                try:
                    qa = _jsonable(lesion_qa(_cached_t_vector(entry, bundle), degree if degree is not False else None))
                except LESION_ERRORS as exc:
                    emit("warning", code="qa", participant_id=item["participant_id"], message=f"The cached T map cannot be read ({exc}); the QA of this lesion is not available.")
                else:
                    _store_entry_qa(entry, context, qa, item["participant_id"], emit)
                    fresh_qa[item["key"]] = qa
            qc = entry.get("qc") or {}
            volume_mm3 = qc.get("volume_mm3")
            rows.append({
                "participant_id": item["participant_id"],
                "lesion": portable_lesion_path(item["lesion"], out_dir),
                "lesion_sha256": item["lesion_sha256"],
                "key": item["key"],
                **entry["maps"],
                "volume_ml": None if volume_mm3 is None else round(float(volume_mm3) / 1000.0, 4),
                "voxels_in_mask": qc.get("voxels_in_mask"),
                "fractional_sum": qc.get("weight_total"),
                "weight_outside_fraction": qc.get("weight_outside_fraction"),
                "cache": "new" if item["key"] in computed_keys else "hit",
                "qa": qa,
                **_qa_row(qa),
            })
            computed_keys.discard(item["key"])
        for reason, sids in late.items():
            excluded.setdefault(reason, []).extend(sids)
            emit("warning", code="excluded", reason=reason, participants=sids, message=f"{len(sids)} lesion(s) excluded ({reason}): {', '.join(sids[:10])}{' ...' if len(sids) > 10 else ''}")
        record["excluded_n"] = sum(len(sids) for sids in excluded.values())
        key = compatibility_key(record)
        for row in rows:
            row.update(dict(zip(COMPATIBILITY_COLUMNS, key, strict=True)))
        if not rows:
            raise jobs.JobError("No lesion could be mapped: every lesion was excluded", code="no_lesions")
        maps_path = write_maps_tsv(os.path.join(out_dir, MAPS_TSV), rows)
        emit("result", kind="maps", path=maps_path, n=len(rows))

        # Probabilistic volumes are weighted sums, not the binary volume the study column holds
        inbox = [] if context.probabilistic else _post_volumes(study_obj, rows, run_id, emit)
        if review is None and study_obj is not None:
            review = {"required": study_obj.review_required, "include_unreviewed": None}
        record.update({
            "status": "done",
            "end": now_iso(),
            "elapsed": round(time.monotonic() - started, 3),
            "compatibility_key": list(key),
            "maps_tsv": maps_path,
            "lesions": {
                "requested": n_requested,  # = mapped + excluded_n
                "mapped": len(rows),
                "cache_hits": sum(1 for row in rows if row["cache"] == "hit"),
                "computed": sum(1 for row in rows if row["cache"] == "new"),
                "qa_computed": sum(1 for item in work if item["hit"]),
            },
            "qa": {row["participant_id"]: row["qa"] for row in rows},
            "review": review,
            "inbox": inbox,
        })
        json_path = os.path.join(out_dir, COHORTMAPS_JSON)
        jobs.write_json_atomic(json_path, _file_record(record, out_dir))
        emit("result", kind="cohortmaps", path=json_path)
        state.clear()
        if run_id:
            runs.update(run_root, run_id, status="done", ended=record["end"], outputs=[MAPS_TSV, COHORTMAPS_JSON], summary={"mapped": len(rows), "excluded": record["excluded_n"], **{k: record["lesions"][k] for k in ("cache_hits", "computed")}})
        emit("done", outputs=[maps_path, json_path], elapsed=round(time.monotonic() - started, 3), run_id=run_id, n=len(rows), **{k: record["lesions"][k] for k in ("cache_hits", "computed")})
        return record
    except jobs.Cancelled as exc:
        if record is not None:
            record.update({"status": "cancelled", "end": now_iso(), "stop_reason": exc.reason})
            jobs.write_json_atomic(os.path.join(out_dir, COHORTMAPS_JSON), _file_record(record, out_dir))
        if run_id:
            runs.update(run_root, run_id, status="cancelled", ended=now_iso())
        raise
    except BaseException as exc:
        if run_id:
            try:
                runs.update(run_root, run_id, status="failed", ended=now_iso(), error=f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
        raise
    finally:
        lock.release()


def _read_degree(bundle, emit):
    """The bundle's degree map, or False when there is none or it cannot be read."""
    try:
        degree = bundle.degree
    except (OSError, ValueError) as exc:
        emit("warning", code="degree", message=f"The connectome degree map cannot be read ({exc}); degree similarity is not computed.")
        return False
    return False if degree is None else degree


def _prepare_item(context, item):
    """(weights, qc) of a work item, from the lesion bytes (new entries) or the cached weights (QA only),
    or (None, (reason, detail or None)) when the lesion is excluded: the bytes read now are hashed and must
    still have the sha256 of the cache key, and must parse as a 3D NIfTI-1 lesion.
    """
    if item["hit"]:
        try:
            return dense_weights(item["entry"], context.bundle.n_voxels), dict(item["entry"].get("qc") or {})
        except (*LESION_ERRORS, KeyError) as exc:  # the entry's maps are still used; only its relocation QA is missing
            return None, (EXCLUDED_UNREADABLE, _unreadable_detail(item["entry"]["dir"], exc))
    try:
        data = _read_lesion_bytes(item["lesion"])
    except OSError as exc:
        return None, (EXCLUDED_UNREADABLE, _unreadable_detail(item["lesion"], exc))
    if hashlib.sha256(data).hexdigest() != item["lesion_sha256"]:
        return None, (EXCLUDED_CHANGED, None)
    try:
        volume, affine, header = _load_lesion(item["lesion"], data)
        weights, qc = engine.prepare_lesion((volume, affine), context.bundle, supersample=context.supersample, binarize=not context.probabilistic)
        qc["xform_disagreement_mm"] = float(xform_disagreement(header))  # reported by _lesion_warnings, not stored
    except LESION_ERRORS as exc:
        return None, (EXCLUDED_UNREADABLE, _unreadable_detail(item["lesion"], exc))
    return weights, qc


def _lesion_warnings(item, qc, emit):
    """The engine's warnings about a newly mapped lesion: no overlap with the connectome mask, sform/qform disagreement."""
    if qc["voxels_in_mask"] == 0:
        emit("warning", code="outside_mask", participant_id=item["participant_id"], message="The lesion does not overlap the connectome mask; its maps are all zero.")
    disagreement = qc.pop("xform_disagreement_mm", 0.0)
    if disagreement > XFORM_WARNING_MM:
        emit(
            "warning",
            code="xform",
            participant_id=item["participant_id"],
            message=f"The sform and qform of the lesion file disagree by up to {disagreement:.2g} mm; the maps use the sform (as FSL does), while Slicer may display the file with the qform. Check that the lesion lies where it was drawn.",
        )


def _compute_batches(context, work, batches, state, cancel, emit, started):
    """Compute the work items batch by batch; publish each batch's entries (or QA files) when its pass completes.

    :return: ({cache key: (reason, detail or None)} of the lesions excluded while preparing them,
      {cache key: QA values} computed for cache hits).
    """
    from . import qa as qa_lib

    dropped, fresh_qa = {}, {}
    if not batches:
        return dropped, fresh_qa
    bundle = context.bundle
    n_subjects = len(context.ids)
    total_units = len(batches) * n_subjects
    degree = _read_degree(bundle, emit)
    degree = None if degree is False else degree
    brain_mask = engine._brain_mask_vector(context.brain_mask, bundle) if context.brain_mask and context.null_k > 0 else None
    done_lesions = 0
    for index, (start, stop) in enumerate(batches):
        if cancel is not None and _is_cancelled(cancel):
            raise jobs.Cancelled(done=done_lesions, reason=getattr(cancel, "reason", None))
        batch = work[start:stop]
        emit("stage", name="batch", index=index + 1, total=len(batches), lesions=[item["participant_id"] for item in batch])
        prepared = []
        for item in batch:
            if cancel is not None and _is_cancelled(cancel):
                raise jobs.Cancelled(done=done_lesions, reason=getattr(cancel, "reason", None))
            weights, qc = _prepare_item(context, item)
            if weights is None:
                reason, detail = qc
                if item["hit"]:  # the cached maps are still reported, without the relocation QA
                    emit("warning", code="qa", participant_id=item["participant_id"], message=f"The cached lesion weights cannot be read ({detail['message']}); the relocation QA of this lesion is not computed.")
                    continue
                dropped[item["key"]] = (reason, detail)
                if detail is not None:
                    _emit_unreadable(emit, item["participant_id"], detail)
                continue
            if not item["hit"]:
                _lesion_warnings(item, qc, emit)
            prepared.append((item, weights, qc))
        if not prepared:
            units = (index + 1) * n_subjects
            emit("progress", done=units, total=total_units, elapsed=round(time.monotonic() - started, 3), eta=None, batch=index + 1, batches=len(batches), subject=n_subjects, subjects=n_subjects)
            continue
        null_columns, null_counts = [], []
        if context.null_k > 0:
            for item, weights, _ in prepared:
                relocated = qa_lib.relocated_lesions(weights, bundle, brain_mask=brain_mask, k=context.null_k, seed=context.null_seed, sparse=True)
                null_counts.append(len(relocated))
                null_columns.extend(relocated)
                if len(relocated) < context.null_k and np.any(weights > 0):
                    emit("warning", code="relocations", participant_id=item["participant_id"], message=f"Only {len(relocated)} of {context.null_k} relocated lesions fit inside the brain mask.")
        else:
            null_counts = [0] * len(prepared)
        base = index * n_subjects

        def on_progress(done, total, elapsed, base=base, index=index):
            spent = time.monotonic() - started
            units = base + done
            eta = spent / units * (total_units - units) if units else None
            emit("progress", done=units, total=total_units, elapsed=round(spent, 3), eta=None if eta is None else round(eta, 3), batch=index + 1, batches=len(batches), subject=done, subjects=total)

        maps = engine.compute_network_maps(
            bundle,
            np.stack([weights for _, weights, _ in prepared]),
            subjects=context.ids,
            progress=on_progress,
            cancel=cancel,
            null_weights=null_columns or None,
            null_voxels=context.null_voxels,
        )
        if maps.cancelled:
            # Partial maps of this batch are discarded: only complete entries are ever published
            raise jobs.Cancelled(done=done_lesions, reason=getattr(cancel, "reason", None) or "stopped")
        offset = 0
        for column, (item, weights, qc) in enumerate(prepared):
            count = null_counts[column]
            nulls = maps.null_t[offset:offset + count] if count else None
            offset += count
            t_vector = maps.t[column]
            if item["hit"]:  # the QA describes the cached map that is reported, not this pass's recomputation
                try:
                    t_vector = _cached_t_vector(item["entry"], bundle)
                except LESION_ERRORS:
                    pass
            values = _jsonable(qa_lib.lesion_qa(t_vector, degree, nulls, maps.null_voxels))
            qa_record = {"key": context.qa_key, "settings": context.qa_settings, "values": values}
            if item["hit"]:
                fresh_qa[item["key"]] = values
                _store_entry_qa(item["entry"], context, values, item["participant_id"], emit)
            else:
                build = functools.partial(
                    _write_entry_files,
                    key=item["key"],
                    key_fields=context.key_fields(item["lesion_sha256"]),
                    maps=maps,
                    index=column,
                    bundle=bundle,
                    weights=weights,
                    qc=qc,
                    lesion_path=item["lesion"],
                    participant_id=item["participant_id"],
                    qa_record=qa_record,
                )
                _publish_entry(context.cache_dir, item["key"], build)
            state.mark_done(item["key"], save=False)
        done_lesions += len(prepared)
        state.save()
        emit("snapshot", batch=index + 1, batches=len(batches), lesions_done=done_lesions, lesions_total=len(work))
    return dropped, fresh_qa


def _post_volumes(study, rows, run_id, emit):
    """Post the lesion volumes that the study does not have yet (volume_ml n/a) to its inbox; return the inbox files."""
    if study is None:
        return []
    from .study import Study

    values = {}
    for row in rows:
        sid = row["participant_id"]
        if sid in study and study.value(sid, "volume_ml") is None and row.get("volume_ml") is not None:
            values[sid] = {"volume_ml": row["volume_ml"]}
    if not values:
        return []
    path = Study.post_results(study.path, values, "cohortmaps", audit={"event": "cohortmaps", "run_id": run_id, "columns": ["volume_ml"], "sids": sorted(values), "at": now_iso()})
    emit("result", kind="inbox", path=path, n=len(values))
    return [path]


# Command line


def _budget_bytes(gigabytes):
    return None if gigabytes is None else int(gigabytes * 1024**3)


def main(argv=None):
    """Command-line entry point (see the module docstring); returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.cohortmaps", description="Compute cohort lesion network maps in batches with a per-lesion cache.")
    jobs.add_common_arguments(parser)
    parser.add_argument("--study", default=None, help="study folder: its lesions (with review gating), volume posting and the default cache")
    parser.add_argument("--lesions-tsv", default=None, help="TSV of participant_id, lesion and optionally lesion_sha256 (used instead of the study's lesion paths)")
    parser.add_argument("--bundle", required=True, help="connectome bundle directory (.lnmc)")
    parser.add_argument("--out", required=True, help="output folder (maps.tsv, cohortmaps.json)")
    parser.add_argument("--subjects", type=jobs.positive_int, default=None, help="use only N connectome subjects (default: all)")
    parser.add_argument("--random", action="store_true", help="choose the N subjects at random")
    parser.add_argument("--seed", type=int, default=0, help="random seed for --random")
    parser.add_argument("--subject-list", default=None, metavar="SPEC", help="a named subset of the bundle, a text file of subject ids or a comma-separated list of ids")
    parser.add_argument("--cache", default=None, help="cache folder (default: <study>/derivatives/lnm/cache, or <out>/cache without a study)")
    parser.add_argument("--budget-gb", type=float, default=None, help="memory budget of one engine pass in GB (default: min(25%% of the memory, 4 GB))")
    parser.add_argument("--include-unreviewed", action="store_true", help="also map lesions that review has not approved when the study requires review")
    parser.add_argument("--probabilistic", action="store_true", help="use lesion image values as weights instead of binarizing")
    parser.add_argument("--supersample", type=jobs.positive_int, default=2, help="minimum sub-samples per axis when resampling lesions on oblique grids")
    parser.add_argument("--null-relocations", type=int, default=0, help="specificity QA: map K rigid relocations of each lesion (default: 0, off)")
    parser.add_argument("--null-seed", type=int, default=0, help="random seed of the lesion relocations")
    parser.add_argument("--null-voxels", type=int, default=engine.NULL_MAX_VOXELS, help=f"compute the null maps on a random sample of this many voxels (0: all; default {engine.NULL_MAX_VOXELS})")
    parser.add_argument("--brain-mask", default=None, help="brain mask NIfTI that relocated lesions must stay in (default: the connectome mask)")
    parser.add_argument("--dry-run", action="store_true", help="only look the lesions up in the cache and report the work (nothing is computed or written)")
    args = parser.parse_args(argv)
    if not args.study and not args.lesions_tsv:
        parser.error("give --study or --lesions-tsv")
    if args.subject_list is not None and (args.subjects is not None or args.random):
        parser.error("--subject-list cannot be combined with --subjects or --random")
    if args.budget_gb is not None and args.budget_gb <= 0:
        parser.error("--budget-gb must be positive")
    if args.null_relocations < 0 or args.null_voxels < 0:
        parser.error("--null-relocations and --null-voxels must be at least 0")
    cancel = jobs.CancelToken(args.cancel_file)
    try:
        lesions = read_lesions_tsv(args.lesions_tsv) if args.lesions_tsv else None
        excluded, review = {}, None
        if args.study:
            from .study import Study

            study = Study.open(args.study)
            lesions, excluded = study_lesions(study, args.include_unreviewed, lesions)
            review = {"required": study.review_required, "include_unreviewed": bool(args.include_unreviewed)}
        else:
            study = None
    except (OSError, ValueError, KeyError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
    options = {
        "subjects": args.subjects,
        "random_subset": args.random,
        "seed": args.seed,
        "subject_list": args.subject_list,
        "cache_root": args.cache,
        "supersample": args.supersample,
        "probabilistic": args.probabilistic,
        "null_relocations": args.null_relocations,
        "null_seed": args.null_seed,
        "null_voxels": args.null_voxels,
        "brain_mask": args.brain_mask,
        "budget_bytes": _budget_bytes(args.budget_gb),
    }
    try:
        if args.dry_run:
            jobs.emit("start", job="cohortmaps", argv=argv, threads=args.threads, dry_run=True)
            plan = lookup(lesions, args.bundle, study_path=study.path if study is not None else None, out_dir=args.out, **options)
            for reason, sids in excluded.items():
                plan["excluded"].setdefault(reason, []).extend(sids)
            for sid, detail in plan["excluded_details"].items():
                _emit_unreadable(jobs.emit, sid, detail)
            jobs.emit("result", kind="plan", **plan)
            jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None, n=plan["n"], new=plan["new"], hits=plan["hits"])
            return jobs.EXIT_OK
        run(
            lesions,
            args.bundle,
            args.out,
            study=study,
            threads=args.threads,
            cancel=cancel,
            resume=args.resume,
            excluded=excluded,
            review=review,
            run_root=args.run_root,
            parent_runs=args.parent_run,
            argv=argv,
            **options,
        )
    except (jobs.Cancelled, jobs.JobError):
        raise
    except (OSError, ValueError, KeyError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="exception") from None
    return jobs.EXIT_OK


if __name__ == "__main__":
    jobs.run_job(main, job="cohortmaps")

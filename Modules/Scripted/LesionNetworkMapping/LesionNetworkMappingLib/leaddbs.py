"""Lead-DBS interoperability and measured parity (addendum F19; SPEC 7.11 G0-L09).

Three parts, standard library, numpy and the library's own modules only (no Qt):

- **Naming.** Lead-DBS names a network map ``<lesion>_conn-<tag>x<Subset>_desc-{AvgR,AvgR_Fz,T}_funcmap.nii[.gz]``
  (for example ``sub_M2001_lesion.nii_conn-GSP1000v2xFullSet_desc-T_funcmap.nii.gz``). :func:`leaddbs_label` builds
  the ``<tag>x<Subset>`` label of a bundle (the Lead-DBS dataset tag in ``connectome.json`` "source", else the bundle
  name) and :func:`map_paths` gives the per-lesion file names in either style (``style="leaddbs"`` or ``"slicer"``,
  the latter being ``export.map_paths``). The desc labels are this library's (AvgR, AvgRFz, T).
- **Import** (:func:`import_leaddbs_maps`). Existing Lead-DBS outputs (``*_conn-*_desc-{AvgR, AvgR_Fz, AvgRFz,
  T}_funcmap.nii[.gz]``) are copied, header bytes unchanged, into ``<study>/derivatives/lnm/conn-<label>/`` with one
  sidecar per lesion (``provenance.source`` "Lead-DBS (imported)"), a ``mask.nii.gz`` (the voxels where every
  imported map is defined), a ``maps.tsv`` that the GLM, sLNM, cohort and figure jobs read, and a ``cohortmaps.json``
  record (format ``slicer-lnm-leaddbs-import``). The subject list of an imported map is unknown: its pooling key is
  ``unknown:<label>`` (connectome and subject list), so the pooling guard (``glm`` and ``export.check_poolable``)
  allows pooling only among imported maps of the same label, never with maps computed here. Imports are not
  registered in runs.jsonl (they are not runs of this library).
- **Parity** (:func:`run_parity`). The reference lesions of ``catalog/parity_leaddbs.json`` (24 lesions of the van
  den Heuvel 2026 repository, raw URLs pinned to one commit) with their published Lead-DBS GSP1000v2 FullSet AvgR
  and T maps are fetched on request (git blob id checked, sha256 recorded at the first download), mapped with the
  engine on the user's bundle, and compared per lesion: r(T), r(AvgR), the partial r given [muC_r, PC1] when the
  bundle's reference maps exist, max |dT|, the voxels whose status at |T| >= 7 changes and the Dice of the top-10%
  voxels. The bundle's degree map is compared with van den Heuvel's ``GSP1000_degree.nii.gz``. A bundle not derived
  from the Lead-DBS release (doi 10.7910/DVN/KKTJQC) is flagged "release differences included". Nothing is
  promised in advance: ``parity.json`` and ``methods.md`` report the measured numbers. The GSP data use terms
  must have been shown and accepted by the caller (``--accept-terms GSP-DUT-2014``); every output carries
  ``terms: GSP-DUT-2014``. Nothing GSP-derived ships with the app.

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage, 3 cancelled, 75 locked)::

    python -m LesionNetworkMappingLib.leaddbs import --folder DIR (--study DIR | --out DIR) [--label L] [--n N]
        [--terms auto|GSP-DUT-2014|none] [--recursive] [--overwrite]
    python -m LesionNetworkMappingLib.leaddbs parity --bundle B --out DIR --accept-terms GSP-DUT-2014
        [--cache DIR] [--lesions ID,ID] [--subject-list SPEC] [--offline] [--save-maps] [--run-root DIR]
        [--chunk N] [--catalog FILE] [--cancel-file F] [--threads K]
    python -m LesionNetworkMappingLib.leaddbs list [--cache DIR]
    python -m LesionNetworkMappingLib.leaddbs terms [--timeout S]
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import gzip
import hashlib
import json
import math
import re
import shutil
import time

from . import jobs

IMPORT_SOURCE = "Lead-DBS (imported)"
IMPORT_FORMAT = "slicer-lnm-leaddbs-import"
PARITY_FORMAT = "slicer-lnm-parity"
FORMAT_VERSION = 1
PARITY_CATALOG = "parity_leaddbs"
GSP_TERMS = "GSP-DUT-2014"
#: The Lead-DBS release of GSP1000 (Cohen 2023, Harvard Dataverse): the only release on which parity is like-for-like
KKTJQC_DOI = "10.7910/DVN/KKTJQC"
LEADDBS_TAGS = ("GSP1000v2",)
FULL_SET = "Full Set"
STYLES = ("slicer", "leaddbs")
#: Lead-DBS "desc" label -> map key of this library (maps.tsv column)
DESC_KEYS = {"AvgR": "AvgR", "AvgR_Fz": "AvgFz", "AvgRFz": "AvgFz", "T": "T"}
MAP_KEYS = ("AvgR", "AvgFz", "T")
FUNCMAP_PATTERN = re.compile(r"^(?P<lesion>.+?)_conn-(?P<conn>.+?)_desc-(?P<desc>AvgR_Fz|AvgRFz|AvgR|T)_funcmap\.nii(?:\.gz)?$")
#: Lead-DBS named subsets of the GSP1000 v2 dataset_info.json and their sizes (KKTJQC v4.0)
LEADDBS_SUBSET_N = {"FullSet": None, "First100Subjects": 100, "First5Subjects": 5, "FirstSubject": 1}
T_FLIP = 7.0
TOP_FRACTION = 0.1
RELEASE_CAVEAT = (
    "The connectome is not the Lead-DBS release of GSP1000 (doi:10.7910/DVN/KKTJQC): release, brain mask, run handling and "
    "preprocessing differences are included in these numbers"
)
SUBSET_CAVEAT = "The connectome subjects differ from Lead-DBS's Full Set (N={n} here, 1000 there): sampling differences are included in these numbers"
IMPORT_DESCRIPTION = "Lesion network map imported from Lead-DBS: computed outside this application, copied unchanged"
LOCK_NAME = ".leaddbs-import.lock"


#
# Naming
#


def _alnum(text):
    return re.sub(r"[^A-Za-z0-9]", "", str(text or ""))


def _source(bundle):
    """The "source" provenance of a bundle, a bundle info dict (``export.bundle_info``) or a record holding one."""
    if bundle is None or isinstance(bundle, str):
        return {}
    if isinstance(bundle, dict):
        source = bundle.get("source")
        if isinstance(source, dict):
            return source
        return {key: bundle.get(key) for key in ("tag", "doi") if bundle.get(key)}
    try:
        return (bundle.provenance or {}).get("source") or {}
    except ValueError:
        return {}


def _bundle_name(bundle):
    if isinstance(bundle, str):
        return bundle
    if isinstance(bundle, dict):
        return bundle.get("name") or ""
    return getattr(bundle, "name", "") or ""


def bundle_tag(bundle):
    """The Lead-DBS dataset tag of a bundle ("GSP1000v2" for a KKTJQC conversion), else the letters and digits of its name."""
    tag = _alnum(_source(bundle).get("tag"))
    if tag:
        return tag
    from .export import connectome_label

    return connectome_label(_bundle_name(bundle))


def leaddbs_label(bundle, subset_name=None):
    """The Lead-DBS connectome label ``<tag>x<Subset>`` (e.g. "GSP1000v2xFullSet") of a bundle and a named subset.

    :param bundle: a ``ConnectomeBundle``, a bundle info dict, or a connectome name.
    :param subset_name: the subset name ("First 100 Subjects"; a "user:" prefix is dropped); None or "" is the Full Set.
    """
    subset = str(subset_name or "").strip()
    if subset.lower().startswith("user:"):
        subset = subset[5:]
    return f"{bundle_tag(bundle)}x{_alnum(subset) or _alnum(FULL_SET)}"


def is_leaddbs_release(bundle):
    """True when the bundle is derived from the Lead-DBS GSP1000 release (its DOI is KKTJQC, or its source tag is a Lead-DBS tag)."""
    source = _source(bundle)
    doi = str(source.get("doi") or "").lower()
    return KKTJQC_DOI.lower() in doi or _alnum(source.get("tag")) in LEADDBS_TAGS


def map_paths(out_dir, lesion_name, bundle, subset_name=None, style="leaddbs", preview_n=None, desc_suffix=None):
    """Map file names of one lesion in ``style`` "leaddbs" (``conn-<tag>x<Subset>``) or "slicer" (``export.map_paths``).

    :return: {"AvgR", "AvgFz", "T": path}; the desc labels are AvgR, AvgRFz and T in both styles.
    """
    from .export import map_paths as slicer_map_paths

    if style not in STYLES:
        raise ValueError(f"Unknown naming style {style!r} (use {' or '.join(STYLES)})")
    if style == "leaddbs":
        return slicer_map_paths(out_dir, lesion_name, leaddbs_label(bundle, subset_name), preview_n=preview_n, desc_suffix=desc_suffix)
    return slicer_map_paths(out_dir, lesion_name, _bundle_name(bundle), preview_n=preview_n, desc_suffix=desc_suffix)


def export_leaddbs_names(maps, out_dir, bundle, subset_name=None):
    """Copy map files to their Lead-DBS names: ``maps`` is {lesion name: {"AvgR", "AvgFz", "T": path}}; returns the same with the copies."""
    os.makedirs(out_dir, exist_ok=True)
    out = {}
    for name, files in maps.items():
        targets = map_paths(out_dir, name, bundle, subset_name, style="leaddbs")
        out[name] = {}
        for key, source in files.items():
            if not source:
                continue
            if not (os.path.exists(targets[key]) and os.path.samefile(source, targets[key])):
                shutil.copyfile(source, targets[key])
            out[name][key] = targets[key]
    return out


#
# Import
#


def parse_funcmap_name(filename):
    """{"lesion", "conn", "desc", "key"} of a Lead-DBS map file name, or None when it is not one."""
    match = FUNCMAP_PATTERN.match(os.path.basename(os.fspath(filename)))
    if not match:
        return None
    lesion = re.sub(r"\.nii(\.gz)?$", "", match.group("lesion"), flags=re.IGNORECASE)
    return {"lesion": lesion, "conn": match.group("conn"), "desc": match.group("desc"), "key": DESC_KEYS[match.group("desc")]}


def scan_folder(folder, recursive=False):
    """Lead-DBS maps of a folder: {(lesion, conn): {key: path}} (sorted file names; a key found twice keeps the first)."""
    folder = os.fspath(folder)
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Folder not found: {folder}")
    paths = []
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs.sort()
            paths += [os.path.join(root, name) for name in files]
    else:
        paths = [os.path.join(folder, name) for name in os.listdir(folder)]
    found = {}
    for path in sorted(paths):
        info = parse_funcmap_name(path) if os.path.isfile(path) else None
        if info is not None:
            found.setdefault((info["lesion"], info["conn"]), {}).setdefault(info["key"], os.path.abspath(path))
    return found


def _tokens(text):
    return [token.lower() for token in re.split(r"[^A-Za-z0-9]+", text) if token]


def derived_participant_id(lesion_name):
    """A BIDS participant id for a lesion name: its "sub-<label>" entity, "sub_<label>" (Lead-DBS style), else "sub-" + its letters and digits."""
    match = re.match(r"^sub-([A-Za-z0-9]+)", lesion_name)
    if match:
        return f"sub-{match.group(1)}"
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", lesion_name) if part]
    if len(parts) > 1 and parts[0].lower() == "sub":
        return f"sub-{parts[1]}"
    return f"sub-{_alnum(lesion_name) or 'lesion'}"


def match_participants(lesion_names, participant_ids=()):
    """{lesion name: participant id}: the study participant whose label (without "sub-") is a whole token of the
    lesion name when exactly one matches, else :func:`derived_participant_id`. Returns (mapping, unmatched names).
    """
    labels = {}
    for pid in participant_ids or ():
        label = pid[4:] if pid.lower().startswith("sub-") else pid
        labels.setdefault(label.lower(), []).append(pid)
    mapping, unmatched = {}, []
    for name in lesion_names:
        tokens = set(_tokens(name))
        hits = sorted({pid for label, pids in labels.items() if label in tokens for pid in pids})
        if len(hits) == 1:
            mapping[name] = hits[0]
        else:
            mapping[name] = derived_participant_id(name)
            if labels:
                unmatched.append(name)
    return mapping, unmatched


def infer_n(label):
    """Connectome subjects of a Lead-DBS label: the named subset's size, or N of "GSP<N>" for the Full Set, else None."""
    match = re.match(r"^(?P<tag>.+?)x(?P<subset>[A-Za-z0-9]+)$", str(label or ""))
    if not match:
        return None
    subset = match.group("subset")
    if LEADDBS_SUBSET_N.get(subset):
        return LEADDBS_SUBSET_N[subset]
    if subset == "FullSet":
        number = re.match(r"^GSP(\d+)", match.group("tag"))
        return int(number.group(1)) if number else None
    return None


def imported_compatibility(label):
    """The pooling key of imported maps (``cohortmaps.COMPATIBILITY_COLUMNS`` order): unknown subjects, keyed by the label."""
    unknown = f"unknown:{label}"
    return (unknown, unknown, "T", None, None, IMPORT_SOURCE)


def _terms_for(label, terms):
    if terms in (None, "none", ""):
        return None
    if terms == "auto":
        return GSP_TERMS if "GSP" in str(label).upper() else None
    return terms


def _copy_map(source, target):
    """Copy a NIfTI map to ``target`` (.nii.gz): a .nii.gz byte for byte, a .nii gzip-compressed (same header and data bytes)."""
    tmp = f"{target}.{os.getpid()}.tmp"
    try:
        if source.lower().endswith(".gz"):
            shutil.copyfile(source, tmp)
        else:
            with open(source, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return target


def _same_grid(a, b, tolerance=1e-4):
    import numpy as np

    return tuple(a["shape"][:3]) == tuple(b["shape"][:3]) and np.allclose(np.asarray(a["affine"]), np.asarray(b["affine"]), atol=tolerance)


def _imported_mask(paths, header):
    """uint8 mask of the voxels where every map is finite and non-zero (Lead-DBS writes 0 outside its brain mask)."""
    import numpy as np

    from .nifti import read_nifti

    mask = None
    for path in paths:
        data = np.asarray(read_nifti(path)[0], dtype=np.float64).reshape(tuple(header["shape"][:3]))
        inside = np.isfinite(data) & (data != 0)
        mask = inside if mask is None else (mask & inside)
    return mask.astype(np.uint8)


def import_leaddbs_maps(folder, study=None, connectome_label=None, out_dir=None, n=None, terms="auto", recursive=False, overwrite=False, emit=None, cancel=None):
    """Import Lead-DBS network maps for cohort statistics and figures; return the import record (absolute paths).

    :param folder: folder of Lead-DBS outputs (``*_conn-*_desc-{AvgR,AvgR_Fz,AvgRFz,T}_funcmap.nii[.gz]``).
    :param study: a ``study.Study`` or a study folder: the maps go to ``<study>/derivatives/lnm/conn-<label>/`` and the
      lesions are matched to its participants (:func:`match_participants`). Without a study give ``out_dir``.
    :param connectome_label: the label of the maps to import (the ``conn-`` value of the file names, letters and
      digits); default: the only label in the folder. When no file has this label but the folder holds one label,
      those maps are imported under the given label (the original is kept in the sidecars).
    :param n: connectome subjects of the maps (df = N - 1 of the T maps); default: :func:`infer_n` of the label.
    :param terms: "auto" (GSP-DUT-2014 for a GSP label), a licence label, or "none".
    :param overwrite: replace a folder that holds maps computed by this library (an earlier import is always replaced).
    """
    from .cohortmaps import MAPS_COLUMNS, write_maps_tsv
    from .export import DISCLAIMER, SIDECAR_FORMAT, derivatives_dir, file_sha256, now_iso, sidecar_path
    from .export import map_paths as slicer_map_paths
    from .nifti import read_nifti_header, write_nifti
    from .study import Study

    emit = emit or (lambda *args, **kwargs: None)
    found = scan_folder(folder, recursive=recursive)
    if not found:
        raise jobs.JobError(f"No Lead-DBS maps (*_conn-*_desc-{{AvgR,AvgR_Fz,T}}_funcmap.nii[.gz]) in {folder}", code="no_maps")
    labels = sorted({_alnum(conn) for _, conn in found})
    wanted = _alnum(connectome_label) if connectome_label else None
    if wanted is None:
        if len(labels) > 1:
            raise jobs.JobError(f"The folder holds maps of several connectomes ({', '.join(labels)}): choose one label", code="usage", exit_code=jobs.EXIT_USAGE, labels=labels)
        wanted = labels[0]
    selected = {key: files for key, files in found.items() if _alnum(key[1]) == wanted}
    if not selected and len(labels) == 1:
        selected = found
        emit("warning", code="relabelled", message=f"The maps are labelled conn-{labels[0]}; they are imported as conn-{wanted}")
    if not selected:
        raise jobs.JobError(f"No map has the connectome label {wanted} (found: {', '.join(labels)})", code="usage", exit_code=jobs.EXIT_USAGE, labels=labels)
    skipped = sorted({_alnum(conn) for (_, conn) in found if (_, conn) not in selected})
    if skipped:
        emit("warning", code="other_labels", message=f"Maps of other connectomes were not imported: {', '.join(skipped)}")
    label = wanted

    # Destination and participants
    study_obj = None
    if study is not None:
        study_obj = study if isinstance(study, Study) else Study.open(study)
    if out_dir is None:
        if study_obj is None:
            raise jobs.JobError("Give a study or an output folder", code="usage", exit_code=jobs.EXIT_USAGE)
        out_dir = derivatives_dir(study_obj.path, label)
    out_dir = os.path.abspath(os.fspath(out_dir))
    record_path = os.path.join(out_dir, "cohortmaps.json")
    if os.path.isdir(out_dir) and os.listdir(out_dir) and not overwrite:
        previous = _read_json(record_path)
        if not (previous and previous.get("format") == IMPORT_FORMAT):
            raise jobs.JobError(f"{out_dir} already holds maps that were not imported from Lead-DBS: choose another label or allow overwriting", code="exists", exit_code=jobs.EXIT_USAGE)
    os.makedirs(out_dir, exist_ok=True)
    lock = jobs.OutputLock(os.path.join(out_dir, LOCK_NAME))
    lock.acquire()
    try:
        names = sorted({lesion for lesion, _ in selected})
        mapping, unmatched = match_participants(names, study_obj.subject_ids if study_obj is not None else ())
        if unmatched:
            emit("warning", code="unmatched", lesions=unmatched, message=f"{len(unmatched)} map(s) match no participant of the study; they get their own ids: {', '.join(mapping[name] for name in unmatched[:5])}")
        duplicates = sorted({pid for pid in mapping.values() if list(mapping.values()).count(pid) > 1})
        if duplicates:
            raise jobs.JobError(f"Several lesions map to the same participant ({', '.join(duplicates[:5])}): rename the files or import them separately", code="duplicates", duplicates=duplicates)

        # One grid for every map (the maps are pooled voxel by voxel)
        reference_header, grid_problems = None, []
        for (lesion, _), files in sorted(selected.items()):
            for path in files.values():
                header = read_nifti_header(path)
                if reference_header is None:
                    reference_header = header
                elif not _same_grid(reference_header, header):
                    grid_problems.append(os.path.basename(path))
        if grid_problems:
            raise jobs.JobError(f"{len(grid_problems)} map(s) are not on the grid of the others ({', '.join(grid_problems[:3])}); they cannot be pooled", code="grid")
        if n is None:
            n = infer_n(label)
        terms_value = _terms_for(label, terms)
        compat = imported_compatibility(label)

        # Copy the maps, write the sidecars
        rows, total, t_paths = [], len(selected), []
        started = time.monotonic()
        stale = {name for name in os.listdir(out_dir) if name.endswith(("_funcmap.nii.gz", "_lnm.json"))}
        for index, ((lesion, conn), files) in enumerate(sorted(selected.items())):
            if cancel is not None:
                cancel.raise_if_cancelled(done=index)
            pid = mapping[lesion]
            targets = slicer_map_paths(out_dir, pid, label)
            copied, originals = {}, {}
            for key in MAP_KEYS:
                source = files.get(key)
                if not source:
                    continue
                _copy_map(source, targets[key])
                copied[key] = targets[key]
                originals[key] = {"file": os.path.basename(source), "sha256": file_sha256(source)}
                stale.discard(os.path.basename(targets[key]))
            if "T" in copied:
                t_paths.append(copied["T"])
            sidecar = {
                "format": SIDECAR_FORMAT,
                "version": 1,
                "Description": IMPORT_DESCRIPTION,
                "provenance": {"source": IMPORT_SOURCE, "lesion_name": lesion, "connectome_label_in_files": conn, "original_files": originals, "imported": now_iso()},
                "connectome": {"name": label, "label": label, "n_subjects": n, "manifest_sha256": compat[0]},
                "lesion": {"name": lesion, "file": None, "sha256": None},
                "N": n,
                "df": None if n is None else max(int(n) - 1, 0),
                "subject_ids_sha256": compat[1],
                "compat": dict(zip(("manifest_sha256", "subject_ids_sha256", "statistic", "supersample", "probabilistic", "library_version"), compat, strict=True)),
                "maps": {key: os.path.basename(path) for key, path in copied.items()},
                "disclaimer": DISCLAIMER,
            }
            if terms_value:
                sidecar["terms"] = terms_value
            side = sidecar_path(out_dir, pid, label)
            jobs.write_json_atomic(side, sidecar)
            stale.discard(os.path.basename(side))
            key_text = (originals.get("T") or next(iter(originals.values())))["sha256"]
            rows.append({
                "participant_id": pid,
                "lesion": None,
                "lesion_sha256": None,
                "key": f"leaddbs-{key_text[:16]}",
                **{key: copied.get(key) for key in MAP_KEYS},
                "cache": "imported",
                **dict(zip(("manifest_sha256", "subject_ids_sha256", "statistic", "supersample", "probabilistic", "library_version"), compat, strict=True)),
            })
            emit("progress", **_progress_fields(index + 1, total, started))
        for name in sorted(stale):  # maps of an earlier import that this one does not have
            os.remove(os.path.join(out_dir, name))

        # Mask, maps.tsv and the record
        mask_path = os.path.join(out_dir, "mask.nii.gz")
        mask_sources = t_paths or [path for row in rows for path in (row.get("AvgR"), row.get("AvgFz")) if path]
        mask = _imported_mask(mask_sources, reference_header)
        write_nifti(mask_path, mask, reference_header["affine"], descrip="LNM mask of imported Lead-DBS maps")
        rows.sort(key=lambda row: row["participant_id"])
        maps_tsv = write_maps_tsv(os.path.join(out_dir, "maps.tsv"), [{column: row.get(column) for column in MAPS_COLUMNS} for row in rows])
        record = {
            "format": IMPORT_FORMAT,
            "version": FORMAT_VERSION,
            "status": "done",
            "run_id": None,
            "source": IMPORT_SOURCE,
            "created": now_iso(),
            "bundle": {"name": label, "label": label, "path": None, "manifest_sha256": compat[0], "licence": terms_value, "n_subjects": n},
            "n": n,
            "statistic": "T",
            "subject_ids_sha256": compat[1],
            "compatibility_key": list(compat),
            "versions": {"LesionNetworkMappingLib": IMPORT_SOURCE},
            "maps_tsv": "maps.tsv",
            "mask": "mask.nii.gz",
            "mask_voxels": int(mask.sum()),
            "lesions": {"requested": len(selected), "mapped": len(rows)},
            "participants": {mapping[name]: name for name in names},
            "unmatched": unmatched,
            "terms": terms_value,
            "note": "Maps computed by Lead-DBS outside this application; their subject list is unknown, so they pool only with imported maps of the same label",
        }
        jobs.write_json_atomic(record_path, record)
        emit("result", kind="maps", path=maps_tsv, n=len(rows))
        emit("result", kind="import", path=record_path)
        return {**record, "maps_tsv": maps_tsv, "mask": mask_path, "path": record_path, "out_dir": out_dir}
    finally:
        lock.release()


def _progress_fields(done, total, started):
    """The fields of a progress event (as ``jobs.progress``), for an ``emit`` callable."""
    spent = max(time.monotonic() - started, 0.0)
    eta = spent / done * max(total - done, 0) if done > 0 and total else None
    return {"done": done, "total": total, "elapsed": round(spent, 3), "eta": None if eta is None else round(eta, 3)}


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


#
# Parity: reference files
#


def load_parity_catalog(path=None):
    """The parity catalog (``fetch.Catalog``; its JSON in ``.data`` holds "lesions", "reference", "thresholds")."""
    from . import fetch

    return fetch.load_catalog(path or os.path.join(fetch.CATALOG_DIR, f"{PARITY_CATALOG}.json"))


def parity_lesions(catalog, lesion_ids=None):
    """The lesion rows of the catalog ({"id", "set", "mask", "AvgR", "T"[, "pairing_note"]}), all or those of ``lesion_ids``."""
    rows = list(catalog.data.get("lesions") or [])
    if not lesion_ids:
        return rows
    wanted = [text.strip() for text in lesion_ids if text and text.strip()]
    known = {row["id"]: row for row in rows}
    unknown = [text for text in wanted if text not in known]
    if unknown:
        raise jobs.JobError(f"Unknown parity lesion(s): {', '.join(unknown)} (see 'leaddbs list')", code="usage", exit_code=jobs.EXIT_USAGE)
    return [known[text] for text in wanted]


def git_blob_sha1(path):
    """The git blob id of a file (sha1 of "blob <size>", a NUL byte and the content): what a pinned repository commit records."""
    digest = hashlib.sha1(f"blob {os.path.getsize(path)}\0".encode("ascii"))  # noqa: S324 (git's object id, not a security hash)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def reference_entries(catalog, lesions, degree=True):
    """The fetch entries (``fetch.Resource``) the parity of ``lesions`` needs: masks, Lead-DBS maps and the degree map."""
    ids = [row[key] for row in lesions for key in ("mask", "AvgR", "T")]
    if degree and "degree" in catalog:
        ids.append("degree")
    return [catalog.get(entry_id) for entry_id in ids]


def missing_files(catalog=None, lesion_ids=None, cache=None, degree=True):
    """Ids of the reference files that are not in the cache (no network)."""
    from . import fetch

    catalog = catalog or load_parity_catalog()
    return [resource.id for resource in reference_entries(catalog, parity_lesions(catalog, lesion_ids), degree) if not fetch.is_cached(resource, cache)]


def fetch_reference(catalog, lesions, cache=None, offline=None, cancel=None, emit=None, degree=True):
    """Fetch (or reuse) the reference files; return {entry id: local path}. The git blob id of each file is checked."""
    from . import fetch

    emit = emit or (lambda *args, **kwargs: None)
    resources = reference_entries(catalog, lesions, degree)
    paths, started = {}, time.monotonic()
    for index, resource in enumerate(resources):
        if cancel is not None:
            cancel.raise_if_cancelled(done=index)
        try:
            path = fetch.fetch(resource, cache, cancel=cancel, offline=offline)
        except fetch.FetchError as exc:
            raise jobs.JobError(f"{resource.title}: {exc.message}", code=f"fetch_{exc.code}", resource_id=resource.id) from None
        expected = resource.get("git_blob")
        if expected and git_blob_sha1(path) != expected:
            raise jobs.JobError(f"{resource.title} does not match the pinned repository file (git blob {expected[:12]}); the cached copy at {path} was not used", code="checksum", resource_id=resource.id)
        paths[resource.id] = path
        emit("progress", **_progress_fields(index + 1, len(resources), started), stage="fetch")
    return paths


def live_terms_source():
    """(api URL, JSON field, terms URL) of the live GSP terms text of the Lead-DBS GSP1000 release (catalog/connectomes.json)."""
    from . import catalog as catalogs

    entry = catalogs.load_catalog().get("gsp1000-kktjqc")
    terms = entry.get("terms") or {}
    return terms.get("api"), terms.get("field"), terms.get("url")


def fetch_live_terms(timeout=15.0, session=None):
    """The provider's live GSP data use terms text (Harvard Dataverse, KKTJQC), fetched now; raises ``fetch.FetchError``."""
    from . import fetch

    api, field, _ = live_terms_source()
    if fetch.offline_requested():
        raise fetch.OfflineError("The network is switched off: the data use terms cannot be fetched.")
    session = session or fetch.make_session()
    try:
        response = session.get(api, timeout=(fetch.CONNECT_TIMEOUT_S, timeout))
        response.raise_for_status()
        value = response.json()
    except Exception as exc:
        raise fetch.OfflineError(f"The data use terms could not be fetched from {api}: {exc}") from None
    for part in str(field).split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise fetch.FetchError(f"The answer of {api} has no terms text at {field}", code="terms")
    return value


#
# Parity: metrics
#


def _finite_pair(a, b):
    import numpy as np

    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    if a.shape != b.shape:
        raise ValueError(f"The maps have {a.size} and {b.size} voxels")
    valid = np.isfinite(a) & np.isfinite(b)
    return a, b, valid


def top_fraction_dice(a, b, fraction=TOP_FRACTION):
    """Dice of the top ``fraction`` voxels (highest values) of two maps over their common finite voxels; NaN when empty."""
    import numpy as np

    a, b, valid = _finite_pair(a, b)
    count = math.ceil(fraction * int(np.count_nonzero(valid)))
    if count < 1:
        return float("nan")
    idx = np.flatnonzero(valid)
    top_a = idx[np.argpartition(-a[idx], count - 1)[:count]]
    top_b = idx[np.argpartition(-b[idx], count - 1)[:count]]
    return float(2.0 * np.intersect1d(top_a, top_b, assume_unique=True).size / (2 * count))


def threshold_flips(a, b, threshold=T_FLIP):
    """Voxels whose status at |T| >= threshold (above, below -threshold, or neither) differs between two T maps."""
    import numpy as np

    a, b, valid = _finite_pair(a, b)

    def status(values):
        return np.where(values >= threshold, 1, np.where(values <= -threshold, -1, 0))

    sa, sb = status(a[valid]), status(b[valid])
    return {"n_flip": int(np.count_nonzero(sa != sb)), "n_supra_ours": int(np.count_nonzero(sa)), "n_supra_leaddbs": int(np.count_nonzero(sb)), "n_voxels": int(valid.sum())}


def parity_metrics(ours_t, ref_t, ours_r=None, ref_r=None, covariates=None, threshold=T_FLIP, fraction=TOP_FRACTION):
    """Per-lesion parity numbers of our maps against Lead-DBS's (all (V,) vectors over the bundle voxels; NaN = no value).

    Keys: n_voxels (common finite voxels of the T maps), r_T, r_AvgR, partial_r_T (given ``covariates``, e.g. [muC_r,
    PC1]; None without them), max_abs_dT, mean_abs_dT, n_flip (status changes at |T| >= threshold), flip_fraction,
    n_supra_ours, n_supra_leaddbs, dice_top (the top ``fraction`` voxels by T), t_threshold and top_fraction.
    """
    import numpy as np

    from . import similarity

    a, b, valid = _finite_pair(ours_t, ref_t)
    out = {"n_voxels": int(valid.sum()), "t_threshold": float(threshold), "top_fraction": float(fraction)}
    out["r_T"] = similarity.pearson(a, b) if valid.sum() >= similarity.MIN_VOXELS else float("nan")
    out["r_AvgR"] = similarity.pearson(ours_r, ref_r) if ours_r is not None and ref_r is not None else None
    out["partial_r_T"] = similarity.partial_spatial_correlation(a, b, covariates) if covariates is not None else None
    diff = np.abs(a[valid] - b[valid])
    out["max_abs_dT"] = float(diff.max()) if diff.size else float("nan")
    out["mean_abs_dT"] = float(diff.mean()) if diff.size else float("nan")
    flips = threshold_flips(a, b, threshold)
    out.update({key: flips[key] for key in ("n_flip", "n_supra_ours", "n_supra_leaddbs")})
    out["flip_fraction"] = flips["n_flip"] / flips["n_voxels"] if flips["n_voxels"] else float("nan")
    out["dice_top"] = top_fraction_dice(a, b, fraction)
    return out


def _stats(values):
    import numpy as np

    values = [float(value) for value in values if value is not None and np.isfinite(value)]
    if not values:
        return None
    return {"median": float(np.median(values)), "min": float(min(values)), "max": float(max(values)), "n": len(values)}


def summarize(lesion_rows):
    """Across-lesion summary: median/min/max of every metric, and the total number of flipping voxels."""
    summary = {}
    for key in ("r_T", "r_AvgR", "partial_r_T", "max_abs_dT", "mean_abs_dT", "n_flip", "flip_fraction", "dice_top"):
        summary[key] = _stats(row["metrics"].get(key) for row in lesion_rows)
    summary["n_flip_total"] = int(sum(int(row["metrics"].get("n_flip") or 0) for row in lesion_rows))
    summary["n_lesions"] = len(lesion_rows)
    return summary


def _fmt(value, digits=3):
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:.{digits}f}"


def _range_text(stats, digits=3):
    if not stats:
        return "not computed"
    return f"median {_fmt(stats['median'], digits)} (range {_fmt(stats['min'], digits)} to {_fmt(stats['max'], digits)})"


def methods_sentence(result):
    """The methods sentence of a parity result, with the measured numbers (no a-priori target)."""
    summary = result["summary"]
    connectome = result["connectome"]
    sets = sorted({row["set"] for row in result["lesions"]})
    thresholds = result["thresholds"]
    partial = summary.get("partial_r_T")
    partial_text = (
        f"the partial correlation of the T maps given the connectome's mean connectivity (muC_r) and first principal component (PC1) had a {_range_text(partial)}"
        if partial else f"the partial correlation given muC_r and PC1 was not computed ({result.get('partial_note') or 'no reference maps'})"
    )
    degree = result.get("degree") or {}
    degree_text = (
        f" The connectome's degree map correlated with van den Heuvel et al.'s GSP1000 degree map at r = {_fmt(degree.get('r'))} (rho = {_fmt(degree.get('rho'))})."
        if degree.get("r") is not None else f" The degree map comparison was not computed ({degree.get('note') or 'no degree map'})."
    )
    caveats = "".join(f" {text}." for text in result.get("caveats") or [])
    return (
        f"Parity with Lead-DBS was measured on {summary['n_lesions']} reference lesions ({', '.join(sets)}) of van den Heuvel et al. (2026), "
        f"whose Lead-DBS {result['reference']['label']} maps are published (repository commit {result['reference']['commit'][:7]}). "
        f"The lesions were mapped with {connectome.get('name')} (N = {_fmt(result['n'])}). Across lesions, the spatial correlation with the Lead-DBS maps "
        f"was a {_range_text(summary.get('r_T'), 4)} for T and a {_range_text(summary.get('r_AvgR'), 4)} for AvgR; {partial_text}; "
        f"the largest absolute T difference was {_fmt((summary.get('max_abs_dT') or {}).get('max'), 2)}; "
        f"{_fmt(summary['n_flip_total'])} voxels in total changed status at |T| >= {thresholds['t_flip']:g}; "
        f"the top-{thresholds['top_fraction'] * 100:g}% voxels had a Dice coefficient with a {_range_text(summary.get('dice_top'))}."
        f"{degree_text}{caveats} These are measured values; no agreement level was set in advance."
    )


#
# Parity: the run
#


def _subject_ids(bundle, subject_list):
    if subject_list:
        resolved = bundle.resolve_subject_list(subject_list)
        return list(resolved.ids), resolved.name or resolved.source
    return list(bundle.subject_ids), None


def run_parity(bundle, cache=None, out=None, lesion_ids=None, subject_list=None, terms_accepted=False, offline=None, save_maps=False,
               chunk=None, run_root=None, cancel=None, emit=None, argv=None, register=True, catalog=None):
    """Map the parity reference lesions with the engine on ``bundle`` and compare them with Lead-DBS's maps.

    User-initiated only: ``terms_accepted`` must be True (the caller showed the GSP data use terms and the user
    accepted them). Writes ``<out>/parity.json`` and ``<out>/methods.md`` (and with ``save_maps`` our maps under
    ``<out>/maps`` in Lead-DBS naming), registers the run (kind "parity") in ``<run_root>`` (default ``out``)
    and returns the result dict.
    """
    import numpy as np

    from . import engine, reference, runs
    from .connectome import ConnectomeBundle, subjects_sha256
    from .export import DISCLAIMER, bundle_info, now_iso, versions

    emit = emit or (lambda *args, **kwargs: None)
    if not terms_accepted:
        raise jobs.JobError(
            f"The Lead-DBS reference maps are derived from GSP1000 ({GSP_TERMS}): the data use terms must be shown and accepted before they are fetched",
            code="terms", exit_code=jobs.EXIT_USAGE)
    if out is None:
        raise jobs.JobError("Give an output folder", code="usage", exit_code=jobs.EXIT_USAGE)
    out = os.path.abspath(os.fspath(out))
    os.makedirs(out, exist_ok=True)
    bundle = bundle if isinstance(bundle, ConnectomeBundle) else ConnectomeBundle.open(bundle)
    if bundle.kind != "timeseries":
        raise jobs.JobError(f"{bundle.name} is a {bundle.kind} connectome: parity needs time series", code="input")
    catalog = catalog or load_parity_catalog()
    lesions = parity_lesions(catalog, lesion_ids)
    thresholds = {"t_flip": float((catalog.data.get("thresholds") or {}).get("t_flip", T_FLIP)), "top_fraction": float((catalog.data.get("thresholds") or {}).get("top_fraction", TOP_FRACTION))}
    ids, subset_name = _subject_ids(bundle, subject_list)
    run_root = os.path.abspath(run_root or out)
    reference_meta = dict(catalog.data.get("reference") or {})
    reference_meta["commit"] = (catalog.data.get("source") or {}).get("commit", "")
    params = {"lesions": [row["id"] for row in lesions], "subjects_sha256": subjects_sha256(ids), "catalog_commit": reference_meta["commit"], **thresholds}
    run_id = runs.register(run_root, "parity", out, params=params) if register else None
    started = time.monotonic()
    try:
        # 1. Reference files
        emit("stage", name="fetch", index=1, total=4)
        paths = fetch_reference(catalog, lesions, cache=cache, offline=offline, cancel=cancel, emit=emit)

        # 2. Our maps
        emit("stage", name="map", index=2, total=4)
        weights, qc = [], []
        for row in lesions:
            w, info = engine.prepare_lesion(paths[row["mask"]], bundle)
            weights.append(w)
            qc.append(info)
        weights = np.vstack(weights)
        progress_start = time.monotonic()

        def progress(done, total, elapsed):
            emit("progress", **_progress_fields(done, total, progress_start), stage="map")

        maps = engine.compute_network_maps(bundle, weights, subjects=ids, chunk=chunk or engine.DEFAULT_CHUNK, progress=progress, cancel=cancel)
        if maps.cancelled:
            raise jobs.Cancelled("stopped while mapping")

        # 3. Comparison
        emit("stage", name="compare", index=3, total=4)
        ref = reference.load_reference(bundle, subjects_sha=subjects_sha256(ids) if subject_list else None)
        covariates = ref.covariates(("mu_r", "pc1")) if ref is not None else None
        partial_note = None if ref is not None else "no reference maps for this connectome and subject list: build them in the connectome manager"
        rows = []
        for index, row in enumerate(lesions):
            ref_t = reference.external_on_grid(paths[row["T"]], bundle)
            ref_r = reference.external_on_grid(paths[row["AvgR"]], bundle)
            metrics = parity_metrics(maps.t[index], ref_t, maps.avg_r[index], ref_r, covariates, thresholds["t_flip"], thresholds["top_fraction"])
            entry = {"id": row["id"], "set": row["set"], "metrics": metrics, "lesion_voxels": int(qc[index].get("voxels_in_mask") or 0), "volume_mm3": qc[index].get("volume_mm3")}
            if row.get("pairing_note"):
                entry["pairing_note"] = row["pairing_note"]
            if not entry["lesion_voxels"]:
                entry["warning"] = "the lesion has no voxel in the connectome mask"
            rows.append(entry)
            emit("result", kind="parity_lesion", id=row["id"], **{key: metrics[key] for key in ("r_T", "r_AvgR", "partial_r_T", "max_abs_dT", "n_flip", "dice_top")})
        degree = {"reference": catalog.get("degree").get("repo_path") if "degree" in catalog else None}
        own_degree = bundle.degree
        if own_degree is None:
            degree["note"] = "the connectome has no degree map: build the hubness map in the connectome manager"
        elif "degree" in paths:
            degree["r"], degree["rho"] = reference.compare_to_external(own_degree, paths["degree"], bundle)
        saved = []
        if save_maps:
            from .nifti import write_nifti

            maps_dir = os.path.join(out, "maps")
            os.makedirs(maps_dir, exist_ok=True)
            label = leaddbs_label(bundle, subset_name)
            for index, row in enumerate(lesions):
                for key, path in map_paths(maps_dir, row["id"].replace("-", ""), bundle, subset_name, style="leaddbs").items():
                    intent = {"intent_code": 3, "intent_params": (max(maps.n - 1, 0), 0.0, 0.0)} if key == "T" else {}
                    write_nifti(path, maps.volume(index, key, bundle), bundle.affine, descrip=f"LNM {key} N={maps.n} parity {label}", **intent)
                    saved.append(os.path.relpath(path, out).replace(os.sep, "/"))

        # 4. Report
        emit("stage", name="write", index=4, total=4)
        caveats = []
        release = not is_leaddbs_release(bundle)
        if release:
            caveats.append(RELEASE_CAVEAT)
        if len(ids) != int(reference_meta.get("n_subjects") or len(ids)):
            caveats.append(SUBSET_CAVEAT.format(n=len(ids)))
        if any(row.get("pairing_note") for row in rows):
            caveats.append("For some lesions the Lead-DBS output keeps an earlier file name; a mask that differs from the one Lead-DBS used lowers their numbers")
        info = bundle_info(bundle)
        info.pop("path", None)  # parity.json is shareable: no local path
        result = {
            "format": PARITY_FORMAT,
            "version": FORMAT_VERSION,
            "run_id": run_id,
            "created": now_iso(),
            "connectome": info,
            "leaddbs_label": leaddbs_label(bundle, subset_name),
            "n": len(ids),
            "subject_list": subset_name,
            "subject_ids_sha256": subjects_sha256(ids),
            "release_differences_included": release,
            "caveats": caveats,
            "reference": reference_meta,
            "thresholds": thresholds,
            "partial_covariates": ["mu_r", "pc1"] if covariates is not None else None,
            "partial_note": partial_note,
            "reference_maps_sha256": ref.sha256 if ref is not None else None,
            "lesions": rows,
            "summary": summarize(rows),
            "degree": degree,
            "maps": saved,
            "terms": GSP_TERMS,
            "acknowledgement": info.get("acknowledgement"),
            "citations": [(catalog.data.get("source") or {}).get("citation"), reference_meta.get("citation")],
            "versions": versions(),
            "elapsed": round(time.monotonic() - started, 3),
            "disclaimer": DISCLAIMER,
        }
        result["methods"] = methods_sentence(result)
        jobs.write_json_atomic(os.path.join(out, "parity.json"), result)
        with open(os.path.join(out, "methods.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write("# Parity with Lead-DBS\n\n" + result["methods"] + "\n\n" + f"Terms: {GSP_TERMS} (GSP-derived results).\n")
        emit("result", kind="parity", path=os.path.join(out, "parity.json"), release_differences_included=release)
        if run_id:
            summary = result["summary"]
            runs.update(run_root, run_id, status="done", ended=now_iso(), outputs=["parity.json", "methods.md", *saved],
                        summary={"lesions": summary["n_lesions"], "r_T_median": (summary.get("r_T") or {}).get("median"), "max_abs_dT": (summary.get("max_abs_dT") or {}).get("max"),
                                 "n_flip_total": summary["n_flip_total"], "release_differences_included": release})
        return result
    except jobs.Cancelled:
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


#
# Command line
#


def _import_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.leaddbs import", description="Import Lead-DBS network maps for cohort statistics and figures.")
    parser.add_argument("--folder", required=True, help="folder of Lead-DBS outputs (*_conn-*_desc-{AvgR,AvgR_Fz,T}_funcmap.nii[.gz])")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--study", default=None, help="study folder: the maps go to derivatives/lnm/conn-<label>/")
    target.add_argument("--out", default=None, help="output folder (without a study)")
    parser.add_argument("--label", default=None, help="connectome label of the maps to import (default: the only one in the folder)")
    parser.add_argument("--n", type=jobs.positive_int, default=None, help="connectome subjects of the maps (default: from the label, e.g. GSP1000v2xFullSet = 1000)")
    parser.add_argument("--terms", default="auto", help="data use terms of the maps: auto (GSP-DUT-2014 for a GSP label), a licence label, or none")
    parser.add_argument("--recursive", action="store_true", help="also look in subfolders")
    parser.add_argument("--overwrite", action="store_true", help="replace a folder that holds maps computed here")
    jobs.add_common_arguments(parser)
    args = parser.parse_args(argv)
    cancel = jobs.CancelToken(args.cancel_file)
    jobs.emit("start", job="leaddbs-import", argv=argv)
    try:
        record = import_leaddbs_maps(args.folder, study=args.study, connectome_label=args.label, out_dir=args.out, n=args.n, terms=args.terms,
                                     recursive=args.recursive, overwrite=args.overwrite, emit=jobs.emit, cancel=cancel)
    except (OSError, ValueError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
    jobs.emit("done", outputs=[record["maps_tsv"], record["path"], record["mask"]], elapsed=round(jobs.elapsed(), 3), run_id=None, n=record["lesions"]["mapped"], label=record["bundle"]["label"])
    return jobs.EXIT_OK


def _parity_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.leaddbs parity", description="Verify the engine against Lead-DBS on the pinned reference lesions (measured, no target).")
    parser.add_argument("--bundle", required=True, help="connectome bundle directory (.lnmc)")
    parser.add_argument("--out", required=True, help="output folder (parity.json, methods.md)")
    parser.add_argument("--accept-terms", default=None, metavar=GSP_TERMS, help=f"confirm that the GSP data use terms were shown and accepted (must be {GSP_TERMS})")
    parser.add_argument("--cache", default=None, help="download cache (default: $SLICER_LNM_CACHE or ~/.cache/SlicerStudio/LNM)")
    parser.add_argument("--lesions", default=None, help="comma-separated lesion ids (default: all 24; see 'list')")
    parser.add_argument("--subject-list", default=None, metavar="SPEC", help="a named subset of the bundle, a file of ids or a comma-separated list")
    parser.add_argument("--offline", action="store_true", help="use cached files only")
    parser.add_argument("--save-maps", action="store_true", help="also write our maps (Lead-DBS names) under <out>/maps")
    parser.add_argument("--chunk", type=jobs.positive_int, default=None, help="voxels per matrix product")
    parser.add_argument("--catalog", default=None, help="parity catalog file (default: the shipped catalog/parity_leaddbs.json)")
    jobs.add_common_arguments(parser)
    args = parser.parse_args(argv)
    if args.accept_terms != GSP_TERMS:
        raise jobs.JobError(f"The reference maps are GSP-derived: show the GSP data use terms to the user and pass --accept-terms {GSP_TERMS} once they are accepted", code="terms", exit_code=jobs.EXIT_USAGE)
    cancel = jobs.CancelToken(args.cancel_file)
    jobs.emit("start", job="leaddbs-parity", argv=argv, threads=args.threads)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    lock = jobs.OutputLock(os.path.join(out, ".parity.lock"))
    lock.acquire()
    try:
        result = run_parity(args.bundle, cache=args.cache, out=out, lesion_ids=args.lesions.split(",") if args.lesions else None, subject_list=args.subject_list,
                            terms_accepted=True, offline=True if args.offline else None, save_maps=args.save_maps, chunk=args.chunk, run_root=args.run_root,
                            cancel=cancel, emit=jobs.emit, argv=argv, catalog=load_parity_catalog(args.catalog) if args.catalog else None)
    except (OSError, ValueError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
    finally:
        lock.release()
    jobs.emit("done", outputs=[os.path.join(out, "parity.json"), os.path.join(out, "methods.md")], elapsed=round(jobs.elapsed(), 3), run_id=result["run_id"])
    return jobs.EXIT_OK


def _list_main(argv):
    from . import fetch

    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.leaddbs list", description="The parity reference lesions and whether their files are cached (no network).")
    parser.add_argument("--cache", default=None, help="download cache")
    jobs.add_common_arguments(parser)  # the job runner adds --cancel-file and --threads
    args = parser.parse_args(argv)
    catalog = load_parity_catalog()
    jobs.emit("start", job="leaddbs-list", argv=argv)
    for row in parity_lesions(catalog):
        cached = all(fetch.is_cached(catalog.get(row[key]), args.cache) for key in ("mask", "AvgR", "T"))
        jobs.emit("result", kind="lesion", id=row["id"], set=row["set"], cached=cached, pairing_note=row.get("pairing_note"))
    jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None, missing=len(missing_files(catalog, cache=args.cache)))
    return jobs.EXIT_OK


def _terms_main(argv):
    from . import catalog as catalogs
    from . import fetch

    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.leaddbs terms", description="Fetch the live GSP data use terms text (Harvard Dataverse) to show it to the user.")
    parser.add_argument("--timeout", type=float, default=15.0, help="read timeout in seconds")
    jobs.add_common_arguments(parser)  # the job runner adds --cancel-file and --threads
    args = parser.parse_args(argv)
    jobs.emit("start", job="leaddbs-terms", argv=argv)
    try:
        text = fetch_live_terms(args.timeout)
    except fetch.FetchError as exc:
        raise jobs.JobError(exc.message, code=f"terms_{exc.code}") from None
    _, _, url = live_terms_source()
    jobs.emit("result", kind="terms", text=text, sha256=catalogs.terms_sha256(text), url=url, licence=GSP_TERMS)
    jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None)
    return jobs.EXIT_OK


_COMMANDS = {"import": _import_main, "parity": _parity_main, "list": _list_main, "terms": _terms_main}


def main(argv=None):
    """Command-line entry point (see the module docstring)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in _COMMANDS:
        parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.leaddbs", description="Lead-DBS interoperability: import maps, verify parity.")
        parser.add_argument("command", choices=list(_COMMANDS))
        parser.parse_args(argv[:1])
        return jobs.EXIT_USAGE
    return _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    jobs.run_job(main, job="leaddbs")

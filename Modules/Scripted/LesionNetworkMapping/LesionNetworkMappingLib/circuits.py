"""Circuits: re-seed a binary result as a circuit, score lesion damage against it and score user-given targets.

The published circuit workflow (Ferguson et al. 2019; Padmanabhan et al. 2019; SPEC_LNM_ADDENDUM §3.8, §9):

- :func:`seed_this` writes a binary result (a conjunction map, a cluster, or a sphere around a peak) as an ROI NIfTI
  and maps it with the engine as a seed. The outputs are the circuit maps ``circuit-<name>_conn-<label>_desc-{T,AvgRFz,
  AvgR}_funcmap.nii.gz`` (exact connectome header) and ``circuit.json``, which links the parent: the map the ROI came
  from (file and sha256), its run (``runs.jsonl`` parents) and the lesions that made it (their sha256 and participant
  ids), so that a later damage score can tell whether it scores the same lesions.
- :func:`damage_scores` scores each lesion of a cohort (the occupancy of the cohortmaps cache, weights.npz) against a
  circuit: the sum of the circuit over the lesion (occupancy x circuit; "sum of T" for a T circuit), the
  occupancy-weighted mean within the lesion, the percentage of the lesion inside the thresholded circuit and, with
  ``residualize_volume``, the same three residualised on lesion volume. They are written to ``damage.tsv`` and
  ``damage.json`` and posted to the study as ``dmg_<circuit>`` columns through the inbox. Every result carries a
  ``provenance``:

  - ``frozen``: the circuit is independent of the scored lesions: it was made from other lesions (circuit.json), or
    it was declared frozen with its sha256 (a circuit fixed by another study);
  - ``same_cohort``: the circuit was made from some of the scored lesions, or its independence is not established.

- :func:`association` tests damage scores against an outcome only for a frozen circuit. A same-cohort circuit is
  refused (:class:`CircularityError`): selecting a circuit on a cohort and testing it on the same lesions is circular
  (double dipping). Use Validate (in-fold circuits, validate.py) or a circuit frozen from an independent cohort.
- :func:`score_targets` scores user-given stimulation targets (label, x, y, z, radius in RAS/MNI mm) against chance:
  the mean and peak circuit value in the sphere, their percentiles among ``k`` random spheres of the same radius
  centred in gray matter (GM prior probability > 0.5, of the circuit's template family), and the sign. Written to
  ``targets.tsv`` (every row labelled "research-only") and ``targets.json``. Nothing is searched: no coordinate is
  ever proposed (addendum §9, "no neuromodulation target finder").

Paths in circuit.json, damage.json and targets.json are relative to their folder (SPEC 9.7 rule 10).

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage, 3 cancelled)::

    python -m LesionNetworkMappingLib.circuits seed --bundle B --out DIR --name NAME
        (--roi MASK [--sign both|pos|neg] | --peak X Y Z --radius MM) [--parent MAP] [--source-maps maps.tsv]
        [--subjects N | --subject-list SPEC] [--threads K] [--cancel-file F] [--run-root DIR] [--parent-run ID]
    python -m LesionNetworkMappingLib.circuits damage --maps maps.tsv --circuit MAP --out DIR [--name NAME]
        [--threshold T] [--sign both|pos|neg] [--mask MASK | --bundle B] [--frozen-sha256 HEX] [--no-volume]
        [--study DIR [--no-post]] [--outcome COLUMN [--participants TSV] [--positive LABEL]] [--run-root DIR]
    python -m LesionNetworkMappingLib.circuits targets --targets CSV --circuit MAP --gm-mask PROBSEG --out DIR
        [--radius MM] [--k 1000] [--seed S] [--circuit-space SPACE] [--gm-space SPACE] [--mask MASK] [--run-root DIR]

Each command registers a run of kind "circuits" in ``<run root>/derivatives/lnm/runs.jsonl`` (run root: --run-root,
else the study, else the output folder).
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import csv
import io
import json
import math
import re
import time

import numpy as np

from . import jobs, runs

FORMAT_CIRCUIT = "slicer-lnm-circuit"
FORMAT_DAMAGE = "slicer-lnm-damage"
FORMAT_TARGETS = "slicer-lnm-targets"
FORMAT_VERSION = 1
CIRCUIT_JSON = "circuit.json"
DAMAGE_TSV = "damage.tsv"
DAMAGE_JSON = "damage.json"
TARGETS_TSV = "targets.tsv"
TARGETS_JSON = "targets.json"
KIND = runs.register_kind("circuits")
INBOX_SOURCE = "circuits"
SIGNS = ("both", "pos", "neg")
PROVENANCES = ("frozen", "same_cohort")
COLUMN_PREFIX = "dmg_"
#: Damage score keys: the sum of the circuit over the lesion, the mean within it, the % of the lesion in the circuit
SCORE_KEYS = ("sum", "mean", "pct")
SCORE_TEXT = {
    "sum": "sum over the lesion of occupancy x circuit (the sum of T over the lesion for a T circuit)",
    "mean": "occupancy-weighted mean of the circuit within the lesion",
    "pct": "percentage of the lesion occupancy inside the thresholded circuit",
}
RESID_SUFFIX = "_resid"
#: Records next to a parent map that tell its run and its lesions (in this order)
PARENT_RECORDS = ("circuit.json", "cohort.json", "glm.json", "slnm.json", "validation.json", "nulls.json", "cohortmaps.json", "run.json")
#: Gray-matter prior probability above which a voxel is gray matter (random target spheres are centred there)
GM_THRESHOLD = 0.5
DEFAULT_K = 1000
DEFAULT_RADIUS_MM = 5.0
MAX_RADIUS_MM = 30.0
SPHERE_CHUNK_VOXELS = 1_000_000  # sphere voxels handled at once by sphere_stats (about 60 MB of work arrays)
RESEARCH_ONLY = "research-only"
TARGETS_CAPTION = (
    "Research use only. Each user-given target is scored against random gray-matter spheres of the same radius "
    "(the percentile of its mean and peak circuit value); no stimulation site is proposed or recommended."
)
CIRCULARITY_MESSAGE = (
    "These damage scores come from a circuit made from the same lesions: testing them against an outcome of these "
    "lesions is circular (double dipping). Use Validate (in-fold circuits), or a circuit frozen from an independent "
    "cohort (declared with its sha256)."
)
NOT_ESTABLISHED = "independence not established (no circuit record with its source lesions, and not declared frozen)"
NA = "n/a"
MAX_STUDY_LEVELS = 8  # folders walked up from a maps.tsv to find its study (study.json)


class CircularityError(ValueError):
    """An association of damage scores with an outcome of the lesions that made the circuit (double dipping)."""


class FamilyError(ValueError):
    """The gray-matter mask and the circuit are not in the same template space family (or one is unknown)."""


#
# Small helpers
#


def circuit_name(text):
    """A circuit name made of letters and digits (it goes into file names "circuit-<name>" and columns "dmg_<name>")."""
    name = re.sub(r"[^A-Za-z0-9]", "", str(text or ""))
    if not name:
        raise ValueError(f"A circuit name needs letters or digits, got {text!r}")
    return name


def default_name(path):
    """A circuit name from a map file name: "circuit-Conj_conn-X_desc-T_funcmap.nii.gz" gives "Conj", "dlpfc_map.nii" "dlpfc"."""
    stem = os.path.basename(os.fspath(path))
    for extension in (".nii.gz", ".nii"):
        if stem.lower().endswith(extension):
            stem = stem[: -len(extension)]
            break
    stem = stem.split("_")[0]
    return stem[len("circuit-"):] if stem.startswith("circuit-") else stem


def damage_column(name, key="sum"):
    """The participants column of a damage score: ``dmg_<name>`` (sum), ``dmg_<name>_mean``, ``dmg_<name>_pct``."""
    base = COLUMN_PREFIX + circuit_name(name)
    return base if key == "sum" else f"{base}_{key}"


def _now():
    from .export import now_iso

    return now_iso()


def _sha256(path):
    from .export import file_sha256

    return file_sha256(path)


def _rel(path, folder):
    """``path`` relative to ``folder`` with "/" separators (the file name alone on another drive), None for None."""
    if not path:
        return None
    try:
        relative = os.path.relpath(os.path.abspath(os.fspath(path)), os.path.abspath(os.fspath(folder)))
    except ValueError:
        return os.path.basename(os.fspath(path))
    return relative.replace(os.sep, "/")


def _inside(path, root):
    try:
        path, root = os.path.realpath(os.fspath(path)), os.path.realpath(os.fspath(root))
        return os.path.commonpath([path, root]) == root
    except ValueError:  # another drive
        return False


def _portable(path, folder, root):
    """``path`` relative to ``folder`` when it lies inside ``root`` (the study, else the run root), else its file name
    alone: a shared record never encodes the user's folders (SPEC 9.7 rule 10; the sha256 next to it identifies the file).
    """
    if not path:
        return None
    if root and not _inside(path, root):
        return os.path.basename(os.fspath(path))
    return _rel(path, folder)


def _share_root(out_dir, run_root=None):
    """The folder that shared records may point into: the study holding ``out_dir``, else the run root, else ``out_dir``."""
    return find_study_root(os.path.join(os.fspath(out_dir), "_")) or run_root or out_dir


def study_key(study):
    """A portable identity of a study folder: {"name", "created"} of its study.json, or None."""
    from .study import STUDY_JSON

    record = _read_json(os.path.join(os.fspath(study), STUDY_JSON)) if study else None
    if not isinstance(record, dict) or not record.get("created"):
        return None
    return {"name": record.get("name"), "created": record.get("created")}


def _resolve(value, folder):
    if not value or not isinstance(value, str):
        return None
    return value if os.path.isabs(value) else os.path.normpath(os.path.join(os.fspath(folder), *value.split("/")))


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _format(value, digits=6):
    if value is None:
        return NA
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.{digits}g}" if math.isfinite(float(value)) else NA
    return str(value).replace("\t", " ").replace("\n", " ")


def _write_tsv(path, columns, rows):
    lines = ["\t".join(columns)] + ["\t".join(_format(row.get(column)) for column in columns) for row in rows]
    from .schema import write_text_atomic

    write_text_atomic(path, "\n".join(lines) + "\n")
    return path


def _volume_of(path):
    """(float64 data [i, j, k], affine) of a 3D NIfTI (a 4D image with one volume is accepted)."""
    from .nifti import read_nifti

    data, affine, _header = read_nifti(path)
    data = np.asarray(data, dtype=np.float64)
    if data.ndim > 3:
        if any(extent != 1 for extent in data.shape[3:]):
            raise ValueError(f"{os.path.basename(os.fspath(path))} is not a 3D image (shape {data.shape})")
        data = data.reshape(data.shape[:3])
    if data.ndim != 3:
        raise ValueError(f"{os.path.basename(os.fspath(path))} is not a 3D image (shape {data.shape})")
    return data, np.asarray(affine, dtype=np.float64)


def _same_grid(shape_a, affine_a, shape_b, affine_b, atol=1e-4):
    return tuple(shape_a)[:3] == tuple(shape_b)[:3] and np.allclose(affine_a, affine_b, atol=atol, rtol=0)


def find_study_root(path):
    """The study folder (holding study.json) that contains ``path``, or None."""
    from .study import STUDY_JSON

    folder = os.path.dirname(os.path.abspath(os.fspath(path)))
    for _level in range(MAX_STUDY_LEVELS):
        if os.path.isfile(os.path.join(folder, STUDY_JSON)):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return None


#
# ROIs
#


def binary_roi(data, sign="both"):
    """A binary (uint8) ROI from a map: its non-zero voxels ("both"), its positive ("pos") or negative ("neg") ones.

    A conjunction or sensitivity map (+1 / -1) and a cluster mask are binary results; NaN voxels are outside.
    """
    if sign not in SIGNS:
        raise ValueError(f"sign must be one of {', '.join(SIGNS)}, got {sign!r}")
    data = np.nan_to_num(np.asarray(data, dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    inside = data > 0 if sign == "pos" else data < 0 if sign == "neg" else data != 0
    return inside.astype(np.uint8)


def sphere_mask(center_xyz, radius_mm, shape, affine):
    """Boolean volume of the voxels whose centres lie within ``radius_mm`` of a world point (RAS mm)."""
    from .geometry import Grid

    radius_mm = float(radius_mm)
    if not 0.0 < radius_mm <= MAX_RADIUS_MM:
        raise ValueError(f"The sphere radius must be in (0, {MAX_RADIUS_MM:g}] mm, got {radius_mm:g}")
    grid = Grid(shape, affine)
    world = grid.voxel_to_world(np.indices(grid.shape).reshape(3, -1).T)
    return (np.linalg.norm(world - np.asarray(center_xyz, dtype=np.float64)[None, :], axis=1) <= radius_mm).reshape(grid.shape)


#
# Parent records (what a map was made from)
#


def _maps_tsv_of(record, folder):
    """The maps.tsv a run record names (top-level "maps_tsv" or "inputs.maps_tsv"), absolute, or None."""
    for value in (record.get("maps_tsv"), (record.get("inputs") or {}).get("maps_tsv") if isinstance(record.get("inputs"), dict) else None):
        path = _resolve(value, folder)
        if path and os.path.isfile(path):
            return path
    return None


def lesions_of_maps(maps_tsv):
    """{"sha256": [...], "participants": [...], "pairs": [(participant_id, lesion_sha256)]} of the lesions of a maps.tsv
    (the lists sorted, n/a left out; the pairs in file order, with None for n/a).
    """
    from .cohortmaps import read_maps_tsv

    def value(row, key):
        text = row.get(key)
        return None if text in (None, "", NA) else text

    rows = read_maps_tsv(maps_tsv, resolve_paths=False)
    pairs = [(value(row, "participant_id"), value(row, "lesion_sha256")) for row in rows]
    sha = sorted({item for _sid, item in pairs if item})
    sids = sorted({sid for sid, _item in pairs if sid})
    return {"sha256": sha, "participants": sids, "pairs": pairs}


def parent_record(map_path):
    """What a parent map was made from: {"record": file name, "run_id", "kind", "maps_tsv", "lesions": {sha256, participants},
    "study"} from the run record next to it (PARENT_RECORDS), or {} when there is none.
    """
    folder = os.path.dirname(os.path.abspath(os.fspath(map_path)))
    for name in PARENT_RECORDS:
        record = _read_json(os.path.join(folder, name))
        if record is None:
            continue
        out = {"record": name, "run_id": record.get("run_id") or record.get("id"), "kind": record.get("kind") or os.path.splitext(name)[0]}
        if name == CIRCUIT_JSON:
            source = record.get("source") or {}
            out["lesions"] = {"sha256": list(source.get("lesion_sha256") or []), "participants": list(source.get("participants") or [])}
            out["study"] = _resolve(source.get("study"), folder)
            out["study_key"] = source.get("study_key")
            return out
        maps_tsv = _maps_tsv_of(record, folder)
        if maps_tsv is None and name == "cohortmaps.json" and os.path.isfile(os.path.join(folder, "maps.tsv")):
            maps_tsv = os.path.join(folder, "maps.tsv")
        if maps_tsv is not None:
            out["maps_tsv"] = maps_tsv
            out["lesions"] = lesions_of_maps(maps_tsv)
            out["study"] = find_study_root(maps_tsv)
            return out
        if name == "run.json" and isinstance(record.get("lesions"), list):  # an engine run of single lesions
            out["kind"] = "map"
            out["lesions"] = {"sha256": sorted({item.get("sha256") for item in record["lesions"] if isinstance(item, dict) and item.get("sha256")}), "participants": []}
            out["study"] = find_study_root(map_path)
            return out
        return out
    return {}


def _lineage_maps(run_root, parent_runs):
    """maps.tsv files of the cohortmaps runs among the parents (and their ancestors) in a run registry."""
    found = []
    if not run_root or not parent_runs:
        return found
    try:
        registry = runs.load(run_root)
    except OSError:
        return found
    for run_id in parent_runs:
        chain = [registry.get(run_id) or {"id": run_id}, *runs.lineage(run_root, run_id, runs=registry)]
        for record in chain:
            if record.get("kind") == "cohortmaps":
                folder = runs.run_dir(run_root, record)
                path = os.path.join(folder, "maps.tsv") if folder else None
                if path and os.path.isfile(path) and path not in found:
                    found.append(path)
    return found


#
# Seed this
#


def seed_this(bundle, out_dir, name, roi=None, peak=None, radius_mm=None, sign="both", subjects=None, subject_list=None, parent=None, parent_runs=(),
              source_maps=None, run_root=None, register=True, cancel=None, emit=None, chunk=None, argv=None):
    """Re-seed a binary result as a circuit: write the ROI NIfTI and map it with the engine as the seed.

    :param bundle: ``ConnectomeBundle`` or its folder (a time series connectome).
    :param name: circuit name (letters and digits are kept): the outputs are ``circuit-<name>_conn-<label>_desc-*``.
    :param roi: a binary result: NIfTI path, or (data [i, j, k], affine); ``sign`` selects its non-zero, positive or
      negative voxels. Or ``peak`` (x, y, z in RAS mm) and ``radius_mm``: a sphere on the connectome grid.
    :param subjects, subject_list: connectome subjects (as the engine's --subjects / --subject-list); default all.
    :param parent: the map the ROI comes from (a conjunction map, the thresholded map of a cluster, a result map): its
      file and sha256 and the run record next to it (its run id and lesions) are recorded.
    :param parent_runs: ids of parent runs (added to the ones found next to ``parent``).
    :param source_maps: maps.tsv of the lesions that made the parent (default: found from ``parent`` or the run registry).
    :param run_root: folder of the run registry (default: the study of the source lesions, else ``out_dir``).
    :return: the circuit.json record with absolute paths (``maps``: {"T", "AvgFz", "AvgR"}, ``roi``, ``record``).
    """
    from . import engine
    from .connectome import ConnectomeBundle
    from .export import bundle_info, subjects_sha256, write_json_atomic
    from .nifti import write_nifti

    emit = emit or jobs.emit
    started = time.monotonic()
    name = circuit_name(name)
    if (roi is None) == (peak is None):
        raise ValueError("Give a binary ROI, or a peak and a radius (not both)")
    bundle = ConnectomeBundle.open(bundle) if isinstance(bundle, (str, os.PathLike)) else bundle
    if bundle.kind != "timeseries":
        raise ValueError(f"The connectome {bundle.name!r} is a {bundle.kind!r} bundle; a circuit needs a time series connectome")
    out_dir = os.path.abspath(os.fspath(out_dir))
    os.makedirs(out_dir, exist_ok=True)
    stem = f"circuit-{name}"
    roi_path = os.path.join(out_dir, f"{stem}_desc-seed_mask.nii.gz")

    # The ROI, written as given (binary, on its own grid) or as a sphere on the connectome grid
    if roi is not None:
        if isinstance(roi, (str, os.PathLike)):
            data, affine = _volume_of(roi)
        else:
            data, affine = np.asarray(roi[0], dtype=np.float64), np.asarray(roi[1], dtype=np.float64)
        mask = binary_roi(data, sign)
        seed_info = {"kind": "binary", "sign": sign, "voxels_source": int(mask.sum())}
    else:
        if radius_mm is None:
            raise ValueError("A peak seed needs a radius (mm)")
        affine = np.asarray(bundle.affine, dtype=np.float64)
        mask = sphere_mask(peak, radius_mm, bundle.shape, affine).astype(np.uint8)
        seed_info = {"kind": "peak", "peak_xyz": [float(v) for v in peak], "radius_mm": float(radius_mm), "voxels_source": int(mask.sum())}
    if not mask.any():
        raise ValueError("The seed is empty: the binary result has no voxel of the chosen sign")
    write_nifti(roi_path, mask, affine, descrip=f"LNM circuit seed {name}")
    weights, qc = engine.prepare_lesion(roi_path, bundle, binarize=True)
    if qc["voxels_in_mask"] == 0:
        raise ValueError("The seed does not overlap the connectome mask")
    seed_info.update({"voxels_in_mask": int(qc["voxels_in_mask"]), "volume_ml": qc["volume_ml"], "weight_outside_fraction": qc["weight_outside_fraction"]})
    seed_info["resampled"] = not _same_grid(mask.shape, affine, bundle.shape, bundle.affine)

    # The parent and the lesions behind it
    parents = [str(run_id) for run_id in (parent_runs or ()) if run_id]
    parent_info = None
    source = {"lesion_sha256": [], "participants": [], "maps_tsv": None, "study": None, "study_key": None, "known": False}
    if parent is not None:
        parent = os.path.abspath(os.fspath(parent))
        found = parent_record(parent)
        parent_info = {"file": parent, "sha256": _sha256(parent), **{key: found.get(key) for key in ("record", "run_id", "kind")}}
        if found.get("run_id") and found["run_id"] not in parents:
            parents.append(found["run_id"])
        if found.get("lesions") is not None:
            source.update({"lesion_sha256": list(found["lesions"]["sha256"]), "participants": list(found["lesions"]["participants"]),
                           "maps_tsv": found.get("maps_tsv"), "study": found.get("study"), "study_key": found.get("study_key"),
                           "known": bool(found["lesions"]["sha256"] or found["lesions"]["participants"])})
    maps_candidates = [source_maps] if source_maps else []
    if not source["known"]:
        maps_candidates += _lineage_maps(run_root, parents)
    for path in maps_candidates:
        if path and os.path.isfile(path):
            lesions = lesions_of_maps(path)
            source.update({"lesion_sha256": sorted(set(source["lesion_sha256"]) | set(lesions["sha256"])),
                           "participants": sorted(set(source["participants"]) | set(lesions["participants"])),
                           "maps_tsv": os.path.abspath(path), "study": find_study_root(path), "known": True})
    run_root = os.path.abspath(os.fspath(run_root or source.get("study") or out_dir))
    if source.get("study") and os.path.isdir(source["study"]):
        source["study_key"] = study_key(source["study"]) or source.get("study_key")

    if subject_list is not None:
        ids = bundle.resolve_subject_list(subject_list).ids
    else:
        ids = engine.resolve_subjects(bundle, subjects)
    run_id = None
    if register:
        run_id = runs.register(run_root, KIND, out_dir, parents=parents, params={"command": "seed", "name": name, "seed": seed_info, "subjects": len(ids), "subject_ids_sha256": subjects_sha256(ids)})
    emit("start", job="circuits", command="seed", argv=list(argv or []), name=name, subjects=len(ids), seed=seed_info, run_id=run_id)
    try:
        def progress(done, total, elapsed):
            eta = elapsed / done * max(total - done, 0) if done > 0 else None
            emit("progress", done=done, total=total, elapsed=round(elapsed, 3), eta=None if eta is None else round(eta, 3))

        maps = engine.compute_network_maps(bundle, weights[None, :], subjects=ids, progress=progress, cancel=cancel, chunk=chunk or engine.DEFAULT_CHUNK)
        if maps.cancelled:
            raise jobs.Cancelled(done=maps.n, reason=getattr(cancel, "reason", None))
        written = engine.write_maps(maps, [stem], bundle, out_dir)
        paths = engine.output_paths(out_dir, stem, bundle.name)
        info = bundle_info(bundle)
        record = {
            "format": FORMAT_CIRCUIT,
            "version": FORMAT_VERSION,
            "name": name,
            "run_id": run_id,
            "parents": parents,
            "created": _now(),
            "parent": parent_info,
            "seed": seed_info,
            "roi": roi_path,
            "roi_sha256": _sha256(roi_path),
            "maps": {key: paths[key] for key in ("T", "AvgFz", "AvgR")},
            "sha256": {key: _sha256(paths[key]) for key in ("T", "AvgFz", "AvgR")},
            "statistic": "T",
            "n": maps.n,
            "subject_ids_sha256": subjects_sha256(maps.subject_ids),
            "bundle": {key: info.get(key) for key in ("name", "label", "manifest_sha256", "space", "n_subjects", "n_voxels", "doi", "gsr")},
            "source": {key: source[key] for key in ("lesion_sha256", "participants", "maps_tsv", "study", "study_key", "known")},
            "note": "A circuit made from a cohort's result is not independent of that cohort: damage scores of the same lesions are descriptive only.",
            "software": runs.software(),
        }
        write_json_atomic(os.path.join(out_dir, CIRCUIT_JSON), _portable_circuit(record, out_dir, _share_root(out_dir, run_root)))
        if run_id:
            runs.update(run_root, run_id, status="done", ended=_now(), outputs=[os.path.basename(path) for path in written] + [os.path.basename(roi_path), CIRCUIT_JSON],
                        summary={"name": name, "n": maps.n, "seed": seed_info["kind"], "voxels": seed_info["voxels_in_mask"]})
        record["record"] = os.path.join(out_dir, CIRCUIT_JSON)
        emit("done", outputs=[*written, roi_path, record["record"]], elapsed=round(time.monotonic() - started, 3), run_id=run_id, name=name, n=maps.n)
        return record
    except jobs.Cancelled:
        if run_id:
            runs.update(run_root, run_id, status="cancelled", ended=_now())
        raise
    except BaseException as exc:
        if run_id:
            try:
                runs.update(run_root, run_id, status="failed", ended=_now(), error=f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
        raise


def _portable_circuit(record, folder, root=None):
    out = json.loads(jobs.to_json(record))
    out["roi"] = _rel(out.get("roi"), folder)
    out["maps"] = {key: _rel(value, folder) for key, value in (out.get("maps") or {}).items()}
    if out.get("parent"):
        out["parent"]["file"] = _portable(out["parent"].get("file"), folder, root)
    source = out.get("source") or {}
    for key in ("maps_tsv", "study"):
        source[key] = _portable(source.get(key), folder, root)
    return out


def read_circuit(path):
    """The circuit.json of a circuit (the file itself, its folder or one of its maps) with absolute paths, or None."""
    path = os.path.abspath(os.fspath(path))
    candidate = path if os.path.basename(path) == CIRCUIT_JSON else os.path.join(path if os.path.isdir(path) else os.path.dirname(path), CIRCUIT_JSON)
    record = _read_json(candidate)
    if record is None or record.get("format") != FORMAT_CIRCUIT:
        return None
    folder = os.path.dirname(candidate)
    record["roi"] = _resolve(record.get("roi"), folder)
    record["maps"] = {key: _resolve(value, folder) for key, value in (record.get("maps") or {}).items()}
    if record.get("parent"):
        record["parent"]["file"] = _resolve(record["parent"].get("file"), folder)
    source = record.get("source") or {}
    for key in ("maps_tsv", "study"):
        source[key] = _resolve(source.get(key), folder)
    record["source"] = source
    record["record"] = candidate
    return record


def find_circuits(folder):
    """circuit.json records (absolute paths) in the sub-folders of ``folder`` (``<run root>/circuits``), newest first."""
    found = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return found
    for entry in names:
        record = read_circuit(os.path.join(folder, entry, CIRCUIT_JSON))
        if record is not None:
            found.append(record)
    found.sort(key=lambda record: str(record.get("created") or ""), reverse=True)
    return found


#
# Damage scores
#


def circuit_on_mask(circuit, mask_shape, mask_affine, indices):
    """(V,) float64 circuit values at the connectome mask voxels. The circuit must be on the mask grid (no silent resampling).

    :param circuit: NIfTI path or (data, affine).
    """
    if isinstance(circuit, (str, os.PathLike)):
        data, affine = _volume_of(circuit)
    else:
        data, affine = np.asarray(circuit[0], dtype=np.float64), np.asarray(circuit[1], dtype=np.float64)
    if not _same_grid(data.shape, affine, mask_shape, mask_affine):
        raise ValueError("The circuit is not on the grid of the connectome mask: make it with the same connectome (it is not resampled)")
    return data.reshape(-1)[indices]


def _mask_of(mask=None, bundle=None, record=None):
    """(shape, affine, indices) of the connectome mask: ``mask`` (NIfTI), else the bundle's mask.nii.gz (``bundle`` or the cohortmaps record).

    The mask must be the one the occupancy was computed on: when the cohortmaps record names its connectome's mask
    (``bundle.mask_sha256``, ``bundle.affine``), another mask (e.g. of another connectome with as many voxels) is refused.
    """
    from .connectome import mask_content_sha256
    from .geometry import mask_indices

    if mask is None:
        folder = bundle if isinstance(bundle, (str, os.PathLike)) else getattr(bundle, "path", None)
        folder = folder or ((record or {}).get("bundle") or {}).get("path")
        candidate = os.path.join(folder, "mask.nii.gz") if folder else None
        if not candidate or not os.path.isfile(candidate):
            raise ValueError("Give the connectome mask or its bundle: the circuit is read at the mask voxels")
        mask = candidate
    data, affine = _volume_of(mask)
    expected = (record or {}).get("bundle") or {}
    if not isinstance(expected, dict):
        expected = {}
    name = expected.get("name") or "the cohort maps' connectome"
    if expected.get("mask_sha256") and mask_content_sha256(data != 0) != expected["mask_sha256"]:
        raise ValueError(f"The connectome mask is not the mask of {name}, which the cohort maps were made with: use that connectome")
    if expected.get("affine") is not None and not _same_grid(data.shape, affine, expected.get("shape") or data.shape, np.asarray(expected["affine"], dtype=np.float64)):
        raise ValueError(f"The connectome mask is not on the grid of {name}, which the cohort maps were made with: use that connectome")
    return data.shape, affine, mask_indices(data != 0)


def _read_occupancy(path, n_voxels):
    """(indices, values) of the lesion occupancy next to a cache map (weights.npz), or raise ValueError."""
    weights = os.path.join(os.path.dirname(os.fspath(path)), "weights.npz")
    try:
        with np.load(weights) as stored:
            indices, values, stored_voxels = stored["indices"], stored["values"], int(stored["n_voxels"])
    except (OSError, KeyError, ValueError) as exc:
        raise ValueError(f"no lesion occupancy (weights.npz) next to {os.path.basename(os.fspath(path))}") from exc
    if stored_voxels != n_voxels:
        raise ValueError(f"the occupancy has {stored_voxels} voxels, the mask {n_voxels}")
    return np.asarray(indices, dtype=np.int64), np.asarray(values, dtype=np.float64)


def is_binary(vector):
    """True when every finite value is 0, 1 or -1 (a binary circuit or ROI)."""
    values = np.asarray(vector, dtype=np.float64)
    values = values[np.isfinite(values)]
    return bool(values.size) and bool(np.all(np.isin(values, (-1.0, 0.0, 1.0))))


def residualize(values, covariate):
    """Residuals of ``values`` on [1, covariate] (ordinary least squares over the finite pairs; NaN elsewhere)."""
    values = np.asarray(values, dtype=np.float64)
    covariate = np.asarray(covariate, dtype=np.float64)
    out = np.full(values.shape, np.nan)
    valid = np.isfinite(values) & np.isfinite(covariate)
    if valid.sum() < 3:
        return out
    design = np.column_stack([np.ones(int(valid.sum())), covariate[valid]])
    beta, *_ = np.linalg.lstsq(design, values[valid], rcond=None)
    out[valid] = values[valid] - design @ beta
    return out


def damage_values(occupancy, circuit, threshold=None, sign="both"):
    """{"sum", "mean", "pct"} of one lesion: ``scores.damage_score``, ``scores.mean_in_lesion`` and
    ``scores.pct_lesion_in_circuit`` (``threshold`` None: |circuit| >= 0.5 for a binary circuit, NaN otherwise).
    """
    from . import scores

    out = {"sum": scores.damage_score(occupancy, circuit), "mean": scores.mean_in_lesion(occupancy, circuit)}
    if threshold is None:
        out["pct"] = scores.pct_lesion_in_circuit(occupancy, circuit, 0.5, sign) if is_binary(circuit) else float("nan")
    else:
        out["pct"] = scores.pct_lesion_in_circuit(occupancy, circuit, threshold, sign)
    return out


def provenance_of(circuit_path, sha256, scored, scored_study=None, frozen_sha256=None):
    """Whether a circuit is independent of the scored lesions.

    :param scored: {"sha256": [...], "participants": [...]} of the scored lesions (:func:`lesions_of_maps`).
    :param scored_study: the study folder of the scored lesions (participant ids count only within one study).
    :param frozen_sha256: the sha256 the circuit was frozen with (declared by the user), or None.
    :return: {"provenance": "frozen" | "same_cohort", "basis", "overlap", "declared_frozen", "warnings"}.
    :raises ValueError: ``frozen_sha256`` is not the circuit's sha256.
    """
    declared = bool(frozen_sha256)
    if declared and str(frozen_sha256).strip().lower() != sha256:
        raise ValueError(f"The circuit has sha256 {sha256}, not the frozen {frozen_sha256}: it is not the frozen circuit")
    record = read_circuit(circuit_path)
    warnings = []
    source = (record or {}).get("source") or {}
    known = bool(record) and bool(source.get("known"))
    source_sha = set(source.get("lesion_sha256") or ())
    same_study = bool(scored_study and source.get("study") and os.path.normcase(os.path.realpath(source["study"])) == os.path.normcase(os.path.realpath(scored_study)))
    if not same_study and scored_study and source.get("study_key"):  # the study moved, or circuit.json names it by its identity only
        same_study = study_key(scored_study) == source["study_key"]
    source_sids = set(source.get("participants") or ()) if same_study else set()
    pairs = scored.get("pairs")
    if pairs is None:
        pairs = [(None, sha) for sha in scored.get("sha256") or ()] + [(sid, None) for sid in scored.get("participants") or ()]
    # A scored lesion overlaps when its file made the circuit, or its participant did in the same study (a re-tracing)
    overlap = sum(1 for sid, sha in pairs if (sha and sha in source_sha) or (sid and sid in source_sids))
    if overlap:
        basis = f"the circuit was made from {overlap} of the scored lesions"
        if declared:
            warnings.append(f"Declared frozen, but {basis}: it is treated as a same-cohort circuit.")
        return {"provenance": "same_cohort", "basis": basis, "overlap": overlap, "declared_frozen": declared, "warnings": warnings}
    if known:
        return {"provenance": "frozen", "basis": "made from other lesions (circuit.json)", "overlap": 0, "declared_frozen": declared, "warnings": warnings}
    if declared:
        return {"provenance": "frozen", "basis": "declared frozen with its sha256", "overlap": 0, "declared_frozen": True, "warnings": warnings}
    return {"provenance": "same_cohort", "basis": NOT_ESTABLISHED, "overlap": None, "declared_frozen": False, "warnings": warnings}


def damage_scores(maps_tsv, circuit, threshold=None, sign="both", out_dir=None, name=None, mask=None, bundle=None, frozen_sha256=None, residualize_volume=True,
                  study=None, post=True, run_root=None, parent_runs=(), register=True, emit=None, argv=None):
    """Damage scores of every lesion of a cohort against a circuit (module docstring), with their provenance.

    :param maps_tsv: the cohortmaps maps.tsv of the lesions (their occupancy is read from the cache's weights.npz).
    :param circuit: circuit NIfTI on the connectome grid (e.g. a :func:`seed_this` T map).
    :param threshold: the % of the lesion in the circuit counts the voxels with |circuit| >= threshold (``sign``
      "both"), circuit >= threshold ("pos") or <= -threshold ("neg"); None: |circuit| >= 0.5 for a binary circuit,
      not computed for other circuits.
    :param out_dir: folder of damage.tsv and damage.json (None: nothing is written).
    :param name: circuit name (default: the circuit.json name, else the file name).
    :param mask, bundle: the connectome mask (default: the bundle of the maps' cohortmaps.json).
    :param frozen_sha256: declare the circuit frozen (fixed by another study) with its sha256.
    :param study: a study (folder or ``Study``): the scores are posted to its inbox as ``dmg_<name>`` columns (``post``).
    :return: {"rows": [...], "provenance": {...}, "columns", "excluded", "record", "tsv", "inbox"}.
    """
    from .cohortmaps import COHORTMAPS_JSON, read_maps_tsv, read_record
    from .export import DISCLAIMER, write_json_atomic

    emit = emit or (lambda *args, **kwargs: None)
    started = time.monotonic()
    maps_tsv = os.path.abspath(os.fspath(maps_tsv))
    circuit = os.path.abspath(os.fspath(circuit))
    if sign not in SIGNS:
        raise ValueError(f"sign must be one of {', '.join(SIGNS)}, got {sign!r}")
    circuit_record = read_circuit(circuit)
    name = circuit_name(name or (circuit_record or {}).get("name") or default_name(circuit))
    cohort_record = read_record(os.path.join(os.path.dirname(maps_tsv), COHORTMAPS_JSON)) or {}
    shape, affine, indices = _mask_of(mask, bundle, cohort_record)
    vector = circuit_on_mask(circuit, shape, affine, indices)
    if not np.any(np.isfinite(vector) & (vector != 0)):
        raise ValueError("The circuit is empty inside the connectome mask")
    sha256 = _sha256(circuit)
    rows_in = read_maps_tsv(maps_tsv)
    scored = lesions_of_maps(maps_tsv)
    scored_study = find_study_root(maps_tsv)
    provenance = provenance_of(circuit, sha256, scored, scored_study, frozen_sha256)
    for text in provenance["warnings"]:
        emit("warning", code="provenance", message=text)

    rows, excluded = [], {}
    for row in rows_in:
        sid = row.get("participant_id")
        source = row.get("T") if row.get("T") not in (None, "", NA) else row.get("AvgFz")
        try:
            occupancy = _read_occupancy(source, indices.size)
        except (ValueError, TypeError) as exc:
            excluded.setdefault(str(exc), []).append(sid)
            continue
        values = damage_values(occupancy, vector, threshold, sign)
        rows.append({"participant_id": sid, "lesion_sha256": row.get("lesion_sha256"), "volume_ml": _finite(row.get("volume_ml")), **values})
    if not rows:
        raise ValueError("No lesion of maps.tsv has its occupancy (weights.npz of the cohortmaps cache)")
    for reason, members in excluded.items():
        emit("warning", code="excluded", message=f"{len(members)} lesion(s) left out: {reason}")
    keys = list(SCORE_KEYS)
    if residualize_volume:
        volume = np.array([np.nan if row["volume_ml"] is None else row["volume_ml"] for row in rows])
        for key in SCORE_KEYS:
            resid = residualize([row[key] for row in rows], volume)
            for row, value in zip(rows, resid, strict=True):
                row[key + RESID_SUFFIX] = float(value)
            keys.append(key + RESID_SUFFIX)
    for row in rows:
        row["provenance"] = provenance["provenance"]
    columns = {key: damage_column(name, key) for key in SCORE_KEYS}
    result = {"name": name, "rows": rows, "provenance": {**provenance, "sha256": sha256}, "columns": columns, "keys": keys, "excluded": excluded,
              "threshold": threshold, "sign": sign, "binary": is_binary(vector), "record": None, "tsv": None, "inbox": None, "run_id": None}
    if out_dir is None:
        return result

    out_dir = os.path.abspath(os.fspath(out_dir))
    os.makedirs(out_dir, exist_ok=True)
    study_path = getattr(study, "path", study)
    run_root = os.path.abspath(os.fspath(run_root or study_path or scored_study or out_dir))
    root = _share_root(out_dir, run_root)
    parents = [str(run_id) for run_id in parent_runs or () if run_id]
    for run_id in ((circuit_record or {}).get("run_id"), cohort_record.get("run_id")):
        if run_id and run_id not in parents:
            parents.append(run_id)
    if register:
        result["run_id"] = runs.register(run_root, KIND, out_dir, parents=parents, params={"command": "damage", "name": name, "circuit_sha256": sha256, "threshold": threshold, "sign": sign,
                                                                                          "provenance": provenance["provenance"], "residualize_volume": bool(residualize_volume)})
    tsv_path = _write_tsv(os.path.join(out_dir, DAMAGE_TSV), ["participant_id", "lesion_sha256", "volume_ml", *keys, "provenance"], rows)
    record = {
        "format": FORMAT_DAMAGE,
        "version": FORMAT_VERSION,
        "created": _now(),
        "run_id": result["run_id"],
        "parents": parents,
        "circuit": {"name": name, "file": _portable(circuit, out_dir, root), "sha256": sha256, "record": _portable((circuit_record or {}).get("record"), out_dir, root)},
        "provenance": provenance["provenance"],
        "provenance_basis": provenance["basis"],
        "overlap": provenance["overlap"],
        "declared_frozen": provenance["declared_frozen"],
        "association": "allowed (frozen circuit)" if provenance["provenance"] == "frozen" else "refused: " + CIRCULARITY_MESSAGE,
        "maps_tsv": _portable(maps_tsv, out_dir, root),
        "threshold": threshold,
        "sign": sign,
        "scores": {key: SCORE_TEXT[key] for key in SCORE_KEYS},
        "residualized_on": "volume_ml" if residualize_volume else None,
        "columns": columns,
        "n": len(rows),
        "excluded": excluded,
        "disclaimer": DISCLAIMER,
        "software": runs.software(),
        "elapsed": round(time.monotonic() - started, 3),
    }
    json_path = os.path.join(out_dir, DAMAGE_JSON)
    write_json_atomic(json_path, record)
    result.update({"record": json_path, "tsv": tsv_path})
    if study_path and post:
        result["inbox"] = post_damage(study_path, result, json_path)
    if result["run_id"]:
        runs.update(run_root, result["run_id"], status="done", ended=_now(), outputs=[DAMAGE_TSV, DAMAGE_JSON],
                    summary={"name": name, "n": len(rows), "provenance": provenance["provenance"]})
    return result


def post_damage(study_path, result, json_path=None):
    """Post the damage scores to a study's inbox as ``dmg_<name>`` columns (sum, mean, pct), with their provenance."""
    from .study import Study

    study_path = os.path.abspath(os.fspath(study_path))
    values = {}
    for row in result["rows"]:
        if not row.get("participant_id"):
            continue
        values[row["participant_id"]] = {column: (row[key] if row[key] is not None and math.isfinite(row[key]) else NA) for key, column in result["columns"].items()}
    if not values:
        return None
    audit = {
        "event": "circuits_damage",
        "circuit": result["name"],
        "circuit_sha256": result["provenance"]["sha256"],
        "provenance": result["provenance"]["provenance"],
        "provenance_basis": result["provenance"]["basis"],
        "columns": sorted(result["columns"].values()),
        "file": _portable(json_path, study_path, study_path) if json_path else None,
        "n": len(values),
        "at": _now(),
    }
    return Study.post_results(study_path, values, INBOX_SOURCE, audit=audit)


def read_damage(path):
    """A damage.json record (``path`` is the file or its folder); "tsv" and "rows" are added. None when unreadable."""
    path = os.path.abspath(os.fspath(path))
    if os.path.isdir(path):
        path = os.path.join(path, DAMAGE_JSON)
    record = _read_json(path)
    if record is None or record.get("format") != FORMAT_DAMAGE:
        return None
    from .study import read_tsv

    tsv = os.path.join(os.path.dirname(path), DAMAGE_TSV)
    record["tsv"] = tsv
    record["rows"] = read_tsv(tsv)[1] if os.path.isfile(tsv) else []
    return record


def check_association(damage):
    """Raise :class:`CircularityError` unless the damage scores come from a frozen circuit (``damage``: a record,
    a :func:`damage_scores` result or a damage.json path).
    """
    if isinstance(damage, (str, os.PathLike)):
        damage = read_damage(damage)
        if damage is None:
            raise ValueError("The damage scores cannot be read")
    provenance = damage.get("provenance")
    provenance = provenance.get("provenance") if isinstance(provenance, dict) else provenance
    if provenance != "frozen":
        raise CircularityError(CIRCULARITY_MESSAGE)
    return True


def association(damage, participants, outcome, outcome_type=None, positive=None, score="sum", n_boot=1000, seed=0, dataset_column=None):
    """Association of damage scores of a frozen circuit with an outcome: AUC and OR per SD (binary) or r (continuous)
    with bootstrap CIs, and the same for lesion volume (``validate.frozen_metrics``).

    :param damage: damage.json path, its record or a :func:`damage_scores` result.
    :param participants: participants.tsv (or a study folder) with the ``outcome`` column.
    :param score: "sum", "mean" or "pct" (or a residualised "<key>_resid").
    :raises CircularityError: the circuit is a same-cohort circuit.
    """
    from . import validate
    from .study import NA as STUDY_NA
    from .study import is_missing, read_tsv

    record = read_damage(damage) if isinstance(damage, (str, os.PathLike)) else damage
    if record is None:
        raise ValueError("The damage scores cannot be read")
    check_association(record)
    provenance = record.get("provenance")
    participants = os.fspath(participants)
    if os.path.isdir(participants):
        participants = os.path.join(participants, "participants.tsv")
    columns, table = read_tsv(participants)
    if outcome not in columns:
        raise ValueError(f"{os.path.basename(participants)} has no column {outcome}")
    by_id = {row["participant_id"]: row for row in table}
    rows = [row for row in record["rows"] if row.get("participant_id") in by_id]
    raw = [by_id[row["participant_id"]].get(outcome) for row in rows]
    present = {str(value) for value in raw if not is_missing(value)}
    if outcome_type is None:
        outcome_type = "binary" if len(present) == 2 else "continuous"
    if outcome_type == "binary":
        codes, positive = validate.binary_codes(raw, positive)
    else:
        codes = [_finite(value) if not is_missing(value) else None for value in raw]
    if present and all(code is None for code in codes):
        raise ValueError(f"The outcome {outcome} must be binary (two values) or numeric; it has {len(present)} non-numeric values")
    keep = [i for i, row in enumerate(rows) if codes[i] is not None and _finite(row.get(score)) is not None]
    if len(keep) < 4:
        raise ValueError(f"Only {len(keep)} lesion(s) have a damage score and an outcome")
    values = np.array([float(rows[i][score]) for i in keep])
    y = np.array([float(codes[i]) for i in keep])
    datasets = None
    if dataset_column and dataset_column in columns:
        datasets = [str(by_id[rows[i]["participant_id"]].get(dataset_column) or STUDY_NA) for i in keep]
    volume = np.array([_finite(rows[i].get("volume_ml")) if _finite(rows[i].get("volume_ml")) is not None else np.nan for i in keep])
    out = {
        "outcome": outcome,
        "outcome_type": outcome_type,
        "positive": positive,
        "score": score,
        "n": len(keep),
        "metrics": validate.frozen_metrics(values, y, outcome_type, datasets, n_boot, seed),
        "volume_metrics": validate.frozen_metrics(volume, y, outcome_type, datasets, n_boot, seed) if np.all(np.isfinite(volume)) else None,
        "circuit_sha256": (record.get("circuit") or {}).get("sha256") or (provenance.get("sha256") if isinstance(provenance, dict) else None),
    }
    return out


#
# Targets
#


def _target(label, x, y, z, radius, default_radius):
    radius = _finite(radius)
    radius = float(default_radius) if radius is None or radius <= 0 else radius
    coords = [_finite(value) for value in (x, y, z)]
    if any(value is None for value in coords):
        raise ValueError(f"The target {label!r} has no valid x, y, z")
    if not 0.0 < radius <= MAX_RADIUS_MM:
        raise ValueError(f"The radius of the target {label!r} must be in (0, {MAX_RADIUS_MM:g}] mm, got {radius:g}")
    return {"label": str(label), "x": coords[0], "y": coords[1], "z": coords[2], "radius_mm": radius}


def _cell(record, index, key):
    """The stripped text of column ``key`` of a CSV record, or None when the column or the cell is missing."""
    return record[index[key]].strip() if key in index and index[key] < len(record) else None


def _read_markups_json(path, default_radius):
    """Targets of a Slicer markups .mrk.json file: the control points of its point lists (LPS converted to RAS)."""
    with open(path, encoding="utf-8-sig") as f:
        document = json.load(f)
    targets = []
    for markup in (document.get("markups") or []) if isinstance(document, dict) else []:
        lps = str(markup.get("coordinateSystem") or "LPS").upper() == "LPS"  # the format's default is LPS
        for point in markup.get("controlPoints") or []:
            position = point.get("position") or [None, None, None]
            target = _target(point.get("label") or f"target{len(targets) + 1}", *list(position)[:3], None, default_radius)
            if lps:
                target["x"], target["y"] = -target["x"], -target["y"]
            targets.append(target)
    return targets


def read_targets(path, default_radius=DEFAULT_RADIUS_MM):
    """Targets of a CSV or TSV file with a header (label, x, y, z[, radius]; RAS/MNI mm), of a Slicer markups
    .fcsv file (its "# columns =" line names the columns; LPS files, "CoordinateSystem = LPS" or "= 1", are converted
    to RAS) or of a markups .mrk.json file.
    """
    if os.fspath(path).lower().endswith(".json"):
        return _read_markups_json(path, default_radius)
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    lines = text.splitlines()
    header, lps, body = None, False, []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            lower = stripped.lower()
            if lower.startswith("# columns ="):
                header = [part.strip() for part in stripped.split("=", 1)[1].split(",")]
            elif "coordinatesystem" in lower.replace(" ", ""):
                value = lower.split("=", 1)[1].strip() if "=" in lower else ""
                lps = value in ("lps", "1")  # vtkMRMLStorageNode::CoordinateSystemLPS = 1 (Slicer 4.x files)
            continue
        body.append(line)
    if not body:
        return []
    delimiter = "\t" if "\t" in body[0] or os.fspath(path).lower().endswith(".tsv") else ","
    reader = csv.reader(io.StringIO("\n".join(body)), delimiter=delimiter)
    records = list(reader)
    if header is None:
        header, records = [part.strip() for part in records[0]], records[1:]
    lower = [name.lower() for name in header]
    index = {name: lower.index(name) for name in ("label", "x", "y", "z", "radius", "id", "name") if name in lower}
    if not all(key in index for key in ("x", "y", "z")):
        raise ValueError(f"{os.path.basename(os.fspath(path))} needs the columns x, y and z (and label, radius)")
    label_column = index.get("label", index.get("name", index.get("id")))
    targets = []
    for number, record in enumerate(records, start=1):
        if not any(cell.strip() for cell in record):
            continue
        label = record[label_column].strip() if label_column is not None and label_column < len(record) and record[label_column].strip() else f"target{number}"
        target = _target(label, *(_cell(record, index, key) for key in ("x", "y", "z", "radius")), default_radius)
        if lps:
            target["x"], target["y"] = -target["x"], -target["y"]
        targets.append(target)
    return targets


def normalize_targets(targets, default_radius=DEFAULT_RADIUS_MM):
    """Targets as dicts {label, x, y, z, radius_mm} from dicts, (label, x, y, z[, radius]) tuples or a file path."""
    if isinstance(targets, (str, os.PathLike)):
        return read_targets(targets, default_radius)
    out = []
    for number, item in enumerate(targets, start=1):
        if isinstance(item, dict):
            out.append(_target(item.get("label") or f"target{number}", item.get("x"), item.get("y"), item.get("z"), item.get("radius_mm", item.get("radius")), default_radius))
        else:
            values = list(item) + [None] * 5
            out.append(_target(values[0] or f"target{number}", values[1], values[2], values[3], values[4], default_radius))
    return out


def space_of_file(path):
    """The TemplateFlow space of a file name ("tpl-MNI152NLin2009cAsym_..." or "..._space-X_..."), or None."""
    name = os.path.basename(os.fspath(path))
    match = re.match(r"^tpl-([A-Za-z0-9]+)", name) or re.search(r"(?:^|_)space-([A-Za-z0-9]+)", name)
    return match.group(1) if match else None


def check_families(circuit_space, gm_space):
    """Raise :class:`FamilyError` unless the circuit and the gray-matter mask are in the same known template family."""
    from .geometry import space_family

    circuit_family, gm_family = space_family(circuit_space), space_family(gm_space)
    if circuit_family in ("unknown", "native"):
        raise FamilyError(f"The template space of the circuit is not known ({circuit_space!r}): say which space it is in")
    if gm_family in ("unknown", "native"):
        raise FamilyError(f"The template space of the gray-matter mask is not known ({gm_space!r}): use a TemplateFlow GM prior of the circuit's family")
    if circuit_family != gm_family:
        raise FamilyError(f"The gray-matter mask is in the {gm_family} family ({gm_space}) and the circuit in the {circuit_family} family ({circuit_space}): "
                          f"use a GM prior of the {circuit_family} family (templates of different families differ by a few millimetres)")
    return circuit_family


def gm_mask_on_grid(gm, shape, affine, threshold=GM_THRESHOLD):
    """Boolean gray-matter mask on a grid: the GM prior probability > ``threshold`` (a file on another grid of the same
    family is resampled by the mean probability over each voxel). Returns (mask, resampled).
    """
    from .geometry import Grid, resample_to_grid

    if isinstance(gm, (str, os.PathLike)):
        data, gm_affine = _volume_of(gm)
    elif isinstance(gm, tuple):
        data, gm_affine = np.asarray(gm[0], dtype=np.float64), np.asarray(gm[1], dtype=np.float64)
    else:
        data, gm_affine = np.asarray(gm, dtype=np.float64), np.asarray(affine, dtype=np.float64)
    resampled = not _same_grid(data.shape, gm_affine, shape, affine)
    if resampled:
        data = resample_to_grid(np.nan_to_num(data), gm_affine, Grid(shape, affine), mode="fraction")
    return np.nan_to_num(np.asarray(data, dtype=np.float64).reshape(tuple(shape)[:3])) > float(threshold), resampled


def sphere_offsets(affine, radius_mm):
    """(m, 3) integer voxel offsets whose centres lie within ``radius_mm`` of a voxel centre (through the affine)."""
    matrix = np.asarray(affine, dtype=np.float64)[:3, :3]
    spacing = np.sqrt((matrix**2).sum(axis=0))
    reach = np.ceil(float(radius_mm) / np.maximum(spacing, 1e-9)).astype(int)
    grid = np.stack(np.meshgrid(*[np.arange(-r, r + 1) for r in reach], indexing="ij"), axis=-1).reshape(-1, 3)
    return grid[np.linalg.norm(grid @ matrix.T, axis=1) <= float(radius_mm) + 1e-9]


def sphere_stats(volume, support, centers, offsets):
    """(mean, peak, voxels) of the volume in spheres around ``centers`` ((k, 3) voxel indices), over the ``support``
    voxels; peak is the value of largest magnitude (with its sign). NaN where a sphere holds no support voxel.

    The centres are taken ``SPHERE_CHUNK_VOXELS // len(offsets)`` at a time, so memory stays bounded for large radii.
    """
    centers = np.asarray(centers, dtype=np.int64).reshape(-1, 3)
    step = max(1, SPHERE_CHUNK_VOXELS // max(len(offsets), 1))
    if centers.shape[0] > step:
        parts = [_sphere_stats(volume, support, centers[start:start + step], offsets) for start in range(0, centers.shape[0], step)]
        return tuple(np.concatenate(values) for values in zip(*parts, strict=True))
    return _sphere_stats(volume, support, centers, offsets)


def _sphere_stats(volume, support, centers, offsets):
    shape = np.array(volume.shape)
    coords = np.asarray(centers, dtype=np.int64)[:, None, :] + offsets[None, :, :]
    inside = np.all((coords >= 0) & (coords < shape), axis=2)
    flat = np.ravel_multi_index(tuple(np.clip(coords, 0, shape - 1).transpose(2, 0, 1)), volume.shape)
    valid = inside & support.reshape(-1)[flat]
    values = np.where(valid, volume.reshape(-1)[flat], np.nan)
    count = valid.sum(axis=1)
    with np.errstate(invalid="ignore"):
        mean = np.where(count > 0, np.nansum(values, axis=1) / np.maximum(count, 1), np.nan)
    magnitude = np.where(valid, np.abs(values), -1.0)
    best = np.argmax(magnitude, axis=1)
    peak = np.where(count > 0, values[np.arange(len(best)), best], np.nan)
    return mean, peak, count


def percentile(observed, null):
    """Percentile (0-100) of a value among null values: 100 x (#{null < x} + 0.5 #{null == x}) / K (NaN without nulls)."""
    null = np.asarray(null, dtype=np.float64)
    null = null[np.isfinite(null)]
    if not null.size or not math.isfinite(observed):
        return float("nan")
    return 100.0 * (float((null < observed).sum()) + 0.5 * float((null == observed).sum())) / null.size


def score_targets(targets, circuit, gm_mask, k=DEFAULT_K, seed=0, circuit_space=None, gm_space=None, mask=None, default_radius=DEFAULT_RADIUS_MM, gm_threshold=GM_THRESHOLD,
                  out_dir=None, run_root=None, parent_runs=(), register=True, argv=None):
    """Score user-given targets against random gray-matter spheres of the same radius (research-only; nothing is proposed).

    :param targets: a CSV/TSV/.fcsv path, or dicts / (label, x, y, z[, radius]) tuples in RAS (MNI) mm.
    :param circuit: circuit NIfTI (or (data, affine)).
    :param gm_mask: GM prior (probability) NIfTI or (data, affine) of the circuit's template family.
    :param k: random spheres per radius; ``seed`` their random seed (the same centres for every target of a radius).
    :param circuit_space, gm_space: space labels (default: circuit.json's connectome space or the grid; the GM file name).
    :param mask: optional support mask of the circuit (default: its finite non-zero voxels).
    :return: {"rows": [...], "k", "seed", "family", "caption", "record", "tsv"}.
    :raises FamilyError: the GM mask and the circuit are not of the same known family.
    """
    from .atlases import mask_family
    from .export import DISCLAIMER, write_json_atomic

    targets = normalize_targets(targets, default_radius)
    if not targets:
        raise ValueError("No target to score")
    k = int(k)
    if k < 10:
        raise ValueError("Use at least 10 random spheres")
    circuit_path = os.path.abspath(os.fspath(circuit)) if isinstance(circuit, (str, os.PathLike)) else None
    if circuit_path:
        data, affine = _volume_of(circuit_path)
    else:
        data, affine = np.asarray(circuit[0], dtype=np.float64), np.asarray(circuit[1], dtype=np.float64)
    if circuit_space is None and circuit_path:
        circuit_space = ((read_circuit(circuit_path) or {}).get("bundle") or {}).get("space")
    if circuit_space is None:
        circuit_space = mask_family(data.shape, affine)[1]
    if gm_space is None and isinstance(gm_mask, (str, os.PathLike)):
        gm_space = space_of_file(gm_mask)
    if gm_space is None and isinstance(gm_mask, (str, os.PathLike)):
        gm_data, gm_affine = _volume_of(gm_mask)
        gm_space = mask_family(gm_data.shape, gm_affine)[1]
    family = check_families(circuit_space, gm_space)
    gm, resampled = gm_mask_on_grid(gm_mask, data.shape, affine, gm_threshold)
    if mask is not None:
        support_data, support_affine = _volume_of(mask) if isinstance(mask, (str, os.PathLike)) else (np.asarray(mask[0]), np.asarray(mask[1]))
        if not _same_grid(support_data.shape, support_affine, data.shape, affine):
            raise ValueError("The support mask is not on the grid of the circuit")
        support = np.asarray(support_data) != 0
    else:
        support = np.isfinite(data) & (data != 0)
    support &= np.isfinite(data)
    gm_centres = np.argwhere(gm & support)
    if gm_centres.shape[0] < 10:
        raise ValueError("The gray-matter mask overlaps fewer than 10 voxels of the circuit")
    rng = np.random.default_rng(int(seed))
    centres = gm_centres[rng.choice(gm_centres.shape[0], size=k, replace=k > gm_centres.shape[0])]
    from .geometry import world_to_voxel

    nulls = {}
    rows = []
    for target in targets:
        radius = target["radius_mm"]
        if radius not in nulls:
            offsets = sphere_offsets(affine, radius)
            null_mean, null_peak, _null_count = sphere_stats(data, support, centres, offsets)
            nulls[radius] = (offsets, null_mean, null_peak, int(np.isfinite(null_mean).sum()))
        offsets, null_mean, null_peak, k_used = nulls[radius]
        ijk = np.rint(world_to_voxel(np.array([[target["x"], target["y"], target["z"]]]), affine)[0]).astype(np.int64)
        inside_grid = bool(np.all((ijk >= 0) & (ijk < np.array(data.shape))))
        mean, peak, count = sphere_stats(data, support, ijk[None, :], offsets)
        mean, peak, count = float(mean[0]), float(peak[0]), int(count[0])
        note = ""
        if not inside_grid:
            note = "outside the circuit's grid"
        elif count == 0:
            note = "no circuit voxel in the sphere"
        rows.append({
            **target,
            "voxels": count,
            "mean": mean,
            "peak": peak,
            "sign": "+" if mean > 0 else "-" if mean < 0 else ("0" if math.isfinite(mean) else NA),
            "percentile_mean": percentile(mean, null_mean),
            "percentile_peak": percentile(peak, null_peak),
            "k": k_used,
            "in_gm": bool(inside_grid and gm[tuple(ijk)]),
            "use": RESEARCH_ONLY,
            "note": note,
        })
    result = {"rows": rows, "k": k, "seed": int(seed), "family": family, "circuit_space": circuit_space, "gm_space": gm_space, "gm_resampled": resampled,
              "caption": TARGETS_CAPTION, "record": None, "tsv": None, "run_id": None}
    if out_dir is None:
        return result
    out_dir = os.path.abspath(os.fspath(out_dir))
    os.makedirs(out_dir, exist_ok=True)
    run_root = os.path.abspath(os.fspath(run_root or out_dir))
    root = _share_root(out_dir, run_root)
    parents = [str(run_id) for run_id in parent_runs or () if run_id]
    circuit_record = read_circuit(circuit_path) if circuit_path else None
    if circuit_record and circuit_record.get("run_id") and circuit_record["run_id"] not in parents:
        parents.append(circuit_record["run_id"])
    if register:
        result["run_id"] = runs.register(run_root, KIND, out_dir, parents=parents, params={"command": "targets", "k": k, "seed": int(seed), "targets": len(rows), "gm_threshold": gm_threshold}, seeds={"spheres": int(seed)})
    columns = ["label", "x", "y", "z", "radius_mm", "voxels", "mean", "peak", "sign", "percentile_mean", "percentile_peak", "k", "in_gm", "use", "note"]
    gm_file = isinstance(gm_mask, (str, os.PathLike))
    tsv_path = _write_tsv(os.path.join(out_dir, TARGETS_TSV), columns, rows)
    record = {
        "format": FORMAT_TARGETS,
        "version": FORMAT_VERSION,
        "created": _now(),
        "run_id": result["run_id"],
        "parents": parents,
        "use": RESEARCH_ONLY,
        "caption": TARGETS_CAPTION,
        "disclaimer": DISCLAIMER,
        "circuit": {"file": _portable(circuit_path, out_dir, root) if circuit_path else None, "sha256": _sha256(circuit_path) if circuit_path else None, "space": circuit_space},
        "gm_mask": {"file": _portable(gm_mask, out_dir, root) if gm_file else None, "sha256": _sha256(gm_mask) if gm_file else None, "space": gm_space, "family": family,
                    "threshold": gm_threshold, "resampled": resampled},
        "null": {"kind": "random spheres of the same radius centred on gray-matter voxels of the circuit", "k": k, "seed": int(seed),
                 "percentile": "100 x (#{null < x} + 0.5 #{null == x}) / K", "peak": "the value of largest magnitude in the sphere"},
        "coordinates": "RAS mm (MNI); the sphere is centred on the voxel nearest the target",
        "columns": columns,
        "n": len(rows),
        "software": runs.software(),
    }
    json_path = os.path.join(out_dir, TARGETS_JSON)
    write_json_atomic(json_path, record)
    result.update({"record": json_path, "tsv": tsv_path})
    if result["run_id"]:
        runs.update(run_root, result["run_id"], status="done", ended=_now(), outputs=[TARGETS_TSV, TARGETS_JSON], summary={"targets": len(rows), "k": k})
    return result


#
# Command line
#


def build_parser():
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.circuits", description="Circuits: re-seed a binary result, damage scores and target scores (research use only).")
    commands = parser.add_subparsers(dest="command", required=True)

    seed = commands.add_parser("seed", help="map a binary result (ROI) or a peak sphere as a seed: circuit-<name> maps")
    jobs.add_common_arguments(seed)
    seed.add_argument("--bundle", required=True, help="connectome bundle (.lnmc)")
    seed.add_argument("--out", required=True, help="output folder")
    seed.add_argument("--name", required=True, help="circuit name (letters and digits)")
    source = seed.add_mutually_exclusive_group(required=True)
    source.add_argument("--roi", default=None, help="binary result NIfTI (a conjunction map, a cluster mask)")
    source.add_argument("--peak", type=float, nargs=3, default=None, metavar=("X", "Y", "Z"), help="peak in RAS mm (with --radius)")
    seed.add_argument("--radius", type=float, default=None, help="sphere radius in mm (with --peak)")
    seed.add_argument("--sign", choices=SIGNS, default="both", help="ROI voxels: non-zero (both), positive or negative")
    seed.add_argument("--parent", default=None, help="the map the ROI comes from (recorded with its run and lesions)")
    seed.add_argument("--source-maps", default=None, help="maps.tsv of the lesions that made the parent")
    subjects = seed.add_mutually_exclusive_group()
    subjects.add_argument("--subjects", type=jobs.positive_int, default=None, help="use the first N connectome subjects")
    subjects.add_argument("--subject-list", default=None, help="a named subset, a file of subject ids or comma-separated ids")

    damage = commands.add_parser("damage", help="damage scores of a cohort's lesions against a circuit")
    jobs.add_common_arguments(damage)
    damage.add_argument("--maps", required=True, help="maps.tsv of a cohortmaps run")
    damage.add_argument("--circuit", required=True, help="circuit NIfTI on the connectome grid")
    damage.add_argument("--out", required=True, help="output folder (damage.tsv, damage.json)")
    damage.add_argument("--name", default=None, help="circuit name (default: from circuit.json or the file name)")
    damage.add_argument("--threshold", type=float, default=None, help="threshold of the % of the lesion in the circuit (default: 0.5 for a binary circuit)")
    damage.add_argument("--sign", choices=SIGNS, default="both", help="both (|circuit| >= t), pos or neg")
    masks = damage.add_mutually_exclusive_group()
    masks.add_argument("--mask", default=None, help="connectome mask NIfTI (default: the bundle of the maps)")
    masks.add_argument("--bundle", default=None, help="connectome bundle of the maps")
    damage.add_argument("--frozen-sha256", default=None, help="declare the circuit frozen (fixed by an independent study) with its sha256")
    damage.add_argument("--no-volume", action="store_true", help="do not add the volume-residualised scores")
    damage.add_argument("--study", default=None, help="study folder: post the scores as dmg_<name> columns through its inbox")
    damage.add_argument("--no-post", action="store_true", help="with --study: do not post the scores")
    damage.add_argument("--outcome", default=None, help="test the association with this participants column (frozen circuits only)")
    damage.add_argument("--participants", default=None, help="participants.tsv with --outcome (default: the study's)")
    damage.add_argument("--positive", default=None, help="positive class of a binary --outcome")

    targets = commands.add_parser("targets", help="score user-given targets against random gray-matter spheres (research only)")
    jobs.add_common_arguments(targets)
    targets.add_argument("--targets", required=True, help="CSV/TSV (label, x, y, z, radius; RAS mm) or a markups .fcsv")
    targets.add_argument("--circuit", required=True, help="circuit NIfTI")
    targets.add_argument("--gm-mask", required=True, help="GM probability (TemplateFlow probseg) of the circuit's template family")
    targets.add_argument("--out", required=True, help="output folder (targets.tsv, targets.json)")
    targets.add_argument("--radius", type=float, default=DEFAULT_RADIUS_MM, help=f"radius of targets without one (mm; default {DEFAULT_RADIUS_MM:g})")
    targets.add_argument("--k", type=jobs.positive_int, default=DEFAULT_K, help=f"random spheres (default {DEFAULT_K})")
    targets.add_argument("--seed", type=int, default=0, help="random seed of the sphere centres")
    targets.add_argument("--circuit-space", default=None, help="template space of the circuit (default: circuit.json or the grid)")
    targets.add_argument("--gm-space", default=None, help="template space of the GM mask (default: its TemplateFlow file name)")
    targets.add_argument("--mask", default=None, help="support mask of the circuit (default: its finite non-zero voxels)")
    return parser


def main(argv=None):
    """Command-line entry point (see the module docstring); returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    cancel = jobs.CancelToken(args.cancel_file)
    started = time.monotonic()
    try:
        if args.command == "seed":
            if args.peak is not None and args.radius is None:
                raise jobs.JobError("--peak needs --radius", code="usage", exit_code=jobs.EXIT_USAGE)
            seed_this(args.bundle, args.out, args.name, roi=args.roi, peak=args.peak, radius_mm=args.radius, sign=args.sign, subjects=args.subjects, subject_list=args.subject_list,
                      parent=args.parent, parent_runs=args.parent_run, source_maps=args.source_maps, run_root=args.run_root, cancel=cancel, argv=argv)
            return jobs.EXIT_OK
        if args.command == "damage":
            jobs.emit("start", job="circuits", command="damage", argv=argv, threads=args.threads)
            result = damage_scores(args.maps, args.circuit, threshold=args.threshold, sign=args.sign, out_dir=args.out, name=args.name, mask=args.mask, bundle=args.bundle,
                                   frozen_sha256=args.frozen_sha256, residualize_volume=not args.no_volume, study=args.study, post=not args.no_post,
                                   run_root=args.run_root, parent_runs=args.parent_run, emit=jobs.emit, argv=argv)
            jobs.emit("result", kind="damage", path=result["tsv"], provenance=result["provenance"]["provenance"], basis=result["provenance"]["basis"], n=len(result["rows"]))
            if args.outcome:
                participants = args.participants or (os.path.join(args.study, "participants.tsv") if args.study else None)
                if not participants:
                    raise jobs.JobError("--outcome needs --participants or --study", code="usage", exit_code=jobs.EXIT_USAGE)
                try:
                    found = association(result["record"], participants, args.outcome, positive=args.positive)
                except CircularityError as exc:
                    raise jobs.JobError(str(exc), code="circular") from None
                jobs.emit("result", kind="association", **found)
            jobs.emit("done", outputs=[result["tsv"], result["record"]], elapsed=round(time.monotonic() - started, 3), run_id=result["run_id"])
            return jobs.EXIT_OK
        jobs.emit("start", job="circuits", command="targets", argv=argv, threads=args.threads)
        result = score_targets(args.targets, args.circuit, args.gm_mask, k=args.k, seed=args.seed, circuit_space=args.circuit_space, gm_space=args.gm_space, mask=args.mask,
                               default_radius=args.radius, out_dir=args.out, run_root=args.run_root, parent_runs=args.parent_run, argv=argv)
        jobs.emit("result", kind="targets", path=result["tsv"], use=RESEARCH_ONLY, n=len(result["rows"]))
        jobs.emit("done", outputs=[result["tsv"], result["record"]], elapsed=round(time.monotonic() - started, 3), run_id=result["run_id"])
        return jobs.EXIT_OK
    except (jobs.Cancelled, jobs.JobError):
        raise
    except FamilyError as exc:
        raise jobs.JobError(str(exc), code="family") from None
    except (OSError, ValueError, KeyError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None


if __name__ == "__main__":
    jobs.run_job(main, job="circuits")

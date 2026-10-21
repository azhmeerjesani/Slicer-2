"""Group maps of a lesion cohort: overlap, sensitivity, the legacy two-group maps and conjunction (SPEC 7.8, 7.11).

Pure numpy and scipy (no Qt, no Slicer), so that it runs in a subprocess and in-process (A-04 calls
:func:`group_maps` and :func:`conjunction` directly). The per-lesion network maps come from a cohortmaps
run (``maps.tsv``, L-08): they are never recomputed here and never loaded as scene nodes.

- **Binarization.** Each lesion's map is thresholded with the threshold *rule* (thresholds.py), resolved
  for that map's N and the connectome mask size V (a rule is never a bare number reused across N). The
  rule's statistic chooses the map: T for the T rules, mean Fz (AvgFz) for ``fz_abs`` and for ``top_pct``
  on Fz. ``sign`` is "pos" (map >= t), "neg" (map <= -t) or "both" (|map| >= t).
- **Overlap** (``stats.overlap``): the number and the percent of lesions whose map passes, per voxel.
  With groups, the overlap and the sensitivity are over the cases (the control lesions are the
  comparison, never part of the "shared network").
- **Sensitivity** (``stats.sensitivity_map``): the voxels where at least k % of the maps pass (default
  90). With sign "both" it is sign-specific: +1 where at least k % of the maps are positive, -1 where at
  least k % are negative (a voxel half positive and half negative is not "shared").
  This is the classic, hub-inflated overlap map: it is always labelled with ``CLASSIC_NOTE`` and is
  shown next to a null-referenced version (the Nulls card), never alone (addendum §3.3).
- **Legacy two-group maps** ("legacy two-group, no covariates"), only with at least 2 cases and 2
  controls: the Welch t of the mean Fz maps (``stats.welch_t``) and the one-sided Liebermeister p of the
  binarized maps (``stats.liebermeister``, minimum fraction 0.1). They are descriptive: the primary
  specificity inference is the GLM with covariates and permutations (glm.py, the Contrast tab).
- **Conjunction** (:func:`conjunction`): the voxels that pass in every given map, sign-specific (a binary
  map, such as the Liebermeister specificity p <= 0.05, is a mask that passes whatever the sign).

Outputs (export naming, exact mask header; ``group_conn-<label>_desc-<desc>_map.nii.gz``)::

    overlapCount, overlapPct, sens<k>, legacyWelchT, legacyLiebermeisterP, conj
    peaks.tsv    peaks of overlapPct (x, y, z MNI mm, value, label), at least 10 mm apart
    cohort.json  inputs, the rule resolved per N, groups, exclusions, notes, outputs, disclaimer

Command line (JSON events with ``--json`` or when standard output is not a terminal, else readable
progress lines; exit codes 0 done, 1 error, 2 usage, 3 cancelled, 75 locked)::

    python -m LesionNetworkMappingLib.cohort (--maps maps.tsv | --maps-dir DIR | --study DIR | --lesions-from FILE|DIR)
        --out DIR [--bundle B | --mask MASK] [--participants participants.tsv] [--group-column group]
        [--rule JSON] [--sign both|pos|neg] [--sensitivity 90] [--legacy welch,liebermeister|none]
        [--conj-with MAP [--conj-alpha A] | --no-conjunction] [--n N] [--json] [--threads K] [--cancel-file F]
        [--run-root DIR] [--parent-run ID] [cohortmaps options: --subjects N --random --seed S
        --subject-list SPEC --cache DIR --budget-gb G --include-unreviewed]

``--study`` and ``--lesions-from`` (a TSV of participant_id and lesion, or a folder of lesion NIfTI files)
first map the lesions with cohortmaps (batched, cached: one engine pass per batch, never one process
per lesion) into ``--out``, then compute the group maps. ``--maps-dir`` is a folder that holds a maps.tsv,
or per-lesion engine outputs (``*_desc-T_funcmap.nii.gz`` and ``*_desc-AvgRFz_funcmap.nii.gz``).
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import json
import math
import re
import time

import numpy as np

from . import jobs, runs, stats, thresholds

FORMAT_NAME = "slicer-lnm-cohort"
FORMAT_VERSION = 1
COHORT_JSON = "cohort.json"
PEAKS_TSV = "peaks.tsv"
MAPS_TSV = "maps.tsv"
COHORTMAPS_JSON = "cohortmaps.json"
LOCK_NAME = ".cohort.lock"
DEFAULT_SENSITIVITY = 90
SENSITIVITY_RANGE = (50, 100)
LIEBERMEISTER_MIN_FRACTION = 0.1
LEGACY_METHODS = ("welch", "liebermeister")
LEGACY_LABEL = "legacy two-group, no covariates"
CLASSIC_NOTE = "Classic overlap map (hub-inflated); see Nulls for a null-referenced version."
LEGACY_NOTE = "Legacy two-group maps without covariates or permutation inference (descriptive); the specificity analysis is the GLM (Contrast tab)."
PEAKS_TOP = 10
PEAKS_MIN_DISTANCE_MM = 10.0
#: The map column of maps.tsv that each rule statistic reads
MAP_COLUMNS = {"T": "T", "Fz": "AvgFz"}
#: Engine output "desc" labels of the per-lesion maps (engine.OUTPUT_KEYS)
_ENGINE_DESCS = {"T": "T", "AvgFz": "AvgRFz", "AvgR": "AvgR"}
_SUBJECT_PATTERN = re.compile(r"(sub-[A-Za-z0-9]+)")
_NIFTI_EXTENSIONS = (".nii.gz", ".nii")

runs.register_kind("cohort")


class GroupMaps:
    """Result of :func:`group_maps`.

    ``maps``: {desc: (V,) array} in output order (overlapCount int32, overlapPct float32, sens<k> int8 or
    uint8, legacyWelchT float32, legacyLiebermeisterP float64, conj int8 or uint8); ``resolved``: the
    :class:`thresholds.Resolved` of each lesion map; ``ids``, ``groups``, ``cases`` (indices of the maps
    the overlap is over), ``controls``; ``sign``, ``sensitivity``, ``statistic``, ``rule``; ``notes``
    (classic and legacy labels, warnings); ``welch_df`` ((V,) Welch degrees of freedom or None).
    """

    def __init__(self):
        self.maps = {}
        self.resolved = []
        self.ids = []
        self.groups = []
        self.cases = []
        self.controls = []
        self.sign = "both"
        self.sensitivity = DEFAULT_SENSITIVITY
        self.statistic = "T"
        self.rule = thresholds.DEFAULT_RULE
        self.notes = []
        self.welch_df = None

    @property
    def n(self):
        return len(self.resolved)

    def summary(self):
        """JSON-compatible description (no arrays)."""
        values = sorted({round(float(item.value), 6) for item in self.resolved if math.isfinite(item.value)})
        return {
            "n_maps": self.n,
            "n_cases": len(self.cases),
            "n_controls": len(self.controls),
            "overlap_over": "cases" if self.controls or len(self.cases) < self.n else "all",
            "sign": self.sign,
            "sensitivity_pct": self.sensitivity,
            "statistic": self.statistic,
            "rule": self.rule,
            "thresholds": values,
            "resolved": [item.to_dict() for item in self.resolved[:1]],
            "outputs": list(self.maps),
            "legacy": LEGACY_LABEL if any(desc.startswith("legacy") for desc in self.maps) else None,
            "notes": list(self.notes),
        }


def sensitivity_desc(k):
    """``desc`` label of a sensitivity map: "sens90" (k is rounded to an integer percent)."""
    return f"sens{round(float(k))}"


def rule_statistic(rule):
    """The statistic a threshold rule applies to: "T", or "Fz" for ``fz_abs`` and ``top_pct`` on Fz."""
    rule = thresholds.normalize_rule(rule)
    if rule["kind"] == "fz_abs":
        return "Fz"
    if rule["kind"] == "top_pct":
        return rule.get("statistic", "Fz")
    return "T"


def _as_array(maps, mask=None):
    """(n, V) float32 array of in-mask values from an array or from map files (``mask``: indices, boolean volume or path)."""
    if isinstance(maps, np.ndarray) and maps.ndim == 2:
        return np.asarray(maps, dtype=np.float32)
    paths = list(maps)
    if paths and not isinstance(paths[0], (str, os.PathLike)):
        return np.asarray(paths, dtype=np.float32)
    indices, shape, _affine = mask_geometry(mask)
    return load_masked(paths, indices, shape)


def mask_geometry(mask):
    """(indices, shape, affine) of a mask given as a NIfTI path, a boolean volume or (indices, shape[, affine])."""
    from .nifti import read_nifti

    if mask is None:
        raise ValueError("Map files need the connectome mask (its path or volume)")
    if isinstance(mask, (str, os.PathLike)):
        data, affine, _header = read_nifti(os.fspath(mask))
        volume = np.asarray(data).reshape(np.asarray(data).shape[:3]) != 0
        return np.flatnonzero(volume.ravel()), volume.shape, affine
    if isinstance(mask, np.ndarray) and mask.ndim == 3:
        return np.flatnonzero(mask.ravel() != 0), mask.shape, None
    indices, shape = mask[0], mask[1]
    return np.asarray(indices), tuple(shape), mask[2] if len(mask) > 2 else None


def load_masked(paths, indices, shape, affine=None, cancel=None, on_map=None):
    """(n, V) float32 in-mask values of NIfTI maps on one grid (``shape``; ``affine`` checked when given)."""
    from .nifti import read_nifti

    out = np.empty((len(paths), len(indices)), dtype=np.float32)
    for row, path in enumerate(paths):
        if cancel is not None and cancel.cancelled():
            cancel.raise_if_cancelled(done=row)
        data, map_affine, _header = read_nifti(os.fspath(path))
        data = np.asarray(data)
        if data.ndim > 3:
            data = data.reshape(data.shape[:3])
        if tuple(data.shape) != tuple(shape) or (affine is not None and not np.allclose(map_affine, affine, atol=1e-4, rtol=0)):
            raise ValueError(f"The map {path} is not on the connectome grid (shape {tuple(data.shape)}, expected {tuple(shape)})")
        out[row] = np.asarray(data, dtype=np.float32).reshape(-1)[indices]
        if on_map is not None:
            on_map(row + 1, len(paths))
    return out


def resolve_rules(Y, rule, n, v, gsr=None):
    """The rule resolved for each map (row of ``Y``): ``n`` is one N for all maps or one per map, ``v`` the mask size."""
    ns = list(n) if isinstance(n, (list, tuple, np.ndarray)) else [n] * Y.shape[0]
    if len(ns) != Y.shape[0]:
        raise ValueError(f"{len(ns)} N values for {Y.shape[0]} maps")
    rule = thresholds.normalize_rule(rule)
    needs_values = rule["kind"] == "top_pct"
    resolved, cache = [], {}
    for row, map_n in enumerate(ns):
        if needs_values:
            resolved.append(thresholds.resolve(rule, int(map_n), v, gsr=gsr, values=Y[row]))
        else:
            key = int(map_n)
            if key not in cache:
                cache[key] = thresholds.resolve(rule, key, v, gsr=gsr)
            resolved.append(cache[key])
    return resolved


def binarize_maps(Y, values, sign="both"):
    """(n, V) boolean maps: row i passes ``values[i]`` with ``sign`` (``stats.binarize``)."""
    out = np.zeros(Y.shape, dtype=bool)
    for row, value in enumerate(values):
        if math.isfinite(value):
            out[row] = stats.binarize(Y[row], value, sign)
    return out


def sensitivity(binary_pos, binary_neg=None, k=DEFAULT_SENSITIVITY):
    """Sensitivity map: at least ``k`` % of the maps pass. With ``binary_neg`` it is sign-specific (int8 +1 / -1)."""
    pos = stats.sensitivity_map(stats.overlap(binary_pos)[1], k)
    if binary_neg is None:
        return pos.astype(np.uint8)
    neg = stats.sensitivity_map(stats.overlap(binary_neg)[1], k)
    return (pos & ~neg).astype(np.int8) - (neg & ~pos).astype(np.int8)


def _is_mask(array):
    """True for a sign-free 0/1 map (bool or unsigned integer): it passes wherever it is non-zero."""
    return array.dtype == np.bool_ or np.issubdtype(array.dtype, np.unsignedinteger)


def as_mask_if_binary(values):
    """``values`` as a uint8 mask when every value is 0 or 1 (a binary map read from a file), else unchanged."""
    array = np.asarray(values)
    if _is_mask(array):
        return array
    finite = array[np.isfinite(array)] if np.issubdtype(array.dtype, np.floating) else array
    if finite.size == array.size and np.all((finite == 0) | (finite == 1)):
        return (array != 0).astype(np.uint8)
    return array


def conjunction(*maps, sign="both"):
    """Voxels that pass in every map, sign-specific.

    A bool or unsigned-integer map is a sign-free mask (non-zero passes, whatever the sign); any other map
    is signed (the sign of each voxel is kept, for example a sign-specific sensitivity map or a thresholded
    T map). A voxel is positive where every signed map is > 0 and every mask is non-zero, negative where
    every signed map is < 0 and every mask is non-zero. ``sign`` "pos": positive voxels (uint8 1); "neg":
    negative voxels (uint8 1; with masks only, the masks' intersection: they already hold the negative
    network); "both": +1 positive, -1 negative (int8; with masks only, +1 on their intersection). For 0/1
    maps "pos" and "both" equal ``stats.conjunction``.
    """
    if not maps:
        raise ValueError("conjunction needs at least one map")
    if sign not in ("both", "pos", "neg"):
        raise ValueError("sign must be 'both', 'pos' or 'neg'")
    arrays = [np.asarray(item) for item in maps]
    masks = [array != 0 for array in arrays if _is_mask(array)]
    signed = [array for array in arrays if not _is_mask(array)]
    inside = np.logical_and.reduce(masks) if masks else True
    if not signed:
        return inside.astype(np.int8 if sign == "both" else np.uint8)
    positive = inside & np.logical_and.reduce([array > 0 for array in signed])
    negative = inside & np.logical_and.reduce([array < 0 for array in signed])
    if sign == "pos":
        return positive.astype(np.uint8)
    if sign == "neg":
        return negative.astype(np.uint8)
    return positive.astype(np.int8) - negative.astype(np.int8)


def _group_indices(groups, n):
    if groups is None:
        return list(range(n)), []
    labels = [str(label).strip().lower() if label is not None else "" for label in groups]
    if len(labels) != n:
        raise ValueError(f"{len(labels)} group labels for {n} maps")
    cases = [index for index, label in enumerate(labels) if label == "case"]
    controls = [index for index, label in enumerate(labels) if label == "control"]
    return cases, controls


def group_maps(maps, rule=None, groups=None, sensitivity_pct=DEFAULT_SENSITIVITY, sign="both", n=None, mask=None, fz_maps=None,
               legacy=LEGACY_METHODS, min_fraction=LIEBERMEISTER_MIN_FRACTION, conj_with=None, ids=None, gsr=None, conjunction_map=True):
    """Overlap, sensitivity, the legacy two-group maps and conjunction of a cohort's lesion maps (module docstring).

    :param maps: (n, V) in-mask values of the rule's statistic (T, or mean Fz for Fz rules), or map files
      (then ``mask`` gives the connectome mask: a NIfTI path, a boolean volume or (indices, shape)).
    :param rule: threshold rule (thresholds.py; default FWE), resolved per map for ``n`` and V.
    :param groups: one label per map ("case", "control", anything else) or None (one cohort).
    :param sensitivity_pct: k of the sensitivity map (50-100).
    :param sign: "both", "pos" or "neg".
    :param n: connectome subjects of the maps (one value, or one per map); required for T rules.
    :param fz_maps: mean Fz maps (same layout as ``maps``) for the legacy Welch t; default ``maps``.
    :param legacy: legacy maps to compute with >= 2 cases and >= 2 controls (subset of ``LEGACY_METHODS``).
    :param conj_with: optional map (V,) intersected with the sensitivity map: signed (the sign must match), or
      binary (bool, unsigned, or only 0/1 values: a sign-free mask that passes wherever it is non-zero); without it
      the conjunction uses the legacy Liebermeister specificity (p <= 0.05, uncorrected) when computed.
    :param conjunction_map: False leaves the legacy conjunction out (a ``conj_with`` map is always intersected).
    :return: :class:`GroupMaps`.
    """
    if sign not in thresholds.SIGNS:
        raise ValueError(f"sign must be one of {', '.join(thresholds.SIGNS)}")
    low, high = SENSITIVITY_RANGE
    if not low <= float(sensitivity_pct) <= high:
        raise ValueError(f"The sensitivity must be between {low} and {high} %, got {sensitivity_pct}")
    rule = thresholds.normalize_rule(rule)
    Y = _as_array(maps, mask)
    if Y.ndim != 2 or Y.shape[0] < 1:
        raise ValueError("group_maps needs at least one map")
    count, v = Y.shape
    statistic = rule_statistic(rule)
    if n is None:
        if statistic == "T" and rule["kind"] != "top_pct":
            raise ValueError("The number of connectome subjects N is needed to resolve a T rule")
        n = 1
    result = GroupMaps()
    result.ids = list(ids) if ids is not None else [str(index) for index in range(count)]
    result.groups = list(groups) if groups is not None else [None] * count
    result.sign, result.sensitivity, result.statistic, result.rule = sign, float(sensitivity_pct), statistic, rule
    result.resolved = resolve_rules(Y, rule, n, v, gsr=gsr)
    values = [item.value if (item.allowed or rule.get("override")) else math.nan for item in result.resolved]
    refused = sum(1 for item in result.resolved if not (item.allowed or rule.get("override")))
    if refused:
        banner = next((item.banner for item in result.resolved if item.banner), None)
        result.notes.append(f"The rule is not applied to {refused} map(s): {banner or 'not allowed for their N'}")
    cases, controls = _group_indices(groups, count)
    if groups is not None and not cases:
        result.notes.append("No case lesions: the overlap is over all lesions.")
        cases = list(range(count))
    result.cases, result.controls = cases, controls
    Yc = Y[cases]
    case_values = [values[index] for index in cases]
    binary = binarize_maps(Yc, case_values, sign)
    overlap_count, overlap_pct = stats.overlap(binary)
    result.maps["overlapCount"] = overlap_count
    result.maps["overlapPct"] = overlap_pct.astype(np.float32)
    if sign == "both":
        sens = sensitivity(binarize_maps(Yc, case_values, "pos"), binarize_maps(Yc, case_values, "neg"), sensitivity_pct)
    else:
        sens = sensitivity(binary, None, sensitivity_pct)
    result.maps[sensitivity_desc(sensitivity_pct)] = sens
    result.notes.append(CLASSIC_NOTE)

    legacy = tuple(method for method in (legacy or ()) if method in LEGACY_METHODS)
    specificity = None
    if legacy and len(cases) >= 2 and len(controls) >= 2:
        if "welch" in legacy:
            F = _as_array(fz_maps, mask) if fz_maps is not None else Y
            t, df = stats.welch_t(F[cases], F[controls])
            result.maps["legacyWelchT"] = t.astype(np.float32)
            result.welch_df = df
        if "liebermeister" in legacy:
            control_binary = binarize_maps(Y[controls], [values[index] for index in controls], sign)
            p = stats.liebermeister(binary, control_binary, min_fraction=min_fraction)
            result.maps["legacyLiebermeisterP"] = p
            specificity = (p <= 0.05).astype(np.uint8)
        result.notes.append(LEGACY_NOTE)
    elif legacy and controls:
        result.notes.append("The legacy two-group maps need at least 2 cases and 2 controls.")
    if conj_with is not None:
        other = as_mask_if_binary(np.asarray(conj_with).ravel())
        if other.size != v:
            raise ValueError(f"The conjunction map has {other.size} voxels, the maps {v}")
        result.maps["conj"] = conjunction(sens, other, sign=sign)
    elif specificity is not None and conjunction_map:
        result.maps["conj"] = conjunction(sens, specificity, sign=sign)
    return result


#
# Files
#


def group_map_paths(out_dir, label, descs):
    """{desc: group_conn-<label>_desc-<desc>_map.nii.gz} (export.group_map_path naming)."""
    from .export import group_map_path

    return {desc: group_map_path(out_dir, label, desc) for desc in descs}


_DESCRIPTIONS = {
    "overlapCount": "number of lesion maps that pass the rule",
    "overlapPct": "percent of lesion maps that pass the rule",
    "legacyWelchT": "legacy Welch t (cases vs controls, mean Fz, no covariates)",
    "legacyLiebermeisterP": "legacy one-sided Liebermeister p (uncorrected, no covariates)",
    "conj": "conjunction",
}


def write_group_maps(result, out_dir, mask_path, label):
    """Write the maps of a :class:`GroupMaps` with the mask's exact header; return {desc: path}."""
    from . import nifti
    from .glm import _write_on_mask

    record = nifti.read_raw_header(mask_path)
    indices, shape, _affine = mask_geometry(mask_path)
    paths = group_map_paths(out_dir, label, result.maps)
    for desc, vector in result.maps.items():
        fill = 1.0 if desc.endswith("P") else 0.0
        intent = {}
        if desc == "legacyWelchT":
            df = result.welch_df
            intent = {"intent_code": 3, "intent_params": (float(np.median(df)) if df is not None and np.size(df) else 0.0, 0.0, 0.0)}
        description = _DESCRIPTIONS.get(desc) or f"sensitivity >= {result.sensitivity:g}% ({result.sign})"
        _write_on_mask(paths[desc], vector, record, indices, shape, fill=fill, descrip=f"LNM cohort {description}", **intent)
    return paths


def write_peaks(result, out_dir, mask_path, top_k=PEAKS_TOP):
    """peaks.tsv of the overlapPct map (``stats.peak_table``, positive values, at least 10 mm apart); return its path."""
    from .export import write_peaks_tsv
    from .geometry import Grid

    indices, shape, affine = mask_geometry(mask_path)
    peaks = stats.peak_table(result.maps["overlapPct"], Grid(shape, affine), top_k=top_k, min_distance_mm=PEAKS_MIN_DISTANCE_MM, mask_indices=indices, sign="pos")
    return write_peaks_tsv(os.path.join(out_dir, PEAKS_TSV), peaks)


#
# Inputs of the command line
#


def lesions_from(path):
    """[(participant_id, lesion path)] of a lesions TSV (participant_id, lesion) or of a folder of lesion NIfTI files."""
    from .cohortmaps import read_lesions_tsv

    path = os.fspath(path)
    if os.path.isfile(path):
        return [(sid, lesion) for sid, lesion, *_rest in read_lesions_tsv(path)]
    if not os.path.isdir(path):
        raise FileNotFoundError(f"No lesions TSV or folder at {path}")
    out, seen = [], set()
    for name in sorted(os.listdir(path)):
        if not name.lower().endswith(_NIFTI_EXTENSIONS) or name.startswith("."):
            continue
        match = _SUBJECT_PATTERN.search(name)
        stem = re.sub(r"(?i)\.nii(\.gz)?$", "", name)
        sid = match.group(1) if match else re.sub(r"[^A-Za-z0-9]", "", stem) or stem
        if sid in seen:
            raise ValueError(f"Two lesion files of {sid} in {path}: name them sub-<id>_... or use a lesions TSV")
        seen.add(sid)
        out.append((sid, os.path.join(path, name)))
    if not out:
        raise FileNotFoundError(f"No lesion NIfTI files (.nii, .nii.gz) in {path}")
    return out


def maps_from_dir(path):
    """Rows {participant_id, T, AvgFz} of a folder of per-lesion engine outputs (``<name>_conn-<label>_desc-T_funcmap.nii.gz``).

    Preview outputs (``desc-TPreviewN100``) are read too; a lesion with full and preview maps uses the full
    ones, and one with only previews uses the largest preview (the maps of one lesion never mix runs).
    """
    pattern = re.compile(r"^(?P<name>.+?)_conn-(?P<label>[A-Za-z0-9]+)_desc-(?P<desc>T|AvgRFz|AvgR)(?P<suffix>[A-Za-z0-9]*)_funcmap\.nii(\.gz)?$")
    keys = {value: key for key, value in _ENGINE_DESCS.items()}
    found = {}
    for name in sorted(os.listdir(path)):
        match = pattern.match(name)
        if match is None:
            continue
        runs_of = found.setdefault(match.group("name"), {})
        row = runs_of.setdefault(match.group("suffix"), {"participant_id": match.group("name"), "label": match.group("label")})
        row[keys[match.group("desc")]] = os.path.join(path, name)

    def preference(suffix):
        digits = re.search(r"(\d+)$", suffix)
        return (suffix == "", int(digits.group(1)) if digits else 0, suffix)

    rows = []
    for runs_of in found.values():
        usable = [suffix for suffix, row in runs_of.items() if "T" in row or "AvgFz" in row]
        if usable:
            rows.append(runs_of[max(usable, key=preference)])
    return rows


def read_groups(participants_path, column="group"):
    """{participant_id: value of ``column``} of a participants.tsv ({} when the column is missing)."""
    from .study import read_tsv

    columns, rows = read_tsv(participants_path)
    if column not in columns:
        return {}
    return {row["participant_id"]: row.get(column) for row in rows}


def _n_from_header(path):
    """N of a T map from its NIfTI intent (code 3, df = N - 1), else None."""
    from .nifti import read_nifti_header

    header = read_nifti_header(path)
    if header.get("intent_code") == 3 and header["intent_params"][0] > 0:
        return round(header["intent_params"][0]) + 1
    return None


#
# Command line
#


def _human_emitter(stream=None):
    """An ``emit(event, **fields)`` that prints readable lines (a terminal) instead of JSON events."""
    stream = stream or sys.stdout
    state = {"stage": None, "last": 0.0}

    def emit(event, **fields):
        fields.pop("t", None)
        record = {"event": event, **fields}
        line = None
        if event == "start":
            line = f"{fields.get('job', 'job')}: started (pid {fields.get('pid', os.getpid())})"
        elif event == "stage":
            state["stage"] = fields.get("name")
            line = f"{fields.get('name')} ({fields.get('index')}/{fields.get('total')})" if fields.get("total") else f"{fields.get('name')}"
        elif event == "progress":
            now = time.monotonic()
            done, total = fields.get("done"), fields.get("total")
            if now - state["last"] < 0.5 and done != total:
                return record
            state["last"] = now
            eta = fields.get("eta")
            left = "" if eta is None else (f" · {max(1, round(eta / 60))} min left" if eta >= 60 else " · < 1 min left")
            unit = "maps read" if fields.get("stage") == "lesions" else "units"
            if fields.get("subjects") and fields.get("subject") is not None:
                line = f"  batch {fields.get('batch')}/{fields.get('batches')}: {fields.get('subject')}/{fields.get('subjects')} subjects{left}"
            else:
                line = f"  {done}/{total} {unit}{left}"
        elif event == "warning":
            line = f"warning: {fields.get('message')}"
        elif event == "result" and fields.get("kind") == "mapped":
            line = f"  {fields.get('n')} lesions mapped ({fields.get('cache_hits') or 0} from the cache)"
        elif event == "result" and fields.get("path"):
            line = f"wrote {fields.get('path')}"
        elif event == "done":
            line = f"done in {fields.get('elapsed', 0):.1f} s: {len(fields.get('outputs') or [])} files"
        elif event == "error":
            line = f"error: {fields.get('message')}"
        elif event == "cancelled":
            line = "stopped"
        if line is not None:
            try:
                stream.write(line + "\n")
                stream.flush()
            except (OSError, ValueError):
                pass
        return record

    return emit


def _bundle_mask(bundle_path):
    path = os.path.join(os.fspath(bundle_path), "mask.nii.gz")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"The connectome bundle has no mask.nii.gz: {bundle_path}")
    return path


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _parse_rule(text):
    if not text:
        return thresholds.normalize_rule(None)
    try:
        return thresholds.rule_from_json(text)
    except ValueError as exc:
        raise jobs.JobError(f"Invalid --rule: {exc}", code="usage", exit_code=jobs.EXIT_USAGE) from None


def _parse_legacy(text):
    if text is None:
        return LEGACY_METHODS
    if text.strip().lower() in ("", "none", "off"):
        return ()
    methods = tuple(item.strip().lower() for item in text.split(",") if item.strip())
    unknown = [method for method in methods if method not in LEGACY_METHODS]
    if unknown:
        raise jobs.JobError(f"Unknown --legacy method(s): {', '.join(unknown)} (use {', '.join(LEGACY_METHODS)} or none)", code="usage", exit_code=jobs.EXIT_USAGE)
    return methods


def build_parser():
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.cohort", description="Cohort group maps: overlap, sensitivity, legacy two-group maps and conjunction.")
    jobs.add_common_arguments(parser)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--maps", default=None, help="maps.tsv of a cohortmaps run (participant_id, AvgFz, T)")
    source.add_argument("--maps-dir", default=None, help="folder with a maps.tsv, or with per-lesion engine outputs (*_desc-T_funcmap.nii.gz)")
    source.add_argument("--study", default=None, help="study folder: map its analysable lesions with cohortmaps first (needs --bundle)")
    source.add_argument("--lesions-from", default=None, help="lesions TSV (participant_id, lesion) or a folder of lesion NIfTI files: mapped with cohortmaps first (needs --bundle)")
    parser.add_argument("--out", required=True, help="output folder (group maps, peaks.tsv, cohort.json; maps.tsv when the lesions are mapped here)")
    parser.add_argument("--bundle", default=None, help="connectome bundle (.lnmc): its mask, and the mapping of --study/--lesions-from")
    parser.add_argument("--mask", default=None, help="connectome mask NIfTI (default: the bundle's mask.nii.gz)")
    parser.add_argument("--participants", default=None, help="participants.tsv with the group column (default: the study's)")
    parser.add_argument("--group-column", default="group", help="column with case / control (default: group)")
    parser.add_argument("--rule", default=None, help="threshold rule as JSON (default FWE: {\"kind\": \"fwe_bonferroni\", \"alpha\": 0.05})")
    parser.add_argument("--sign", choices=thresholds.SIGNS, default="both", help="both (|map| >= t, sign-specific sensitivity), pos or neg (default: both)")
    parser.add_argument("--sensitivity", type=float, default=DEFAULT_SENSITIVITY, help=f"percent of the maps that must pass (50-100; default {DEFAULT_SENSITIVITY})")
    parser.add_argument("--legacy", default=None, help="legacy two-group maps: welch,liebermeister (default) or none")
    parser.add_argument("--conj-with", default=None, help="map intersected with the sensitivity map (binary or signed; a p map with --conj-alpha)")
    parser.add_argument("--conj-alpha", type=float, default=None, help="--conj-with is a p map: voxels with p <= this value pass")
    parser.add_argument("--no-conjunction", action="store_true", help="leave out the conjunction with the legacy specificity map")
    parser.add_argument("--n", type=jobs.positive_int, default=None, help="connectome subjects of the maps (default: from cohortmaps.json or the T maps' headers)")
    parser.add_argument("--json", action="store_true", help="JSON events on standard output even on a terminal")
    group = parser.add_argument_group("cohort maps (with --study or --lesions-from)")
    group.add_argument("--subjects", type=jobs.positive_int, default=None, help="use only N connectome subjects (default: all)")
    group.add_argument("--random", action="store_true", help="choose the N subjects at random")
    group.add_argument("--seed", type=int, default=0, help="random seed for --random")
    group.add_argument("--subject-list", default=None, metavar="SPEC", help="a named subset of the bundle, a file of subject ids or a list of ids")
    group.add_argument("--cache", default=None, help="per-lesion map cache (default: <study>/derivatives/lnm/cache, or <out>/cache)")
    group.add_argument("--budget-gb", type=float, default=None, help="memory budget of one engine pass in GB")
    group.add_argument("--include-unreviewed", action="store_true", help="also map lesions that review has not approved")
    return parser


def _map_cohort(args, emit, cancel, argv):
    """Run cohortmaps for --study or --lesions-from into --out; return the maps.tsv path and its run id."""
    from . import cohortmaps

    if not args.bundle:
        raise jobs.JobError("--study and --lesions-from need --bundle (the connectome that maps the lesions)", code="usage", exit_code=jobs.EXIT_USAGE)
    study, excluded, review = None, {}, None
    try:
        if args.study:
            from .study import Study

            study = Study.open(args.study)
            lesions, excluded = cohortmaps.study_lesions(study, args.include_unreviewed)
            review = {"required": study.review_required, "include_unreviewed": bool(args.include_unreviewed)}
        else:
            lesions = lesions_from(args.lesions_from)
    except (OSError, ValueError, KeyError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
    if not lesions:
        raise jobs.JobError("No lesion to map (check include, review and the lesion files)", code="no_lesions")
    budget = cohortmaps._budget_bytes(args.budget_gb)

    def inner(event, **fields):
        # cohortmaps is a stage of this job: its start and done events are not this job's
        if event == "start":
            return None
        if event == "done":
            return emit("result", kind="mapped", path=os.path.join(args.out, MAPS_TSV), n=fields.get("n"), cache_hits=fields.get("cache_hits"), computed=fields.get("computed"))
        return emit(event, **fields)

    record = cohortmaps.run(
        lesions, args.bundle, args.out, subjects=args.subjects, random_subset=args.random, seed=args.seed, subject_list=args.subject_list,
        cache_root=args.cache, study=study, threads=args.threads, cancel=cancel, resume=args.resume, budget_bytes=budget, excluded=excluded,
        review=review, run_root=args.run_root or (study.path if study is not None else None), parent_runs=args.parent_run, argv=argv, emit=inner)
    return os.path.join(args.out, MAPS_TSV), (record or {}).get("run_id")


def main(argv=None, keep_emitter=False):
    """Command-line entry point (see the module docstring); returns the exit code.

    On a terminal the events are readable text: ``jobs.emit`` is the text emitter while main runs, and
    afterwards only with ``keep_emitter`` (the ``python -m`` entry point, whose ``jobs.run_job`` reports
    errors and cancellation after main returns). An in-process caller gets ``jobs.emit`` back unchanged.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    human = not args.json and hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    emit = _human_emitter() if human else jobs.emit
    if not human:
        return _main(parser, args, argv, emit)
    original = jobs.emit
    jobs.emit = emit  # the job protocol's own error and cancelled events read as text too
    try:
        return _main(parser, args, argv, emit)
    finally:
        if not keep_emitter:
            jobs.emit = original


def _main(parser, args, argv, emit):
    low, high = SENSITIVITY_RANGE
    if not low <= args.sensitivity <= high:
        parser.error(f"--sensitivity must be between {low} and {high}")
    if args.conj_alpha is not None and not args.conj_with:
        parser.error("--conj-alpha needs --conj-with")
    rule = _parse_rule(args.rule)
    legacy = _parse_legacy(args.legacy)
    cancel = jobs.CancelToken(args.cancel_file)
    os.makedirs(args.out, exist_ok=True)
    lock = jobs.OutputLock(os.path.join(args.out, LOCK_NAME))
    lock.acquire()
    try:
        return _run(args, argv, rule, legacy, emit, cancel)
    finally:
        lock.release()


def _run(args, argv, rule, legacy, emit, cancel):
    emit("start", job="cohort", argv=argv, threads=args.threads)
    started = time.monotonic()
    parents = list(args.parent_run)
    stages = ["maps", "group maps", "write"] if (args.study or args.lesions_from) else ["group maps", "write"]
    stage = 0
    maps_tsv, cohortmaps_run = args.maps, None
    if args.study or args.lesions_from:
        stage += 1
        emit("stage", name="maps", index=stage, total=len(stages))
        maps_tsv, cohortmaps_run = _map_cohort(args, emit, cancel, argv)
        if cohortmaps_run:
            parents.append(cohortmaps_run)
    elif args.maps_dir and os.path.isfile(os.path.join(args.maps_dir, MAPS_TSV)):
        maps_tsv = os.path.join(args.maps_dir, MAPS_TSV)
    cancel.raise_if_cancelled(done=0)

    # The lesion maps and their metadata
    meta, label, n = {}, None, args.n
    if maps_tsv:
        from .cohortmaps import read_maps_tsv

        if not os.path.isfile(maps_tsv):
            raise jobs.JobError(f"maps.tsv not found: {maps_tsv}", code="input")
        rows = read_maps_tsv(maps_tsv)
        meta = _read_json(os.path.join(os.path.dirname(os.path.abspath(maps_tsv)), COHORTMAPS_JSON)) or {}
        bundle_meta = meta.get("bundle") or {}
        label = bundle_meta.get("label")
        n = n or meta.get("n")
        if not cohortmaps_run and meta.get("run_id"):
            parents.append(meta["run_id"])
    else:
        rows = maps_from_dir(args.maps_dir)
        label = rows[0]["label"] if rows else None
    if not rows:
        raise jobs.JobError("No lesion maps to combine", code="no_maps")
    statistic = rule_statistic(rule)
    column = MAP_COLUMNS[statistic]
    missing = [row["participant_id"] for row in rows if not row.get(column) or row.get(column) == "n/a"]
    if missing:
        raise jobs.JobError(f"{len(missing)} lesion(s) have no {column} map: {', '.join(missing[:5])}", code="input")
    paths = [row[column] for row in rows]
    if n is None and statistic == "T":
        n = _n_from_header(rows[0]["T"]) if rows[0].get("T") else None
        if n is None:
            raise jobs.JobError("The number of connectome subjects is unknown: give --n", code="usage", exit_code=jobs.EXIT_USAGE)
    mask_path = args.mask or (_bundle_mask(args.bundle) if args.bundle else None)
    if mask_path is None:
        recorded = (meta.get("bundle") or {}).get("path")
        if recorded and os.path.isfile(os.path.join(recorded, "mask.nii.gz")):
            mask_path = os.path.join(recorded, "mask.nii.gz")
    if mask_path is None:
        raise jobs.JobError("The connectome mask is needed: give --bundle or --mask", code="usage", exit_code=jobs.EXIT_USAGE)
    if label is None:
        from .connectome import ConnectomeBundle
        from .export import connectome_label

        label = connectome_label(ConnectomeBundle.open(args.bundle).name) if args.bundle else "connectome"
    gsr = (meta.get("bundle") or {}).get("gsr")

    # Groups (listwise: a lesion without a group is in the cohort, not in a two-group comparison)
    ids = [row["participant_id"] for row in rows]
    participants = args.participants or (os.path.join(args.study, "participants.tsv") if args.study else None)
    groups = None
    if participants and os.path.isfile(participants):
        values = read_groups(participants, args.group_column)
        if values:
            groups = [values.get(sid) for sid in ids]
            unknown = [sid for sid in ids if sid not in values]
            if unknown:
                emit("warning", code="no_group", participants=unknown, message=f"{len(unknown)} lesion(s) are not in {os.path.basename(participants)}: they are in no group")
    elif participants:
        raise jobs.JobError(f"participants file not found: {participants}", code="input")

    stage += 1
    emit("stage", name="group maps", index=stage, total=len(stages))
    indices, shape, affine = mask_geometry(mask_path)
    fz_paths = None
    if statistic == "T" and "welch" in legacy and groups is not None and all(row.get("AvgFz") not in (None, "", "n/a") for row in rows):
        fz_paths = [row["AvgFz"] for row in rows]
    total = len(paths) + (len(fz_paths) if fz_paths else 0)
    progress_started = time.monotonic()

    def on_map(done, _count, offset=0):
        spent = time.monotonic() - progress_started
        eta = spent / (done + offset) * (total - done - offset) if done + offset else None
        emit("progress", stage="lesions", done=done + offset, total=total, elapsed=round(spent, 3), eta=None if eta is None else round(eta, 3))
        cancel.raise_if_cancelled(done=done + offset)

    try:
        Y = load_masked(paths, indices, shape, affine, cancel=cancel, on_map=on_map)
        F = load_masked(fz_paths, indices, shape, affine, cancel=cancel, on_map=lambda done, count: on_map(done, count, len(paths))) if fz_paths else None
        conj_with = None
        if args.conj_with:
            other = load_masked([args.conj_with], indices, shape, affine)[0]
            conj_with = (other <= args.conj_alpha).astype(np.uint8) if args.conj_alpha is not None else other
        result = group_maps(Y, rule, groups, args.sensitivity, args.sign, n=n, fz_maps=F if F is not None else (Y if statistic == "Fz" else None),
                            legacy=legacy if (F is not None or statistic == "Fz") else tuple(method for method in legacy if method != "welch"),
                            conj_with=conj_with, ids=ids, gsr=gsr, conjunction_map=not args.no_conjunction)
    except ValueError as exc:
        raise jobs.JobError(str(exc), code="input") from None
    if statistic == "T" and "welch" in legacy and F is None and groups is not None:
        result.notes.append("The legacy Welch t needs the mean Fz maps (AvgFz column): not computed.")
    for note in result.notes:
        if note not in (CLASSIC_NOTE, LEGACY_NOTE):
            emit("warning", code="cohort", message=note)
    cancel.raise_if_cancelled(done=len(paths))

    stage += 1
    emit("stage", name="write", index=stage, total=len(stages))
    run_root = args.run_root or (args.study if args.study else None)
    run_id = None
    params = {"rule": rule, "sign": args.sign, "sensitivity": args.sensitivity, "legacy": list(legacy), "n": n, "label": label, "group_column": args.group_column}
    if run_root:
        run_id = runs.register(run_root, "cohort", args.out, parents=parents, params=params)
    try:
        return _write(args, result, maps_tsv, label, n, ids, parents, mask_path, run_root, run_id, emit, started)
    except BaseException as exc:
        if run_id:
            from .export import now_iso

            cancelled = isinstance(exc, (jobs.Cancelled, KeyboardInterrupt))
            runs.update(run_root, run_id, status="cancelled" if cancelled else "failed", ended=now_iso(), **({} if cancelled else {"error": f"{type(exc).__name__}: {exc}"}))
        raise


def _write(args, result, maps_tsv, label, n, ids, parents, mask_path, run_root, run_id, emit, started):
    """The write stage of the command line: group maps, peaks.tsv and cohort.json."""
    outputs = write_group_maps(result, args.out, mask_path, label)
    for desc, path in outputs.items():
        emit("result", kind="group_map", desc=desc, path=path)
    peaks_path = write_peaks(result, args.out, mask_path)
    emit("result", kind="peaks", path=peaks_path)
    from .export import DISCLAIMER, now_iso

    record = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "run_id": run_id,
        "created": now_iso(),
        "maps_tsv": os.path.relpath(maps_tsv, args.out).replace(os.sep, "/") if maps_tsv else None,
        "connectome": label,
        "n_subjects": n,
        "participants": [{"participant_id": sid, "group": group} for sid, group in zip(ids, result.groups, strict=True)],
        **result.summary(),
        "parents": parents,
        "outputs": {desc: os.path.basename(path) for desc, path in outputs.items()} | {"peaks": PEAKS_TSV},
        "disclaimer": DISCLAIMER,
    }
    json_path = os.path.join(args.out, COHORT_JSON)
    jobs.write_json_atomic(json_path, record)
    emit("result", kind="cohort", path=json_path)
    if run_id:
        runs.update(run_root, run_id, status="done", ended=now_iso(), outputs=[os.path.basename(path) for path in outputs.values()] + [PEAKS_TSV, COHORT_JSON],
                    summary={"n_maps": result.n, "n_cases": len(result.cases), "n_controls": len(result.controls)})
    emit("done", outputs=[*outputs.values(), peaks_path, json_path], elapsed=round(time.monotonic() - started, 3), run_id=run_id, n=result.n)
    return jobs.EXIT_OK


if __name__ == "__main__":
    jobs.run_job(lambda argv: main(argv, keep_emitter=True), job="cohort")

"""Blind double tracing, mask-level agreement and adjudication (L-44; SPEC_LNM_ADDENDUM 3.10, RW-11).

Tracing reliability is measured on the masks, never on the network maps: two tracings of the same lesion give network
maps that correlate highly even when the masks barely overlap, so network similarity overstates tracing agreement
(van den Heuvel et al. 2026). The masks are compared with the distance between *different* subjects' lesions as the
baseline (Cotovio et al. 2020: 8 mm between raters against 51 mm between lesions).

- :func:`pair_metrics`: Dice, centroid distance (mm), volume ratio (smaller / larger) and HD95 (the 95th percentile of the
  symmetric surface distances, ``scipy.ndimage.distance_transform_edt`` on the cropped union) on the tracing grid and on
  the 2 mm connectome grid of the lesion space.
- :func:`between_lesion_baseline`: the median centroid distance between different subjects' primary lesions.
- :func:`network_r`: Pearson r of two network maps, always with :data:`NETWORK_LABEL` (optional, never the agreement).
- :func:`summary`: every pair of the study → ``derivatives/qc/interrater.tsv`` (one row per pair: no path, rater labels
  only; read by the methods text, ``methods.py``) and ``derivatives/qc/interrater.json`` (medians, baseline, seeds).
- :func:`assign`: a seeded random fraction (or chosen subjects) gets a second tracing (``rater2_status`` = assigned);
  the seed, the fraction, the mode and the blind level are recorded in ``study.json`` ``agreement.assignments`` and
  in the audit log.
- :func:`save_second_tracing`: the second rater's mask goes to ``lesions/rater-<initials>/<sub>_space-<sp>_desc-lesion_mask
  .nii.gz`` with a BIDS sidecar; the primary lesion is never read for it (blinding, :func:`check_second_tracing`).
  Intra-rater mode (the same person traces again) needs :data:`INTRA_MIN_DAYS` days since the first tracing.
- :func:`adjudicate`: A | B | Union | Intersection | Edit → the consensus becomes ``desc-lesion`` (``rater`` =
  consensus); the first tracing is kept as ``desc-preadjudication`` and the second stays in its rater folder; one audit
  record per decision, undone by :func:`undo_adjudication`.

Columns (registered optional columns, :func:`register_columns`): ``rater2_status`` (assigned, inprogress, done, adjudicated),
``rater2`` (the second rater), ``rater2_mode`` (inter, intra), ``rater2_level`` (full, trace_only), ``rater2_lesion``,
``rater2_date``, ``rater2_dice``, ``rater1_lesion`` (the kept first tracing after adjudication) and ``adjudication``.

Pure numpy / scipy and the standard library (no Slicer, Qt or VTK). Shared files hold study-relative paths only.
"""

import contextlib
import datetime
import json
import math
import os
import random
import re
import secrets
import shutil
import statistics

import numpy as np

from . import geometry, nifti, schema

#: Second tracing states (``rater2_status``; "n/a" when the subject has no second tracing)
ASSIGNED = "assigned"
#: Work in progress written in passing (Skip, Exit, another subject): still in the queue, not a pair yet
IN_PROGRESS = "inprogress"
DONE = "done"
ADJUDICATED = "adjudicated"
STATUSES = (ASSIGNED, IN_PROGRESS, DONE, ADJUDICATED)
#: Pairs of the agreement (a finished second tracing)
PAIR_STATUSES = (DONE, ADJUDICATED)
#: Second tracings still to do (``StudioLib.study_ui.SECOND_TRACING_TODO`` also accepts "pending", "untraced", "inprogress")
TODO_STATUSES = (ASSIGNED, "pending", "untraced", IN_PROGRESS)
MODES = ("inter", "intra")
#: "full": the second rater makes their own figure crop and fit; "trace_only": the first tracing's figure fit is reused
LEVELS = ("full", "trace_only")
DEFAULT_FRACTION = 0.2
DEFAULT_LEVEL = "trace_only"
#: Pairs with a Dice below this go to the Adjudicate queue (``study.json`` ``agreement.dice_threshold``)
DICE_THRESHOLD = 0.5
#: Intra-rater mode: days between the first and the second tracing
INTRA_MIN_DAYS = 7
#: Adjudication choices
CHOICES = ("A", "B", "union", "intersection", "edit")
CONSENSUS_RATER = "consensus"
#: Voxels with at least this value count as lesion (fractional masks; as lesionops.LESION_THRESHOLD)
LESION_THRESHOLD = 0.5
HD_PERCENTILE = 95.0
#: The label shown with every network similarity of two tracings
NETWORK_LABEL = "network similarity overstates tracing agreement (van den Heuvel 2026)"
BASELINE_NOTE = "Cotovio et al. 2020: 8 mm between raters vs 51 mm between lesions"
#: Files
INTERRATER_TSV = ("derivatives", "qc", "interrater.tsv")
INTERRATER_JSON = ("derivatives", "qc", "interrater.json")
SUMMARY_FORMAT = "slicer-lnm-interrater"
SUMMARY_VERSION = 1
#: Additive key of study.json
STUDY_KEY = "agreement"
RATER_DIR_PREFIX = "rater-"
PREADJUDICATION_DESC = "preadjudication"
#: Audit events (``.studio/audit.jsonl``, shared with review.py)
ASSIGN_EVENT = "second_tracing_assign"
TRACE_EVENT = "second_tracing"
ADJUDICATE_EVENT = "adjudicate"
UNDO_EVENT = "adjudicate_undo"
#: Columns of interrater.tsv ("dice" is the column methods.py reads)
TSV_COLUMNS = (
    "participant_id", "rater_a", "rater_b", "mode", "level", "dice", "centroid_mm", "volume_ratio", "hd95_mm", "volume_a_ml", "volume_b_ml",
    "grid", "dice_2mm", "centroid_2mm_mm", "volume_ratio_2mm", "hd95_2mm_mm", "grid_2mm", "adjudicated", "below_threshold",
)
#: interrater.tsv columns of the metrics on the 2 mm grid
_COARSE_COLUMNS = {"dice": "dice_2mm", "centroid_mm": "centroid_2mm_mm", "volume_ratio": "volume_ratio_2mm", "hd95_mm": "hd95_2mm_mm"}
#: Columns a consensus replaces (restored by :func:`undo_adjudication`)
_CONSENSUS_COLUMNS = ("rater", "rater2_status", "rater1_lesion", "adjudication", "volume_ml", "n_parts", "centroid_x", "centroid_y", "centroid_z",
                      "modified", "lesion_mtime", "lesion_size")
#: Review columns an adjudication that reopened an approval replaced (restored by :func:`undo_adjudication`)
_REVIEW_COLUMNS = ("review_status", "reviewer", "review_comment")
_NA = "n/a"


class AgreementError(ValueError):
    """A second tracing or an adjudication that the rules do not allow (blinding, intra-rater interval, missing files)."""


#
# Columns
#

COLUMN_SPECS = (
    schema.ColumnSpec("rater2_status", "categorical", "bookkeeping", section="workflow", strict=True,
                      levels={ASSIGNED: "Second tracing to do", IN_PROGRESS: "Second tracing in progress", DONE: "Second tracing done",
                              ADJUDICATED: "Consensus written"},
                      description="Second (blind) tracing for inter-rater agreement"),
    schema.ColumnSpec("rater2", "text", "bookkeeping", section="workflow", description="Second rater"),
    schema.ColumnSpec("rater2_mode", "categorical", "bookkeeping", section="workflow", levels={"inter": "Another rater", "intra": "The same rater, later"},
                      description="Inter- or intra-rater second tracing"),
    schema.ColumnSpec("rater2_level", "categorical", "bookkeeping", section="workflow",
                      levels={"full": "Own figure crop and fit", "trace_only": "Reuses the first tracing's figure fit"}, description="Blinding level of the second tracing"),
    schema.ColumnSpec("rater2_lesion", "path", "bookkeeping", section="imaging", description="Second tracing (path relative to the study)"),
    schema.ColumnSpec("rater2_date", "date", "bookkeeping", section="workflow", description="When the second tracing was saved"),
    schema.ColumnSpec("rater2_dice", "numeric", "bookkeeping", section="analysis", description="Dice of the first and the second tracing (tracing grid)"),
    schema.ColumnSpec("rater1_lesion", "path", "bookkeeping", section="imaging", description="First tracing kept when a consensus replaced it"),
    schema.ColumnSpec("adjudication", "categorical", "bookkeeping", section="workflow",
                      levels={"A": "First tracing", "B": "Second tracing", "union": None, "intersection": None, "edit": "Edited consensus"},
                      description="How the consensus lesion was made"),
)


def register_columns():
    """Register the optional columns of double tracing (idempotent)."""
    for spec in COLUMN_SPECS:
        schema.register_column(spec)


register_columns()


#
# Helpers
#


def _now():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _missing(value):
    return value is None or (isinstance(value, str) and value.strip() in ("", _NA))


def _float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_time(text):
    """A datetime (UTC) of ISO 8601 text, or None."""
    if _missing(text):
        return None
    text = str(text).strip().replace("Z", "+00:00")
    try:
        value = datetime.datetime.fromisoformat(text)
    except ValueError:
        try:
            value = datetime.datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.UTC)
    return value


def _audit(study, record):
    from . import review

    review.append_audit(study, record)
    return record


def rater_label(name):
    """The BIDS label of a rater (``rater-<label>``): a short name or initials as typed ("AB", "ab2"), else the
    initials of the words ("Ana Bell" → "AB"); letters and digits only. Empty → ValueError.
    """
    text = str(name or "").strip()
    compact = re.sub(r"[^A-Za-z0-9]", "", text)
    if not compact:
        raise ValueError(f"A rater name needs letters or digits: {name!r}")
    if len(compact) <= 6 and re.fullmatch(r"[A-Za-z0-9]+", text):
        return compact
    words = [word for word in re.split(r"[^A-Za-z0-9]+", text) if word]
    return "".join(word[0] for word in words).upper() or compact[:6]


#: Names that do not identify a rater (review.py records "unnamed" when no rater name is set)
_UNNAMED = ("unnamed", CONSENSUS_RATER)


def _named(value):
    return not _missing(value) and str(value).strip().lower() not in _UNNAMED


def done_tracers(study):
    """{sid: who} of the first review record written by a tracer's Done (``review`` event, ``via`` "done": who pressed
    Done when the lesion went to review), read once from the audit log.
    """
    from . import review

    out = {}
    for record in review.read_audit(study, event=review.AUDIT_EVENT):
        sid = record.get("sid")
        if sid and sid not in out and record.get("via") == "done" and _named(record.get("who")):
            out[sid] = str(record["who"]).strip()
    return out


def primary_rater(row, study=None, tracers=None):
    """Who traced the primary lesion, or None when it is not recorded.

    The ``tracer`` column (Studio's Done records it, :mod:`StudioLib.adjudicate`), else ``rater`` (not a consensus); with
    ``study``, also the review audit (who pressed Done, :func:`done_tracers`; ``tracers`` that map when the caller has
    it) and the lesion sidecar (``SlicerStudio`` ``tracer`` or ``rater``).
    """
    row = row or {}
    for key in ("tracer", "rater"):
        value = row.get(key)
        if _named(value):
            return str(value)
    if study is None:
        return None
    sid = row.get("participant_id")
    tracers = done_tracers(study) if tracers is None else tracers
    if sid in tracers:
        return tracers[sid]
    lesion = row.get("rater1_lesion") if not _missing(row.get("rater1_lesion")) else row.get("lesion")
    path = None if _missing(lesion) or str(lesion).startswith("controlset:") else study.abspath(lesion)
    if path and os.path.isfile(_sidecar_path(path)):
        with contextlib.suppress(OSError, ValueError, AttributeError):
            with open(_sidecar_path(path), encoding="utf-8") as f:
                studio = (json.load(f) or {}).get(schema.FORK_KEY) or {}
            for key in ("tracer", "rater"):
                if _named(studio.get(key)):
                    return str(studio[key])
    return None


def _same_rater(a, b):
    if not a or not b:
        return False
    with contextlib.suppress(ValueError):
        return rater_label(a).lower() == rater_label(b).lower() or str(a).strip().lower() == str(b).strip().lower()
    return False


def settings(study):
    """``study.json`` ``agreement``: {"dice_threshold", "assignments": [...]} (a copy, with defaults)."""
    stored = dict(getattr(study, "metadata", {}).get(STUDY_KEY) or {})
    stored.setdefault("dice_threshold", DICE_THRESHOLD)
    stored.setdefault("assignments", [])
    return stored


def dice_threshold(study):
    value = _float(settings(study).get("dice_threshold"))
    return DICE_THRESHOLD if value is None else value


def set_dice_threshold(study, value, who=None):
    """Change the Dice below which pairs are adjudicated (audited as a study setting). Return the record or None."""
    value = float(value)
    if not 0.0 < value <= 1.0:
        raise ValueError("The adjudication threshold is a Dice between 0 and 1")
    before = dice_threshold(study)
    if before == value:
        return None
    stored = dict(study.metadata.get(STUDY_KEY) or {})
    stored["dice_threshold"] = value
    study.metadata[STUDY_KEY] = stored
    return _audit(study, {"event": "study_setting", "key": "agreement.dice_threshold", "from": before, "to": value, "who": (who or "").strip() or None, "when": _now()})


#
# Rows and queues
#


def status_of(row):
    value = (row or {}).get("rater2_status")
    return None if _missing(value) else str(value)


def second_tracing_todo(row):
    """A second tracing to do (the 'Second tracing' queue)."""
    status = status_of(row)
    return status is not None and status.lower() in TODO_STATUSES


def needs_adjudication(row, threshold=DICE_THRESHOLD):
    """A finished pair whose Dice is below ``threshold`` (the 'Adjudicate' queue)."""
    if status_of(row) != DONE:
        return False
    dice = _float((row or {}).get("rater2_dice"))
    return dice is not None and dice < threshold


#
# Masks
#


def load_mask(path):
    """(data [i, j, k] float, affine) of a lesion file (the first volume of a 4D file)."""
    data, affine, _header = nifti.read_nifti(path)
    if data.ndim > 3:
        data = data[..., 0]
    return np.asarray(data, dtype=np.float32), np.asarray(affine, dtype=np.float64)


def _binary(mask):
    array = np.asarray(mask)
    if array.dtype == bool:
        return array
    return np.nan_to_num(array.astype(np.float32, copy=False)) >= LESION_THRESHOLD


def _spacing(affine):
    return np.sqrt((np.asarray(affine, dtype=np.float64)[:3, :3] ** 2).sum(axis=0))


def _voxel_ml(affine):
    return abs(float(np.linalg.det(np.asarray(affine, dtype=np.float64)[:3, :3]))) / 1000.0


def centroid_mm(mask, affine):
    """World (RAS) centroid of a mask, or None when it is empty."""
    ijk = np.argwhere(_binary(mask))
    if ijk.size == 0:
        return None
    center = ijk.mean(axis=0)
    return (np.asarray(affine, dtype=np.float64) @ np.append(center, 1.0))[:3]


def _bbox(mask):
    """[lo, hi) of the voxels of a mask along each axis, or None."""
    lo, hi = [], []
    for axis in range(3):
        used = np.flatnonzero(mask.any(axis=tuple(a for a in range(3) if a != axis)))
        if used.size == 0:
            return None
        lo.append(int(used[0]))
        hi.append(int(used[-1]) + 1)
    return lo, hi


def _surface(mask):
    """Voxels of a mask with a face neighbour outside it."""
    from scipy import ndimage

    eroded = ndimage.binary_erosion(mask, structure=ndimage.generate_binary_structure(3, 1), border_value=0)
    return mask & ~eroded


def surface_distances(a, b, spacing):
    """Distances (mm) from each surface voxel of ``a`` to the surface of ``b`` and back, on the union's bounding box
    (padded by one voxel); ``spacing`` the voxel size along i, j, k. None when a mask is empty.
    """
    from scipy import ndimage

    a, b = _binary(a), _binary(b)
    if not a.any() or not b.any():
        return None
    box = _bbox(a | b)
    lo, hi = box
    crop = tuple(slice(low, high) for low, high in zip(lo, hi, strict=True))
    ca = np.pad(a[crop], 1)
    cb = np.pad(b[crop], 1)
    sa, sb = _surface(ca), _surface(cb)
    spacing = [float(value) for value in spacing]
    to_b = ndimage.distance_transform_edt(~sb, sampling=spacing)
    to_a = ndimage.distance_transform_edt(~sa, sampling=spacing)
    return np.concatenate([to_b[sa], to_a[sb]])


def hd95(a, b, spacing, percentile=HD_PERCENTILE):
    """The 95th percentile Hausdorff distance (mm): the percentile of the symmetric surface distances (both directions
    pooled, as medpy's ``hd95``). None when a mask is empty; 0 for identical masks.
    """
    distances = surface_distances(a, b, spacing)
    if distances is None or distances.size == 0:
        return None
    return float(np.percentile(distances, percentile))


def grid_metrics(a, b, affine):
    """Dice, centroid distance, volume ratio and HD95 of two masks on one grid (``affine``)."""
    a, b = _binary(a), _binary(b)
    if a.shape != b.shape:
        raise ValueError(f"The masks have different shapes: {a.shape} and {b.shape}")
    na, nb = int(np.count_nonzero(a)), int(np.count_nonzero(b))
    common = int(np.count_nonzero(a & b))
    voxel_ml = _voxel_ml(affine)
    ca, cb = centroid_mm(a, affine), centroid_mm(b, affine)
    return {
        "dice": (2.0 * common / (na + nb)) if na + nb else None,
        "centroid_mm": float(np.linalg.norm(ca - cb)) if ca is not None and cb is not None else None,
        "volume_ratio": (min(na, nb) / max(na, nb)) if max(na, nb) else None,
        "hd95_mm": hd95(a, b, _spacing(affine)),
        "volume_a_ml": na * voxel_ml,
        "volume_b_ml": nb * voxel_ml,
        "voxels_a": na,
        "voxels_b": nb,
    }


def _to_grid(mask, affine, grid):
    """A mask resampled onto ``grid`` (fractional occupancy ≥ 0.5; a mask smaller than that keeps its fullest voxels)."""
    array = np.asarray(mask, dtype=np.float32)
    support = _bbox(_binary(array) | (array > 0))
    if support is None:
        return np.zeros(grid.shape, dtype=bool)
    lo, hi = support
    shift = np.eye(4)
    shift[:3, 3] = lo
    crop = array[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]
    fraction = geometry.resample_to_grid(crop, np.asarray(affine, dtype=np.float64) @ shift, grid, mode="fraction")
    keep = fraction >= LESION_THRESHOLD
    if not keep.any() and fraction.max() > 0:
        keep = fraction == fraction.max()
    return keep


def _as_grid(grid):
    if grid is None:
        return None
    if isinstance(grid, geometry.Grid):
        return grid
    if isinstance(grid, str) and grid in geometry.GRID_BY_ID:
        return geometry.GRID_BY_ID[grid].grid
    if isinstance(grid, geometry.GridSpec):
        return grid.grid
    shape, affine = grid
    return geometry.Grid(shape, affine)


def connectome_grid(space):
    """The 2 mm connectome grid id of a lesion space ("NLin6-2mm", "TF2009-2mm"), or None (native or unknown spaces)."""
    family = geometry.space_family(space)
    for spec in geometry.GRID_REGISTRY:
        if spec.spacing == 2.0 and spec.family == family:
            return spec.grid_id
    return None


def pair_metrics(a, b, affine, grid2mm=None, affine_b=None):
    """Agreement of two tracings of one lesion.

    :param a, b: masks indexed [i, j, k] (values ≥ 0.5 are lesion).
    :param affine: voxel-to-RAS matrix of ``a`` (and of ``b`` unless ``affine_b``).
    :param grid2mm: the 2 mm connectome grid (a registry id such as "NLin6-2mm", a ``geometry.Grid`` or (shape, affine));
      None: the tracing grid only.
    :param affine_b: the grid of ``b`` when it differs: ``b`` is resampled onto ``a``'s grid (fractional occupancy ≥ 0.5).
    :return: {"dice", "centroid_mm", "volume_ratio" (smaller / larger), "hd95_mm", "volume_a_ml", "volume_b_ml",
      "voxels_a", "voxels_b", "grid2mm": the same keys on the 2 mm grid or None}. A metric that is not defined (an empty
      mask) is None.
    """
    a = np.asarray(a)
    b = np.asarray(b)
    affine = np.asarray(affine, dtype=np.float64)
    if affine_b is not None and not geometry.Grid(b.shape, affine_b).matches(geometry.Grid(a.shape, affine)):
        b = _to_grid(b, affine_b, geometry.Grid(a.shape, affine))
    result = grid_metrics(a, b, affine)
    grid = _as_grid(grid2mm)
    result["grid2mm"] = None
    if grid is not None:
        coarse = grid_metrics(_to_grid(a, affine, grid), _to_grid(b, affine, grid), grid.affine)
        result["grid2mm"] = coarse
    return result


#
# Pairs of a study
#


def pair_paths(study, row):
    """(first tracing, second tracing) absolute paths of a row (the kept first tracing after adjudication), or None."""
    second = row.get("rater2_lesion")
    if _missing(second):
        return None
    first = row.get("rater1_lesion") if not _missing(row.get("rater1_lesion")) else row.get("lesion")
    if _missing(first):
        return None
    first, second = study.abspath(first), study.abspath(second)
    if not (first and second and os.path.isfile(first) and os.path.isfile(second)):
        return None
    return first, second


def _space_of(study, row):
    space = None if _missing(row.get("space")) else row["space"]
    return space or getattr(study, "space", None)


def study_pair(study, sid, grid2mm="auto"):
    """:func:`pair_metrics` of a subject's two tracings (the 2 mm grid of its space with ``grid2mm`` "auto"), or None."""
    row = study.subject(sid)
    paths = pair_paths(study, row)
    if paths is None:
        return None
    a, affine_a = load_mask(paths[0])
    b, affine_b = load_mask(paths[1])
    grid = connectome_grid(_space_of(study, row)) if grid2mm == "auto" else grid2mm
    result = pair_metrics(a, b, affine_a, grid2mm=grid, affine_b=affine_b)
    result["grid_2mm_id"] = grid if isinstance(grid, str) else None
    return result


def between_lesion_baseline(study, sids=None):
    """The centroid distance between different subjects' primary lesions: {"median_mm", "q1_mm", "q3_mm", "n_lesions",
    "n_pairs", "note"} (median None with fewer than 2 lesions).

    Centroids come from ``centroid_x/y/z`` when the row has them, else from the lesion file (the first tracing before an
    adjudication). Included subjects with a lesion only; lesions of different spaces are not compared across families.
    """
    rows = [row for row in study.subjects if str(row.get("include", "1")) != "0" and (sids is None or row["participant_id"] in sids)]
    centroids = {}
    for row in rows:
        values = [_float(row.get(key)) for key in ("centroid_x", "centroid_y", "centroid_z")]
        family = geometry.space_family(_space_of(study, row))
        if family in ("native", "unknown") and not _missing(row.get("space")):
            continue  # native-space lesions are not in a common space
        if all(value is not None for value in values) and _missing(row.get("rater1_lesion")):
            centroids[row["participant_id"]] = (family, np.array(values))
            continue
        path = study.abspath(row.get("rater1_lesion") if not _missing(row.get("rater1_lesion")) else row.get("lesion"))
        if not path or not os.path.isfile(path):
            continue
        with contextlib.suppress(OSError, ValueError):
            mask, affine = load_mask(path)
            center = centroid_mm(mask, affine)
            if center is not None:
                centroids[row["participant_id"]] = (family, center)
    ids = list(centroids)
    distances = []
    for index, first in enumerate(ids):
        for second in ids[index + 1:]:
            if centroids[first][0] != centroids[second][0]:
                continue
            distances.append(float(np.linalg.norm(centroids[first][1] - centroids[second][1])))
    out = {"median_mm": None, "q1_mm": None, "q3_mm": None, "n_lesions": len(ids), "n_pairs": len(distances), "note": BASELINE_NOTE}
    if distances:
        q1, median, q3 = np.percentile(distances, [25, 50, 75])
        out.update(median_mm=float(median), q1_mm=float(q1), q3_mm=float(q3))
    return out


def network_r(map_a, map_b, mask=None):
    """Pearson r of two network maps (arrays or NIfTI paths on one grid), with :data:`NETWORK_LABEL`.

    Voxels: finite in both, inside ``mask`` when given, else where either map is non-zero.
    :return: {"r", "n_voxels", "label"}; r None with fewer than 3 voxels or a constant map.
    """
    if isinstance(map_a, (str, os.PathLike)):
        map_a = load_mask(map_a)[0]
    if isinstance(map_b, (str, os.PathLike)):
        map_b = load_mask(map_b)[0]
    a = np.asarray(map_a, dtype=np.float64)
    b = np.asarray(map_b, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"The maps have different shapes: {a.shape} and {b.shape}")
    keep = np.isfinite(a) & np.isfinite(b)
    keep &= np.asarray(mask, dtype=bool) if mask is not None else ((a != 0) | (b != 0))
    x, y = a[keep], b[keep]
    r = None
    if x.size >= 3 and x.std() > 0 and y.std() > 0:
        r = float(np.corrcoef(x, y)[0, 1])
    return {"r": r, "n_voxels": int(x.size), "label": NETWORK_LABEL}


def _rounded(value, digits=4):
    return None if value is None else round(float(value), digits)


def _median(values):
    values = [value for value in values if value is not None]
    return float(statistics.median(values)) if values else None


def summary(study, threshold=None, grid2mm="auto", write=True, update_rows=True):
    """Agreement of every pair of the study.

    :param threshold: Dice below which a pair is adjudicated (default: the study's, :func:`dice_threshold`).
    :param write: write ``derivatives/qc/interrater.tsv`` and ``interrater.json`` (atomically).
    :param update_rows: set ``rater2_dice`` of the rows in memory (the caller saves the study).
    :return: {"format", "version", "created", "n_pairs", "threshold", "median": {metric: value}, "median_2mm",
      "baseline" (:func:`between_lesion_baseline`), "below_threshold": [ids], "adjudicated", "modes", "assignments",
      "pairs": [tsv rows], "label_network", "text"}.
    """
    threshold = dice_threshold(study) if threshold is None else float(threshold)
    pairs = []
    tracers = done_tracers(study)
    for row in study.subjects:
        sid = row["participant_id"]
        if status_of(row) not in PAIR_STATUSES:
            continue  # a second tracing in progress is not a pair yet
        try:
            metrics = study_pair(study, sid, grid2mm)
        except (OSError, ValueError) as error:
            metrics = None
            row_error = str(error)
        else:
            row_error = None
        if metrics is None:
            if row_error:
                pairs.append({"participant_id": sid, "error": row_error})
            continue
        coarse = metrics.get("grid2mm") or {}
        dice = metrics["dice"]
        first = primary_rater(row, study, tracers) if _missing(row.get("rater1_lesion")) else _first_rater_of(study, sid, row)
        pair = {
            "participant_id": sid,
            "rater_a": _label_or_none(first),
            "rater_b": _label_or_none(row.get("rater2")),
            "mode": None if _missing(row.get("rater2_mode")) else row["rater2_mode"],
            "level": None if _missing(row.get("rater2_level")) else row["rater2_level"],
            "dice": _rounded(dice),
            "centroid_mm": _rounded(metrics["centroid_mm"], 2),
            "volume_ratio": _rounded(metrics["volume_ratio"]),
            "hd95_mm": _rounded(metrics["hd95_mm"], 2),
            "volume_a_ml": _rounded(metrics["volume_a_ml"]),
            "volume_b_ml": _rounded(metrics["volume_b_ml"]),
            "grid": "tracing",
            "dice_2mm": _rounded(coarse.get("dice")),
            "centroid_2mm_mm": _rounded(coarse.get("centroid_mm"), 2),
            "volume_ratio_2mm": _rounded(coarse.get("volume_ratio")),
            "hd95_2mm_mm": _rounded(coarse.get("hd95_mm"), 2),
            "grid_2mm": metrics.get("grid_2mm_id"),
            "adjudicated": "1" if status_of(row) == ADJUDICATED else "0",
            "below_threshold": "1" if dice is not None and dice < threshold else "0",
        }
        pairs.append(pair)
        if update_rows and dice is not None:
            study.set(sid, rater2_dice=round(dice, 4))
    good = [pair for pair in pairs if "error" not in pair]
    baseline = between_lesion_baseline(study)
    out = {
        "format": SUMMARY_FORMAT,
        "version": SUMMARY_VERSION,
        "created": _now(),
        "n_pairs": len(good),
        "threshold": threshold,
        "median": {key: _median(pair[key] for pair in good) for key in ("dice", "centroid_mm", "volume_ratio", "hd95_mm")},
        "median_2mm": {key: _median(pair[column] for pair in good) for key, column in _COARSE_COLUMNS.items()},
        "baseline": baseline,
        "below_threshold": [pair["participant_id"] for pair in good if pair["below_threshold"] == "1" and pair["adjudicated"] == "0"],
        "adjudicated": sum(1 for pair in good if pair["adjudicated"] == "1"),
        "modes": {mode: sum(1 for pair in good if pair["mode"] == mode) for mode in MODES},
        "assignments": settings(study)["assignments"],
        "errors": [{"participant_id": pair["participant_id"], "error": pair["error"]} for pair in pairs if "error" in pair],
        "pairs": good,
        "label_network": NETWORK_LABEL,
    }
    out["text"] = methods_text(out)
    if write:
        write_summary(study, out)
    return out


def _label_or_none(name):
    if _missing(name):
        return None
    try:
        return rater_label(name)
    except ValueError:
        return None


def _first_rater_of(study, sid, row):
    """The first tracing's rater after a consensus replaced ``rater`` (from the adjudication record)."""
    from . import review

    records = [record for record in review.read_audit(study, sid=sid, event=ADJUDICATE_EVENT) if record.get("raters")]
    if records:
        return records[-1]["raters"][0]
    return primary_rater(row, study)


def write_summary(study, data):
    """Write interrater.tsv (pairs) and interrater.json (the rest) under ``derivatives/qc``. Return the two paths."""
    from .study import write_tsv

    tsv = os.path.join(study.path, *INTERRATER_TSV)
    path = os.path.join(study.path, *INTERRATER_JSON)
    os.makedirs(os.path.dirname(tsv), exist_ok=True)
    write_tsv(tsv, list(TSV_COLUMNS), data["pairs"])
    content = {key: value for key, value in data.items() if key != "pairs"}
    schema.write_text_atomic(path, json.dumps(content, indent=2, ensure_ascii=False) + "\n")
    return tsv, path


def read_summary(study):
    """The stored interrater.json (a dict), or None."""
    try:
        with open(os.path.join(study.path, *INTERRATER_JSON), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _fmt(value, digits=2, unit=""):
    return "not recorded" if value is None else f"{value:.{digits}f}{unit}"


def methods_text(data):
    """One methods sentence of a :func:`summary` (mask-level agreement against the between-lesion baseline)."""
    n = data.get("n_pairs") or 0
    if not n:
        return "Inter-rater agreement: no lesion was traced twice."
    median = data.get("median") or {}
    baseline = (data.get("baseline") or {}).get("median_mm")
    modes = data.get("modes") or {}
    kinds = ", ".join(f"{count} {mode}-rater" for mode, count in modes.items() if count) or f"{n}"
    text = (f"Tracing reliability was measured on the masks of {n} lesion(s) traced twice, blind to the first tracing ({kinds}): "
            f"median Dice {_fmt(median.get('dice'))}, centroid distance {_fmt(median.get('centroid_mm'), 1, ' mm')}, "
            f"volume ratio {_fmt(median.get('volume_ratio'))} and HD95 {_fmt(median.get('hd95_mm'), 1, ' mm')}")
    if baseline is not None:
        text += f", against a median centroid distance of {baseline:.1f} mm between different subjects' lesions"
    text += f"; {data.get('adjudicated', 0)} pair(s) were adjudicated to a consensus (Dice below {data.get('threshold', DICE_THRESHOLD):g})."
    return text


#
# Assignment
#


def eligible(study, rater=None, mode="inter"):
    """Subjects that may get a second tracing, in table order: included, traced, with a lesion file in the study, without
    one already; inter mode leaves out the rater's own lesions (:func:`primary_rater`), intra mode keeps only them and the
    lesions whose tracer is not recorded (the assigner names the rater who traced them, checked against ``rater2``).
    """
    out = []
    tracers = None
    for row in study.subjects:
        if str(row.get("include", "1")) == "0" or row.get("status") not in ("traced", "mapped") or status_of(row) is not None:
            continue
        lesion = row.get("lesion")
        if _missing(lesion) or str(lesion).startswith("controlset:"):
            continue
        path = study.abspath(lesion)
        if not path or not os.path.isfile(path):
            continue
        if mode == "intra" or rater:
            if tracers is None:
                tracers = done_tracers(study)
            first = primary_rater(row, study, tracers)
            if mode == "inter" and _same_rater(first, rater):
                continue
            if mode == "intra" and first is not None and not _same_rater(first, rater):
                continue
        out.append(row["participant_id"])
    return out


def assign(study, fraction=DEFAULT_FRACTION, rater=None, seed=None, sids=None, mode="inter", level=DEFAULT_LEVEL, who=None, when=None, save=False):
    """Assign second tracings: a seeded random ``fraction`` of the eligible subjects (:func:`eligible`), or ``sids``.

    :param rater: the planned second rater (any rater when None, inter mode); required for intra mode, which keeps only
      that rater's own lesions.
    :param seed: the random seed (a new one when None); recorded with the fraction, mode, level and the subjects in
      ``study.json`` ``agreement.assignments`` and in the audit log, so the selection can be reproduced.
    :return: the assignment record {"seed", "fraction", "sids", "rater", "mode", "level", "who", "when", "eligible"}.
    :raises ValueError: an unknown mode or level, intra mode without a rater, a fraction outside (0, 1], nothing eligible.
    """
    if mode not in MODES:
        raise ValueError(f"Unknown mode {mode!r} (one of {', '.join(MODES)})")
    if level not in LEVELS:
        raise ValueError(f"Unknown blind level {level!r} (one of {', '.join(LEVELS)})")
    if mode == "intra" and not rater:
        raise ValueError("Intra-rater agreement needs the rater who traces again")
    pool = eligible(study, rater, mode)
    if sids is not None:
        chosen = [sid for sid in study.subject_ids if sid in set(sids) and sid in pool]
        skipped = [sid for sid in sids if sid not in chosen]
        seed_value = None
    else:
        fraction = float(fraction)
        if not 0.0 < fraction <= 1.0:
            raise ValueError("The fraction of subjects traced twice is between 0 and 1")
        seed_value = int(seed) if seed is not None else secrets.randbelow(2**31)
        count = min(len(pool), max(1, math.ceil(fraction * len(pool)))) if pool else 0
        picked = set(random.Random(seed_value).sample(sorted(pool), count))
        chosen = [sid for sid in study.subject_ids if sid in picked]
        skipped = []
    if not chosen:
        raise ValueError("No subject can get a second tracing (traced, included, with a lesion file, not assigned yet)")
    for sid in chosen:
        values = {"rater2_status": ASSIGNED, "rater2_mode": mode, "rater2_level": level}
        if rater:
            values["rater2"] = rater
        study.set(sid, values)
    record = {"seed": seed_value, "fraction": None if sids is not None else fraction, "sids": chosen, "rater": rater or None, "mode": mode,
              "level": level, "who": (who or "").strip() or None, "when": when or _now(), "eligible": len(pool)}
    if skipped:
        record["skipped"] = skipped
    if mode == "intra":
        tracers = done_tracers(study)
        unrecorded = [sid for sid in chosen if primary_rater(study.subject(sid), study, tracers) is None]
        if unrecorded:
            # The assigner names the rater who traced these (no tracer recorded): the second tracing is checked against rater2
            record["tracer_named_by_assigner"] = unrecorded
    stored = dict(study.metadata.get(STUDY_KEY) or {})
    stored["assignments"] = [*list(stored.get("assignments") or []), record]
    study.metadata[STUDY_KEY] = stored
    if save:
        study.save()
    _audit(study, {"event": ASSIGN_EVENT, **record})
    return record


def unassign(study, sids, who=None):
    """Remove second tracings that are not done yet (assigned → n/a, audited). Return the ids changed."""
    changed = [sid for sid in sids if sid in study and second_tracing_todo(study.subject(sid))]
    for sid in changed:
        study.set(sid, {"rater2_status": None, "rater2": None, "rater2_mode": None, "rater2_level": None})
    if changed:
        _audit(study, {"event": ASSIGN_EVENT, "sids": changed, "unassigned": True, "who": (who or "").strip() or None, "when": _now()})
    return changed


#
# Second tracing (blind)
#


def intra_not_before(row):
    """The earliest time an intra-rater second tracing may start (the lesion's last change + 7 days), or None."""
    traced = _parse_time(row.get("modified"))
    return None if traced is None else traced + datetime.timedelta(days=INTRA_MIN_DAYS)


def check_second_tracing(study, sid, rater, now=None):
    """Raise :class:`AgreementError` if ``rater`` may not trace ``sid`` again now; return the row.

    - The subject must be assigned (or have a second tracing in progress or being redone by the same rater); a planned
      second rater (``rater2``) is the only one who may trace it.
    - Inter-rater: the second rater is not the one who traced the lesion (:func:`primary_rater`).
    - Intra-rater: the same rater (the recorded tracer, else the planned ``rater2`` the assigner named), at least
      :data:`INTRA_MIN_DAYS` days after the first tracing (date-checked against the lesion's ``modified`` time; a lesion
      without one cannot be checked and is refused).
    """
    row = study.subject(sid)
    status = status_of(row)
    if status is None:
        raise AgreementError(f"{sid} is not assigned a second tracing")
    if status == ADJUDICATED:
        raise AgreementError(f"{sid} already has a consensus lesion")
    if not rater:
        raise AgreementError("A second tracing needs the rater's name")
    planned = None if _missing(row.get("rater2")) else row["rater2"]
    if planned and not _same_rater(planned, rater):
        if status == DONE:
            raise AgreementError(f"{sid} was traced a second time by {planned}")
        raise AgreementError(f"The second tracing of {sid} is assigned to {planned}")
    mode = row.get("rater2_mode") if row.get("rater2_mode") in MODES else "inter"
    first = primary_rater(row, study)
    if mode == "inter" and _same_rater(first, rater):
        raise AgreementError(f"{rater} traced {sid} first: an inter-rater second tracing is by another rater")
    if mode == "intra":
        if first and not _same_rater(first, rater):
            raise AgreementError(f"{sid} is an intra-rater second tracing: only {first}, who traced it first, traces it again")
        if not first and not planned:
            raise AgreementError(f"{sid}: who traced it first is not recorded, so an intra-rater second tracing needs a named rater")
        not_before = intra_not_before(row)
        if not_before is None:
            raise AgreementError(f"{sid}: the date of the first tracing is not recorded, so the {INTRA_MIN_DAYS}-day interval cannot be checked")
        now = now or datetime.datetime.now(datetime.UTC)
        if now < not_before:
            raise AgreementError(f"Intra-rater agreement needs {INTRA_MIN_DAYS} days between tracings: {sid} can be traced again from {not_before.date().isoformat()}")
    return row


def second_relpath(study, sid, rater, space=None):
    """``lesions/rater-<label>/<sid>_space-<space>_desc-lesion_mask.nii.gz``."""
    from .study import LESIONS_DIR

    row = study.subject(sid)
    space = space or _space_of(study, row) or "native"
    if not re.fullmatch(r"[A-Za-z0-9]+", space):
        space = "native"
    return f"{LESIONS_DIR}/{RATER_DIR_PREFIX}{rater_label(rater)}/{sid}_space-{space}_desc-lesion_mask.nii.gz"


def _sidecar_path(path):
    for extension in (".nii.gz", ".nii"):
        if path.lower().endswith(extension):
            return path[: -len(extension)] + ".json"
    return os.path.splitext(path)[0] + ".json"


def _relative_to(folder, path):
    try:
        return os.path.relpath(os.path.abspath(path), folder).replace(os.sep, "/")
    except ValueError:
        return None


def save_second_tracing(study, sid, array_kji, ijk_to_ras, reference_path, rater, now=None, extra=None, compute=True, final=True):
    """Write a second tracing and record it (the row in memory; the caller saves the study).

    ``final`` (Save second tracing): the pair is done (``rater2_status`` = done, ``rater2_date``, ``rater2_dice``). Work
    kept in passing (Skip, Exit, another subject) is written with ``final`` False: ``rater2_status`` = inprogress, so the
    subject stays in the Second tracing queue and counts in no agreement (no Dice, no date) until it is finished.

    :param array_kji, ijk_to_ras, reference_path: as ``nifti.write_like`` (the grid the second rater traced on: the scan,
      or the template of a figure tracing); the primary lesion is never read here.
    :param extra: more ``SlicerStudio`` sidecar values (for example {"figure_fits": [...]}).
    :param compute: compute ``rater2_dice`` right away (reads both files; a final tracing only).
    :return: {"path" (relative), "dice", "record"}.
    :raises AgreementError: see :func:`check_second_tracing`; an empty mask.
    """
    row = check_second_tracing(study, sid, rater, now)
    array = np.asarray(array_kji)
    if not np.any(array):
        raise AgreementError(f"The second tracing of {sid} is empty: nothing was saved")
    relative = second_relpath(study, sid, rater)
    target = study.abspath(relative)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    temporary = target[: -len(".nii.gz")] + f".{os.getpid()}.tmp.nii.gz"
    try:
        nifti.write_like(temporary, (array != 0).astype(np.uint8), ijk_to_ras, reference_path, dtype=np.uint8, descrip="Slicer Studio second tracing")
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(OSError):
            os.remove(temporary)
    when = _now()
    folder = os.path.dirname(target)
    mode = row.get("rater2_mode") if row.get("rater2_mode") in MODES else "inter"
    level = row.get("rater2_level") if row.get("rater2_level") in LEVELS else DEFAULT_LEVEL
    studio = {"participant_id": sid, "rater": rater, "rater_label": rater_label(rater), "role": "second tracing", "mode": mode, "level": level,
              "blind": True, "primary_loaded": False, "final": bool(final), "space": _space_of(study, row) or "native", "modified": when}
    studio.update(extra or {})
    sidecar = {"Type": "Lesion"}
    inside = study.relpath(reference_path) if reference_path else None
    if inside and not inside.startswith("../") and not os.path.isabs(inside):
        # Only a source inside the study is named (relative to the sidecar); a template elsewhere is a local path
        source = _relative_to(folder, reference_path)
        sidecar["Sources"] = [source]
        sidecar["Source"] = source
    sidecar[schema.FORK_KEY] = studio
    schema.write_text_atomic(_sidecar_path(target), json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n")
    study.set(sid, {"rater2_status": DONE if final else IN_PROGRESS, "rater2": rater, "rater2_lesion": relative, "rater2_date": when if final else None,
                    "rater2_mode": mode, "rater2_level": level, "rater2_dice": None})
    dice = None
    if compute and final:
        with contextlib.suppress(OSError, ValueError):
            metrics = study_pair(study, sid, grid2mm=None)
            dice = None if metrics is None else metrics["dice"]
        study.set(sid, rater2_dice=None if dice is None else round(dice, 4))
    record = _audit(study, {"event": TRACE_EVENT, "sid": sid, "who": rater, "when": when, "path": relative, "mode": mode, "level": level, "blind": True,
                            "primary_loaded": False, "final": bool(final), "dice": None if dice is None else round(dice, 4)})
    return {"path": relative, "dice": dice, "record": record}


#
# Adjudication
#


def consensus_mask(a, b, choice):
    """The consensus of two masks on one grid: "A", "B", "union" or "intersection" (bool array)."""
    a, b = _binary(a), _binary(b)
    if choice == "A":
        return a.copy()
    if choice == "B":
        return b.copy()
    if choice == "union":
        return a | b
    if choice == "intersection":
        return a & b
    raise ValueError(f"Unknown consensus {choice!r} (A, B, union, intersection; edit needs a mask)")


def _desc_path(path, desc):
    folder, name = os.path.split(path)
    if "desc-lesion_mask" in name:
        return os.path.join(folder, name.replace("desc-lesion_mask", f"desc-{desc}_mask"))
    for extension in (".nii.gz", ".nii"):
        if name.lower().endswith(extension):
            return os.path.join(folder, f"{name[:-len(extension)]}_desc-{desc}{name[-len(extension):]}")
    return os.path.join(folder, f"{name}_desc-{desc}")


def _copy(source, target):
    temporary = f"{target}.{os.getpid()}.tmp"
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(OSError):
            os.remove(temporary)


def _metrics_values(mask, affine):
    from scipy import ndimage

    mask = _binary(mask)
    center = centroid_mm(mask, affine)
    parts = int(ndimage.label(mask, structure=ndimage.generate_binary_structure(3, 3))[1]) if mask.any() else 0
    values = {"volume_ml": round(int(np.count_nonzero(mask)) * _voxel_ml(affine), 4), "n_parts": parts}
    if center is not None:
        values.update(centroid_x=round(float(center[0]), 2), centroid_y=round(float(center[1]), 2), centroid_z=round(float(center[2]), 2))
    return values


def adjudicate(study, sid, choice, who, array_kji=None, ijk_to_ras=None, when=None):
    """Write the consensus of a subject's two tracings as its ``desc-lesion`` (the row in memory; the caller saves).

    The first tracing is kept as ``desc-preadjudication`` (``rater1_lesion``), the second stays in its rater folder; the
    row gets ``rater`` = consensus, ``rater2_status`` = adjudicated and ``adjudication`` = the choice; the sidecar records
    the raters and the choice; one audit record holds the replaced values (:func:`undo_adjudication`). An approved review
    is reopened when the consensus changes the approved voxels (review.py: an approval is for the voxels reviewed).

    :param choice: "A" (first), "B" (second), "union", "intersection" or "edit" (``array_kji`` and ``ijk_to_ras``: the
      edited mask, as ``nifti.write_like``; it may cover part of the grid).
    :return: the audit record.
    """
    from . import review

    if choice not in CHOICES:
        raise AgreementError(f"Unknown adjudication {choice!r} (one of {', '.join(CHOICES)})")
    if not who or not str(who).strip():
        raise AgreementError("An adjudication needs the adjudicator's name")
    row = study.subject(sid)
    if status_of(row) == ADJUDICATED:
        raise AgreementError(f"{sid} already has a consensus (undo it first)")
    if status_of(row) != DONE:
        raise AgreementError(f"{sid} has no finished second tracing")
    paths = pair_paths(study, row)
    if paths is None:
        raise AgreementError(f"The two tracings of {sid} are not both on disk")
    primary, second = paths
    a, affine_a = load_mask(primary)
    b, affine_b = load_mask(second)
    grid_a = geometry.Grid(a.shape, affine_a)
    b_on_a = b if geometry.Grid(b.shape, affine_b).matches(grid_a) else _to_grid(b, affine_b, grid_a)
    dice = grid_metrics(a, b_on_a, affine_a)["dice"]
    kept = _desc_path(primary, PREADJUDICATION_DESC)
    # Not adjudicated (checked above): desc-lesion IS the first tracing now, so the kept copy is always refreshed (a copy
    # left by an earlier adjudication would be older than the tracer's later edits)
    _copy(primary, kept)
    sidecar = _sidecar_path(primary)
    if os.path.isfile(sidecar):
        _copy(sidecar, _sidecar_path(kept))
    else:
        with contextlib.suppress(OSError):
            os.remove(_sidecar_path(kept))
    extension = ".nii.gz" if primary.lower().endswith(".nii.gz") else ".nii"
    temporary = f"{primary[: -len(extension)]}.{os.getpid()}.tmp{extension}"
    if choice == "edit":
        if array_kji is None or ijk_to_ras is None:
            raise AgreementError("An edited consensus needs the edited mask")
        out_kji, out_matrix = (np.asarray(array_kji) != 0).astype(np.uint8), ijk_to_ras
    else:
        consensus = consensus_mask(a, b_on_a, choice)
        if not consensus.any():
            raise AgreementError(f"The {choice} of the two tracings of {sid} is empty")
        out_kji, out_matrix = np.ascontiguousarray(np.transpose(consensus.astype(np.uint8), (2, 1, 0))), affine_a
    if not np.any(out_kji):
        raise AgreementError(f"The consensus of {sid} is empty")
    try:
        nifti.write_like(temporary, out_kji, out_matrix, kept, dtype=np.uint8, descrip="Slicer Studio consensus lesion")
        written, _affine = load_mask(temporary)
        os.replace(temporary, primary)
    finally:
        with contextlib.suppress(OSError):
            os.remove(temporary)
    before = {key: row.get(key, _NA) for key in _CONSENSUS_COLUMNS}
    when = when or _now()
    first = primary_rater(row, study)
    raters = [_label_or_none(first) or "A", _label_or_none(row.get("rater2")) or "B"]
    values = _metrics_values(written, affine_a)
    stat = os.stat(primary)
    values.update({"rater": CONSENSUS_RATER, "rater2_status": ADJUDICATED, "rater1_lesion": study.relpath(kept), "adjudication": choice, "modified": when,
                   "lesion_mtime": round(stat.st_mtime, 3), "lesion_size": stat.st_size})
    study.set(sid, values)
    record = {"event": ADJUDICATE_EVENT, "sid": sid, "choice": choice, "who": str(who).strip(), "when": when, "raters": raters,
              "dice": None if dice is None else round(dice, 4), "kept": [study.relpath(kept), study.relpath(second)], "before": before}
    changed = not np.array_equal(_binary(written), _binary(a))
    if review.status_of(row) == "approved" and changed:
        record["review_before"] = {key: row.get(key, _NA) for key in _REVIEW_COLUMNS}
        review.apply_transition(study, sid, "pending", str(who).strip(), extra={"via": "adjudicate"})
        record["review_reopened"] = True
    with contextlib.suppress(OSError, ValueError):
        study.write_lesion_sidecar(sid, studio={"rater": CONSENSUS_RATER, "adjudication": {"choice": choice, "raters": raters, "who": str(who).strip(),
                                                                                             "when": when, "dice": record["dice"]}})
    return _audit(study, record)


def undo_adjudication(study, sid, who):
    """Put the first tracing back as ``desc-lesion`` and the row values the consensus replaced (audited). Return the record.

    The kept ``desc-preadjudication`` copy is removed once restored (the lesion is the first tracing again; a later
    adjudication keeps the tracing it replaces). An approval that the consensus reopened is restored when the review is
    still pending (the voxels are the approved ones again), with a review audit record (``via`` "adjudicate_undo").
    """
    from . import review

    row = study.subject(sid)
    if status_of(row) != ADJUDICATED:
        raise AgreementError(f"{sid} has no consensus to undo")
    records = review.read_audit(study, sid=sid, event=ADJUDICATE_EVENT)
    if not records:
        raise AgreementError(f"The adjudication of {sid} is not in the audit log")
    last = records[-1]
    kept = study.abspath(row.get("rater1_lesion"))
    primary = study.abspath(row.get("lesion"))
    if not kept or not os.path.isfile(kept) or not primary:
        raise AgreementError(f"The first tracing of {sid} was not kept")
    _copy(kept, primary)
    sidecar = _sidecar_path(kept)
    if os.path.isfile(sidecar):
        _copy(sidecar, _sidecar_path(primary))
    for path in (kept, sidecar):
        with contextlib.suppress(OSError):
            os.remove(path)
    before = dict(last.get("before") or {})
    before["rater2_status"] = DONE
    stat = os.stat(primary)
    before.update(lesion_mtime=round(stat.st_mtime, 3), lesion_size=stat.st_size)
    study.set(sid, {key: (None if _missing(value) else value) for key, value in before.items()})
    who = (who or "").strip() or None
    record = {"event": UNDO_EVENT, "sid": sid, "who": who, "when": _now(), "choice": last.get("choice")}
    review_before = last.get("review_before") if last.get("review_reopened") else None
    if review_before and review.status_of(study.subject(sid)) == "pending":
        study.set(sid, {key: (None if _missing(value) else value) for key, value in review_before.items() if key in _REVIEW_COLUMNS})
        review.append_audit(study, {"event": review.AUDIT_EVENT, "sid": sid, "from": "pending", "to": review.status_of(study.subject(sid)), "who": who,
                                    "when": record["when"], "via": UNDO_EVENT, "restored": True})
        record["review_restored"] = True
    return _audit(study, record)


def history(study, sid=None):
    """The double-tracing records of the audit log (assignments, second tracings, adjudications), oldest first."""
    from . import review

    events = (ASSIGN_EVENT, TRACE_EVENT, ADJUDICATE_EVENT, UNDO_EVENT)
    return [record for record in review.read_audit(study) if record.get("event") in events and (sid is None or record.get("sid") == sid or sid in (record.get("sids") or ()))]

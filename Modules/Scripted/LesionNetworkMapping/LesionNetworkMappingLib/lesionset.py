"""Lesion-set diagnostics: the properties of a lesion set that drive network-mapping results.

Shown before any statistics are run (van den Heuvel 2026: set size, heterogeneity and pairwise
overlap drive convergence on the degree map; Boes 2021; Cotovio 2022: 2D and 3D tracings):

- the exact overlap map behind the canonical figure: integer counts (int16) and percentages, so a
  "count >= 3" threshold selects exactly the voxels covered by 3 or more lesions (viewers that
  interpolate or bin a float map, e.g. MRIcroGL's threshold, can be off by one count);
- pairwise Dice of the binarized lesions (bounding-box prefilter, condensed array, histogram);
- lesion volumes (median, IQR, skew; log(volume) is suggested when skew > 2);
- tissue fractions from gray matter, white matter and CSF priors of the same template family
  (a prior of another family is refused with a reason; :func:`warp_priors` warps priors once with
  ``transforms.warp_image`` and records it);
- hemisphere (the intake side check's rule, ``lesionops.side_check``), dimensionality (the
  dimensionality column, else ``lesionops.detect_dimensionality`` of the mask: 2D, multi2D or 3D; a
  lesion one voxel thick, such as a small lacune at 2 mm, is 2D) and source-type tallies;
- case/control balance (standardized mean differences, |SMD| > 0.25 flagged) and a seeded
  nearest-neighbour matched subsample for a named study subset (include flags are never edited);
- neutral literature context strings with citations.

Lesions are loaded with :func:`load_occupancies` onto one grid (the 2 mm template grid of the
study's family, a connectome bundle's grid or any :class:`.geometry.Grid`) as sparse fractional
occupancies (``geometry.resample_to_grid`` "fraction" mode, through the affines). Rows come from
``Study.analysis_rows`` (listwise exclusion; the reasons are kept); lesions of another template
family are excluded, never warped silently. This module imports numpy and scipy only (no Slicer,
VTK or Qt).

Outputs (single-writer rule: per-subject values go through ``Study.post_results``, never into
participants.tsv)::

    <study>/derivatives/lesions/group_space-<sp>_desc-overlapCount_map.nii.gz   int16 counts (+ .json)
    <study>/derivatives/lesions/group_space-<sp>_desc-overlapPct_map.nii.gz     float32 percent (+ .json)
    <study>/derivatives/lnm/lesionset.json     {"format": "slicer-lnm-lesionset", "version": 1, ...}
    inbox (source "lesionset"): gm_frac, wm_frac, csf_frac, hemisphere, and dimensionality for rows
                                that have none

Command line (JSON events, see :mod:`.jobs`; exit codes 0 done, 1 error, 2 usage, 3 cancelled)::

    python -m LesionNetworkMappingLib.lesionset --study DIR [--grid template2mm|BUNDLE] [--space SPACE]
        [--priors DIR] [--out DIR] [--groups case,control] [--binarize-at 0.5] [--probabilistic] [--no-post]
        [--match-subset NAME --match-covariates volume_ml,wm_frac [--match-seed S]]
        [--template-dir DIR] [--threads K] [--cancel-file F]

Without ``--out`` the overlap maps go to ``<study>/derivatives/lesions`` and lesionset.json to
``<study>/derivatives/lnm``; with ``--out`` all files go to that folder. A matched subsample is
recorded in lesionset.json (``matched``); the session stores it as a study subset with
:func:`add_subset` (jobs do not write study.json).
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import datetime
import math
import re
import time
from typing import NamedTuple

import numpy as np

from . import jobs, lesionops
from .geometry import GRID_BY_ID, Grid, GridSpec, detect_grid_candidates, resample_to_grid, space_family
from .nifti import read_nifti, read_nifti_header, write_like, write_nifti
from .study import Study, is_missing

FORMAT = "slicer-lnm-lesionset"
VERSION = 1
JOB = "lesionset"
LESIONSET_JSON = "lesionset.json"
INBOX_SOURCE = "lesionset"
BINARIZE_AT = 0.5
SMD_FLAG = 0.25  # |SMD| above this is flagged (Austin 2009 uses 0.1-0.25)
SKEW_LOG = 2.0  # log(volume) is suggested above this skew (addendum §3.4)
DIMENSIONALITY_SHARE_POINTS = 20.0  # dimensionality is suggested as a covariate above this 2D share difference
HEMISPHERE_DOMINANCE = lesionops.SIDE_MAJORITY  # share of the lesion on one side for "L" or "R" (the intake side check's rule); else "bilateral"
DICE_EDGES = tuple(round(0.05 * k, 2) for k in range(21))
TISSUES = ("gm", "wm", "csf")
TISSUE_LABELS = {"gm": "GM", "wm": "WM", "csf": "CSF"}
TEMPLATE_GRID = "template2mm"
# 2 mm grid and template of each template family (the header of the overlap maps)
TEMPLATE_2MM = {"2009": ("TF2009-2mm", "MNI152NLin2009cAsym"), "fsl6": ("NLin6-2mm", "MNI152NLin6Asym")}
DEFAULT_SPACE = "MNI152NLin2009cAsym"
POSTED_COLUMNS = ("gm_frac", "wm_frac", "csf_frac", "hemisphere", "dimensionality")
BALANCE_COVARIATES = ("volume_ml", "wm_frac", "dim_2d", "hemisphere_left")
OVERLAP_NAME = "group_space-{space}_desc-overlap{kind}_map.nii.gz"
EXCLUDED_MISSING_FILE = "lesion file not found"
EXCLUDED_UNREADABLE = "lesion file unreadable"
EXCLUDED_EMPTY = "empty lesion"

CITATION_VDH = "van den Heuvel 2026"
CITATION_COTOVIO = "Cotovio 2022"
CITATION_CUI = "Cui 2026"


class PriorFamilyError(ValueError):
    """Tissue priors of another template family than the lesions (``reason`` says which)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class GridChoice(NamedTuple):
    """The grid lesions are loaded onto: :class:`.geometry.Grid`, its space label, a NIfTI file whose
    header the overlap maps copy (or None), and a short label ("template2mm", a bundle name, ...).
    """

    grid: object
    space: str
    reference: object
    label: str


# Grid


def _template_reference(template_id, dirs=()):
    """The 2 mm brain mask of a template (bundled for 2009c; NLin6 when downloaded), or None."""
    from . import normalize

    try:
        found = normalize.find_template(template_id, dirs=dirs)
    except jobs.JobError:
        return None
    return found["files"].get("mask")


def resolve_grid(study=None, grid=None, space=None, template_dirs=()):
    """The grid diagnostics run on.

    :param grid: None or "template2mm" (the 2 mm grid of the study space's family: TF2009-2mm for
      2009c/2009a, NLin6-2mm for NLin6), a registry grid id ("TF2009-2mm"), a :class:`.geometry.GridSpec`,
      a :class:`.geometry.Grid`, a connectome bundle (object or folder path) or a NIfTI path.
    :param space: space label of a :class:`.geometry.Grid` or NIfTI grid (default: detected).
    :return: :class:`GridChoice`.
    :raises ValueError: for a family without a 2 mm template grid or an unrecognized grid without ``space``.
    """
    study_space = space or (study.space if study is not None else None) or DEFAULT_SPACE
    if grid is None or grid == TEMPLATE_GRID:
        family = space_family(study_space)
        if family not in TEMPLATE_2MM:
            raise ValueError(f"No 2 mm template grid for space {study_space!r} (family {family}); give a connectome bundle or a grid")
        grid_id, template_id = TEMPLATE_2MM[family]
        spec = GRID_BY_ID[grid_id]
        reference = _template_reference(template_id, template_dirs)
        if reference is not None:
            header = read_nifti_header(reference)
            match = detect_grid_candidates(header["shape"][:3], header["affine"])
            if match is not None and match.grid_id == grid_id:
                return GridChoice(Grid(header["shape"][:3], header["affine"]), study_space, reference, TEMPLATE_GRID)
        return GridChoice(spec.grid, study_space, None, TEMPLATE_GRID)
    if isinstance(grid, str) and grid in GRID_BY_ID:
        grid = GRID_BY_ID[grid]
    if isinstance(grid, GridSpec):
        return GridChoice(grid.grid, space or (study_space if space_family(study_space) == grid.family else grid.space), None, grid.grid_id)
    if isinstance(grid, (str, os.PathLike)) and os.path.isdir(grid):
        from .connectome import ConnectomeBundle

        grid = ConnectomeBundle.open(grid)
    if hasattr(grid, "mask_indices") and hasattr(grid, "grid"):  # a connectome bundle
        bundle = grid
        label = getattr(bundle, "name", None) or os.path.basename(os.fspath(bundle.path))
        reference = os.path.join(os.fspath(bundle.path), "mask.nii.gz")
        bundle_space = space or (bundle.metadata.get("space") if isinstance(getattr(bundle, "metadata", None), dict) else None)
        return GridChoice(bundle.grid, _grid_space(bundle.grid, bundle_space), reference if os.path.isfile(reference) else None, str(label))
    if isinstance(grid, (str, os.PathLike)):
        header = read_nifti_header(grid)
        nifti_grid = Grid(header["shape"][:3], header["affine"])
        return GridChoice(nifti_grid, _grid_space(nifti_grid, space), os.fspath(grid), os.path.basename(os.fspath(grid)))
    if isinstance(grid, Grid):
        return GridChoice(grid, _grid_space(grid, space), None, "grid")
    raise TypeError(f"Unsupported grid {grid!r}")


def _grid_space(grid, space):
    if space:
        return str(space)
    match = detect_grid_candidates(grid.shape, grid.affine)
    if match is None:
        raise ValueError(f"{grid} is not a standard template grid; give its space")
    return match.space


def _space_label(space):
    """A BIDS entity value: letters and digits only."""
    return re.sub(r"[^A-Za-z0-9]", "", str(space)) or "unknown"


# Loading


class LesionSet:
    """Sparse fractional occupancies of lesions on one grid.

    :ivar sids: participant ids (table order).
    :ivar groups: group of each lesion ("case", "control" or "n/a").
    :ivar matrix: scipy.sparse CSR matrix (n lesions x grid voxels) of float32 occupancies (0..1]
      in flat C order of the grid's [i, j, k] array.
    :ivar volumes_ml: lesion volumes in mL from the source files (sum of the clipped values x voxel volume).
    :ivar rows: {sid: copy of the participants.tsv row} ({} for lesions given as arrays).
    :ivar dimensionality: per lesion: (value, "column" or "mask"); from the mask, "2D", "multi2D" or
      "3D" of the source voxels (``lesionops.detect_dimensionality``: anatomical planes through the affine).
    :ivar excluded: {reason: [sids]} of the rows not loaded.
    """

    def __init__(self, sids, groups, matrix, grid, space, volumes_ml, rows=None, dimensionality=None, excluded=None, reference=None, label=None):
        self.sids = list(sids)
        self.groups = list(groups)
        self.matrix = matrix.tocsr()
        self.grid = grid
        self.space = space
        self.volumes_ml = np.asarray(volumes_ml, dtype=np.float64)
        self.rows = dict(rows or {})
        self.dimensionality = list(dimensionality) if dimensionality is not None else [(None, None)] * len(self.sids)
        self.excluded = dict(excluded or {})
        self.reference = reference
        self.label = label
        self.errors = {}  # {sid: message} of the unreadable lesion files
        if self.matrix.shape != (len(self.sids), grid.n_voxels) or len(self.groups) != len(self.sids) or len(self.volumes_ml) != len(self.sids):
            raise ValueError("Inconsistent lesion set sizes")

    def __len__(self):
        return len(self.sids)

    def __repr__(self):
        return f"LesionSet(n={len(self)}, space={self.space!r}, grid={self.grid})"

    @classmethod
    def from_volumes(cls, volumes, grid, space, sids=None, groups=None, volumes_ml=None, rows=None, dimensionality=None):
        """A lesion set from occupancy arrays already on ``grid`` (each indexed [i, j, k], values clipped to 0..1)."""
        from scipy import sparse

        volumes = [np.clip(np.nan_to_num(np.asarray(volume, dtype=np.float64)), 0.0, 1.0).reshape(-1) for volume in volumes]
        n = len(volumes)
        sids = list(sids) if sids is not None else [f"sub-{k + 1:03d}" for k in range(n)]
        groups = list(groups) if groups is not None else ["n/a"] * n
        indices = [np.flatnonzero(volume) for volume in volumes]
        values = [volume[index].astype(np.float32) for volume, index in zip(volumes, indices, strict=True)]
        matrix = _csr(indices, values, grid.n_voxels, sparse)
        if volumes_ml is None:
            volumes_ml = [float(np.sum(value, dtype=np.float64)) * grid.voxel_volume / 1000.0 for value in values]
        if dimensionality is None:
            dimensionality = [(_mask_dimensionality(volume.reshape(grid.shape), grid.affine), "mask") if index.size else (None, None) for volume, index in zip(volumes, indices, strict=True)]
        return cls(sids, groups, matrix, grid, space, volumes_ml, rows=rows, dimensionality=dimensionality)

    def binary(self, binarize_at=BINARIZE_AT):
        """CSR matrix (n x V) of the voxels with occupancy >= ``binarize_at`` (dtype int32 ones)."""
        matrix = self.matrix
        keep = matrix.data >= np.float32(binarize_at)
        return _csr_mask(matrix, keep)

    def subset(self, keep):
        """A new set with the lesions selected by a boolean mask, indices or participant ids."""
        keep = _selection(keep, self.sids)
        return LesionSet(
            [self.sids[k] for k in keep],
            [self.groups[k] for k in keep],
            self.matrix[keep],
            self.grid,
            self.space,
            self.volumes_ml[keep],
            rows={self.sids[k]: self.rows[self.sids[k]] for k in keep if self.sids[k] in self.rows},
            dimensionality=[self.dimensionality[k] for k in keep],
            excluded=self.excluded,
            reference=self.reference,
            label=self.label,
        )

    def group(self, name):
        """The lesions of one group ("case", "control", ...)."""
        return self.subset([k for k, group in enumerate(self.groups) if group == name])

    def column(self, name):
        """Values of a participants.tsv column for each lesion (None when missing)."""
        out = []
        for sid in self.sids:
            value = self.rows.get(sid, {}).get(name)
            out.append(None if is_missing(value) else value)
        return out


def _selection(keep, sids):
    if isinstance(keep, np.ndarray) and keep.dtype == np.bool_:
        return [int(k) for k in np.flatnonzero(keep)]
    keep = list(keep)
    if keep and all(isinstance(item, str) for item in keep):
        index = {sid: k for k, sid in enumerate(sids)}
        return [index[sid] for sid in keep]
    return [int(k) for k in keep]


def _csr(indices, values, n_voxels, sparse=None):
    if sparse is None:
        from scipy import sparse
    lengths = np.array([len(index) for index in indices], dtype=np.int64)
    indptr = np.concatenate(([0], np.cumsum(lengths)))
    flat_indices = np.concatenate(indices).astype(np.int64) if len(indices) else np.zeros(0, dtype=np.int64)
    flat_values = np.concatenate(values).astype(np.float32) if len(values) else np.zeros(0, dtype=np.float32)
    return sparse.csr_matrix((flat_values, flat_indices, indptr), shape=(len(indices), int(n_voxels)))


def _csr_mask(matrix, keep):
    """Ones at the stored entries of ``matrix`` selected by ``keep`` (a mask over ``matrix.data``)."""
    from scipy import sparse

    rows = np.repeat(np.arange(matrix.shape[0]), np.diff(matrix.indptr))[keep]
    counts = np.bincount(rows, minlength=matrix.shape[0])
    indptr = np.concatenate(([0], np.cumsum(counts)))
    return sparse.csr_matrix((np.ones(int(keep.sum()), dtype=np.int32), matrix.indices[keep], indptr), shape=matrix.shape)


def _nonzero_box(mask):
    """[(lo, hi)] per axis of the non-zero voxels of a boolean 3D array, or None when it is empty."""
    box = []
    for axis in range(3):
        used = np.flatnonzero(mask.any(axis=tuple(a for a in range(3) if a != axis)))
        if used.size == 0:
            return None
        box.append((int(used[0]), int(used[-1]) + 1))
    return box


def _mask_dimensionality(mask, affine):
    """"2D", "multi2D" or "3D" of the non-zero voxels of a 3D array (``lesionops.detect_dimensionality``
    on their bounding box), or None when it is empty.
    """
    mask = np.asarray(mask) > 0
    box = _nonzero_box(mask)
    if box is None:
        return None
    offset = np.array([a for a, _ in box], dtype=np.float64)
    affine = np.asarray(affine, dtype=np.float64).copy()
    affine[:3, 3] += affine[:3, :3] @ offset
    return lesionops.detect_dimensionality(mask[tuple(slice(a, b) for a, b in box)], affine)["dimensionality"]


def _row_dimensionality(row, crop, crop_affine):
    """(value, origin): the dimensionality column when set, else "2D"/"multi2D"/"3D" of the source
    mask (``crop``: its bounding box, ``crop_affine``: the box's affine).
    """
    value = row.get("dimensionality")
    if not is_missing(value):
        return value, "column"
    value = lesionops.detect_dimensionality(crop > 0, crop_affine)["dimensionality"]
    return (value, "mask") if value else (None, None)


def _lesion_family(row, header):
    """(family, space) of a lesion: its space column, else its grid (registry grids)."""
    space = row.get("space")
    family = space_family(space) if not is_missing(space) else "unknown"
    if family in ("native", "unknown"):
        match = detect_grid_candidates(header["shape"][:3], header["affine"])
        if match is not None:
            return match.family, match.space
    return family, space


def load_occupancies(study, grid=None, include_only=True, groups=None, probabilistic=False, supersample=2, require_review=None, space=None, template_dirs=(), cancel=None, progress=None):
    """Load the lesions of a study onto one grid as sparse fractional occupancies.

    :param study: :class:`.study.Study` (or its folder).
    :param grid: see :func:`resolve_grid` (default: the 2 mm template grid of the study's family).
    :param include_only: rows with include = 0 are excluded (``Study.analysis_rows``).
    :param groups: optional group values to keep ("case", "control"); other rows are skipped.
    :param probabilistic: keep fractional lesion values (else every non-zero voxel is lesion).
    :param require_review: passed to ``Study.analysis_rows`` (None: the study setting).
    :param cancel: optional callable; :class:`.jobs.Cancelled` is raised when it returns True.
    :param progress: optional callable(done, total).
    :return: :class:`LesionSet` (``excluded`` lists the rows not loaded, by reason).
    """
    from scipy import sparse

    if not isinstance(study, Study):
        study = Study.open(study)
    choice = grid if isinstance(grid, GridChoice) else resolve_grid(study, grid, space=space, template_dirs=template_dirs)
    target, family = choice.grid, space_family(choice.space)
    rows, excluded = study.analysis_rows(columns=("lesion",), include_only=include_only, require_review=require_review)
    excluded = {reason: list(ids) for reason, ids in excluded.items()}
    if groups is not None:
        groups = {groups} if isinstance(groups, str) else set(groups)
        rows = [row for row in rows if row.get("group") in groups]
    sids, group_values, indices, values, volumes, dims, kept_rows, errors = [], [], [], [], [], [], {}, {}
    for done, row in enumerate(rows):
        if cancel is not None and cancel():
            raise jobs.Cancelled(done=done)
        if progress is not None and done:
            progress(done, len(rows))  # rows done so far, loaded or excluded
        sid = row["participant_id"]
        path = study.abspath(row["lesion"])
        if not path or not os.path.isfile(path):
            excluded.setdefault(EXCLUDED_MISSING_FILE, []).append(sid)
            continue
        try:
            header = read_nifti_header(path)
            lesion_family, lesion_space = _lesion_family(row, header)
            if lesion_family != family:
                excluded.setdefault(f"space {lesion_space or 'unknown'} is not in the {family} family of {choice.space}", []).append(sid)
                continue
            data, affine, _ = read_nifti(path)
        except (OSError, ValueError, EOFError) as exc:
            excluded.setdefault(EXCLUDED_UNREADABLE, []).append(sid)
            errors[sid] = f"{type(exc).__name__}: {exc}"
            continue
        data = np.asarray(data)
        if data.ndim > 3:
            if any(n != 1 for n in data.shape[3:]):
                excluded.setdefault(EXCLUDED_UNREADABLE, []).append(sid)
                errors[sid] = f"not a 3D image (shape {data.shape})"
                continue
            data = data.reshape(data.shape[:3])
        # Work on the bounding box of the lesion voxels: whole-image passes are boolean only
        if np.issubdtype(data.dtype, np.integer) or data.dtype == np.bool_:
            nonzero = data != 0 if not probabilistic else data > 0
        else:
            nonzero = (data > 0) if probabilistic else (data != 0) & ~np.isnan(data)
        box = _nonzero_box(nonzero)
        if box is None:
            excluded.setdefault(EXCLUDED_EMPTY, []).append(sid)
            continue
        crop_slices = tuple(slice(a, b) for a, b in box)
        crop = data[crop_slices]
        if probabilistic:
            crop = np.clip(np.nan_to_num(crop.astype(np.float64), posinf=1.0, neginf=0.0), 0.0, 1.0)
        else:
            crop = nonzero[crop_slices].astype(np.float64)
        offset = np.array([a for a, _ in box], dtype=np.float64)
        crop_affine = np.asarray(affine, dtype=np.float64).copy()
        crop_affine[:3, 3] += crop_affine[:3, :3] @ offset
        voxel_ml = abs(float(np.linalg.det(crop_affine[:3, :3]))) / 1000.0
        volume_ml = float(crop.sum()) * voxel_ml
        if data.shape == target.shape and np.allclose(affine, target.affine, atol=1e-4, rtol=0):
            local = np.nonzero(crop)
            index = np.ravel_multi_index(tuple(axis + a for axis, (a, _) in zip(local, box, strict=True)), target.shape)
            order = np.argsort(index, kind="stable")
            index = index[order]
            value = crop[local][order].astype(np.float32)
        else:
            occupancy = resample_to_grid(crop, crop_affine, target, mode="fraction", supersample=supersample).reshape(-1)
            index = np.flatnonzero(occupancy > 0)
            value = np.minimum(occupancy[index], 1.0).astype(np.float32)
        if index.size == 0:
            excluded.setdefault(f"outside the {choice.label} grid", []).append(sid)
            continue
        sids.append(sid)
        group_values.append(row.get("group") or "n/a")
        indices.append(index)
        values.append(value)
        volumes.append(volume_ml)
        dims.append(_row_dimensionality(row, crop, crop_affine))
        kept_rows[sid] = row
    if progress is not None and rows:
        progress(len(rows), len(rows))
    matrix = _csr(indices, values, target.n_voxels, sparse)
    occ = LesionSet(sids, group_values, matrix, target, choice.space, volumes, rows=kept_rows, dimensionality=dims, excluded=excluded, reference=choice.reference, label=choice.label)
    occ.errors = errors
    return occ


# Overlap


class OverlapMaps(NamedTuple):
    """Exact overlap maps: ``count`` int16 [i, j, k] (lesions covering each voxel), ``percent``
    float32 (count / n x 100), ``n`` lesions, ``max_count`` and ``legend`` ("max 6/23 (26%)").
    """

    count: object
    percent: object
    n: int
    max_count: int
    legend: str
    binarize_at: float


def overlap_legend(max_count, n):
    """Legend of an overlap map: "max 6/23 (26%)"."""
    if not n:
        return "no lesions"
    return f"max {int(max_count)}/{int(n)} ({round(100.0 * max_count / n):.0f}%)"


def overlap_maps(occ, grid=None, binarize_at=BINARIZE_AT):
    """Count and percent overlap maps with exact integer bins.

    A voxel counts for a lesion when its occupancy is >= ``binarize_at``. The count map is int16
    (int32 above 32767 lesions), so thresholds are exact: ``count >= 3`` is the set of voxels covered
    by at least 3 lesions.

    :param occ: :class:`LesionSet`.
    :param grid: the grid (default: ``occ.grid``).
    :return: :class:`OverlapMaps`.
    """
    grid = grid or occ.grid
    binary = occ.binary(binarize_at)
    counts = np.bincount(binary.indices, minlength=grid.n_voxels)
    n = len(occ)
    dtype = np.int16 if n <= np.iinfo(np.int16).max else np.int32
    count = counts.astype(dtype).reshape(grid.shape)
    percent = (counts.astype(np.float64) * (100.0 / n) if n else np.zeros(grid.n_voxels)).astype(np.float32).reshape(grid.shape)
    max_count = int(counts.max()) if counts.size else 0
    return OverlapMaps(count, percent, n, max_count, overlap_legend(max_count, n), float(binarize_at))


def overlap_at_least(count, k):
    """Boolean map of the voxels covered by at least ``k`` lesions (exact on the integer count map)."""
    return np.asarray(count) >= int(k)


def overlap_paths(folder, space):
    """(count path, percent path) of the overlap maps in ``folder``."""
    label = _space_label(space)
    return tuple(os.path.join(os.fspath(folder), OVERLAP_NAME.format(space=label, kind=kind)) for kind in ("Count", "Pct"))


def write_overlap(study, maps, occ=None, out_dir=None, grid=None, space=None, reference=None):
    """Write the overlap maps (with JSON sidecars) in the template header.

    :param study: :class:`.study.Study` or folder (the default folder is ``<study>/derivatives/lesions``).
    :param maps: :class:`OverlapMaps`.
    :param occ: the :class:`LesionSet` (grid, space and header reference; or give ``grid``, ``space``, ``reference``).
    :param reference: NIfTI file whose exact geometry header the maps copy (sform, qform, byte order);
      without one the maps are written with the grid affine (sform/qform code MNI).
    :return: (count path, percent path).
    """
    grid = grid or occ.grid
    space = space or occ.space
    reference = reference if reference is not None else (occ.reference if occ is not None else None)
    if out_dir is None:
        root = study.path if isinstance(study, Study) else os.fspath(study)
        out_dir = os.path.join(root, "derivatives", "lesions")
    os.makedirs(out_dir, exist_ok=True)
    count_path, percent_path = overlap_paths(out_dir, space)
    for path, data, descrip in ((count_path, maps.count, f"lesion overlap count of {maps.n}"), (percent_path, maps.percent, f"lesion overlap percent of {maps.n}")):
        _write_map(path, data, grid, reference, descrip)
        sidecar = {
            "Description": f"Lesion overlap {'count' if path == count_path else 'percent'}: {maps.legend}",
            "Space": space,
            "Units": "lesions" if path == count_path else "%",
            "SlicerStudio": {"format": FORMAT, "n": maps.n, "max_count": maps.max_count, "legend": maps.legend, "binarize_at": maps.binarize_at, "created": _now()},
        }
        jobs.write_json_atomic(path[: -len(".nii.gz")] + ".json", sidecar)
    return count_path, percent_path


def _write_map(path, data, grid, reference, descrip):
    array = np.asarray(data)
    if reference is not None:
        try:
            write_like(path, np.transpose(array, (2, 1, 0)), grid.affine, reference, dtype=array.dtype, descrip=descrip)
            return
        except ValueError:
            pass  # the reference is not on this grid: write with the grid affine
    write_nifti(path, array, grid.affine, descrip=descrip)


# Pairwise Dice


class DiceResult(NamedTuple):
    """Pairwise Dice: ``condensed`` (pairs i < j in row-major order, like scipy's pdist), ``histogram``
    counts over ``edges``, ``max_overlap_count`` (most lesions covering one voxel), the number of
    pairs whose bounding boxes intersect (``n_candidates``) and summary statistics.
    """

    condensed: object
    histogram: object
    edges: tuple
    max_overlap_count: int
    n_candidates: int
    mean: object
    median: object
    maximum: object


def _boxes(binary, shape):
    """(lo, hi) voxel bounding boxes (n x 3) of the rows of a CSR matrix; empty rows get lo > hi."""
    n = binary.shape[0]
    lo = np.full((n, 3), np.iinfo(np.int64).max, dtype=np.int64)
    hi = np.full((n, 3), -1, dtype=np.int64)
    sizes = np.diff(binary.indptr)
    nonempty = np.flatnonzero(sizes)
    if nonempty.size:
        ijk = np.vstack(np.unravel_index(binary.indices, shape)).T
        starts = binary.indptr[nonempty]
        lo[nonempty] = np.minimum.reduceat(ijk, starts, axis=0)
        hi[nonempty] = np.maximum.reduceat(ijk, starts, axis=0)
    return lo, hi


def pairwise_dice(occ, binarize_at=BINARIZE_AT, edges=DICE_EDGES):
    """Dice coefficient of every pair of binarized lesions.

    Pairs whose bounding boxes do not intersect have Dice 0 and are not computed; the intersections
    of the other pairs come from one sparse product over the lesions in candidate pairs.

    :return: :class:`DiceResult`.
    """
    binary = occ.binary(binarize_at)
    n = binary.shape[0]
    sizes = np.diff(binary.indptr).astype(np.float64)
    condensed = np.zeros(n * (n - 1) // 2, dtype=np.float64)
    lo, hi = _boxes(binary, occ.grid.shape)
    first, second = np.triu_indices(n, 1)  # condensed (row-major) order
    candidate = np.all((lo[first] <= hi[second]) & (lo[second] <= hi[first]), axis=1) if n > 1 else np.zeros(0, dtype=bool)
    n_candidates = int(candidate.sum())
    if n_candidates:
        # One sparse product over the lesions that take part in a candidate pair
        involved = np.unique(np.concatenate((first[candidate], second[candidate])))
        position = np.full(n, -1, dtype=np.int64)
        position[involved] = np.arange(involved.size)
        block = binary[involved]
        products = (block @ block.T).tocsr()
        a, b = position[first[candidate]], position[second[candidate]]
        intersection = np.asarray(products[a, b], dtype=np.float64).reshape(-1)
        condensed[candidate] = 2.0 * intersection / (sizes[first[candidate]] + sizes[second[candidate]])
    histogram = np.histogram(condensed, bins=np.asarray(edges, dtype=np.float64))[0] if condensed.size else np.zeros(len(edges) - 1, dtype=np.int64)
    max_overlap = int(np.bincount(binary.indices).max()) if binary.indices.size else 0
    stats = (float(condensed.mean()), float(np.median(condensed)), float(condensed.max())) if condensed.size else (None, None, None)
    return DiceResult(condensed, histogram, tuple(edges), max_overlap, int(n_candidates), *stats)


def dice_matrix(condensed, n):
    """Square symmetric matrix (diagonal 1) of a condensed Dice array."""
    out = np.eye(n)
    iu = np.triu_indices(n, 1)
    out[iu] = condensed
    out[(iu[1], iu[0])] = condensed
    return out


# Volumes


def volume_summary(volumes):
    """Median, quartiles, IQR, mean, range and skew (adjusted Fisher-Pearson G1) of lesion volumes;
    ``suggest_log`` is True when skew > 2 (log(volume) as the covariate).
    """
    values = np.asarray([v for v in volumes if v is not None and math.isfinite(v)], dtype=np.float64)
    n = int(values.size)
    if n == 0:
        return {"n": 0, "median": None, "q1": None, "q3": None, "iqr": None, "mean": None, "min": None, "max": None, "skew": None, "suggest_log": False}
    q1, median, q3 = (float(q) for q in np.percentile(values, (25, 50, 75)))
    skew = None
    if n >= 3 and float(values.std()) > 0:
        from scipy import stats

        skew = float(stats.skew(values, bias=False))
    return {
        "n": n,
        "median": median,
        "q1": q1,
        "q3": q3,
        "iqr": q3 - q1,
        "mean": float(values.mean()),
        "min": float(values.min()),
        "max": float(values.max()),
        "skew": skew,
        "suggest_log": bool(skew is not None and skew > SKEW_LOG),
    }


# Tissue priors


_PRIOR_NAME = re.compile(r"^tpl-(?P<space>[A-Za-z0-9]+)(?:_res-(?P<res>[0-9]+))?(?:_[a-z]+-[A-Za-z0-9]+)*?_label-(?P<label>GM|WM|CSF)(?:_[a-z]+-[A-Za-z0-9]+)*_probseg\.nii(?:\.gz)?$")


def find_priors(folder, grid=None, space=None):
    """Tissue priors in a folder (TemplateFlow names ``tpl-<space>[_res-<r>]..._label-{GM,WM,CSF}_probseg.nii.gz``).

    Among the complete sets, one of the template family of ``space`` is preferred, then the
    resolution with the voxel size of ``grid``, then the finest.

    :param grid: optional grid (the lesions' grid).
    :param space: optional space label of the lesions.
    :return: {"gm", "wm", "csf": path, "space": space} or None when a tissue is missing.
    """
    found = {}
    for name in sorted(os.listdir(folder)):
        match = _PRIOR_NAME.match(name)
        if match:
            res = int(match["res"]) if match["res"] else 0
            found.setdefault((match["space"], res), {})[match["label"].lower()] = os.path.join(folder, name)
    complete = [(key, files) for key, files in found.items() if all(tissue in files for tissue in TISSUES)]
    if not complete:
        return None
    spacing = float(np.mean(grid.spacing)) if grid is not None else None
    family = space_family(space) if space else None

    def rank(item):
        (prior_space, res), _ = item
        other_family = 0 if family is None or space_family(prior_space) == family else 1
        return (other_family, 0 if spacing is not None and res and abs(res - spacing) < 1e-6 else 1, res)

    (space, _), files = min(complete, key=rank)
    return {**files, "space": space}


def prior_volumes(priors, grid):
    """Prior probabilities on ``grid``: {tissue: float64 flat array (C order)} clipped to 0..1.

    Arrays given in ``priors`` must already be on the grid; files on another grid of the same
    family are resampled ("fraction" mode: the mean probability over each grid voxel).
    """
    out = {}
    for tissue in TISSUES:
        source = priors[tissue]
        if isinstance(source, (str, os.PathLike)):
            data, affine, _ = read_nifti(source)
            data = np.asarray(data, dtype=np.float64).reshape(data.shape[:3])
            if not (data.shape == grid.shape and np.allclose(affine, grid.affine, atol=1e-4, rtol=0)):
                data = resample_to_grid(data, affine, grid, mode="fraction")
        else:
            data = np.asarray(source, dtype=np.float64)
            if data.size != grid.n_voxels:
                raise ValueError(f"The {tissue} prior array has {data.size} voxels; the grid has {grid.n_voxels}")
        out[tissue] = np.clip(np.nan_to_num(data.reshape(-1)), 0.0, 1.0)
    return out


def check_prior_family(priors, space):
    """Raise :class:`PriorFamilyError` unless the priors' space (``priors["space"]`` or ``priors["family"]``)
    is in the template family of ``space``.
    """
    prior_family = priors.get("family") or space_family(priors.get("space"))
    family = space_family(space)
    if prior_family in ("native", "unknown"):
        raise PriorFamilyError(f"The tissue priors have no template space (got {priors.get('space')!r}); priors of the {family} family are needed")
    if prior_family != family:
        raise PriorFamilyError(
            f"The tissue priors are in the {prior_family} family ({priors.get('space')}) and the lesions in the {family} family ({space}); "
            f"use priors of the {family} family, or warp them once with warp_priors (a template-family transform) and record it",
        )


class TissueResult(NamedTuple):
    """Per-lesion tissue fractions {tissue: array} (share of the lesion volume, 0..1; ``outside``
    is the rest), percent means per group and the priors' provenance.
    """

    fractions: dict
    outside: object
    priors: dict


def tissue_fractions(occ, priors):
    """Share of each lesion's volume in gray matter, white matter and CSF.

    fraction_t = sum(occupancy x p_t) / sum(occupancy), with p_t the prior probability on the grid.

    :param priors: {"gm", "wm", "csf": path or array on ``occ.grid``, "space" (or "family"): label,
      optional "warped": provenance}.
    :raises PriorFamilyError: when the priors' family differs from the lesions' (reason in ``.reason``).
    :return: :class:`TissueResult`.
    """
    check_prior_family(priors, occ.space)
    volumes = prior_volumes(priors, occ.grid)
    totals = np.asarray(occ.matrix.sum(axis=1), dtype=np.float64).reshape(-1)
    safe = np.where(totals > 0, totals, 1.0)
    fractions = {tissue: np.where(totals > 0, (occ.matrix @ volumes[tissue]) / safe, np.nan) for tissue in TISSUES}
    outside = 1.0 - sum(fractions.values())
    provenance = {"space": priors.get("space"), "family": priors.get("family") or space_family(priors.get("space"))}
    for tissue in TISSUES:
        if isinstance(priors[tissue], (str, os.PathLike)):
            provenance[tissue] = os.path.basename(os.fspath(priors[tissue]))
    if priors.get("warped"):
        provenance["warped"] = priors["warped"]
    return TissueResult(fractions, outside, provenance)


def warp_priors(priors, xfm_h5, reference_path, out_dir, target_space=None, threads=None):
    """Warp tissue priors once to another template family with ``transforms.warp_image`` (linear).

    :param priors: {"gm", "wm", "csf": path, "space": source space}.
    :param xfm_h5: TemplateFlow transform ``tpl-<target>_from-<source>_mode-image_xfm.h5``.
    :param reference_path: NIfTI file of the target grid.
    :return: priors {"gm", "wm", "csf": path, "space": target space, "warped": [provenance of each warp]}.
    """
    from . import transforms

    os.makedirs(out_dir, exist_ok=True)
    direction = transforms.transform_direction(xfm_h5, "image", priors.get("space"), target_space)
    target = target_space or direction.image_to
    out = {"space": target, "warped": []}
    for tissue in TISSUES:
        name = f"tpl-{_space_label(target)}_label-{TISSUE_LABELS[tissue]}_desc-from{_space_label(direction.image_from)}_probseg.nii.gz"
        path = os.path.join(out_dir, name)
        provenance = transforms.warp_image(priors[tissue], xfm_h5, reference_path, path, interpolation="linear", source_space=priors.get("space"), target_space=target_space, threads=threads)
        out[tissue] = path
        out["warped"].append({"tissue": tissue, "source": os.path.basename(os.fspath(priors[tissue])), **provenance})
    return out


# Tallies and balance


def hemisphere_split(occ, dominance=HEMISPHERE_DOMINANCE):
    """Hemisphere of each lesion from its occupancy-weighted world x (RAS: x < 0 is left).

    The rule of ``lesionops.side_check`` (the traced side shown at intake): each voxel is split
    between the hemispheres by the part of its x extent on each side of x = 0, and a lesion is "L" or
    "R" when that side holds at least ``dominance`` of it. Otherwise it is "bilateral" (the
    hemisphere column has no "midline" level; side_check's "midline" lesions are "bilateral" here).

    :return: (labels: "L", "R", "bilateral" or None for an empty lesion; counts {"L", "R",
      "bilateral"}; left share of each lesion's volume).
    """
    matrix = occ.matrix
    n = matrix.shape[0]
    rows = np.repeat(np.arange(n), np.diff(matrix.indptr))
    ijk = np.vstack(np.unravel_index(matrix.indices, occ.grid.shape)).astype(np.float64)
    x = occ.grid.affine[0, :3] @ ijk + occ.grid.affine[0, 3]
    extent = float(np.abs(occ.grid.affine[0, :3]).sum())  # x extent of a voxel
    side = np.clip((extent / 2.0 - x) / extent, 0.0, 1.0)
    weights = matrix.data.astype(np.float64)
    total = np.bincount(rows, weights=weights, minlength=n)
    left = np.bincount(rows, weights=weights * side, minlength=n)
    share = np.where(total > 0, left / np.where(total > 0, total, 1.0), np.nan)
    labels = []
    for value in share:
        if not math.isfinite(value):
            labels.append(None)
        elif value >= dominance:
            labels.append("L")
        elif value <= 1.0 - dominance:
            labels.append("R")
        else:
            labels.append("bilateral")
    counts = {key: labels.count(key) for key in ("L", "R", "bilateral")}
    return labels, counts, share


def _tally(values, unknown="unknown"):
    counts = {}
    for value in values:
        key = unknown if value is None or is_missing(value) else str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def dimensionality_tally(occ):
    """Counts of "2D", "3D", "multi2D" (and "unknown") lesions: the dimensionality column, else the mask."""
    return _tally(value for value, _ in occ.dimensionality)


def source_type_tally(occ):
    """Counts of the source_type column ("own_scan", "case_report", "dataset", "control_db", "unknown")."""
    return _tally(occ.column("source_type"))


def smd(cases, controls, binary=None):
    """Standardized mean difference (cases - controls) / sqrt((s_case^2 + s_control^2) / 2).

    Binary variables (``binary`` True, or values all 0/1 when None) use p (1 - p) as the variance.
    Missing (None/NaN) values are ignored. Returns None when a group is empty, 0.0 when both
    variances are 0 and the means are equal, and +-inf when only the means differ.
    """
    a = np.asarray([v for v in cases if v is not None and math.isfinite(float(v))], dtype=np.float64)
    b = np.asarray([v for v in controls if v is not None and math.isfinite(float(v))], dtype=np.float64)
    if a.size == 0 or b.size == 0:
        return None
    if binary is None:
        binary = bool(np.all(np.isin(a, (0.0, 1.0))) and np.all(np.isin(b, (0.0, 1.0))))
    mean_a, mean_b = float(a.mean()), float(b.mean())
    if binary:
        var_a, var_b = mean_a * (1.0 - mean_a), mean_b * (1.0 - mean_b)
    else:
        var_a = float(a.var(ddof=1)) if a.size > 1 else 0.0
        var_b = float(b.var(ddof=1)) if b.size > 1 else 0.0
    pooled = math.sqrt((var_a + var_b) / 2.0)
    if pooled == 0.0:
        return 0.0 if mean_a == mean_b else math.copysign(math.inf, mean_a - mean_b)
    return (mean_a - mean_b) / pooled


def lesion_covariates(occ, tissue=None, hemispheres=None):
    """Per-lesion balance covariates: {sid: {"volume_ml", "log_volume_ml", "wm_frac", "dim_2d", "hemisphere_left"}}.

    dim_2d is 1 for 2D and multi2D tracings; hemisphere_left is 1 for left lesions (None when unknown).
    """
    labels = hemispheres if hemispheres is not None else hemisphere_split(occ)[0]
    out = {}
    for k, sid in enumerate(occ.sids):
        dim = occ.dimensionality[k][0]
        volume = float(occ.volumes_ml[k])
        wm = None if tissue is None else float(tissue.fractions["wm"][k])
        out[sid] = {
            "volume_ml": volume,
            "log_volume_ml": math.log(volume) if volume > 0 else None,
            "wm_frac": wm if wm is None or math.isfinite(wm) else None,
            "dim_2d": None if dim is None else (1.0 if dim in ("2D", "multi2D") else 0.0),
            "hemisphere_left": None if labels[k] is None else (1.0 if labels[k] == "L" else 0.0),
        }
    return out


def covariates_from_summary(summary):
    """The :func:`lesion_covariates` of the lesions of a :func:`summarize` result (rounded values)."""
    out = {}
    for lesion in summary["lesions"]:
        volume, dim, side = lesion.get("volume_ml"), lesion.get("dimensionality"), lesion.get("hemisphere")
        out[lesion["participant_id"]] = {
            "volume_ml": volume,
            "log_volume_ml": math.log(volume) if volume else None,
            "wm_frac": lesion.get("wm_frac"),
            "dim_2d": None if dim is None else (1.0 if dim in ("2D", "multi2D") else 0.0),
            "hemisphere_left": None if side is None else (1.0 if side == "L" else 0.0),
        }
    return out


def balance(covariates, case_sids, control_sids, names=BALANCE_COVARIATES, flag=SMD_FLAG):
    """Case/control balance: {name: {"smd", "flagged", "case_mean", "control_mean", "binary"}}."""
    out = {}
    for name in names:
        a = [covariates[sid].get(name) for sid in case_sids]
        b = [covariates[sid].get(name) for sid in control_sids]
        binary = name in ("dim_2d", "hemisphere_left")
        value = smd(a, b, binary=binary)
        finite_a = [v for v in a if v is not None]
        finite_b = [v for v in b if v is not None]
        out[name] = {
            "smd": value,
            "flagged": bool(value is not None and abs(value) > flag),
            "case_mean": float(np.mean(finite_a)) if finite_a else None,
            "control_mean": float(np.mean(finite_b)) if finite_b else None,
            "binary": binary,
        }
    return out


def _records(items):
    if isinstance(items, dict):
        return list(items.items())
    return [(sid, values) for sid, values in items]


def matched_subsample(cases, controls, covariates, seed=0, ratio=1, caliper=None):
    """Nearest-neighbour matching of controls to cases on standardized covariates, without replacement.

    Covariates are standardized with the mean and SD of cases and controls together (a constant
    covariate is ignored). Cases are taken in a seeded random order; each takes the nearest
    available control (Euclidean distance; ties go to the earlier control of a seeded random
    order), ``ratio`` rounds. Rows with a missing covariate are left out and listed.

    :param cases, controls: {sid: {covariate: value}} or [(sid, {covariate: value})].
    :param covariates: covariate names.
    :param caliper: optional largest distance (in SD units) of a match.
    :return: {"sids" (cases with a match and their controls, sorted), "pairs": [[case, control,
      distance]], "unmatched_cases", "missing", "seed", "ratio", "covariates", "smd_before", "smd_after"}.
    """
    covariates = list(covariates)
    rng = np.random.default_rng(int(seed))
    missing = []

    def usable(records):
        out = []
        for sid, values in sorted(_records(records), key=lambda item: item[0]):
            row = [values.get(name) for name in covariates]
            if any(v is None or not math.isfinite(float(v)) for v in row):
                missing.append(sid)
            else:
                out.append((sid, np.asarray(row, dtype=np.float64)))
        return out

    case_rows, control_rows = usable(cases), usable(controls)
    if not covariates:
        raise ValueError("matched_subsample needs at least one covariate")
    both = np.array([row for _, row in case_rows + control_rows], dtype=np.float64).reshape(-1, len(covariates))
    mean = both.mean(axis=0) if both.size else np.zeros(len(covariates))
    sd = both.std(axis=0, ddof=1) if len(both) > 1 else np.ones(len(covariates))
    scale = np.where(sd > 0, sd, np.inf)  # constant covariates contribute 0
    case_z = np.array([(row - mean) / scale for _, row in case_rows]).reshape(-1, len(covariates))
    control_z = np.array([(row - mean) / scale for _, row in control_rows]).reshape(-1, len(covariates))
    case_order = rng.permutation(len(case_rows))
    control_rank = np.empty(len(control_rows), dtype=np.int64)
    control_rank[rng.permutation(len(control_rows))] = np.arange(len(control_rows))
    available = np.ones(len(control_rows), dtype=bool)
    pairs, matched_cases = [], set()
    for _ in range(int(ratio)):
        for c in case_order:
            if not available.any():
                break
            distance = np.sqrt(((control_z - case_z[c]) ** 2).sum(axis=1))
            distance[~available] = np.inf
            best = np.flatnonzero(distance == distance.min())
            j = int(best[np.argmin(control_rank[best])])
            if caliper is not None and distance[j] > caliper:
                continue
            available[j] = False
            pairs.append([case_rows[c][0], control_rows[j][0], float(distance[j])])
            matched_cases.add(case_rows[c][0])
    matched_controls = [pair[1] for pair in pairs]
    case_values = dict(case_rows)
    control_values = dict(control_rows)

    def smds(case_ids, control_ids):
        return {name: smd([case_values[s][k] for s in case_ids], [control_values[s][k] for s in control_ids]) for k, name in enumerate(covariates)}

    return {
        "sids": sorted(matched_cases | set(matched_controls)),
        "pairs": pairs,
        "unmatched_cases": sorted(sid for sid, _ in case_rows if sid not in matched_cases),
        "missing": sorted(missing),
        "seed": int(seed),
        "ratio": int(ratio),
        "caliper": caliper,
        "covariates": covariates,
        "smd_before": smds([sid for sid, _ in case_rows], [sid for sid, _ in control_rows]),
        "smd_after": smds(sorted(matched_cases), matched_controls),
    }


def add_subset(study, name, sids):
    """Store a named subset in study.json ``subsets`` (in memory; the session saves). Include flags are not edited."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(name)):
        raise ValueError(f"Invalid subset name {name!r} (letters, digits, '_', '.', '-')")
    unknown = [sid for sid in sids if sid not in study]
    if unknown:
        raise ValueError(f"Unknown subjects: {', '.join(unknown)}")
    subsets = study.subsets
    subsets[str(name)] = list(sids)
    study.subsets = subsets
    return subsets[str(name)]


# Literature context


def literature_context(summary):
    """Neutral, cited context strings for a summary: [{"key", "text", "citation"}]."""
    out = []
    n = summary.get("n_cases") or summary.get("n") or 0
    out.append(
        {
            "key": "set_size",
            "text": f"This set: {n} lesions. Sets of \u2265 10 heterogeneous lesions approach the degree map (r > 0.44; 20\u201325 lesions r > 0.62; {CITATION_VDH})",
            "citation": CITATION_VDH,
        },
    )
    dice = (summary.get("dice") or {}).get("mean")
    if dice is not None:
        reference = (0.08, 10) if dice < 0.12 else (0.16, 64) if dice < 0.205 else (0.25, 97)  # nearest reference
        relation = "above" if dice > 0.25 else "at"
        out.append(
            {
                "key": "dice",
                "text": f"This set: mean pairwise Dice {dice:.2f}. {relation.capitalize()} mean pairwise Dice {reference[0]:.2f}, conjunction was significant for {reference[1]}% of random sets ({CITATION_VDH})",
                "citation": CITATION_VDH,
            },
        )
    volumes = summary.get("volumes") or {}
    if volumes.get("skew") is not None:
        text = f"Lesion volume skew {volumes['skew']:.1f}"
        out.append({"key": "volume_skew", "text": text + (": log(volume) is suggested as the covariate (skew > 2)" if volumes.get("suggest_log") else ""), "citation": None})
    dims = summary.get("dimensionality") or {}
    flat = dims.get("2D", 0) + dims.get("multi2D", 0)
    if flat:
        out.append(
            {
                "key": "dimensionality",
                "text": f"{flat} of {sum(dims.values())} lesions are 2D tracings; dimensionality is suggested as a covariate when the 2D share differs by more than {DIMENSIONALITY_SHARE_POINTS:.0f} points between groups ({CITATION_COTOVIO})",
                "citation": CITATION_COTOVIO,
            },
        )
    out.append({"key": "overlap", "text": f"The overlap map is hub-inflated; read it next to its null-referenced version ({CITATION_CUI})", "citation": CITATION_CUI})
    return out


# Summary and outputs


def _now():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _round(value, digits=4):
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def summarize(occ, priors=None, binarize_at=BINARIZE_AT, case="case", control="control", maps=None):
    """All diagnostics of a lesion set as a JSON-ready dict (see the module docstring).

    :param priors: optional tissue priors (see :func:`tissue_fractions`); a family mismatch is
      recorded under ``tissue_error`` (with its reason) and no fractions are computed.
    :param maps: optional :class:`OverlapMaps` of ``occ`` at ``binarize_at`` (computed when not given).
    """
    started = time.monotonic()
    overlap = maps if maps is not None else overlap_maps(occ, binarize_at=binarize_at)
    dice = pairwise_dice(occ, binarize_at=binarize_at)
    labels, hemisphere_counts, left_share = hemisphere_split(occ)
    tissue, tissue_error = None, None
    if priors is not None:
        try:
            tissue = tissue_fractions(occ, priors)
        except PriorFamilyError as exc:
            tissue_error = exc.reason
    covariates = lesion_covariates(occ, tissue, labels)
    binary_sizes = np.diff(occ.binary(binarize_at).indptr)
    source_types = occ.column("source_type")
    lesions = []
    for k, sid in enumerate(occ.sids):
        dim, dim_from = occ.dimensionality[k]
        lesion = {
            "participant_id": sid,
            "group": occ.groups[k],
            "volume_ml": _round(occ.volumes_ml[k]),
            "voxels": int(binary_sizes[k]),
            "hemisphere": labels[k],
            "left_share": _round(left_share[k]),
            "dimensionality": dim,
            "dimensionality_from": dim_from,
            "source_type": source_types[k],
        }
        if tissue is not None:
            for name in TISSUES:
                lesion[f"{name}_frac"] = _round(tissue.fractions[name][k])
        lesions.append(lesion)
    groups = sorted(set(occ.groups))
    cases = [sid for sid, group in zip(occ.sids, occ.groups, strict=True) if group == case]
    controls = [sid for sid, group in zip(occ.sids, occ.groups, strict=True) if group == control]
    summary = {
        "n": len(occ),
        "n_cases": len(cases),
        "n_controls": len(controls),
        "groups": {group: occ.groups.count(group) for group in groups},
        "space": occ.space,
        "grid": {"label": occ.label, "shape": list(occ.grid.shape), "affine": np.round(occ.grid.affine, 6).tolist()},
        "binarize_at": float(binarize_at),
        "excluded": occ.excluded,
        "load_errors": dict(occ.errors),
        "overlap": {"max_count": overlap.max_count, "n": overlap.n, "legend": overlap.legend},
        "dice": {
            "mean": _round(dice.mean),
            "median": _round(dice.median),
            "max": _round(dice.maximum),
            "n_pairs": len(dice.condensed),
            "n_candidates": dice.n_candidates,
            "histogram": {"edges": list(dice.edges), "counts": [int(c) for c in dice.histogram]},
            "max_overlap_count": dice.max_overlap_count,
        },
        "volumes": {key: (_round(value) if isinstance(value, float) else value) for key, value in volume_summary(occ.volumes_ml).items()},
        "volumes_by_group": {group: {key: (_round(value) if isinstance(value, float) else value) for key, value in volume_summary(occ.volumes_ml[[g == group for g in occ.groups]]).items()} for group in groups},
        "hemisphere": hemisphere_counts,
        "dimensionality": dimensionality_tally(occ),
        "source_type": source_type_tally(occ),
        "tissue": None,
        "tissue_error": tissue_error,
        "balance": None,
        "lesions": lesions,
    }
    if tissue is not None:
        summary["tissue"] = {
            "priors": tissue.priors,
            "mean_pct": {name: _round(100.0 * float(np.nanmean(tissue.fractions[name])), 2) if len(occ) else None for name in TISSUES},
            "mean_pct_by_group": {
                group: {name: _round(100.0 * float(np.nanmean(tissue.fractions[name][[g == group for g in occ.groups]])), 2) for name in TISSUES} for group in groups
            },
        }
    if cases and controls:
        table = balance(covariates, cases, controls)
        summary["balance"] = {name: {**row, "smd": _round(row["smd"]), "case_mean": _round(row["case_mean"]), "control_mean": _round(row["control_mean"])} for name, row in table.items()}
        dims = {group: [occ.dimensionality[k][0] for k, g in enumerate(occ.groups) if g == group] for group in (case, control)}
        shares = {group: 100.0 * sum(d in ("2D", "multi2D") for d in values) / len(values) for group, values in dims.items()}
        summary["suggest_dimensionality_covariate"] = bool(abs(shares[case] - shares[control]) > DIMENSIONALITY_SHARE_POINTS)
    summary["literature"] = literature_context(summary)
    summary["seconds"] = round(time.monotonic() - started, 3)
    return summary


def posted_values(occ, summary):
    """{sid: {column: value}} to post: tissue fractions and hemisphere, and dimensionality for rows without one.

    gm_frac, wm_frac, csf_frac and hemisphere are derived columns: they are posted on every run, so
    applying the inbox replaces a value typed into them. dimensionality is posted only from the mask
    (rows whose column is empty), so a tracer's or source's value is kept.
    """
    rows = {}
    for lesion in summary["lesions"]:
        values = {}
        for name in TISSUES:
            value = lesion.get(f"{name}_frac")
            if value is not None:
                values[f"{name}_frac"] = value
        if lesion.get("hemisphere"):
            values["hemisphere"] = lesion["hemisphere"]
        if lesion.get("dimensionality_from") == "mask" and lesion.get("dimensionality"):
            values["dimensionality"] = lesion["dimensionality"]
        if values:
            rows[lesion["participant_id"]] = values
    return rows


def lesionset_path(study, out_dir=None):
    root = study.path if isinstance(study, Study) else os.fspath(study)
    return os.path.join(out_dir or os.path.join(root, "derivatives", "lnm"), LESIONSET_JSON)


def write_lesionset_json(study, summary, occ=None, out_dir=None, post=True, outputs=None, extra=None):
    """Write lesionset.json and post the per-subject columns through the study inbox.

    :param study: :class:`.study.Study` or folder.
    :param out_dir: folder of lesionset.json (default ``<study>/derivatives/lnm``).
    :param post: post gm_frac, wm_frac, csf_frac, hemisphere (and dimensionality for rows without
      one) with ``Study.post_results`` (source "lesionset"); participants.tsv is never written.
    :return: (path of lesionset.json, path of the inbox file or None).
    """
    root = study.path if isinstance(study, Study) else os.path.abspath(os.fspath(study))
    path = lesionset_path(root, out_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = {"format": FORMAT, "version": VERSION, "created": _now(), "library": jobs.library_version(), "study": os.path.basename(root), **(extra or {}), "outputs": outputs or {}, "summary": summary}
    jobs.write_json_atomic(path, content)
    inbox = None
    if post:
        values = posted_values(occ, summary)
        if values:
            audit = {"event": "lesionset", "file": _out_rel(root, path), "columns": sorted({c for row in values.values() for c in row}), "n": len(values), "at": _now()}
            inbox = Study.post_results(root, values, INBOX_SOURCE, audit=audit)
    return path, inbox


def run(study, grid=None, priors=None, out_dir=None, groups=None, binarize_at=BINARIZE_AT, probabilistic=False, post=True, match=None, template_dirs=(), cancel=None, progress=None, space=None):
    """Load, summarize and write everything (the command line's work).

    :param priors: a folder of priors (see :func:`find_priors`; a set of the lesions' family is
      preferred) or a priors dict.
    :param space: space label of the grid when it cannot be told (see :func:`resolve_grid`).
    :param match: optional {"name", "covariates", "seed", "ratio"} for a matched subsample.
    :return: {"summary", "lesionset", "inbox", "overlap": [count, percent], "matched"}.
    """
    if not isinstance(study, Study):
        study = Study.open(study)
    occ = load_occupancies(study, grid, groups=groups, probabilistic=probabilistic, space=space, template_dirs=template_dirs, cancel=cancel, progress=progress)
    if isinstance(priors, (str, os.PathLike)):
        folder = priors
        priors = find_priors(folder, occ.grid, occ.space)
        if priors is None:
            raise ValueError(f"No complete GM/WM/CSF priors (tpl-<space>_..._label-<GM|WM|CSF>_probseg.nii.gz) in {folder}")
    maps = overlap_maps(occ, binarize_at=binarize_at)
    summary = summarize(occ, priors, binarize_at, maps=maps)
    count_path, percent_path = write_overlap(study, maps, occ, out_dir=out_dir)
    matched = None
    if match:
        covariates = covariates_from_summary(summary)
        cases = {sid: covariates[sid] for sid, group in zip(occ.sids, occ.groups, strict=True) if group == "case"}
        controls = {sid: covariates[sid] for sid, group in zip(occ.sids, occ.groups, strict=True) if group == "control"}
        matched = {"name": match["name"], **matched_subsample(cases, controls, match["covariates"], seed=match.get("seed", 0), ratio=match.get("ratio", 1))}
    outputs = {"overlap_count": _out_rel(study.path, count_path), "overlap_pct": _out_rel(study.path, percent_path)}
    path, inbox = write_lesionset_json(study, summary, occ, out_dir=out_dir, post=post, outputs=outputs, extra={"matched": matched} if matched else None)
    return {"summary": summary, "lesionset": path, "inbox": inbox, "overlap": [count_path, percent_path], "matched": matched, "lesions": occ}


def _out_rel(root, path):
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel.replace(os.sep, "/")


# Command line


def _parser():
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.lesionset", description="Lesion-set diagnostics: exact overlap maps, pairwise Dice, volumes, tissue fractions, tallies and case/control balance.")
    parser.add_argument("--study", required=True, help="study folder")
    parser.add_argument("--grid", default=TEMPLATE_GRID, help="template2mm (default: the 2 mm grid of the study's template family) or a connectome bundle folder")
    parser.add_argument("--space", default=None, help="space label of the grid when it cannot be told (a bundle without 'space' metadata on a grid outside the registry)")
    parser.add_argument("--priors", default=None, help="folder of GM/WM/CSF priors of the lesions' template family (tpl-<space>_..._label-<GM|WM|CSF>_probseg.nii.gz)")
    parser.add_argument("--out", default=None, help="output folder (default: <study>/derivatives/lesions for the maps, <study>/derivatives/lnm for lesionset.json)")
    parser.add_argument("--groups", default=None, help="comma-separated groups to include (default: all)")
    parser.add_argument("--binarize-at", type=float, default=BINARIZE_AT, help="occupancy at which a grid voxel counts as lesion (default 0.5)")
    parser.add_argument("--probabilistic", action="store_true", help="keep fractional lesion values (default: every non-zero voxel is lesion)")
    parser.add_argument("--no-post", action="store_true", help="do not post the per-subject columns to the study inbox")
    parser.add_argument("--match-subset", default=None, help="name of a matched subsample of controls (recorded in lesionset.json)")
    parser.add_argument("--match-covariates", default="volume_ml", help="comma-separated matching covariates (volume_ml, log_volume_ml, wm_frac, dim_2d, hemisphere_left)")
    parser.add_argument("--match-seed", type=int, default=0, help="matching seed (default 0)")
    parser.add_argument("--template-dir", action="append", default=[], help="folder searched for template files (repeatable)")
    parser.add_argument("--threads", type=jobs.positive_int, default=2, help="BLAS threads (default: 2)")
    parser.add_argument("--cancel-file", default=None, help="stop (exit code 3) as soon as this file exists")
    return parser


def main(argv=None):
    """Command-line entry point (see the module docstring); returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    if not 0.0 < args.binarize_at <= 1.0:
        raise jobs.JobError(f"--binarize-at must be in (0, 1], got {args.binarize_at}", code="usage", exit_code=jobs.EXIT_USAGE)
    cancel = jobs.CancelToken(args.cancel_file)
    jobs.emit("start", job=JOB, argv=argv, threads=args.threads)
    if not Study.is_study(args.study):
        raise jobs.JobError(f"{args.study} is not a study folder (no study.json)", code="usage", exit_code=jobs.EXIT_USAGE)
    study = Study.open(args.study)
    match = None
    if args.match_subset:
        match = {"name": args.match_subset, "covariates": [c.strip() for c in args.match_covariates.split(",") if c.strip()], "seed": args.match_seed}
    groups = [g.strip() for g in args.groups.split(",") if g.strip()] if args.groups else None
    started = time.monotonic()
    jobs.emit("stage", name="load", index=1, total=2)

    def progress(done, total):
        if done == total or done % 25 == 0:
            jobs.progress(done, total, started)

    try:
        result = run(study, args.grid, args.priors, args.out, groups, args.binarize_at, args.probabilistic, not args.no_post, match, args.template_dir, cancel, progress, space=args.space)
    except (ValueError, TypeError) as exc:
        raise jobs.JobError(str(exc), code="input") from None
    jobs.emit("stage", name="write", index=2, total=2)
    summary = result["summary"]
    for reason, sids in summary["excluded"].items():
        jobs.emit("warning", code="excluded", message=f"{len(sids)} excluded: {reason}", reason=reason, sids=sids)
    if summary.get("tissue_error"):
        jobs.emit("warning", code="tissue_family", message=summary["tissue_error"])
    matched = result["matched"]
    if matched and matched.get("missing"):
        missing = matched["missing"]
        jobs.emit("warning", code="match_missing", message=f"{len(missing)} lesions left out of matching: a matching covariate ({', '.join(match['covariates'])}) is missing (wm_frac needs --priors)", sids=missing)
    for path in result["overlap"]:
        jobs.emit("result", kind="image", path=os.path.abspath(path))
    jobs.emit("result", kind="lesionset", path=os.path.abspath(result["lesionset"]), n=summary["n"], legend=summary["overlap"]["legend"], inbox=result["inbox"])
    jobs.emit("done", outputs=[os.path.abspath(p) for p in [*result["overlap"], result["lesionset"]]], elapsed=round(jobs.elapsed(), 3), run_id=None)
    return 0


if __name__ == "__main__":
    from LesionNetworkMappingLib import jobs as _jobs

    _jobs.run_job(main, job=JOB)

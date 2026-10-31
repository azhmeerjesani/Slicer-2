"""Streaming lesion network mapping engine (pure numpy) and its command-line interface.

For every subject of a connectome bundle and every lesion (seed), the engine computes the Pearson
correlation r between the lesion's mean time course and every voxel, averaged over runs (as Lead-DBS
does), converts it to Fisher z and accumulates over subjects: mean r, mean Fz and the one-sample
T statistic of Fz (float64 Welford accumulation, so a valid T map exists after any number of subjects).

Maths: bundle rows X_r[v] are demeaned and unit norm within each run r, with the original norms
stored. The raw seed time course of run r is ``s_r = sum_v w_v * norm[r, v] * X_r[v]``. Each s_r is
demeaned and scaled to unit norm and ``S = concat(s_r) / nRuns``, so that ``X @ S`` is the mean over
runs of the per-run Pearson r. All lesions are columns of S: one GEMM per voxel chunk serves them all.
A run in which the seed (or a voxel) has zero variance contributes r = 0 to that mean.
The time series are stored as float16 and upcast to float32 chunk by chunk (float16 GEMM is very slow).

Seeds can also be the parcels of a label image (``labels``): parcel i's time course is the sum of
its voxels' time courses (unit weights), computed from the voxels sorted by label once, so that the
seed extraction needs O(V) memory instead of an (L, V) weight matrix.

Accumulation over subjects (``accumulate``): "welford" (default) keeps mean r, mean Fz and the T
statistic as above; "mean" keeps only a float32 sum of Fz (and of r, optionally), 4 * V * L bytes
per map, for passes that need the mean Fz only (e.g. the parcel maps of the fast index). With
``split_half`` a float64 sum of Fz over the odd-numbered subjects (1st, 3rd, ...) is also kept, so
that the mean Fz of each half and their spatial correlation (connectome sampling stability) are known.

Command line (JSON events of the job protocol, see jobs.py: start, progress, snapshot, warning, done,
error or cancelled; every event has "t", the seconds since the job started)::

    python -m LesionNetworkMappingLib.engine --bundle B.lnmc --lesion lesion1.nii.gz [--lesion ...]
        [--lesion-space SPACE ...] --out DIR [--subjects N [--random] [--seed S] | --subject-list SPEC] [--threads K] [--snapshot-every K]
        [--null-relocations K [--null-seed S] [--brain-mask MASK] [--null-voxels N] [--write-null]]
        [--accumulate welford|mean] [--split-half] [--desc-suffix PreviewN100] [--threshold-rule JSON]
        [--run-json run.json] [--cancel-file F]
    python -m LesionNetworkMappingLib.engine --bundle B.lnmc --parcels LABELS.nii.gz --out DIR
        [--subjects N [--random] [--seed S] | --subject-list SPEC] [--threads K] [--run-json run.json] [--cancel-file F]

--subject-list takes a named subset of the bundle (e.g. "First 100 Subjects"), a text file of subject
ids or a comma-separated list of ids; a named subset labels the outputs "Subset<Name>" unless
--desc-suffix is given. The "start" event carries the process id ("pid"). Run as a module, the
engine stops (exit code 3, "cancelled" event) on SIGTERM/SIGINT, when the --cancel-file exists or
when the process that started it is gone (the PythonSlicer launcher does not pass SIGTERM on), see
jobs.run_job. The lesion "qc" of the start and done events and of run.json carries volume_ml.

Outputs of a lesion run (Lead-DBS names, exact bundle header): AvgR, AvgRFz and T maps per lesion
(``--accumulate mean``: AvgR and AvgRFz only); with --split-half also desc-AvgFzOddHalf and
desc-AvgFzEvenHalf (the "done" event and qa.json carry their spatial r per lesion as "split_half",
"connectome sampling stability"); with --write-null the 4D desc-RelocatedT and desc-RelocatedFz maps
of the relocated lesions, on all voxels (one volume per relocation).

A parcel run (--parcels; a label image on any grid, resampled to the bundle grid by nearest neighbour;
0 is background) maps every parcel as a seed with the mean accumulator in one pass and writes
<out>/parcel_fz.f16.npy (R, V) float16 mean Fz of each parcel seed over the bundle voxels,
<out>/parcel_C_fz.npy (R, R) float64 mean over subjects of atanh(r) between the parcel seed time courses
(r = the mean over runs of their Pearson r, clipped to +/-RMAX) and <out>/parcel_labels.json (the label
value and voxel count of each row, labels outside the connectome mask, coverage, subjects).

After the maps, the specificity QA (see qa.py) runs: degree similarity when the bundle has a degree
map, the relocated-lesion null when --null-relocations is given (the relocated lesions are extra
seed columns of the same pass) and, when the bundle has valid reference maps (reference.py; never
built here), the reference similarity and calibrated chip. It is reported in the "done" event ("qa")
and in <out>/qa.json, which also identifies the reference ("reference": definition, sha256).

The template space of each lesion (--lesion-space, one per --lesion or one for all; default: the space of
the lesion file's grid when it is a known template grid) is recorded with its lesion in run.json ("space",
"space_notice") and in the QA values ("lesion_space", "space_notice"): the notice says that a lesion on
another MNI template family than the connectome gives an approximate map (see ``geometry.family_notice``).
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import re
import time
import zlib

import numpy as np

from . import jobs
from .connectome import ConnectomeBundle, _is_cancelled
from .geometry import detect_space, family_notice, from_masked_vector, resample_to_grid, to_masked_vector
from .nifti import XFORM_WARNING_MM, read_nifti, read_nifti_header, write_nifti, xform_disagreement

RMAX = 0.999999  # r is clipped to +/-RMAX before the Fisher transform
DEFAULT_CHUNK = 8192
MIN_WEIGHT = 1e-4  # lesion weights at or below this value are ignored (as in Lead-DBS)
OUTPUT_KEYS = (("AvgR", "AvgR"), ("AvgFz", "AvgRFz"), ("T", "T"))  # result key, Lead-DBS "desc" label
NULL_DESC = "RelocatedT"  # "desc" label of the optional 4D null T maps
NULL_FZ_DESC = "RelocatedFz"  # "desc" label of the optional 4D null mean Fz maps
SPLIT_HALF_KEYS = (("FzOdd", "AvgFzOddHalf"), ("FzEven", "AvgFzEvenHalf"))  # result key, "desc" label
SPLIT_HALF_LABEL = "connectome sampling stability"
ACCUMULATE_MODES = ("welford", "mean")
PARCEL_FZ_NAME = "parcel_fz.f16.npy"
PARCEL_C_NAME = "parcel_C_fz.npy"
PARCEL_LABELS_NAME = "parcel_labels.json"
PARCEL_FORMAT = "slicer-lnm-parcels"
# Null maps only feed spatial correlations: by default they are computed on a random sample of this
# many voxels, which bounds their memory and GEMM cost for large connectomes and many lesions.
NULL_MAX_VOXELS = 30_000


class NetworkMaps:
    """Group network maps of L lesions over the V voxels of a bundle mask.

    ``maps[i]`` is a dict of masked vectors ``{"AvgR", "AvgFz", "T", "N"}`` for lesion i; the arrays
    ``avg_r``, ``avg_fz`` and ``t`` have shape (L, V). ``n`` is the number of subjects included and
    ``cancelled`` tells whether the computation stopped early.

    When null seeds were mapped in the same pass (``compute_network_maps(null_weights=...)``),
    ``null_t`` holds their T maps, shape (K, V'), over the voxels ``null_voxels`` (indices into the
    V mask voxels; None means all V voxels). Otherwise both are None. ``null_fz`` holds their mean
    Fz maps (same shape) when they were requested (``null_fz=True``).

    With the "mean" accumulator ``t`` is None (and ``avg_r`` when r was not kept). With
    ``split_half``, ``fz_odd`` and ``fz_even`` are the (L, V) mean Fz over the odd-numbered (1st,
    3rd, ...) and even-numbered subjects (``n_odd`` and ``n_even`` of them). With parcel ``labels``,
    ``c_fz`` is the (L, L) float64 mean over subjects of atanh(r) between the seed time courses.
    """

    def __init__(self, avg_r, avg_fz, t, subject_ids, cancelled=False, elapsed=0.0, null_t=None, null_voxels=None, null_fz=None, fz_odd=None, fz_even=None, n_odd=0, n_even=0, c_fz=None):
        self.avg_r = avg_r
        self.avg_fz = avg_fz
        self.t = t
        self.subject_ids = list(subject_ids)
        self.cancelled = cancelled
        self.elapsed = elapsed
        self.null_t = null_t
        self.null_voxels = null_voxels
        self.null_fz = null_fz
        self.fz_odd = fz_odd
        self.fz_even = fz_even
        self.n_odd = n_odd
        self.n_even = n_even
        self.c_fz = c_fz

    @property
    def n(self):
        return len(self.subject_ids)

    def __len__(self):
        return self.avg_fz.shape[0]

    def __getitem__(self, index):
        row = lambda maps: None if maps is None else maps[index]
        return {"AvgR": row(self.avg_r), "AvgFz": row(self.avg_fz), "T": row(self.t), "N": self.n}

    def available(self):
        """Result keys of :data:`OUTPUT_KEYS` that this object holds ("T" is missing with the mean accumulator)."""
        arrays = {"AvgR": self.avg_r, "AvgFz": self.avg_fz, "T": self.t}
        return [key for key, _ in OUTPUT_KEYS if arrays[key] is not None]

    def __iter__(self):
        return (self[i] for i in range(len(self)))

    def volume(self, index, key, bundle, fill=0.0):
        """Return map ``key`` ("AvgR", "AvgFz" or "T") of lesion ``index`` as a volume on the bundle grid."""
        return unmask(self[index][key], bundle, fill)


def unmask(vector, bundle, fill=0.0):
    """Scatter a masked vector (or an (L, V) stack) into float32 volume(s) indexed [i, j, k] on the bundle grid."""
    return from_masked_vector(np.asarray(vector, dtype=np.float32), bundle.mask_indices, bundle.shape, fill=fill)


# Lesions


def lesion_weights(volume, mask_indices, min_weight=MIN_WEIGHT):
    """Masked float32 weight vector of a lesion volume already on the bundle grid (e.g. fractional occupancy).

    Weights at or below ``min_weight`` (and non-finite values) are set to 0.
    """
    weights = to_masked_vector(np.asarray(volume, dtype=np.float32), mask_indices)
    weights[~(weights > min_weight)] = 0.0
    return weights


def lesion_qc(volume, mask_indices, min_weight=MIN_WEIGHT, voxel_volume=1.0, source_volume_mm3=None):
    """Quality-control numbers of a lesion volume on the bundle grid.

    :param voxel_volume: bundle voxel volume in mm^3.
    :param source_volume_mm3: lesion volume before resampling; when given, lesion parts outside the
      bundle field of view (lost by resampling) also count as outside the mask.
    :return: dict with voxels_in_mask, voxels_total (weight > min_weight), weight_total (sum of the
      weights used), weight_outside_fraction (share of the lesion volume outside the connectome
      mask; weights trimmed by min_weight do not count as outside), volume_mm3 and volume_ml
      (volume_mm3 / 1000, rounded to 4 decimals as in cohort maps.tsv).
    """
    occupancy = np.clip(np.asarray(volume, dtype=np.float64).reshape(-1), 0.0, None)
    used = np.where(occupancy > min_weight, occupancy, 0.0)
    volume_mm3 = float(occupancy.sum() * voxel_volume if source_volume_mm3 is None else source_volume_mm3)
    inside_mm3 = float(occupancy[mask_indices].sum() * voxel_volume)
    return {
        "voxels_in_mask": int(np.count_nonzero(used[mask_indices])),
        "voxels_total": int(np.count_nonzero(used)),
        "weight_total": float(used.sum()),
        "weight_outside_fraction": _clean_fraction(1.0 - inside_mm3 / volume_mm3) if volume_mm3 > 0 else 0.0,
        "volume_mm3": volume_mm3,
        "volume_ml": round(volume_mm3 / 1000.0, 4),
    }


def _clean_fraction(value, eps=1e-9):
    """Clamp a fraction to [0, 1], with values within eps of 0 or 1 (rounding noise) snapped to them."""
    value = min(1.0, max(0.0, float(value)))
    return 0.0 if value < eps else 1.0 if value > 1.0 - eps else value


def _read_lesion_image(path):
    """(data, affine) of a lesion NIfTI file; an unreadable file (not gzip, truncated, corrupt) raises a ValueError that names it."""
    try:
        data, affine, _ = read_nifti(path)
    except FileNotFoundError:
        raise
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError(f"{os.fspath(path)}: cannot be read (not a NIfTI file or corrupt): {type(exc).__name__}: {exc}") from exc
    return data, affine


def prepare_lesion(source, bundle, supersample=2, binarize=True, min_weight=MIN_WEIGHT):
    """Resample a lesion image onto the bundle grid as fractional occupancy and return (weights, qc).

    :param source: NIfTI path, or a (data, affine) tuple with data indexed [i, j, k].
    :param binarize: treat every non-zero voxel as lesion (so label values do not matter);
      set False for probabilistic seeds.
    """
    if isinstance(source, (str, os.PathLike)):
        data, affine = _read_lesion_image(source)
    else:
        data, affine = source
    data = np.nan_to_num(np.asarray(data, dtype=np.float64))
    if data.ndim > 3:
        if any(n != 1 for n in data.shape[3:]):
            raise ValueError(f"A lesion must be a 3D image, got shape {data.shape}")
        data = data.reshape(data.shape[:3])
    if binarize:
        data = (data != 0).astype(np.float64)
    volume = resample_to_grid(data, affine, bundle.grid, mode="fraction", supersample=supersample)
    weights = lesion_weights(volume, bundle.mask_indices, min_weight)
    source_volume = float(np.clip(data, 0.0, None).sum() * abs(np.linalg.det(np.asarray(affine, dtype=np.float64)[:3, :3])))
    return weights, lesion_qc(volume, bundle.mask_indices, min_weight, bundle.grid.voxel_volume, source_volume)


# Core computation


class _Seeds:
    """Lesion weights restricted to the union of their voxels."""

    def __init__(self, weights, min_weight=MIN_WEIGHT):
        weights = np.atleast_2d(np.asarray(weights, dtype=np.float32))
        weights = np.where(weights > min_weight, weights, np.float32(0.0))
        self.n_lesions, self.n_voxels = weights.shape
        self.union = np.flatnonzero((weights > 0).any(axis=0))
        self.weights = np.ascontiguousarray(weights[:, self.union])  # (L, U) float32

    def matrix(self, data, norms, runs, chunk=DEFAULT_CHUNK):
        """Return S (T_total, L) float32 such that ``data @ S`` is the mean over runs of Pearson r.

        The raw seed time courses are accumulated in float64 from float32 products over chunks of
        the lesion voxels. A run in which a seed is constant (all its voxels have zero variance)
        gets a zero seed segment: that run then contributes r = 0 to the mean over runs (Lead-DBS
        would give NaN), as does a voxel with zero variance in a run.
        """
        bounds = np.cumsum((0, *runs))
        raw = np.zeros((self.n_lesions, bounds[-1]), dtype=np.float64)
        for u0 in range(0, self.union.size, chunk):
            rows = self.union[u0:u0 + chunk]
            x = data[rows].astype(np.float32, copy=False)  # (U_chunk, T_total)
            for run in range(len(runs)):
                segment = slice(bounds[run], bounds[run + 1])
                raw[:, segment] += (self.weights[:, u0:u0 + chunk] * norms[run, rows]) @ x[:, segment]
        return _normalized_seeds(raw, runs)


class _SparseSeeds:
    """Seeds given as sparse columns (voxel positions, weights), e.g. many small relocated lesions.

    Same interface as :class:`_Seeds`; each seed time course only reads its own voxels.
    """

    def __init__(self, columns, n_voxels, min_weight=MIN_WEIGHT):
        self.n_voxels = int(n_voxels)
        self.columns = []
        for positions, values in columns:
            positions = np.asarray(positions, dtype=np.int64)
            values = np.asarray(values, dtype=np.float32)
            if positions.shape != values.shape or (positions.size and (positions.min() < 0 or positions.max() >= self.n_voxels)):
                raise ValueError("Each sparse seed needs matching voxel positions (within the connectome mask) and weights")
            keep = values > min_weight
            self.columns.append((positions[keep], values[keep]))
        self.n_lesions = len(self.columns)

    def matrix(self, data, norms, runs, chunk=DEFAULT_CHUNK):
        """Return S (T_total, K) float32 such that ``data @ S`` is the mean over runs of Pearson r."""
        bounds = np.cumsum((0, *runs))
        raw = np.zeros((self.n_lesions, bounds[-1]), dtype=np.float64)
        for column, (positions, values) in enumerate(self.columns):
            if positions.size:
                x = data[positions].astype(np.float32, copy=False)  # (m, T_total)
                for run in range(len(runs)):
                    segment = slice(bounds[run], bounds[run + 1])
                    raw[column, segment] = (values * norms[run, positions]) @ x[:, segment]
        return _normalized_seeds(raw, runs)


class _LabelSeeds:
    """Seeds given as a label per voxel (parcels): seed i is the sum of the voxels whose label is i.

    Same interface as :class:`_Seeds`. The raw time course of seed i in run r is
    ``sum_{v: labels[v] == i} norm[r, v] * X_r[v]`` (unit weights). The voxels are sorted by label
    once; each subject then reads them in that order, chunk by chunk, so that the memory is O(V)
    instead of a dense (L, V) weight matrix. A chunk spans few labels when the parcels are large:
    their sums are then one small GEMM with a local (labels in the chunk, chunk) matrix of the norms
    (float32 products, float64 accumulation over chunks, as :class:`_Seeds`); chunks with many small
    labels use ``np.add.reduceat`` (float64 accumulation) instead.
    """

    MAX_GEMM_LABELS = 256  # labels per chunk up to which the local GEMM is used (at most 8 MB per chunk)

    def __init__(self, labels, n_labels=None):
        """:param labels: (V,) integer labels: 0..n_labels-1 for the parcel of each voxel, negative for none.
        :param n_labels: number of seeds (default: max label + 1); a label without voxels gives a zero seed.
        """
        labels = np.asarray(labels)
        if labels.ndim != 1 or not np.issubdtype(labels.dtype, np.integer):
            raise ValueError("Labels must be a 1D integer vector over the connectome voxels")
        self.n_voxels = int(labels.size)
        top = int(labels.max()) if labels.size else -1
        self.n_lesions = top + 1 if n_labels is None else int(n_labels)
        if top >= self.n_lesions:
            raise ValueError(f"A label ({top}) is not below the number of labels ({self.n_lesions})")
        inside = np.flatnonzero(labels >= 0)
        order = np.argsort(labels[inside], kind="stable")
        self.rows = inside[order]  # voxel indices sorted by label (then by voxel)
        self.row_labels = labels[self.rows].astype(np.int64)
        self.counts = np.bincount(self.row_labels, minlength=self.n_lesions)

    def matrix(self, data, norms, runs, chunk=DEFAULT_CHUNK):
        """Return S (T_total, L) float32 such that ``data @ S`` is the mean over runs of Pearson r."""
        bounds = np.cumsum((0, *runs))
        raw = np.zeros((self.n_lesions, bounds[-1]), dtype=np.float64)
        for u0 in range(0, self.rows.size, chunk):
            rows = self.rows[u0:u0 + chunk]
            ids = self.row_labels[u0:u0 + chunk]
            x = data[rows].astype(np.float32, copy=False)  # (U_chunk, T_total); a copy (fancy indexing)
            first = int(ids[0])
            n_local = int(ids[-1]) - first + 1  # ids are sorted: the chunk spans labels first..ids[-1]
            if n_local <= self.MAX_GEMM_LABELS:
                local = np.zeros((n_local, rows.size), dtype=np.float32)
                columns = np.arange(rows.size)
                for run in range(len(runs)):
                    segment = slice(bounds[run], bounds[run + 1])
                    local[ids - first, columns] = norms[run, rows]
                    raw[first:first + n_local, segment] += local @ x[:, segment]
                continue
            for run in range(len(runs)):
                segment = slice(bounds[run], bounds[run + 1])
                x[:, segment] *= norms[run, rows][:, np.newaxis]
            starts = np.flatnonzero(np.concatenate(([True], ids[1:] != ids[:-1])))
            raw[ids[starts]] += np.add.reduceat(x, starts, axis=0, dtype=np.float64)
        return _normalized_seeds(raw, runs)


def _seed_correlation_fz(seed_matrix, n_runs):
    """(L, L) float64 atanh of the mean over runs of the Pearson r between seed time courses, r clipped to +/-RMAX.

    ``seed_matrix`` is S from a seeds ``matrix()``: per run unit-norm segments divided by nRuns, so
    ``nRuns * S^T S`` is the mean over runs of the per-run r (0 for a run in which a seed is constant).
    """
    s = np.asarray(seed_matrix, dtype=np.float64)
    r = (s.T @ s) * n_runs
    np.clip(r, -RMAX, RMAX, out=r)
    return np.arctanh(r)


def _normalized_seeds(raw, runs):
    """Demean and unit-normalize raw seed time courses (L, T_total) within each run; return (T_total, L) / nRuns."""
    bounds = np.cumsum((0, *runs))
    seeds = np.zeros((bounds[-1], raw.shape[0]), dtype=np.float64)
    for run in range(len(runs)):
        segment = slice(bounds[run], bounds[run + 1])
        s = raw[:, segment] - raw[:, segment].mean(axis=1, keepdims=True)  # (L, T_r)
        norm = np.linalg.norm(s, axis=1)
        valid = norm > 0
        seeds[segment, valid] = (s[valid] / norm[valid, np.newaxis]).T / len(runs)
    return seeds.astype(np.float32)


def iter_correlation_chunks(data, seeds, chunk=DEFAULT_CHUNK):
    """Yield (v0, v1, r) with r = data[v0:v1] @ seeds clipped to +/-RMAX, chunk by chunk.

    float16 data is upcast to float32 one chunk at a time. The yielded array is reused: copy it
    if it must outlive the iteration step.
    """
    n_voxels, n_times = data.shape
    n_columns = seeds.shape[1]
    chunk = max(1, min(int(chunk), n_voxels))
    upcast = data.dtype != np.float32
    x_buffer = np.empty(chunk * n_times, dtype=np.float32) if upcast else None
    r_buffer = np.empty(chunk * n_columns, dtype=np.float32)
    for v0 in range(0, n_voxels, chunk):
        v1 = min(n_voxels, v0 + chunk)
        if upcast:
            x = x_buffer[: (v1 - v0) * n_times].reshape(v1 - v0, n_times)
            np.copyto(x, data[v0:v1])
        else:
            x = data[v0:v1]
        r = r_buffer[: (v1 - v0) * n_columns].reshape(v1 - v0, n_columns)
        np.matmul(x, seeds, out=r)
        np.clip(r, -RMAX, RMAX, out=r)
        yield v0, v1, r


def subject_correlation(data, norms, runs, weights, chunk=DEFAULT_CHUNK, min_weight=MIN_WEIGHT):
    """Correlation maps of one subject.

    :param data: (V, T_total) bundle time series; norms: (nRuns, V); runs: run lengths.
    :param weights: (L, V) or (V,) lesion weights over the bundle voxels.
    :return: (V, L) float32 mean-over-runs Pearson r (clipped to +/-RMAX).
    """
    seeds = _Seeds(weights, min_weight)
    matrix = seeds.matrix(data, norms, runs, chunk)
    out = np.empty((data.shape[0], seeds.n_lesions), dtype=np.float32)
    for v0, v1, r in iter_correlation_chunks(data, matrix, chunk):
        out[v0:v1] = r
    return out


class _Accumulator:
    """Streaming per-voxel statistics over subjects.

    ``mode="welford"`` (default): float64 Welford accumulation of Fz, so that the mean r, mean Fz and
    T maps are valid after any number of subjects. Memory: three (V, L) float64 arrays, i.e.
    24 * V * L bytes (about 5.5 MB per lesion at 230k voxels), plus 12 * V * L bytes for the float32
    maps returned by :meth:`maps`.

    ``mode="mean"``: only a float32 sum of Fz (and of r with ``keep_r``), 4 * V * L bytes each; the
    maps have no T. For passes that need the mean Fz only (e.g. hundreds of parcel seeds).

    ``split_half``: also a float64 sum of Fz over the odd-numbered subjects (1st, 3rd, ...),
    8 * V * L bytes; the even half is the total minus the odd half.

    Null seeds use ``dtype=np.float32`` and ``keep_r=False`` (only :meth:`t_map` and :meth:`fz_map`
    are needed for them), which halves the memory and the per-subject cost.
    """

    def __init__(self, n_voxels, n_lesions, chunk, dtype=np.float64, keep_r=True, mode="welford", split_half=False):
        if mode not in ACCUMULATE_MODES:
            raise ValueError(f"Unknown accumulation mode {mode!r}; use one of {', '.join(ACCUMULATE_MODES)}")
        self.n = 0
        self.mode = mode
        self.dtype = np.dtype(dtype) if mode == "welford" else np.dtype(np.float32)
        self.sum_r = np.zeros((n_voxels, n_lesions), self.dtype) if keep_r else None
        size = min(chunk, n_voxels) * n_lesions
        self._z = np.empty(size, dtype=np.float32)
        if mode == "welford":
            self.mean_z = np.zeros((n_voxels, n_lesions), self.dtype)
            self.m2 = np.zeros((n_voxels, n_lesions), self.dtype)
            self._delta = np.empty(size, self.dtype)
            self._tmp = np.empty(size, self.dtype)
            self.sum_z = None
        else:
            self.mean_z = self.m2 = None
            self.sum_z = np.zeros((n_voxels, n_lesions), np.float32)
        self.odd_z = np.zeros((n_voxels, n_lesions), np.float64) if split_half else None
        self._final = False

    @property
    def n_odd(self):
        return (self.n + 1) // 2

    def add_subject(self, data, seed_matrix, chunk):
        if self._final:
            raise RuntimeError("The maps of this accumulator were finalized")
        self.n += 1
        odd = self.odd_z is not None and self.n % 2 == 1
        n_lesions = seed_matrix.shape[1]
        if self.mode == "mean":
            for v0, v1, r in iter_correlation_chunks(data, seed_matrix, chunk):
                z = self._z[:(v1 - v0) * n_lesions].reshape(r.shape)
                np.arctanh(r, out=z)
                if self.sum_r is not None:
                    self.sum_r[v0:v1] += r
                self.sum_z[v0:v1] += z
                if odd:
                    self.odd_z[v0:v1] += z
            return
        inv_n = self.dtype.type(1.0 / self.n)
        for v0, v1, r in iter_correlation_chunks(data, seed_matrix, chunk):
            size = (v1 - v0) * n_lesions
            z = self._z[:size].reshape(r.shape)
            delta = self._delta[:size].reshape(r.shape)
            tmp = self._tmp[:size].reshape(r.shape)
            np.arctanh(r, out=z)
            if self.sum_r is not None:
                self.sum_r[v0:v1] += r
            if odd:
                self.odd_z[v0:v1] += z
            mean = self.mean_z[v0:v1]
            np.subtract(z, mean, out=delta)
            np.multiply(delta, inv_n, out=tmp)
            mean += tmp
            np.subtract(z, mean, out=tmp)
            tmp *= delta
            self.m2[v0:v1] += tmp

    def maps(self, subject_ids, cancelled=False, elapsed=0.0, final=False):
        """Current maps. T is 0 with fewer than 2 subjects and where Fz has zero variance over the
        subjects (e.g. a seed voxel whose r is clipped to RMAX in every subject).

        :param final: mean mode only: scale the sums in place and return transposed views of them
          (no copies; the accumulator cannot be used afterwards). The (L, V) maps are then not
          C-contiguous.
        """
        as_rows = lambda a: np.ascontiguousarray(a.T, dtype=np.float32)
        n = self.n
        if self.mode == "mean":
            scale = np.float32(1.0 / max(n, 1))
            if final:
                self._final = True
                self.sum_z *= scale
                avg_fz = self.sum_z.T
                avg_r = None
                if self.sum_r is not None:
                    self.sum_r *= scale
                    avg_r = self.sum_r.T
            else:
                avg_fz = as_rows(self.sum_z * scale)
                avg_r = None if self.sum_r is None else as_rows(self.sum_r * scale)
            maps = NetworkMaps(avg_r, avg_fz, None, subject_ids, cancelled, elapsed)
        else:
            avg_r = self.sum_r / max(n, 1)
            t = np.zeros_like(self.mean_z)
            if n >= 2:
                standard_error = np.sqrt(self.m2 / (n - 1) / n)
                np.divide(self.mean_z, standard_error, out=t, where=standard_error > 0)
            maps = NetworkMaps(as_rows(avg_r), as_rows(self.mean_z), as_rows(t), subject_ids, cancelled, elapsed)
        if self.odd_z is not None:
            maps.fz_odd, maps.fz_even, maps.n_odd, maps.n_even = self._split_half(maps.avg_fz)
        return maps

    def _split_half(self, avg_fz):
        """(odd mean Fz, even mean Fz, n_odd, n_even): (L, V) float32 maps (zeros for an empty half)."""
        n_odd = self.n_odd
        n_even = self.n - n_odd
        n_lesions = self.odd_z.shape[1]
        odd = np.zeros((n_lesions, self.odd_z.shape[0]), np.float32)
        even = np.zeros_like(odd)
        for index in range(n_lesions):
            odd_sum = self.odd_z[:, index]
            # Total sum of Fz: n times the mean (float64, before any rounding of the returned maps)
            total = self.mean_z[:, index] * self.n if self.mode == "welford" else np.asarray(avg_fz[index], np.float64) * self.n
            if n_odd:
                odd[index] = odd_sum / n_odd
            if n_even:
                even[index] = (total - odd_sum) / n_even
        return odd, even, n_odd, n_even

    def split_sums(self):
        """(sum of Fz over all subjects, sum over the odd-numbered subjects): (V, L) float64 (split-half accumulators only)."""
        if self.odd_z is None:
            raise ValueError("This accumulator has no split-half sums")
        total = self.mean_z * self.n if self.mode == "welford" else self.sum_z.astype(np.float64)
        return total, self.odd_z.copy()

    def t_map(self):
        """Current T maps only, (L, V) float32 (0 with fewer than 2 subjects or zero variance)."""
        t = np.zeros_like(self.mean_z)
        if self.n >= 2:
            standard_error = np.sqrt(self.m2 / self.dtype.type((self.n - 1) * self.n))
            np.divide(self.mean_z, standard_error, out=t, where=standard_error > 0)
        return np.ascontiguousarray(t.T, dtype=np.float32)

    def fz_map(self):
        """Current mean Fz maps only, (L, V) float32."""
        if self.mode == "welford":
            return np.ascontiguousarray(self.mean_z.T, dtype=np.float32)
        return np.ascontiguousarray((self.sum_z * np.float32(1.0 / max(self.n, 1))).T, dtype=np.float32)


def resolve_subjects(bundle, subjects=None, random_subset=False, seed=0):
    """Subject ids from None (all), an int (first n, or n at random with random_subset) or a list of ids."""
    if subjects is None or isinstance(subjects, (int, np.integer)):
        return bundle.select_subjects(None if subjects is None else int(subjects), random=random_subset, seed=seed)
    return list(subjects)


def compute_network_maps(
    bundle,
    weights,
    subjects=None,
    random_subset=False,
    seed=0,
    chunk=DEFAULT_CHUNK,
    progress=None,
    cancel=None,
    snapshot_every=0,
    snapshot=None,
    prefetch=True,
    min_weight=MIN_WEIGHT,
    null_weights=None,
    null_voxels=None,
    labels=None,
    n_labels=None,
    accumulate="welford",
    keep_r=True,
    split_half=False,
    null_fz=False,
):
    """Compute group network maps for a batch of lesions in one streaming pass over the subjects.

    :param bundle: :class:`ConnectomeBundle`.
    :param weights: (L, V) lesion weights over the bundle voxels (fractional occupancy);
      weights <= min_weight are ignored. A lesion without voxels gives all-zero maps.
      None when ``labels`` gives the seeds.
    :param subjects: None (all), an int (first n, or random n with ``random_subset`` and ``seed``)
      or a list of subject ids.
    :param progress: optional callback(done_subjects, total_subjects, elapsed_s).
    :param cancel: optional threading.Event or callable returning True to stop; checked between
      subjects. The maps of the subjects processed so far are returned with ``cancelled=True``.
    :param snapshot_every: if > 0, call ``snapshot(maps)`` every this many subjects (not after the last).
    :param null_weights: optional null seeds (e.g. relocated lesions, see ``qa.relocated_lesions``)
      mapped in the same pass as extra seed columns: a (K, V) weight matrix, or a list of K sparse
      (voxel positions, weights) pairs. Only their T maps are kept (``NetworkMaps.null_t``), and their
      mean Fz maps with ``null_fz`` (``NetworkMaps.null_fz``); snapshots do not include them.
    :param null_voxels: optional sorted indices into the V voxels at which the null maps are
      computed (see :func:`null_voxel_sample`); default: all voxels.
    :param labels: optional (V,) integer parcel label of each voxel (0..n_labels-1; negative: no
      parcel) instead of ``weights``: each parcel is a seed (sum of its voxels, see
      :class:`_LabelSeeds`), and ``NetworkMaps.c_fz`` is the (L, L) mean over subjects of atanh(r)
      between the parcel time courses (r clipped to +/-RMAX).
    :param n_labels: number of parcel seeds (default: max label + 1).
    :param accumulate: "welford" (mean r, mean Fz and T) or "mean" (float32 sums: mean Fz, and mean r
      with ``keep_r``; no T). See :class:`_Accumulator`.
    :param keep_r: also compute the mean r with the "mean" accumulator.
    :param split_half: also compute the mean Fz over the odd- and even-numbered subjects
      (``NetworkMaps.fz_odd``, ``fz_even``; see :func:`split_half_correlation`).
    :param null_fz: also return the null seeds' mean Fz maps.
    :return: :class:`NetworkMaps`; ``cancelled`` is also True if the iteration stopped early because
      the bundle was closed. T is 0 where Fz has zero variance over the subjects.

    Memory grows with the number of lesions: about 36 * V * L bytes with the "welford" accumulator
    (8 MB per lesion, 830 MB for 100 lesions at 230k voxels), so split large cohorts into batches of
    at most ~100 lesions; 4 * V * L bytes per map kept with the "mean" accumulator (plus 8 * V * L
    with ``split_half``). Null seeds cost 36 * V' * K bytes over their V' voxels.
    """
    chunk = max(1, int(chunk))
    if labels is not None:
        if weights is not None:
            raise ValueError("Give lesion weights or parcel labels, not both")
        seeds = _LabelSeeds(labels, n_labels)
    else:
        seeds = _Seeds(weights, min_weight)
    if seeds.n_voxels != bundle.n_voxels:
        raise ValueError(f"{'Labels' if labels is not None else 'Lesion weights'} have {seeds.n_voxels} voxels, the connectome has {bundle.n_voxels}")
    ids = resolve_subjects(bundle, subjects, random_subset, seed)
    accumulator = _Accumulator(bundle.n_voxels, seeds.n_lesions, chunk, keep_r=keep_r or accumulate == "welford", mode=accumulate, split_half=split_half)
    c_sum = np.zeros((seeds.n_lesions, seeds.n_lesions), np.float64) if labels is not None else None
    nulls = null_rows = null_accumulator = None
    if isinstance(null_weights, (list, tuple)) and null_weights:
        nulls = _SparseSeeds(null_weights, bundle.n_voxels, min_weight)
    elif null_weights is not None and not isinstance(null_weights, (list, tuple)) and np.size(null_weights):
        nulls = _Seeds(null_weights, min_weight)
    if nulls is not None:
        if nulls.n_voxels != bundle.n_voxels:
            raise ValueError(f"Null weights have {nulls.n_voxels} voxels, the connectome has {bundle.n_voxels}")
        if null_voxels is not None:
            null_rows = np.asarray(null_voxels, dtype=np.int64)
            if null_rows.ndim != 1 or (null_rows.size and (null_rows.min() < 0 or null_rows.max() >= bundle.n_voxels or np.any(np.diff(null_rows) <= 0))):
                raise ValueError("null_voxels must be sorted, unique indices of connectome voxels")
        null_accumulator = _Accumulator(bundle.n_voxels if null_rows is None else null_rows.size, nulls.n_lesions, chunk, dtype=np.float32, keep_r=False)
    processed = []
    cancelled = False
    start = time.perf_counter()
    iterator = bundle.iter_subjects(ids, prefetch=prefetch, reuse_buffers=True)
    try:
        for subject in iterator:
            if _is_cancelled(cancel):
                cancelled = True
                break
            seed_matrix = seeds.matrix(subject.data, subject.norms, subject.runs, chunk)
            accumulator.add_subject(subject.data, seed_matrix, chunk)
            if c_sum is not None:
                c_sum += _seed_correlation_fz(seed_matrix, len(subject.runs))
            if nulls is not None:
                null_data = subject.data if null_rows is None else subject.data[null_rows]
                null_accumulator.add_subject(null_data, nulls.matrix(subject.data, subject.norms, subject.runs, chunk), chunk)
            processed.append(subject.id)
            elapsed = time.perf_counter() - start
            if progress:
                progress(len(processed), len(ids), elapsed)
            if snapshot and snapshot_every and len(processed) % snapshot_every == 0 and len(processed) < len(ids):
                snapshot(accumulator.maps(processed, elapsed=elapsed))
    finally:
        iterator.close()
    # The bundle may have been closed (from another thread) while iterating: never report partial maps as complete
    cancelled = cancelled or len(processed) < len(ids)
    maps = accumulator.maps(processed, cancelled, time.perf_counter() - start, final=accumulate == "mean")
    if c_sum is not None:
        maps.c_fz = c_sum / max(len(processed), 1)
    if nulls is not None:
        maps.null_t = null_accumulator.t_map()
        maps.null_voxels = null_rows
        if null_fz:
            maps.null_fz = null_accumulator.fz_map()
    return maps


def split_half_correlation(maps):
    """Spatial Pearson r between the odd- and even-half mean Fz maps of each lesion (connectome sampling stability).

    :return: list of L values (None where a half is empty or a map is constant), or None without split-half maps.
    """
    if maps.fz_odd is None or maps.fz_even is None:
        return None
    values = []
    for odd, even in zip(maps.fz_odd, maps.fz_even, strict=True):
        a, b = np.asarray(odd, np.float64), np.asarray(even, np.float64)
        finite = np.isfinite(a) & np.isfinite(b)
        if not (maps.n_odd and maps.n_even and finite.any()):
            values.append(None)
            continue
        a = a[finite] - a[finite].mean()
        b = b[finite] - b[finite].mean()
        denominator = np.sqrt((a @ a) * (b @ b))
        values.append(float(a @ b / denominator) if denominator > 0 else None)
    return values


def null_voxel_sample(n_voxels, max_voxels=NULL_MAX_VOXELS, seed=0):
    """Sorted random sample of at most ``max_voxels`` voxel indices (all voxels when n_voxels <= max_voxels
    or max_voxels <= 0), for computing null maps where only their spatial correlations are needed.
    """
    if max_voxels is None or max_voxels <= 0 or n_voxels <= max_voxels:
        return None
    return np.sort(np.random.default_rng(seed).choice(n_voxels, size=int(max_voxels), replace=False))


def benchmark(V=230_000, T=240, L=10, n_subjects=3, lesion_voxels=300, n_runs=2, chunk=DEFAULT_CHUNK, seed=0, accumulate="welford", parcels=0):
    """Time the per-subject engine cost on random in-memory data (no disk I/O).

    :param accumulate: accumulator mode ("welford" or "mean", see :class:`_Accumulator`).
    :param parcels: if > 0, map this many parcel seeds that tile all V voxels (label seeds, as a
      parcel run does) instead of L lesions of ``lesion_voxels`` voxels.
    :return: dict with the best and mean per-subject time in seconds and the seed-extraction time.
    """
    rng = np.random.default_rng(seed)
    runs = tuple(T // n_runs + (1 if r < T % n_runs else 0) for r in range(n_runs))
    data = np.empty((V, T), dtype=np.float16)
    for v0 in range(0, V, 16384):
        block = rng.standard_normal((min(V, v0 + 16384) - v0, T), dtype=np.float32)
        start = 0
        for length in runs:
            segment = block[:, start:start + length]
            segment -= segment.mean(axis=1, keepdims=True)
            segment /= np.linalg.norm(segment, axis=1, keepdims=True)
            start += length
        data[v0:v0 + len(block)] = block
    norms = np.ones((n_runs, V), dtype=np.float32)
    if parcels > 0:
        L = int(parcels)
        seeds = _LabelSeeds(rng.integers(0, L, size=V), L)
    else:
        weights = np.zeros((L, V), dtype=np.float32)
        for lesion in range(L):
            weights[lesion, rng.choice(V, size=lesion_voxels, replace=False)] = 1.0
        seeds = _Seeds(weights)
    accumulator = _Accumulator(V, L, chunk, keep_r=accumulate == "welford", mode=accumulate)
    times, seed_times = [], []
    for _ in range(n_subjects):
        t0 = time.perf_counter()
        matrix = seeds.matrix(data, norms, runs, chunk)
        t1 = time.perf_counter()
        accumulator.add_subject(data, matrix, chunk)
        times.append(time.perf_counter() - t0)
        seed_times.append(t1 - t0)
    return {
        "V": V,
        "T": T,
        "L": L,
        "chunk": chunk,
        "accumulate": accumulate,
        "parcels": bool(parcels),
        "blas_threads": os.environ.get("OPENBLAS_NUM_THREADS", "default"),
        "per_subject_best_s": min(times),
        "per_subject_mean_s": float(np.mean(times)),
        "seed_extraction_s": min(seed_times),
    }


# Command-line interface


_BIDS_NAME = re.compile(r"^sub-[A-Za-z0-9]+(?:_[A-Za-z0-9-]+)+$")
_BIDS_ENTITY = re.compile(r"^([A-Za-z0-9]+)-[A-Za-z0-9]+$")
_BIDS_LABEL = re.compile(r"^sub-[A-Za-z0-9]+(?:_[A-Za-z0-9]+-[A-Za-z0-9]+)*$")  # a lesion output name made of entities only
# Entities of a lesion file name that describe the lesion image itself (its grid, label and
# description), not the source acquisition; outputs are on the connectome grid with their own desc.
_LESION_IMAGE_ENTITIES = ("space", "res", "den", "label", "desc")


def connectome_space(bundle):
    """Template space of a bundle: the space of its grid when it is a known template grid, else the space its
    connectome.json declares ("space", e.g. the synthetic demo's), else None.
    """
    try:
        detected = detect_space((tuple(bundle.shape), bundle.affine))
    except (TypeError, ValueError):
        detected = None
    if detected is not None:
        return detected[0]
    declared = (getattr(bundle, "metadata", None) or {}).get("space")
    return str(declared) if declared else None


def _lesion_spaces(paths, given, bundle):
    """[{"space", "space_notice"}] of the lesion files: the --lesion-space values (one per lesion or one for all),
    else the space of each file's grid when it is a known template grid (None otherwise), with the template-family
    notice against the connectome's space ("" when the families match or a space is not known).
    """
    target = connectome_space(bundle)
    out = []
    for index, path in enumerate(paths):
        if given:
            space = given[index] if len(given) > 1 else given[0]
        else:
            try:
                detected = detect_space(path)
            except (OSError, TypeError, ValueError):
                detected = None
            space = detected[0] if detected is not None else None
        space = str(space) if space else None
        out.append({"space": space, "space_notice": family_notice(space, target)})
    return out


def _lesion_name(path):
    """Output name of a lesion file: its name without extension or, for BIDS-style names, its
    key-value entities except the lesion-image entities space, res, den, label and desc
    (sub-01_ses-2_run-1_space-X_desc-lesion_mask.nii.gz gives "sub-01_ses-2_run-1").
    """
    name = os.path.basename(os.fspath(path))
    for ext in (".nii.gz", ".nii"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    if not _BIDS_NAME.match(name):
        return name
    entities = (_BIDS_ENTITY.match(part) for part in name.split("_"))
    return "_".join(entity.group(0) for entity in entities if entity and entity.group(1) not in _LESION_IMAGE_ENTITIES)


def _unique_name(name, used):
    """``name``, or ``name`` with a counter when it is in ``used``: "lesion-2" or, for BIDS-style
    names, an extra entity that keeps the subject label valid ("sub-01_lesion-2", not "sub-01-2").
    """
    pattern = "{}_lesion-{}" if _BIDS_LABEL.match(name) else "{}-{}"
    unique, counter = name, 2
    while unique in used:
        unique, counter = pattern.format(name, counter), counter + 1
    return unique


def connectome_label(bundle_name):
    """BIDS-style label of a connectome (letters and digits of its name), used in "conn-<label>"."""
    return re.sub(r"[^A-Za-z0-9]", "", bundle_name) or "connectome"


def subset_suffix(subset_name):
    """The "desc" suffix of a run on a named subject subset: "Subset" + the letters and digits of its name."""
    return "Subset" + re.sub(r"[^A-Za-z0-9]", "", subset_name)


def output_paths(out_dir, lesion_name, bundle_name, desc_suffix=""):
    """Output file names (Lead-DBS style) for one lesion: {result key: path}.

    :param desc_suffix: appended to the "desc" label, e.g. "PreviewN100" for a preview (subset) run.
    """
    label = connectome_label(bundle_name)
    return {key: os.path.join(out_dir, f"{lesion_name}_conn-{label}_desc-{desc}{desc_suffix}_funcmap.nii.gz") for key, desc in OUTPUT_KEYS}


def null_output_path(out_dir, lesion_name, bundle_name, desc_suffix=""):
    """File name of the optional 4D T maps of a lesion's relocated-lesion null (one volume per relocation)."""
    return os.path.join(out_dir, f"{lesion_name}_conn-{connectome_label(bundle_name)}_desc-{NULL_DESC}{desc_suffix}_funcmap.nii.gz")


def split_half_paths(out_dir, lesion_name, bundle_name, desc_suffix=""):
    """File names of the split-half mean Fz maps of one lesion: {"FzOdd": path, "FzEven": path}."""
    label = connectome_label(bundle_name)
    return {key: os.path.join(out_dir, f"{lesion_name}_conn-{label}_desc-{desc}{desc_suffix}_funcmap.nii.gz") for key, desc in SPLIT_HALF_KEYS}


def write_maps(maps, lesion_names, bundle, out_dir, desc_suffix=""):
    """Write AvgR, AvgRFz and T NIfTI maps (exact bundle affine) for every lesion; return the paths.

    Maps that ``maps`` does not hold (T with the "mean" accumulator) are not written.
    """
    written = []
    note = f" {desc_suffix}" if desc_suffix else ""
    available = maps.available()
    for index, lesion_name in enumerate(lesion_names):
        for key, path in output_paths(out_dir, lesion_name, bundle.name, desc_suffix).items():
            if key not in available:
                continue
            intent = {"intent_code": 3, "intent_params": (max(maps.n - 1, 0), 0.0, 0.0)} if key == "T" else {}
            write_nifti(path, maps.volume(index, key, bundle), bundle.affine, descrip=f"LNM {key} N={maps.n}{note}", **intent)
            written.append(path)
    return written


def null_fz_output_path(out_dir, lesion_name, bundle_name, desc_suffix=""):
    """File name of the optional 4D mean Fz maps of a lesion's relocated-lesion null (one volume per relocation)."""
    return os.path.join(out_dir, f"{lesion_name}_conn-{connectome_label(bundle_name)}_desc-{NULL_FZ_DESC}{desc_suffix}_funcmap.nii.gz")


def write_null_maps(maps, lesion_names, null_counts, bundle, out_dir, desc_suffix=""):
    """Write the null T maps of each lesion as one 4D NIfTI (needs null maps on all voxels); return the paths."""
    if maps.null_t is None or maps.null_voxels is not None:
        raise ValueError("Null maps on all connectome voxels are needed to write them")
    written = []
    start = 0
    for lesion_name, count in zip(lesion_names, null_counts, strict=True):
        if count:
            volumes = unmask(maps.null_t[start:start + count], bundle)  # (k, ni, nj, nk)
            path = null_output_path(out_dir, lesion_name, bundle.name, desc_suffix)
            write_nifti(path, np.moveaxis(volumes, 0, -1), bundle.affine, descrip=f"LNM relocated-lesion T N={maps.n}", intent_code=3, intent_params=(max(maps.n - 1, 0), 0.0, 0.0))
            written.append(path)
        start += count
    return written


def write_null_fz_maps(maps, lesion_names, null_counts, bundle, out_dir, desc_suffix=""):
    """Write the null mean Fz maps of each lesion as one 4D NIfTI (``compute_network_maps(null_fz=True)``
    on all voxels), e.g. for the exact pooled null of cohort analyses; return the paths.
    """
    if maps.null_fz is None or maps.null_voxels is not None:
        raise ValueError("Null mean Fz maps on all connectome voxels are needed to write them")
    written = []
    start = 0
    for lesion_name, count in zip(lesion_names, null_counts, strict=True):
        if count:
            volumes = unmask(maps.null_fz[start:start + count], bundle)  # (k, ni, nj, nk)
            path = null_fz_output_path(out_dir, lesion_name, bundle.name, desc_suffix)
            write_nifti(path, np.moveaxis(volumes, 0, -1), bundle.affine, descrip=f"LNM relocated-lesion AvgFz N={maps.n}")
            written.append(path)
        start += count
    return written


def write_split_half_maps(maps, lesion_names, bundle, out_dir, desc_suffix=""):
    """Write the odd- and even-half mean Fz maps (desc-AvgFzOddHalf, desc-AvgFzEvenHalf) of every lesion; return the paths."""
    if maps.fz_odd is None or maps.fz_even is None:
        raise ValueError("The maps have no split-half maps (compute_network_maps(split_half=True))")
    written = []
    note = f" {desc_suffix}" if desc_suffix else ""
    for index, lesion_name in enumerate(lesion_names):
        paths = split_half_paths(out_dir, lesion_name, bundle.name, desc_suffix)
        for key, half, n in (("FzOdd", maps.fz_odd, maps.n_odd), ("FzEven", maps.fz_even, maps.n_even)):
            write_nifti(paths[key], unmask(half[index], bundle), bundle.affine, descrip=f"LNM AvgFz {key[2:].lower()} half N={n}{note}")
            written.append(paths[key])
    return written


def _brain_mask_vector(path, bundle):
    """Boolean vector over the bundle voxels: voxels at least half covered by a brain mask image (any grid)."""
    data, affine, _ = read_nifti(path)
    data = np.asarray(data).reshape(data.shape[:3]) != 0
    coverage = resample_to_grid(data.astype(np.float64), affine, bundle.grid, mode="fraction")
    vector = to_masked_vector(coverage, bundle.mask_indices) >= 0.5
    if not vector.any():
        raise ValueError(f"The brain mask {path} does not overlap the connectome mask")
    return vector


def parcel_labels(source, bundle):
    """Parcel labels of a label image (any grid) on the bundle voxels, for label seeds.

    The image is resampled to the bundle grid by nearest neighbour; 0 is background. Every
    non-zero label value with at least one voxel in the connectome mask becomes a seed, in
    increasing label order.

    :param source: NIfTI path, or a (data, affine) tuple with data indexed [i, j, k].
    :return: dict with "labels" ((V,) int32 seed index per voxel, -1 outside every parcel), "values"
      (label value of each seed), "voxels" (mask voxels per seed), "missing" (label values of the
      image without a voxel in the mask) and "coverage" (fraction of the mask voxels in a parcel).
    :raise ValueError: for a 4D image, non-integer, negative or non-finite values, or labels that
      do not overlap the connectome mask.
    """
    if isinstance(source, (str, os.PathLike)):
        data, affine, _ = read_nifti(source)
    else:
        data, affine = source
    data = np.asarray(data)
    if data.ndim > 3:
        if any(n != 1 for n in data.shape[3:]):
            raise ValueError(f"A parcellation must be a 3D label image, got shape {data.shape}")
        data = data.reshape(data.shape[:3])
    values = data.astype(np.float64, copy=False)
    if not np.all(np.isfinite(values)):
        raise ValueError("The parcellation has non-finite values; a label image holds non-negative integers")
    if np.any(values < 0) or np.any(np.abs(values - np.round(values)) > 1e-3):
        raise ValueError("The parcellation has negative or non-integer values; a label image holds non-negative integers")
    image_labels = np.unique(np.round(values).astype(np.int64))
    image_labels = image_labels[image_labels != 0]
    on_grid = resample_to_grid(values, affine, bundle.grid, mode="nearest")
    vector = np.round(to_masked_vector(on_grid, bundle.mask_indices)).astype(np.int64)
    present, inverse, counts = np.unique(vector, return_inverse=True, return_counts=True)
    keep = present != 0
    if not keep.any():
        raise ValueError("The parcellation does not overlap the connectome mask")
    index_of = np.full(present.size, -1, dtype=np.int64)
    index_of[keep] = np.arange(int(keep.sum()))
    labels = index_of[inverse.reshape(-1)].astype(np.int32)
    seed_values = present[keep]
    return {
        "labels": labels,
        "values": [int(value) for value in seed_values],
        "voxels": [int(count) for count in counts[keep]],
        "missing": [int(value) for value in np.setdiff1d(image_labels, seed_values)],
        "coverage": float(np.count_nonzero(labels >= 0) / max(labels.size, 1)),
    }


def _save_npy_atomic(path, array):
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "wb") as f:
            np.save(f, array)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def write_parcel_maps(maps, parcels, bundle, out_dir, source=None):
    """Write the outputs of a parcel run: parcel_fz.f16.npy, parcel_C_fz.npy and parcel_labels.json; return the paths.

    :param maps: :class:`NetworkMaps` of ``compute_network_maps(labels=parcels["labels"], ...)``.
    :param parcels: the dict of :func:`parcel_labels`.
    :param source: optional {"file": name, "sha256": ...} of the label image (recorded; no absolute path).
    """
    from .export import file_sha256, now_iso, subjects_sha256, write_json_atomic

    n_parcels = len(parcels["values"])
    if maps.avg_fz.shape != (n_parcels, bundle.n_voxels) or maps.c_fz is None:
        raise ValueError("The maps are not the parcel maps of these labels")
    fz_path = os.path.join(out_dir, PARCEL_FZ_NAME)
    c_path = os.path.join(out_dir, PARCEL_C_NAME)
    labels_path = os.path.join(out_dir, PARCEL_LABELS_NAME)
    if os.path.exists(labels_path):  # parcel_labels.json describes the files next to it: remove it before they change
        os.remove(labels_path)
    fz = np.empty((n_parcels, bundle.n_voxels), dtype=np.float16)
    for v0 in range(0, bundle.n_voxels, DEFAULT_CHUNK * 4):  # row-major float16 without a float32 copy of all rows
        fz[:, v0:v0 + DEFAULT_CHUNK * 4] = maps.avg_fz[:, v0:v0 + DEFAULT_CHUNK * 4]
    _save_npy_atomic(fz_path, fz)
    _save_npy_atomic(c_path, np.ascontiguousarray(maps.c_fz, dtype=np.float64))
    write_json_atomic(labels_path, {
        "format": PARCEL_FORMAT,
        "version": 1,
        "bundle": {"name": bundle.name, "manifest_sha256": file_sha256(os.path.join(bundle.path, "connectome.json")), "n_voxels": int(bundle.n_voxels)},
        "n": maps.n,
        "subject_ids_sha256": subjects_sha256(maps.subject_ids),
        "source": source,
        "resampling": "nearest",
        "n_parcels": n_parcels,
        "labels": parcels["values"],
        "voxels": parcels["voxels"],
        "missing": parcels["missing"],
        "coverage": parcels["coverage"],
        "rmax": RMAX,
        "files": {
            "fz": {"name": PARCEL_FZ_NAME, "shape": [n_parcels, int(bundle.n_voxels)], "dtype": "float16", "sha256": file_sha256(fz_path),
                   "definition": "row i: mean over subjects of atanh(r) between parcel i's seed (sum of its voxels) and each connectome voxel; r is the mean over runs of the Pearson r, clipped to +/-RMAX"},
            "c_fz": {"name": PARCEL_C_NAME, "shape": [n_parcels, n_parcels], "dtype": "float64", "sha256": file_sha256(c_path),
                     "definition": "mean over subjects of atanh(r) between the parcel seed time courses; r is the mean over runs of the Pearson r, clipped to +/-RMAX"},
        },
        "created": now_iso(),
    })
    return [fz_path, c_path, labels_path]


def _load_reference(bundle, subject_ids):
    """(Reference or None, qa.json "reference" record or None): the reference maps of the run's subject list, else the bundle's default ones.

    Never builds reference maps (decision D-A20).
    """
    from . import qa as qa_lib
    from .export import subjects_sha256

    run_sha = subjects_sha256(subject_ids)
    reference = qa_lib.load_reference(bundle, subjects_sha=run_sha)
    if reference is None:
        reference = qa_lib.load_reference(bundle)
    if reference is None:
        return None, None
    return reference, {"definition": reference.definition, "sha256": reference.sha256, "subjects_sha256": reference.subjects_sha256, "run_subjects": reference.subjects_sha256 == run_sha}


def _recorded_argv(argv):
    """The arguments recorded in run.json: all but --cancel-file (it controls this process, not the result)."""
    out, skip = [], False
    for token in argv:
        if skip:
            skip = False
        elif token == "--cancel-file":
            skip = True
        elif not token.startswith("--cancel-file="):
            out.append(token)
    return out


def _progress_callback():
    def on_progress(done, total, elapsed):
        eta = elapsed / done * (total - done) if done else 0.0
        jobs.emit("progress", done=done, total=total, elapsed=round(elapsed, 3), eta=round(eta, 3))

    return on_progress


def _parcel_run(args, bundle, ids, run, cancel, started):
    """The parcel run of :func:`main` (--parcels): label seeds, mean accumulator, parcel_fz/parcel_C_fz/parcel_labels outputs."""
    from .export import file_sha256, now_iso, subjects_sha256, write_json_atomic

    cancel.raise_if_cancelled(done=0)
    parcels = parcel_labels(args.parcels, bundle)
    source = {"file": os.path.basename(args.parcels), "sha256": file_sha256(args.parcels)}
    n_parcels = len(parcels["values"])
    disagreement = xform_disagreement(read_nifti_header(args.parcels))
    if disagreement > XFORM_WARNING_MM:
        jobs.emit("warning", code="xform", message=f"The sform and qform of the parcellation disagree by up to {disagreement:.2g} mm; the parcels use the sform (as FSL does).")
    if parcels["missing"]:
        missing = parcels["missing"]
        jobs.emit("warning", code="parcels_outside", message=f"{len(missing)} label(s) of the parcellation have no voxel in the connectome mask and are left out: {', '.join(map(str, missing[:20]))}{' ...' if len(missing) > 20 else ''}")
    summary = {"n": n_parcels, "coverage": parcels["coverage"], "missing": parcels["missing"]}
    if run is not None:
        run["parcels"] = {**source, **summary}
        run["accumulate"] = "mean"
        write_json_atomic(args.run_json, run)
    jobs.emit("start", job="engine", bundle=bundle.name, lesions=[], parcels=n_parcels, subjects=len(ids), threads=args.threads, qc=[], null_relocations=[], pid=os.getpid())
    maps = compute_network_maps(bundle, None, subjects=ids, chunk=args.chunk, progress=_progress_callback(), cancel=cancel, labels=parcels["labels"], n_labels=n_parcels, accumulate="mean", keep_r=False)
    if maps.cancelled:
        # Only complete parcel maps are written; run.json keeps status "running"
        if run is not None:
            run["stop_reason"] = cancel.reason or "stopped"
            write_json_atomic(args.run_json, run)
        raise jobs.Cancelled(done=maps.n, reason=cancel.reason or "stopped")
    outputs = write_parcel_maps(maps, parcels, bundle, args.out, source)
    done = {"outputs": outputs, "n": maps.n, "elapsed": round(maps.elapsed, 3), "parcels": summary}
    if run is not None:
        run.update({
            "status": "done",
            "n": maps.n,
            "cancelled": False,
            "subject_ids_sha256": subjects_sha256(maps.subject_ids),
            "parcel_outputs": [os.path.abspath(p) for p in outputs],
            "end": now_iso(),
            "elapsed": round(time.perf_counter() - started, 3),
            "engine_elapsed": round(maps.elapsed, 3),
        })
        write_json_atomic(args.run_json, run)
        done["run_json"] = os.path.abspath(args.run_json)
    jobs.emit("done", **done)
    return 0


def main(argv=None):
    """Command-line entry point; returns the process exit code."""
    # No abbreviated options: run.json records the arguments as given, and reproduce.sh rewrites them by their full names
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.engine", description="Compute lesion network maps (or parcel seed maps) from a connectome bundle.")
    parser.add_argument("--bundle", required=True, help="connectome bundle directory (.lnmc)")
    parser.add_argument("--lesion", action="append", default=None, help="lesion NIfTI file (any grid; repeat for several lesions); required unless --parcels is given")
    parser.add_argument("--lesion-space", action="append", default=None, metavar="SPACE", help="template space of the lesion (e.g. MNI152NLin6Asym; one per --lesion in order, or one for all; default: the space of the lesion file's grid when it is a known template grid), recorded with its template-family notice")
    parser.add_argument("--parcels", default=None, metavar="LABELS", help="parcel run instead of lesions: map every label of this label image (any grid; nearest-neighbour resampling, 0 is background) as a seed and write parcel_fz.f16.npy, parcel_C_fz.npy and parcel_labels.json")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--subjects", type=int, default=None, help="use only N subjects (default: all)")
    parser.add_argument("--random", action="store_true", help="choose the N subjects at random")
    parser.add_argument("--seed", type=int, default=0, help="random seed for --random")
    parser.add_argument("--subject-list", default=None, metavar="SPEC", help="use these subjects: a named subset of the bundle, a text file of subject ids or a comma-separated list of ids")
    parser.add_argument("--threads", type=int, default=2, help="BLAS threads (at least 1; default: 2)")
    parser.add_argument("--cancel-file", default=None, help="stop (exit code 3, \"cancelled\" event) as soon as this file exists; not recorded in run.json")
    parser.add_argument("--chunk", type=int, default=DEFAULT_CHUNK, help="voxels per matrix product")
    parser.add_argument("--supersample", type=int, default=2, help="minimum sub-samples per axis when resampling lesions on oblique grids")
    parser.add_argument("--probabilistic", action="store_true", help="use lesion image values as weights instead of binarizing")
    parser.add_argument("--snapshot-every", type=int, default=0, help="write partial maps every K subjects")
    parser.add_argument("--accumulate", choices=ACCUMULATE_MODES, default=None, help="welford (default for lesions): AvgR, AvgRFz and T maps; mean (float32 sums, less memory and time; always used by --parcels): AvgR and AvgRFz only")
    parser.add_argument("--split-half", action="store_true", help="also write the mean Fz maps of the odd- and even-numbered subjects (desc-AvgFzOddHalf, desc-AvgFzEvenHalf) and report their spatial r (connectome sampling stability)")
    parser.add_argument("--null-relocations", type=int, default=0, help="specificity QA: map K rigid relocations of each lesion in the same pass (default: 0, off)")
    parser.add_argument("--null-seed", type=int, default=0, help="random seed of the lesion relocations")
    parser.add_argument("--null-voxels", type=int, default=NULL_MAX_VOXELS, help=f"compute the null maps on a random sample of this many voxels (0: all voxels; default: {NULL_MAX_VOXELS})")
    parser.add_argument("--brain-mask", default=None, help="brain mask NIfTI (any grid) that relocated lesions must stay in (default: the connectome mask)")
    parser.add_argument("--write-null", action="store_true", help="also write the null T and mean Fz maps (4D desc-RelocatedT and desc-RelocatedFz, one volume per relocation; computed on all voxels; Slicer 5.13+ loads them as a sequence)")
    parser.add_argument("--desc-suffix", default="", help="appended to the output desc labels, e.g. PreviewN100 for a preview run (letters and digits)")
    parser.add_argument("--threshold-rule", default=None, help="threshold rule JSON, recorded in the run JSON (maps are written unthresholded)")
    parser.add_argument("--run-json", default=None, help="write the run configuration and provenance (arguments, subjects hash, seeds, versions, times) to this file")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    if not re.fullmatch(r"[A-Za-z0-9]*", args.desc_suffix):
        parser.error("--desc-suffix must contain only letters and digits")
    if args.threads < 1:
        parser.error("--threads must be at least 1")
    if args.null_relocations < 0:
        parser.error("--null-relocations must be at least 0")
    if args.null_voxels < 0:
        parser.error("--null-voxels must be at least 0")
    if args.subject_list is not None and (args.subjects is not None or args.random):
        parser.error("--subject-list cannot be combined with --subjects or --random")
    if (args.lesion is None) == (args.parcels is None):
        parser.error("give one or more --lesion files, or --parcels (not both)")
    if args.lesion_space is not None and (args.lesion is None or len(args.lesion_space) not in (1, len(args.lesion))):
        parser.error("give one --lesion-space per --lesion, or one for all lesions")
    if args.parcels is not None:
        for option, given in (("--null-relocations", args.null_relocations > 0), ("--write-null", args.write_null), ("--split-half", args.split_half), ("--snapshot-every", args.snapshot_every > 0), ("--accumulate welford", args.accumulate == "welford")):
            if given:
                parser.error(f"{option} cannot be combined with --parcels")
    accumulate = args.accumulate or ("mean" if args.parcels is not None else "welford")
    rule = None
    if args.threshold_rule is not None:
        from .thresholds import normalize_rule

        try:
            rule = normalize_rule(args.threshold_rule)
        except ValueError as exc:
            parser.error(f"--threshold-rule: {exc}")
    run = None
    try:
        from . import qa as qa_lib
        from .export import file_sha256, now_iso, run_record, subjects_sha256, write_json_atomic

        started = time.perf_counter()
        # Set by --cancel-file, SIGTERM/SIGINT or the orphan watchdog when run as a module (jobs.run_job)
        cancel = jobs.CancelToken(args.cancel_file, signals=False)
        bundle = ConnectomeBundle.open(args.bundle)
        if bundle.kind != "timeseries":
            raise ValueError(f"The connectome {bundle.name!r} is a {bundle.kind!r} bundle; network maps need a time series connectome")
        subject_list = None
        if args.subject_list is not None:
            subject_list = bundle.resolve_subject_list(args.subject_list)
            ids = subject_list.ids
            if subject_list.name and not args.desc_suffix:
                args.desc_suffix = subset_suffix(subject_list.name)
        else:
            ids = resolve_subjects(bundle, args.subjects, args.random, args.seed)
        os.makedirs(args.out, exist_ok=True)
        if args.run_json:
            run = run_record(_recorded_argv(raw_argv), bundle, ids, args, rule, subject_list)
            write_json_atomic(args.run_json, run)
        if args.parcels is not None:
            return _parcel_run(args, bundle, ids, run, cancel, started)
        names, weights, qc = [], [], []
        spaces = _lesion_spaces(args.lesion, args.lesion_space, bundle)
        for path in args.lesion:
            cancel.raise_if_cancelled(done=0)
            lesion_weights_vector, lesion_qc_values = prepare_lesion(path, bundle, supersample=args.supersample, binarize=not args.probabilistic)
            name = _unique_name(_lesion_name(path), names)
            names.append(name)
            weights.append(lesion_weights_vector)
            qc.append({"lesion": name, **lesion_qc_values})
            if lesion_qc_values["voxels_in_mask"] == 0:
                jobs.emit("warning", lesion=name, message="The lesion does not overlap the connectome mask; its maps are all zero.")
            disagreement = xform_disagreement(read_nifti_header(path))
            if disagreement > XFORM_WARNING_MM:
                jobs.emit(
                    "warning",
                    lesion=name,
                    message=f"The sform and qform of the lesion file disagree by up to {disagreement:.2g} mm; the maps use the sform (as FSL does), while Slicer may display the file with the qform. Check that the lesion lies where it was drawn.",
                )

        # Relocated-lesion null: K rigid translations of each lesion, extra seed columns of the same pass
        null_columns, null_counts, null_voxels = [], [0] * len(names), None
        brain_mask = None
        if args.null_relocations > 0:
            brain_mask = _brain_mask_vector(args.brain_mask, bundle) if args.brain_mask else None
            for index, (name, lesion_weights_vector) in enumerate(zip(names, weights, strict=True)):
                relocated = qa_lib.relocated_lesions(lesion_weights_vector, bundle, brain_mask=brain_mask, k=args.null_relocations, seed=args.null_seed, sparse=True)
                null_counts[index] = len(relocated)
                null_columns.extend(relocated)
                if len(relocated) < args.null_relocations and qc[index]["voxels_in_mask"] > 0:
                    jobs.emit("warning", lesion=name, message=f"Only {len(relocated)} of {args.null_relocations} relocated lesions fit inside the brain mask.")
            if not args.write_null:
                null_voxels = null_voxel_sample(bundle.n_voxels, args.null_voxels, seed=args.null_seed)
        null_settings = {
            "k": args.null_relocations,
            "seed": args.null_seed,
            "min_inside": qa_lib.MIN_INSIDE,
            "counts": dict(zip(names, null_counts, strict=True)),
            "voxels": bundle.n_voxels if null_voxels is None else len(null_voxels),
            "brain_mask": os.path.abspath(args.brain_mask) if args.brain_mask else None,
        }
        map_keys = [key for key, _ in OUTPUT_KEYS if accumulate == "welford" or key != "T"]
        if run is not None:
            # Recorded before the computation so that a stopped run still documents its inputs
            run["lesions"] = [
                {"name": name, "file": os.path.abspath(path), "sha256": file_sha256(path), **{key: value for key, value in values.items() if key != "lesion"}, **space}
                for name, path, values, space in zip(names, args.lesion, qc, spaces, strict=True)
            ]
            run["maps"] = {name: {key: os.path.abspath(p) for key, p in output_paths(args.out, name, bundle.name, args.desc_suffix).items() if key in map_keys} for name in names}
            run["null"] = null_settings
            if accumulate != "welford":
                run["accumulate"] = accumulate
            if args.split_half:
                run["split_half"] = {"label": SPLIT_HALF_LABEL}
            write_json_atomic(args.run_json, run)
        jobs.emit("start", job="engine", bundle=bundle.name, lesions=names, subjects=len(ids), threads=args.threads, qc=qc, null_relocations=null_counts, pid=os.getpid())

        def on_snapshot(partial):
            outputs = write_maps(partial, names, bundle, args.out, args.desc_suffix)
            if run is not None:
                run["snapshot_n"] = partial.n
                write_json_atomic(args.run_json, run)
            jobs.emit("snapshot", n=partial.n, outputs=outputs)

        maps = compute_network_maps(
            bundle,
            np.stack(weights),
            subjects=ids,
            chunk=args.chunk,
            progress=_progress_callback(),
            snapshot_every=args.snapshot_every,
            snapshot=on_snapshot,
            null_weights=null_columns or None,
            null_voxels=null_voxels,
            cancel=cancel,
            accumulate=accumulate,
            split_half=args.split_half,
            null_fz=bool(null_columns) and (args.write_null or accumulate == "mean"),
        )
        if maps.cancelled and cancel.cancelled():
            # Stopped: the snapshots already written stay valid; run.json keeps status "running" (a partial run)
            if run is not None:
                run["stop_reason"] = cancel.reason
                write_json_atomic(args.run_json, run)
            raise jobs.Cancelled(done=maps.n, reason=cancel.reason)
        outputs = write_maps(maps, names, bundle, args.out, args.desc_suffix)
        null_outputs = write_null_maps(maps, names, null_counts, bundle, args.out, args.desc_suffix) if args.write_null and null_columns else []
        null_fz_outputs = write_null_fz_maps(maps, names, null_counts, bundle, args.out, args.desc_suffix) if args.write_null and null_columns else []
        split_half = None
        if args.split_half:
            split_r = split_half_correlation(maps)
            split_half = {
                "label": SPLIT_HALF_LABEL,
                "n_odd": maps.n_odd,
                "n_even": maps.n_even,
                "r": dict(zip(names, split_r, strict=True)),
                "outputs": write_split_half_maps(maps, names, bundle, args.out, args.desc_suffix),
            }

        # QA: degree similarity (when the bundle has a degree map), the relocated-lesion null summary and
        # the reference similarity (when the bundle has valid reference maps)
        try:
            degree = bundle.degree
        except (OSError, ValueError) as exc:
            degree = None
            jobs.emit("warning", message=f"The connectome degree map cannot be read ({exc}); degree similarity is not computed.")
        try:
            reference, reference_info = _load_reference(bundle, maps.subject_ids)
        except Exception as exc:  # never fail a run for its reference QA
            reference, reference_info = None, None
            jobs.emit("warning", code="reference", message=f"The reference maps cannot be read ({type(exc).__name__}: {exc}); the reference QA is not computed.")
        statistic, null_maps = (maps.t, maps.null_t) if accumulate == "welford" else (maps.avg_fz, maps.null_fz)
        qa_values = {}
        start = 0
        for index, name in enumerate(names):
            count = null_counts[index]
            lesion_nulls = null_maps[start:start + count] if count else None
            try:
                qa_values[name] = qa_lib.lesion_qa(statistic[index], degree, lesion_nulls, maps.null_voxels, reference=reference)
            except ValueError as exc:
                if reference is None:
                    raise
                jobs.emit("warning", code="reference", lesion=name, message=f"The reference QA of this lesion is not available ({exc}).")
                qa_values[name] = qa_lib.lesion_qa(statistic[index], degree, lesion_nulls, maps.null_voxels)
            start += count
        for name, space in zip(names, spaces, strict=True):
            # The template space of the lesion and its family notice are QA values (sidecar and qa.json)
            qa_values[name] = {**qa_values[name], "lesion_space": space["space"], "space_notice": space["space_notice"]}
        qa_path = os.path.join(args.out, "qa.json")
        degree_info = qa_lib.read_degree_metadata(bundle.path) if degree is not None else None
        qa_record = {
            "format": "slicer-lnm-qa",
            "version": 1,
            "bundle": bundle.name,
            "n": maps.n,
            "statistic": "T" if accumulate == "welford" else "AvgFz",
            "degree": {"available": degree is not None, **(degree_info or {})},
            "null": null_settings,
            "lesions": qa_values,
        }
        if reference_info is not None:
            qa_record["reference"] = reference_info
        if split_half is not None:
            qa_record["split_half"] = {key: split_half[key] for key in ("label", "n_odd", "n_even", "r")}
        write_json_atomic(qa_path, qa_record)
        done = {"outputs": outputs, "n": maps.n, "elapsed": round(maps.elapsed, 3), "qc": qc, "qa": qa_values, "qa_json": qa_path}
        if null_outputs:
            done["null_outputs"] = null_outputs
        if null_fz_outputs:
            done["null_fz_outputs"] = null_fz_outputs
        if split_half is not None:
            done["split_half"] = split_half
        if reference_info is not None:
            done["reference"] = reference_info
        if run is not None:
            run.update({
                "status": "done",
                "n": maps.n,
                "cancelled": maps.cancelled,
                "subject_ids_sha256": subjects_sha256(maps.subject_ids),
                "null_outputs": [os.path.abspath(p) for p in null_outputs],
                "qa_json": os.path.abspath(qa_path),
                "end": now_iso(),
                "elapsed": round(time.perf_counter() - started, 3),
                "engine_elapsed": round(maps.elapsed, 3),
            })
            if null_fz_outputs:
                run["null_fz_outputs"] = [os.path.abspath(p) for p in null_fz_outputs]
            if split_half is not None:
                run["split_half"] = {**{key: split_half[key] for key in ("label", "n_odd", "n_even", "r")}, "outputs": [os.path.abspath(p) for p in split_half["outputs"]]}
            write_json_atomic(args.run_json, run)
            done["run_json"] = os.path.abspath(args.run_json)
        jobs.emit("done", **done)
        return 0
    except jobs.Cancelled:
        raise
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        if run is not None:
            try:
                run.update({"status": "error", "error": message, "end": now_iso()})
                write_json_atomic(args.run_json, run)
            except Exception:
                pass
        jobs.emit("error", code="exception", message=message)
        return 1


if __name__ == "__main__":
    # Orphan watchdog and SIGTERM/SIGINT as cancellation requests; "cancelled" event and exit code 3 when stopped
    jobs.run_job(main, job="engine")

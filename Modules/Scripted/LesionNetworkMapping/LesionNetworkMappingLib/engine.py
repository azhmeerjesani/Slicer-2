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

Command line (prints one JSON object per line: start, progress, snapshot, warning, done or error events)::

    python -m LesionNetworkMappingLib.engine --bundle B.lnmc --lesion lesion1.nii.gz [--lesion ...]
        --out DIR [--subjects N [--random] [--seed S] | --subject-list SPEC] [--threads K] [--snapshot-every K]
        [--null-relocations K [--null-seed S] [--brain-mask MASK] [--null-voxels N] [--write-null]]
        [--desc-suffix PreviewN100] [--threshold-rule JSON] [--run-json run.json]

--subject-list takes a named subset of the bundle (e.g. "First 100 Subjects"), a text file of subject
ids or a comma-separated list of ids; a named subset labels the outputs "Subset<Name>" unless
--desc-suffix is given. The "start" event carries the process id ("pid"). Run as a module, the
engine stops (exit code 3, "cancelled" event) on SIGTERM/SIGINT or when the process that started it
is gone (the PythonSlicer launcher does not pass SIGTERM on), see jobs.run_job.

After the maps, the specificity QA (see qa.py) runs: degree similarity when the bundle has a degree
map, and the relocated-lesion null when --null-relocations is given (the relocated lesions are extra
seed columns of the same pass). It is reported in the "done" event ("qa") and in <out>/qa.json.
"""

import os
import sys

_THREAD_ENV_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS")


def _set_blas_threads(argv, default=2):
    """Set BLAS thread-count environment variables from a --threads argument.

    Must run before numpy is imported: BLAS libraries read these variables when they are loaded.
    """
    threads = str(default)
    for index, arg in enumerate(argv):
        if arg == "--threads" and index + 1 < len(argv):
            threads = argv[index + 1]
        elif arg.startswith("--threads="):
            threads = arg.split("=", 1)[1]
    if threads.isdigit() and int(threads) > 0:
        for name in _THREAD_ENV_VARS:
            os.environ[name] = threads


if __name__ == "__main__":
    _set_blas_threads(sys.argv[1:])

import argparse
import json
import re
import time

import numpy as np

from . import jobs
from .connectome import ConnectomeBundle, _is_cancelled
from .geometry import from_masked_vector, resample_to_grid, to_masked_vector
from .nifti import XFORM_WARNING_MM, read_nifti, read_nifti_header, write_nifti, xform_disagreement

RMAX = 0.999999  # r is clipped to +/-RMAX before the Fisher transform
DEFAULT_CHUNK = 8192
MIN_WEIGHT = 1e-4  # lesion weights at or below this value are ignored (as in Lead-DBS)
OUTPUT_KEYS = (("AvgR", "AvgR"), ("AvgFz", "AvgRFz"), ("T", "T"))  # result key, Lead-DBS "desc" label
NULL_DESC = "RelocatedT"  # "desc" label of the optional 4D null T maps
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
    V mask voxels; None means all V voxels). Otherwise both are None.
    """

    def __init__(self, avg_r, avg_fz, t, subject_ids, cancelled=False, elapsed=0.0, null_t=None, null_voxels=None):
        self.avg_r = avg_r
        self.avg_fz = avg_fz
        self.t = t
        self.subject_ids = list(subject_ids)
        self.cancelled = cancelled
        self.elapsed = elapsed
        self.null_t = null_t
        self.null_voxels = null_voxels

    @property
    def n(self):
        return len(self.subject_ids)

    def __len__(self):
        return self.avg_r.shape[0]

    def __getitem__(self, index):
        return {"AvgR": self.avg_r[index], "AvgFz": self.avg_fz[index], "T": self.t[index], "N": self.n}

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
      mask; weights trimmed by min_weight do not count as outside) and volume_mm3.
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
    }


def _clean_fraction(value, eps=1e-9):
    """Clamp a fraction to [0, 1], with values within eps of 0 or 1 (rounding noise) snapped to them."""
    value = min(1.0, max(0.0, float(value)))
    return 0.0 if value < eps else 1.0 if value > 1.0 - eps else value


def prepare_lesion(source, bundle, supersample=2, binarize=True, min_weight=MIN_WEIGHT):
    """Resample a lesion image onto the bundle grid as fractional occupancy and return (weights, qc).

    :param source: NIfTI path, or a (data, affine) tuple with data indexed [i, j, k].
    :param binarize: treat every non-zero voxel as lesion (so label values do not matter);
      set False for probabilistic seeds.
    """
    if isinstance(source, (str, os.PathLike)):
        data, affine, _ = read_nifti(source)
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
    """Streaming per-voxel statistics over subjects (float64 Welford for Fz).

    Memory: three (V, L) float64 arrays, i.e. 24 * V * L bytes (about 5.5 MB per lesion at
    230k voxels), plus 12 * V * L bytes for the float32 maps returned by :meth:`maps`.

    Null seeds use ``dtype=np.float32`` and ``keep_r=False`` (only :meth:`t_map` is needed for their
    spatial correlations), which halves the memory and the per-subject cost.
    """

    def __init__(self, n_voxels, n_lesions, chunk, dtype=np.float64, keep_r=True):
        self.n = 0
        self.dtype = np.dtype(dtype)
        self.sum_r = np.zeros((n_voxels, n_lesions), self.dtype) if keep_r else None
        self.mean_z = np.zeros((n_voxels, n_lesions), self.dtype)
        self.m2 = np.zeros((n_voxels, n_lesions), self.dtype)
        size = min(chunk, n_voxels) * n_lesions
        self._z = np.empty(size, dtype=np.float32)
        self._delta = np.empty(size, self.dtype)
        self._tmp = np.empty(size, self.dtype)

    def add_subject(self, data, seed_matrix, chunk):
        self.n += 1
        inv_n = self.dtype.type(1.0 / self.n)
        n_lesions = seed_matrix.shape[1]
        for v0, v1, r in iter_correlation_chunks(data, seed_matrix, chunk):
            size = (v1 - v0) * n_lesions
            z = self._z[:size].reshape(r.shape)
            delta = self._delta[:size].reshape(r.shape)
            tmp = self._tmp[:size].reshape(r.shape)
            np.arctanh(r, out=z)
            if self.sum_r is not None:
                self.sum_r[v0:v1] += r
            mean = self.mean_z[v0:v1]
            np.subtract(z, mean, out=delta)
            np.multiply(delta, inv_n, out=tmp)
            mean += tmp
            np.subtract(z, mean, out=tmp)
            tmp *= delta
            self.m2[v0:v1] += tmp

    def maps(self, subject_ids, cancelled=False, elapsed=0.0):
        """Current maps. T is 0 with fewer than 2 subjects and where Fz has zero variance over the
        subjects (e.g. a seed voxel whose r is clipped to RMAX in every subject).
        """
        n = self.n
        avg_r = self.sum_r / max(n, 1)
        t = np.zeros_like(self.mean_z)
        if n >= 2:
            standard_error = np.sqrt(self.m2 / (n - 1) / n)
            np.divide(self.mean_z, standard_error, out=t, where=standard_error > 0)
        as_rows = lambda a: np.ascontiguousarray(a.T, dtype=np.float32)
        return NetworkMaps(as_rows(avg_r), as_rows(self.mean_z), as_rows(t), subject_ids, cancelled, elapsed)

    def t_map(self):
        """Current T maps only, (L, V) float32 (0 with fewer than 2 subjects or zero variance)."""
        t = np.zeros_like(self.mean_z)
        if self.n >= 2:
            standard_error = np.sqrt(self.m2 / self.dtype.type((self.n - 1) * self.n))
            np.divide(self.mean_z, standard_error, out=t, where=standard_error > 0)
        return np.ascontiguousarray(t.T, dtype=np.float32)


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
):
    """Compute group network maps for a batch of lesions in one streaming pass over the subjects.

    :param bundle: :class:`ConnectomeBundle`.
    :param weights: (L, V) lesion weights over the bundle voxels (fractional occupancy);
      weights <= min_weight are ignored. A lesion without voxels gives all-zero maps.
    :param subjects: None (all), an int (first n, or random n with ``random_subset`` and ``seed``)
      or a list of subject ids.
    :param progress: optional callback(done_subjects, total_subjects, elapsed_s).
    :param cancel: optional threading.Event or callable returning True to stop; checked between
      subjects. The maps of the subjects processed so far are returned with ``cancelled=True``.
    :param snapshot_every: if > 0, call ``snapshot(maps)`` every this many subjects (not after the last).
    :param null_weights: optional null seeds (e.g. relocated lesions, see ``qa.relocated_lesions``)
      mapped in the same pass as extra seed columns: a (K, V) weight matrix, or a list of K sparse
      (voxel positions, weights) pairs. Only their T maps are kept (``NetworkMaps.null_t``);
      snapshots do not include them.
    :param null_voxels: optional sorted indices into the V voxels at which the null maps are
      computed (see :func:`null_voxel_sample`); default: all voxels.
    :return: :class:`NetworkMaps`; ``cancelled`` is also True if the iteration stopped early because
      the bundle was closed. T is 0 where Fz has zero variance over the subjects.

    Memory grows with the number of lesions: about 36 * V * L bytes (8 MB per lesion, 830 MB for
    100 lesions at 230k voxels), so split large cohorts into batches of at most ~100 lesions.
    Null seeds cost 36 * V' * K bytes over their V' voxels.
    """
    chunk = max(1, int(chunk))
    seeds = _Seeds(weights, min_weight)
    if seeds.n_voxels != bundle.n_voxels:
        raise ValueError(f"Lesion weights have {seeds.n_voxels} voxels, the connectome has {bundle.n_voxels}")
    ids = resolve_subjects(bundle, subjects, random_subset, seed)
    accumulator = _Accumulator(bundle.n_voxels, seeds.n_lesions, chunk)
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
            accumulator.add_subject(subject.data, seeds.matrix(subject.data, subject.norms, subject.runs, chunk), chunk)
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
    maps = accumulator.maps(processed, cancelled, time.perf_counter() - start)
    if nulls is not None:
        maps.null_t = null_accumulator.t_map()
        maps.null_voxels = null_rows
    return maps


def null_voxel_sample(n_voxels, max_voxels=NULL_MAX_VOXELS, seed=0):
    """Sorted random sample of at most ``max_voxels`` voxel indices (all voxels when n_voxels <= max_voxels
    or max_voxels <= 0), for computing null maps where only their spatial correlations are needed.
    """
    if max_voxels is None or max_voxels <= 0 or n_voxels <= max_voxels:
        return None
    return np.sort(np.random.default_rng(seed).choice(n_voxels, size=int(max_voxels), replace=False))


def benchmark(V=230_000, T=240, L=10, n_subjects=3, lesion_voxels=300, n_runs=2, chunk=DEFAULT_CHUNK, seed=0):
    """Time the per-subject engine cost on random in-memory data (no disk I/O).

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
    weights = np.zeros((L, V), dtype=np.float32)
    for lesion in range(L):
        weights[lesion, rng.choice(V, size=lesion_voxels, replace=False)] = 1.0
    seeds = _Seeds(weights)
    accumulator = _Accumulator(V, L, chunk)
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
        "blas_threads": os.environ.get("OPENBLAS_NUM_THREADS", "default"),
        "per_subject_best_s": min(times),
        "per_subject_mean_s": float(np.mean(times)),
        "seed_extraction_s": min(seed_times),
    }


# Command-line interface


def _emit(event):
    print(json.dumps(event), flush=True)


_BIDS_NAME = re.compile(r"^sub-[A-Za-z0-9]+(?:_[A-Za-z0-9-]+)+$")
_BIDS_ENTITY = re.compile(r"^([A-Za-z0-9]+)-[A-Za-z0-9]+$")
_BIDS_LABEL = re.compile(r"^sub-[A-Za-z0-9]+(?:_[A-Za-z0-9]+-[A-Za-z0-9]+)*$")  # a lesion output name made of entities only
# Entities of a lesion file name that describe the lesion image itself (its grid, label and
# description), not the source acquisition; outputs are on the connectome grid with their own desc.
_LESION_IMAGE_ENTITIES = ("space", "res", "den", "label", "desc")


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


def write_maps(maps, lesion_names, bundle, out_dir, desc_suffix=""):
    """Write AvgR, AvgRFz and T NIfTI maps (exact bundle affine) for every lesion; return the paths."""
    written = []
    note = f" {desc_suffix}" if desc_suffix else ""
    for index, lesion_name in enumerate(lesion_names):
        for key, path in output_paths(out_dir, lesion_name, bundle.name, desc_suffix).items():
            intent = {"intent_code": 3, "intent_params": (max(maps.n - 1, 0), 0.0, 0.0)} if key == "T" else {}
            write_nifti(path, maps.volume(index, key, bundle), bundle.affine, descrip=f"LNM {key} N={maps.n}{note}", **intent)
            written.append(path)
    return written


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


def _brain_mask_vector(path, bundle):
    """Boolean vector over the bundle voxels: voxels at least half covered by a brain mask image (any grid)."""
    data, affine, _ = read_nifti(path)
    data = np.asarray(data).reshape(data.shape[:3]) != 0
    coverage = resample_to_grid(data.astype(np.float64), affine, bundle.grid, mode="fraction")
    vector = to_masked_vector(coverage, bundle.mask_indices) >= 0.5
    if not vector.any():
        raise ValueError(f"The brain mask {path} does not overlap the connectome mask")
    return vector


class _ArgumentParser(argparse.ArgumentParser):
    """Argument parser that also reports usage errors as a JSON error event (exit code 2)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        _emit({"event": "error", "message": f"Invalid arguments: {message}"})
        self.exit(2)


def main(argv=None):
    """Command-line entry point; returns the process exit code."""
    # No abbreviated options: run.json records the arguments as given, and reproduce.sh rewrites them by their full names
    parser = _ArgumentParser(prog="python -m LesionNetworkMappingLib.engine", description="Compute lesion network maps from a connectome bundle.", allow_abbrev=False)
    parser.add_argument("--bundle", required=True, help="connectome bundle directory (.lnmc)")
    parser.add_argument("--lesion", required=True, action="append", help="lesion NIfTI file (any grid; repeat for several lesions)")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--subjects", type=int, default=None, help="use only N subjects (default: all)")
    parser.add_argument("--random", action="store_true", help="choose the N subjects at random")
    parser.add_argument("--seed", type=int, default=0, help="random seed for --random")
    parser.add_argument("--subject-list", default=None, metavar="SPEC", help="use these subjects: a named subset of the bundle, a text file of subject ids or a comma-separated list of ids")
    parser.add_argument("--threads", type=int, default=2, help="BLAS threads (default: 2)")
    parser.add_argument("--chunk", type=int, default=DEFAULT_CHUNK, help="voxels per matrix product")
    parser.add_argument("--supersample", type=int, default=2, help="minimum sub-samples per axis when resampling lesions on oblique grids")
    parser.add_argument("--probabilistic", action="store_true", help="use lesion image values as weights instead of binarizing")
    parser.add_argument("--snapshot-every", type=int, default=0, help="write partial maps every K subjects")
    parser.add_argument("--null-relocations", type=int, default=0, help="specificity QA: map K rigid relocations of each lesion in the same pass (default: 0, off)")
    parser.add_argument("--null-seed", type=int, default=0, help="random seed of the lesion relocations")
    parser.add_argument("--null-voxels", type=int, default=NULL_MAX_VOXELS, help=f"compute the null maps on a random sample of this many voxels (0: all voxels; default: {NULL_MAX_VOXELS})")
    parser.add_argument("--brain-mask", default=None, help="brain mask NIfTI (any grid) that relocated lesions must stay in (default: the connectome mask)")
    parser.add_argument("--write-null", action="store_true", help="also write the null T maps (4D, one volume per relocation; computed on all voxels; Slicer 5.13+ loads them as a sequence)")
    parser.add_argument("--desc-suffix", default="", help="appended to the output desc labels, e.g. PreviewN100 for a preview run (letters and digits)")
    parser.add_argument("--threshold-rule", default=None, help="threshold rule JSON, recorded in the run JSON (maps are written unthresholded)")
    parser.add_argument("--run-json", default=None, help="write the run configuration and provenance (arguments, subjects hash, seeds, versions, times) to this file")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    if not re.fullmatch(r"[A-Za-z0-9]*", args.desc_suffix):
        parser.error("--desc-suffix must contain only letters and digits")
    if args.null_relocations < 0:
        parser.error("--null-relocations must be at least 0")
    if args.null_voxels < 0:
        parser.error("--null-voxels must be at least 0")
    if args.subject_list is not None and (args.subjects is not None or args.random):
        parser.error("--subject-list cannot be combined with --subjects or --random")
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
        # Set by SIGTERM/SIGINT or the orphan watchdog when run as a module (jobs.run_job)
        cancel = jobs.CancelToken(signals=False)
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
            run = run_record(raw_argv, bundle, ids, args, rule, subject_list)
            write_json_atomic(args.run_json, run)
        names, weights, qc = [], [], []
        for path in args.lesion:
            cancel.raise_if_cancelled(done=0)
            lesion_weights_vector, lesion_qc_values = prepare_lesion(path, bundle, supersample=args.supersample, binarize=not args.probabilistic)
            name = _unique_name(_lesion_name(path), names)
            names.append(name)
            weights.append(lesion_weights_vector)
            qc.append({"lesion": name, **lesion_qc_values})
            if lesion_qc_values["voxels_in_mask"] == 0:
                _emit({"event": "warning", "lesion": name, "message": "The lesion does not overlap the connectome mask; its maps are all zero."})
            disagreement = xform_disagreement(read_nifti_header(path))
            if disagreement > XFORM_WARNING_MM:
                _emit({
                    "event": "warning",
                    "lesion": name,
                    "message": f"The sform and qform of the lesion file disagree by up to {disagreement:.2g} mm; the maps use the sform (as FSL does), while Slicer may display the file with the qform. Check that the lesion lies where it was drawn.",
                })

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
                    _emit({"event": "warning", "lesion": name, "message": f"Only {len(relocated)} of {args.null_relocations} relocated lesions fit inside the brain mask."})
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
        if run is not None:
            # Recorded before the computation so that a stopped run still documents its inputs
            run["lesions"] = [
                {"name": name, "file": os.path.abspath(path), "sha256": file_sha256(path), **{key: value for key, value in values.items() if key != "lesion"}}
                for name, path, values in zip(names, args.lesion, qc, strict=True)
            ]
            run["maps"] = {name: {key: os.path.abspath(p) for key, p in output_paths(args.out, name, bundle.name, args.desc_suffix).items()} for name in names}
            run["null"] = null_settings
            write_json_atomic(args.run_json, run)
        _emit({"event": "start", "bundle": bundle.name, "lesions": names, "subjects": len(ids), "threads": args.threads, "qc": qc, "null_relocations": null_counts, "pid": os.getpid()})

        def on_progress(done, total, elapsed):
            eta = elapsed / done * (total - done) if done else 0.0
            _emit({"event": "progress", "done": done, "total": total, "elapsed": round(elapsed, 3), "eta": round(eta, 3)})

        def on_snapshot(partial):
            outputs = write_maps(partial, names, bundle, args.out, args.desc_suffix)
            if run is not None:
                run["snapshot_n"] = partial.n
                write_json_atomic(args.run_json, run)
            _emit({"event": "snapshot", "n": partial.n, "outputs": outputs})

        maps = compute_network_maps(
            bundle,
            np.stack(weights),
            subjects=ids,
            chunk=args.chunk,
            progress=on_progress,
            snapshot_every=args.snapshot_every,
            snapshot=on_snapshot,
            null_weights=null_columns or None,
            null_voxels=null_voxels,
            cancel=cancel,
        )
        if maps.cancelled and cancel.cancelled():
            # Stopped: the snapshots already written stay valid; run.json keeps status "running" (a partial run)
            if run is not None:
                run["stop_reason"] = cancel.reason
                write_json_atomic(args.run_json, run)
            raise jobs.Cancelled(done=maps.n, reason=cancel.reason)
        outputs = write_maps(maps, names, bundle, args.out, args.desc_suffix)
        null_outputs = write_null_maps(maps, names, null_counts, bundle, args.out, args.desc_suffix) if args.write_null and null_columns else []

        # QA: degree similarity (when the bundle has a degree map) and the relocated-lesion null summary
        try:
            degree = bundle.degree
        except (OSError, ValueError) as exc:
            degree = None
            _emit({"event": "warning", "message": f"The connectome degree map cannot be read ({exc}); degree similarity is not computed."})
        qa_values = {}
        start = 0
        for index, name in enumerate(names):
            count = null_counts[index]
            lesion_nulls = maps.null_t[start:start + count] if count else None
            qa_values[name] = qa_lib.lesion_qa(maps.t[index], degree, lesion_nulls, maps.null_voxels)
            start += count
        qa_path = os.path.join(args.out, "qa.json")
        degree_info = qa_lib.read_degree_metadata(bundle.path) if degree is not None else None
        write_json_atomic(qa_path, {
            "format": "slicer-lnm-qa",
            "version": 1,
            "bundle": bundle.name,
            "n": maps.n,
            "statistic": "T",
            "degree": {"available": degree is not None, **(degree_info or {})},
            "null": null_settings,
            "lesions": qa_values,
        })
        done = {"event": "done", "outputs": outputs, "n": maps.n, "elapsed": round(maps.elapsed, 3), "qc": qc, "qa": qa_values, "qa_json": qa_path}
        if null_outputs:
            done["null_outputs"] = null_outputs
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
            write_json_atomic(args.run_json, run)
            done["run_json"] = os.path.abspath(args.run_json)
        _emit(done)
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
        _emit({"event": "error", "message": message})
        return 1


if __name__ == "__main__":
    # Orphan watchdog and SIGTERM/SIGINT as cancellation requests; "cancelled" event and exit code 3 when stopped
    jobs.run_job(main, job="engine")

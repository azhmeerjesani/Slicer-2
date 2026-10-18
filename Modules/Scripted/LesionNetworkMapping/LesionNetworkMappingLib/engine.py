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

Command line (prints one JSON object per line: progress, snapshot, warning, done or error events)::

    python -m LesionNetworkMappingLib.engine --bundle B.lnmc --lesion lesion1.nii.gz [--lesion ...]
        --out DIR [--subjects N [--random] [--seed S]] [--threads K] [--snapshot-every K]
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

from .connectome import ConnectomeBundle, _is_cancelled
from .geometry import from_masked_vector, resample_to_grid, to_masked_vector
from .nifti import read_nifti, write_nifti

RMAX = 0.999999  # r is clipped to +/-RMAX before the Fisher transform
DEFAULT_CHUNK = 8192
MIN_WEIGHT = 1e-4  # lesion weights at or below this value are ignored (as in Lead-DBS)
OUTPUT_KEYS = (("AvgR", "AvgR"), ("AvgFz", "AvgRFz"), ("T", "T"))  # result key, Lead-DBS "desc" label


class NetworkMaps:
    """Group network maps of L lesions over the V voxels of a bundle mask.

    ``maps[i]`` is a dict of masked vectors ``{"AvgR", "AvgFz", "T", "N"}`` for lesion i; the arrays
    ``avg_r``, ``avg_fz`` and ``t`` have shape (L, V). ``n`` is the number of subjects included and
    ``cancelled`` tells whether the computation stopped early.
    """

    def __init__(self, avg_r, avg_fz, t, subject_ids, cancelled=False, elapsed=0.0):
        self.avg_r = avg_r
        self.avg_fz = avg_fz
        self.t = t
        self.subject_ids = list(subject_ids)
        self.cancelled = cancelled
        self.elapsed = elapsed

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
        seeds = np.zeros((bounds[-1], self.n_lesions), dtype=np.float64)
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
    """

    def __init__(self, n_voxels, n_lesions, chunk):
        self.n = 0
        self.sum_r = np.zeros((n_voxels, n_lesions))
        self.mean_z = np.zeros((n_voxels, n_lesions))
        self.m2 = np.zeros((n_voxels, n_lesions))
        size = min(chunk, n_voxels) * n_lesions
        self._z = np.empty(size, dtype=np.float32)
        self._delta = np.empty(size)
        self._tmp = np.empty(size)

    def add_subject(self, data, seed_matrix, chunk):
        self.n += 1
        inv_n = 1.0 / self.n
        n_lesions = seed_matrix.shape[1]
        for v0, v1, r in iter_correlation_chunks(data, seed_matrix, chunk):
            size = (v1 - v0) * n_lesions
            z = self._z[:size].reshape(r.shape)
            delta = self._delta[:size].reshape(r.shape)
            tmp = self._tmp[:size].reshape(r.shape)
            np.arctanh(r, out=z)
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
    :return: :class:`NetworkMaps`; ``cancelled`` is also True if the iteration stopped early because
      the bundle was closed. T is 0 where Fz has zero variance over the subjects.

    Memory grows with the number of lesions: about 36 * V * L bytes (8 MB per lesion, 830 MB for
    100 lesions at 230k voxels), so split large cohorts into batches of at most ~100 lesions.
    """
    chunk = max(1, int(chunk))
    seeds = _Seeds(weights, min_weight)
    if seeds.n_voxels != bundle.n_voxels:
        raise ValueError(f"Lesion weights have {seeds.n_voxels} voxels, the connectome has {bundle.n_voxels}")
    ids = resolve_subjects(bundle, subjects, random_subset, seed)
    accumulator = _Accumulator(bundle.n_voxels, seeds.n_lesions, chunk)
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
    return accumulator.maps(processed, cancelled, time.perf_counter() - start)


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


def _lesion_name(path):
    name = os.path.basename(os.fspath(path))
    for ext in (".nii.gz", ".nii"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def output_paths(out_dir, lesion_name, bundle_name):
    """Output file names (Lead-DBS style) for one lesion: {result key: path}."""
    label = re.sub(r"[^A-Za-z0-9]", "", bundle_name) or "connectome"
    return {key: os.path.join(out_dir, f"{lesion_name}_conn-{label}_desc-{desc}_funcmap.nii.gz") for key, desc in OUTPUT_KEYS}


def write_maps(maps, lesion_names, bundle, out_dir):
    """Write AvgR, AvgRFz and T NIfTI maps (exact bundle affine) for every lesion; return the paths."""
    written = []
    for index, lesion_name in enumerate(lesion_names):
        for key, path in output_paths(out_dir, lesion_name, bundle.name).items():
            intent = {"intent_code": 3, "intent_params": (max(maps.n - 1, 0), 0.0, 0.0)} if key == "T" else {}
            write_nifti(path, maps.volume(index, key, bundle), bundle.affine, descrip=f"LNM {key} N={maps.n}", **intent)
            written.append(path)
    return written


class _ArgumentParser(argparse.ArgumentParser):
    """Argument parser that also reports usage errors as a JSON error event (exit code 2)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        _emit({"event": "error", "message": f"Invalid arguments: {message}"})
        self.exit(2)


def main(argv=None):
    """Command-line entry point; returns the process exit code."""
    parser = _ArgumentParser(prog="python -m LesionNetworkMappingLib.engine", description="Compute lesion network maps from a connectome bundle.")
    parser.add_argument("--bundle", required=True, help="connectome bundle directory (.lnmc)")
    parser.add_argument("--lesion", required=True, action="append", help="lesion NIfTI file (any grid; repeat for several lesions)")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--subjects", type=int, default=None, help="use only N subjects (default: all)")
    parser.add_argument("--random", action="store_true", help="choose the N subjects at random")
    parser.add_argument("--seed", type=int, default=0, help="random seed for --random")
    parser.add_argument("--threads", type=int, default=2, help="BLAS threads (default: 2)")
    parser.add_argument("--chunk", type=int, default=DEFAULT_CHUNK, help="voxels per matrix product")
    parser.add_argument("--supersample", type=int, default=2, help="minimum sub-samples per axis when resampling lesions on oblique grids")
    parser.add_argument("--probabilistic", action="store_true", help="use lesion image values as weights instead of binarizing")
    parser.add_argument("--snapshot-every", type=int, default=0, help="write partial maps every K subjects")
    args = parser.parse_args(argv)
    try:
        bundle = ConnectomeBundle.open(args.bundle)
        os.makedirs(args.out, exist_ok=True)
        names, weights, qc = [], [], []
        for path in args.lesion:
            lesion_weights_vector, lesion_qc_values = prepare_lesion(path, bundle, supersample=args.supersample, binarize=not args.probabilistic)
            name = _lesion_name(path)
            base, suffix = name, 2
            while name in names:
                name, suffix = f"{base}-{suffix}", suffix + 1
            names.append(name)
            weights.append(lesion_weights_vector)
            qc.append({"lesion": name, **lesion_qc_values})
            if lesion_qc_values["voxels_in_mask"] == 0:
                _emit({"event": "warning", "lesion": name, "message": "The lesion does not overlap the connectome mask; its maps are all zero."})
        ids = resolve_subjects(bundle, args.subjects, args.random, args.seed)
        _emit({"event": "start", "bundle": bundle.name, "lesions": names, "subjects": len(ids), "threads": args.threads, "qc": qc})

        def on_progress(done, total, elapsed):
            eta = elapsed / done * (total - done) if done else 0.0
            _emit({"event": "progress", "done": done, "total": total, "elapsed": round(elapsed, 3), "eta": round(eta, 3)})

        def on_snapshot(partial):
            _emit({"event": "snapshot", "n": partial.n, "outputs": write_maps(partial, names, bundle, args.out)})

        maps = compute_network_maps(bundle, np.stack(weights), subjects=ids, chunk=args.chunk, progress=on_progress, snapshot_every=args.snapshot_every, snapshot=on_snapshot)
        outputs = write_maps(maps, names, bundle, args.out)
        _emit({"event": "done", "outputs": outputs, "n": maps.n, "elapsed": round(maps.elapsed, 3), "qc": qc})
        return 0
    except Exception as exc:
        _emit({"event": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())

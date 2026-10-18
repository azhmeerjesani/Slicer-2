"""Specificity QA for network maps: connectome-degree similarity and the relocated-lesion null.

Lesion network maps partly reflect the connectome's degree (hub) structure, and lesions placed
elsewhere give similar maps (van den Heuvel et al., 2026, Nat Neurosci, doi:10.1038/s41593-025-02196-7).
Two checks are therefore computed for every map:

- degree similarity: spatial Pearson and Spearman r between the map (T) and the connectome degree
  map (``derived/degree.npy``, see ``connectome.compute_degree``);
- relocated null (exact): the same lesion, rigidly translated to K random places in the brain, is
  mapped in the same pass over the connectome (extra seed columns), and the map is compared to these
  null maps.

Command line (prints one JSON object per line: start, progress, done or error events)::

    python -m LesionNetworkMappingLib.qa degree --bundle B.lnmc [--seeds 1000] [--subjects 100] [--seed 0]
"""

import os
import sys

_THREAD_ENV_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS")


def _set_blas_threads(argv, default=2):
    """Set BLAS thread-count environment variables from a --threads argument (before numpy is imported)."""
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
import datetime
import json
import time

import numpy as np

# Degree similarity chips (|r|): below LOW is neutral, below HIGH amber, otherwise red
DEGREE_LOW = 0.3
DEGREE_HIGH = 0.6
DEGREE_TEXTS = {
    "neutral": "low",
    "amber": "moderate",
    "red": "high — map may reflect connectome hubs rather than lesion-specific connectivity",
}
# Random lesions give maps with a median similarity of 0.75-0.84 (van den Heuvel 2026)
NULL_AMBER = 0.75
DEFAULT_RELOCATIONS = 50
MIN_INSIDE = 0.95
DEGREE_JSON = "degree.json"


def _pearson(a, b):
    a = a - a.mean()
    b = b - b.mean()
    denominator = np.sqrt((a @ a) * (b @ b))
    return float((a @ b) / denominator) if denominator > 0 else float("nan")


def degree_similarity(map_vec, degree_vec):
    """Spatial similarity between a map and the connectome degree map over their finite voxels.

    :param map_vec: (V,) map values (e.g. T) over the connectome mask voxels.
    :param degree_vec: (V,) degree map (``ConnectomeBundle.degree``).
    :return: (pearson r, spearman rho); NaN when undefined (fewer than 3 voxels or a constant map).
    """
    from scipy import stats as scipy_stats

    a = np.asarray(map_vec, dtype=np.float64).ravel()
    b = np.asarray(degree_vec, dtype=np.float64).ravel()
    if a.shape != b.shape:
        raise ValueError(f"The map has {a.size} voxels and the degree map {b.size}")
    valid = np.isfinite(a) & np.isfinite(b)
    if np.count_nonzero(valid) < 3:
        return float("nan"), float("nan")
    a, b = a[valid], b[valid]
    return _pearson(a, b), _pearson(scipy_stats.rankdata(a), scipy_stats.rankdata(b))


def chip_level(r):
    """QA chip for a degree similarity r (uses |r|): ("neutral", "low"), ("amber", "moderate") or ("red", "high — ...")."""
    if r is None or not np.isfinite(r):
        return "neutral", "n/a"
    r = abs(float(r))
    level = "neutral" if r < DEGREE_LOW else "amber" if r < DEGREE_HIGH else "red"
    return level, DEGREE_TEXTS[level]


def null_level(r_mean_map):
    """QA chip level of the relocated-lesion null: "amber" when the map resembles the mean null map (r >= 0.75)."""
    if r_mean_map is None or not np.isfinite(r_mean_map):
        return "neutral"
    return "amber" if r_mean_map >= NULL_AMBER else "neutral"


def _grid_and_indices(bundle_or_grid, mask_indices):
    if hasattr(bundle_or_grid, "mask_indices") and hasattr(bundle_or_grid, "shape"):
        shape = tuple(bundle_or_grid.shape)
        indices = bundle_or_grid.mask_indices if mask_indices is None else mask_indices
    else:
        shape = tuple(getattr(bundle_or_grid, "shape", bundle_or_grid))
        if mask_indices is None:
            raise ValueError("mask_indices is needed when no connectome bundle is given")
        indices = mask_indices
    return tuple(int(n) for n in shape[:3]), np.asarray(indices, dtype=np.int64)


def relocated_lesions(weights, bundle_or_grid, mask_indices=None, brain_mask=None, k=DEFAULT_RELOCATIONS, seed=0, min_inside=MIN_INSIDE, max_attempts=None, return_shifts=False):
    """Rigid translations of a lesion's occupancy to random places in the brain (relocated-lesion null).

    Each relocated lesion is the lesion's weight pattern moved by an integer voxel shift on the
    connectome grid, so its shape and weights are preserved exactly. The shifts move the lesion's
    centroid onto brain-mask voxels drawn in random order (without repetition, never the zero
    shift); a shift is accepted when at least ``min_inside`` of the lesion's weight lands on
    connectome voxels inside the brain mask. Weight landing outside the connectome mask is dropped.

    :param weights: (V,) lesion weights over the connectome mask voxels (``engine.prepare_lesion``).
    :param bundle_or_grid: a :class:`ConnectomeBundle`, or a :class:`Grid` or 3D shape together
      with ``mask_indices``.
    :param mask_indices: flat C-order indices of the V mask voxels (default: the bundle's).
    :param brain_mask: optional (V,) boolean vector over the mask voxels; default: all mask voxels.
    :param k: number of relocated lesions.
    :param seed: seed of ``np.random.default_rng``; the result is deterministic for a seed.
    :param max_attempts: maximum number of candidate shifts tested (default ``max(1000, 200 * k)``).
    :param return_shifts: also return the (k, 3) integer voxel shifts.
    :return: (k', V) float32 relocated weights with k' <= k (fewer when not enough valid shifts
      were found within ``max_attempts``), and the shifts when ``return_shifts`` is True.
    """
    shape, indices = _grid_and_indices(bundle_or_grid, mask_indices)
    weights = np.asarray(weights, dtype=np.float32).ravel()
    n_voxels = indices.size
    if weights.size != n_voxels:
        raise ValueError(f"The lesion has {weights.size} weights, the mask has {n_voxels} voxels")
    in_brain = np.ones(n_voxels, dtype=bool) if brain_mask is None else np.asarray(brain_mask, dtype=bool).ravel()
    if in_brain.size != n_voxels:
        raise ValueError(f"The brain mask has {in_brain.size} values, the mask has {n_voxels} voxels")
    empty = np.zeros((0, n_voxels), dtype=np.float32)
    lesion = np.flatnonzero(weights > 0)
    total = float(weights[lesion].sum(dtype=np.float64))
    if lesion.size == 0 or total <= 0 or k <= 0:
        return (empty, np.zeros((0, 3), dtype=np.int64)) if return_shifts else empty

    # Volume lookup: flat grid index -> mask voxel position (-1 outside the connectome mask)
    position = np.full(int(np.prod(shape)), -1, dtype=np.int64)
    position[indices] = np.arange(n_voxels)
    lesion_ijk = np.stack(np.unravel_index(indices[lesion], shape), axis=1)  # (m, 3)
    values = weights[lesion].astype(np.float64)
    centroid = np.round((lesion_ijk * values[:, np.newaxis]).sum(axis=0) / total).astype(np.int64)
    brain_ijk = np.stack(np.unravel_index(indices[in_brain], shape), axis=1)
    if brain_ijk.size == 0:
        return (empty, np.zeros((0, 3), dtype=np.int64)) if return_shifts else empty

    rng = np.random.default_rng(seed)
    max_attempts = max(1000, 200 * int(k)) if max_attempts is None else int(max_attempts)
    candidates = rng.permutation(len(brain_ijk))[:max_attempts]
    shape_array = np.array(shape, dtype=np.int64)
    batch = max(1, 2_000_000 // lesion.size)
    accepted, shifts = [], []
    for start in range(0, len(candidates), batch):
        shift = brain_ijk[candidates[start:start + batch]] - centroid  # (b, 3)
        shift = shift[np.any(shift != 0, axis=1)]
        moved = lesion_ijk[np.newaxis] + shift[:, np.newaxis]  # (b, m, 3)
        inside = np.all((moved >= 0) & (moved < shape_array), axis=2)
        flat = np.ravel_multi_index(tuple(np.where(inside[..., np.newaxis], moved, 0).transpose(2, 0, 1)), shape)
        target = np.where(inside, position[flat], -1)  # (b, m) mask positions, -1 if lost
        landed = target >= 0
        in_brain_mass = (np.where(landed, in_brain[np.maximum(target, 0)], False) * values).sum(axis=1)
        for row in np.flatnonzero(in_brain_mass >= min_inside * total * (1.0 - 1e-12)):
            accepted.append(target[row])
            shifts.append(shift[row])
            if len(accepted) == k:
                break
        if len(accepted) == k:
            break
    out = np.zeros((len(accepted), n_voxels), dtype=np.float32)
    for row, target in enumerate(accepted):
        landed = target >= 0
        out[row, target[landed]] = weights[lesion[landed]]
    if return_shifts:
        return out, np.array(shifts, dtype=np.int64).reshape(-1, 3)
    return out


def null_summary(map_vec, null_maps):
    """Compare a map with the maps of its relocated lesions.

    :param map_vec: (V,) map (e.g. T) of the lesion.
    :param null_maps: (k, V) maps of the relocated lesions over the same voxels.
    :return: dict with
      - ``r_mean_map``: spatial r between the map and the mean null map (high: the map mostly shows
        the generic pattern that any lesion of this shape produces);
      - ``r_median``, ``r_p05``, ``r_p95``: median and 5th/95th percentiles of the spatial r between
        the map and each null map;
      - ``percentile``: percentile of ``r_mean_map`` among the same similarity of each null map to
        the mean of the other null maps (around 50: as generic as a relocated lesion; low: more
        distinctive than relocated lesions);
      - ``k``: number of null maps.
      Values are None when they cannot be computed (fewer than 2 null maps, constant maps).
    """
    observed = np.asarray(map_vec, dtype=np.float64).ravel()
    nulls = np.atleast_2d(np.asarray(null_maps, dtype=np.float64))
    k = nulls.shape[0] if nulls.size else 0
    result = {"r_mean_map": None, "r_median": None, "r_p05": None, "r_p95": None, "percentile": None, "k": int(k)}
    if k == 0:
        return result
    if nulls.shape[1] != observed.size:
        raise ValueError(f"The map has {observed.size} voxels and the null maps {nulls.shape[1]}")
    valid = np.isfinite(observed) & np.all(np.isfinite(nulls), axis=0)
    observed, nulls = observed[valid], nulls[:, valid]
    if observed.size < 3:
        return result

    def standardize(x):
        x = x - x.mean(axis=-1, keepdims=True)
        norm = np.linalg.norm(x, axis=-1, keepdims=True)
        return np.divide(x, norm, out=np.full_like(x, np.nan), where=norm > 0)

    z_observed = standardize(observed)
    r = standardize(nulls) @ z_observed
    finite = r[np.isfinite(r)]
    if finite.size:
        result["r_median"] = float(np.median(finite))
        result["r_p05"], result["r_p95"] = (float(v) for v in np.percentile(finite, [5, 95]))
    total = nulls.sum(axis=0)
    r_mean = float(standardize(total / k) @ z_observed)
    result["r_mean_map"] = r_mean if np.isfinite(r_mean) else None
    if k >= 2 and result["r_mean_map"] is not None:
        # Leave-one-out: similarity of each null map to the mean of the others
        loo = np.einsum("ij,ij->i", standardize(nulls), standardize((total[np.newaxis] - nulls) / (k - 1)))
        loo = loo[np.isfinite(loo)]
        if loo.size:
            result["percentile"] = float(100.0 * np.count_nonzero(loo <= r_mean) / loo.size)
    return result


def lesion_qa(map_vec, degree_vec=None, null_maps=None, null_voxels=None):
    """QA values of one map: degree similarity (when a degree map is given) and the null summary.

    :param null_voxels: optional indices (into the V voxels) at which ``null_maps`` were computed.
    :return: dict {degree_r, degree_rho, degree_level, null (see :func:`null_summary`) or None, null_level}.
    """
    out = {"degree_r": None, "degree_rho": None, "degree_level": "neutral", "null": None, "null_level": "neutral"}
    if degree_vec is not None:
        r, rho = degree_similarity(map_vec, degree_vec)
        out["degree_r"] = r if np.isfinite(r) else None
        out["degree_rho"] = rho if np.isfinite(rho) else None
        out["degree_level"] = chip_level(r)[0]
    if null_maps is not None and len(null_maps):
        values = np.asarray(map_vec)
        out["null"] = null_summary(values if null_voxels is None else values[null_voxels], null_maps)
        out["null_level"] = null_level(out["null"]["r_mean_map"])
    return out


def describe_null(summary):
    """One-line text of a null summary, e.g. "Similarity to relocated lesions of the same shape: r = 0.81
    (median of 50; 5-95%: 0.72-0.86); this map is at the 62nd percentile" (with en dashes).
    """
    if not summary or summary.get("r_median") is None:
        return "Similarity to relocated lesions: not available"
    text = f"Similarity to relocated lesions of the same shape: r = {summary['r_median']:.2f} (median of {summary['k']}; 5\u201395%: {summary['r_p05']:.2f}\u2013{summary['r_p95']:.2f})"
    if summary.get("percentile") is not None:
        text += f"; this map is at the {_ordinal(round(summary['percentile']))} percentile"
    return text


def _ordinal(value):
    value = int(value)
    suffix = "th" if 10 <= value % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def read_degree_metadata(bundle_path):
    """Contents of ``derived/degree.json`` of a bundle, or None (missing, unreadable or without degree.npy)."""
    derived = os.path.join(os.fspath(bundle_path), "derived")
    path = os.path.join(derived, DEGREE_JSON)
    if not (os.path.isfile(path) and os.path.isfile(os.path.join(derived, "degree.npy"))):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# Command-line interface


def _emit(event):
    print(json.dumps(event), flush=True)


class _ArgumentParser(argparse.ArgumentParser):
    """Argument parser that also reports usage errors as a JSON error event (exit code 2)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        _emit({"event": "error", "message": f"Invalid arguments: {message}"})
        self.exit(2)


def _degree_command(args):
    from .connectome import ConnectomeBundle, compute_degree
    from .export import file_sha256, subjects_sha256

    bundle = ConnectomeBundle.open(args.bundle)
    ids = bundle.select_subjects(args.subjects)
    n_seeds = min(int(args.seeds), bundle.n_voxels)
    _emit({"event": "start", "bundle": bundle.name, "subjects": len(ids), "seeds": n_seeds, "threads": args.threads})

    def on_progress(done, total, elapsed):
        eta = elapsed / done * (total - done) if done else 0.0
        _emit({"event": "progress", "done": done, "total": total, "elapsed": round(elapsed, 3), "eta": round(eta, 3)})

    start = time.perf_counter()
    degree = compute_degree(bundle, n_seeds=n_seeds, subjects=ids, measure=args.measure, seed=args.seed, chunk=args.chunk, store=True, progress=on_progress)
    elapsed = time.perf_counter() - start
    metadata = {
        "K": n_seeds,
        "subjects": len(ids),
        "subject_ids_sha256": subjects_sha256(ids),
        "seed": args.seed,
        "measure": args.measure,
        "manifest_sha256": file_sha256(os.path.join(bundle.path, "connectome.json")),
        "created": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "elapsed": round(elapsed, 3),
    }
    json_path = os.path.join(bundle.path, "derived", DEGREE_JSON)
    tmp_path = f"{json_path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    os.replace(tmp_path, json_path)
    _emit({"event": "done", "path": os.path.join(bundle.path, "derived", "degree.npy"), "json": json_path, "elapsed": round(elapsed, 3), "min": float(degree.min()), "max": float(degree.max()), **metadata})


def main(argv=None):
    """Command-line entry point; returns the process exit code."""
    parser = _ArgumentParser(prog="python -m LesionNetworkMappingLib.qa", description="Connectome QA jobs.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_ArgumentParser)
    degree = commands.add_parser("degree", help="compute the connectome degree map (derived/degree.npy and degree.json)")
    degree.add_argument("--bundle", required=True, help="connectome bundle directory (.lnmc)")
    degree.add_argument("--seeds", type=int, default=1000, help="number of random seed voxels (default: 1000)")
    degree.add_argument("--subjects", type=int, default=100, help="use the first N subjects (default: 100)")
    degree.add_argument("--seed", type=int, default=0, help="random seed for the seed voxels")
    degree.add_argument("--measure", choices=("abs_fz", "r"), default="abs_fz", help="mean |Fz| (default) or mean r")
    degree.add_argument("--threads", type=int, default=2, help="BLAS threads (default: 2)")
    degree.add_argument("--chunk", type=int, default=8192, help="voxels per matrix product")
    args = parser.parse_args(argv)
    try:
        _degree_command(args)
        return 0
    except Exception as exc:
        _emit({"event": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())

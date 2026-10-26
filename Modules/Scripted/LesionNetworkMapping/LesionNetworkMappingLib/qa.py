"""Specificity QA for network maps: connectome-degree similarity and the relocated-lesion null.

Lesion network maps partly reflect the connectome's degree (hub) structure, and lesions placed
elsewhere give similar maps (van den Heuvel et al., 2026, Nat Neurosci, doi:10.1038/s41593-025-02196-7).
Two checks are therefore computed for every map:

- degree similarity: spatial Pearson and Spearman r between the map (T) and the connectome degree
  map (``derived/degree.npy``, see ``connectome.compute_degree``);
- relocated null (exact): the same lesion, rigidly translated to K random places in the brain, is
  mapped in the same pass over the connectome (extra seed columns), and the map is compared to these
  null maps.

With the bundle's reference maps (``lnm-reference-v1``, see reference.py and addendum §3.7) the QA
also reports the similarity to μC_r, PC1-3 and hubness, the spatial R² on these 4 references, and a
calibrated chip: the percentile of r(map, μC_r) among the same similarity of the lesion's relocated
null maps (or, for group maps, of null cohorts), with its resolution (100/K %). The fixed |r| cut-offs
of :func:`chip_level` and :func:`null_level` are kept as secondary text. Map comparisons themselves
are in similarity.py.

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

from . import similarity

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


# Calibrated chips (addendum §3.7): percentile of r(map, μC_r) among matched null maps
CALIBRATED_LOW = 5.0
CALIBRATED_HIGH = 95.0
CALIBRATED_TEXTS = {
    "below": "less degree-like than {nulls}",
    "amber": "indistinguishable from {nulls} on this axis",
    "red": "more degree-like than {nulls}",
}
NOT_CALIBRATED = "not_calibrated"
RELOCATED_NULLS = "relocated lesions"
REFERENCE_KEYS = ("mu_r", "pc1", "pc2", "pc3", "hub")  # maps compared in the QA card
R2_REFERENCE_KEYS = ("mu_r", "pc1", "pc2", "pc3")
R2_REFERENCE_LABEL = "spatial R² on these 4 references"
ANALYTIC_RL_LABEL = "approximate random-location null (van den Heuvel Sep 2026): z \u2248 (observed \u2212 \u03bcC)/\u03c3C; descriptive"

# Kept for callers of the former private helpers (the implementation is in similarity.py)
_pearson = similarity._pearson
_ranks = similarity.ranks


def degree_similarity(map_vec, degree_vec):
    """Spatial similarity between a map and the connectome degree map over their finite voxels.

    :param map_vec: (V,) map values (e.g. T) over the connectome mask voxels.
    :param degree_vec: (V,) degree map (``ConnectomeBundle.degree``).
    :return: (pearson r, spearman rho); NaN when undefined (fewer than 3 voxels or a constant map).
    """
    a = np.asarray(map_vec, dtype=np.float64).ravel()
    b = np.asarray(degree_vec, dtype=np.float64).ravel()
    if a.shape != b.shape:
        raise ValueError(f"The map has {a.size} voxels and the degree map {b.size}")
    return similarity.pearson_spearman(a, b)


def chip_level(r):
    """QA chip for a degree similarity r (uses |r|): ("neutral", "low"), ("amber", "moderate") or ("red", "high — ...").

    Fixed cut-offs (0.3, 0.6), kept for API compatibility and shown as secondary text only: the
    primary chip is calibrated against matched nulls when reference maps exist (:func:`calibrated_level`).
    """
    if r is None or not np.isfinite(r):
        return "neutral", "n/a"
    r = abs(float(r))
    level = "neutral" if r < DEGREE_LOW else "amber" if r < DEGREE_HIGH else "red"
    return level, DEGREE_TEXTS[level]


def null_level(r_mean_map):
    """QA chip level of the relocated-lesion null: "amber" when the map resembles the mean null map (r >= 0.75).

    Fixed cut-off, kept for API compatibility and shown as secondary text only (see :func:`calibrated_level`).
    """
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


def relocated_lesions(weights, bundle_or_grid, mask_indices=None, brain_mask=None, k=DEFAULT_RELOCATIONS, seed=0, min_inside=MIN_INSIDE, max_attempts=None, return_shifts=False, sparse=False):
    """Rigid translations of a lesion's occupancy to random places in the brain (relocated-lesion null).

    Each relocated lesion is the lesion's weight pattern moved by an integer voxel shift on the
    connectome grid, so its shape and weights are preserved exactly. The shifts move the lesion's
    centroid onto brain-mask voxels drawn in random order (without repetition, never the zero
    shift); a shift is accepted when at least ``min_inside`` of the lesion's weight lands on
    connectome voxels inside the brain mask. Weight landing outside the connectome mask is dropped.
    Positions are not otherwise constrained, so a relocated lesion may overlap the original one
    (rare on a whole-brain mask: about 1% of the relocations of a 250-voxel lesion on a 2 mm grid).

    :param weights: (V,) lesion weights over the connectome mask voxels (``engine.prepare_lesion``).
    :param bundle_or_grid: a :class:`ConnectomeBundle`, or a :class:`Grid` or 3D shape together
      with ``mask_indices``.
    :param mask_indices: flat C-order indices of the V mask voxels (default: the bundle's).
    :param brain_mask: optional (V,) boolean vector over the mask voxels; default: all mask voxels.
    :param k: number of relocated lesions.
    :param seed: seed of ``np.random.default_rng``; the result is deterministic for a seed.
    :param max_attempts: maximum number of candidate shifts tested (default ``max(1000, 200 * k)``).
    :param return_shifts: also return the (k, 3) integer voxel shifts.
    :param sparse: return a list of (voxel positions, float32 weights) pairs, one per relocated
      lesion, instead of the dense matrix (much smaller for large connectomes; accepted by
      ``engine.compute_network_maps(null_weights=...)``).
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
    empty = [] if sparse else np.zeros((0, n_voxels), dtype=np.float32)
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
    max_batch = max(1, 2_000_000 // lesion.size)  # bounds the (batch, lesion voxels, 3) arrays
    batch = min(max_batch, max(64, 4 * int(k)))  # most shifts are usually valid: start small
    accepted, shifts = [], []
    start = 0
    while start < len(candidates) and len(accepted) < k:
        shift = brain_ijk[candidates[start:start + batch]] - centroid  # (b, 3)
        start += batch
        batch = min(max_batch, 2 * batch)
        shift = shift[np.any(shift != 0, axis=1)]
        moved = lesion_ijk[np.newaxis] + shift[:, np.newaxis]  # (b, m, 3)
        inside = np.all((moved >= 0) & (moved < shape_array), axis=2)
        flat = np.ravel_multi_index(tuple(np.where(inside[..., np.newaxis], moved, 0).transpose(2, 0, 1)), shape)
        target = np.where(inside, position[flat], -1)  # (b, m) mask positions, -1 if lost
        landed = target >= 0
        in_brain_mass = (np.where(landed, in_brain[np.maximum(target, 0)], False) * values).sum(axis=1)
        for row in np.flatnonzero(in_brain_mass >= min_inside * total * (1.0 - 1e-12))[: k - len(accepted)]:
            accepted.append(target[row])
            shifts.append(shift[row])
    columns = []
    for target in accepted:
        landed = target >= 0
        order = np.argsort(target[landed])
        columns.append((target[landed][order], weights[lesion[landed]][order]))
    if sparse:
        out = columns
    else:
        out = np.zeros((len(columns), n_voxels), dtype=np.float32)
        for row, (positions, column_weights) in enumerate(columns):
            out[row, positions] = column_weights
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
    z_nulls = standardize(nulls)
    r = z_nulls @ z_observed
    finite = r[np.isfinite(r)]
    if finite.size:
        result["r_median"] = float(np.median(finite))
        result["r_p05"], result["r_p95"] = (float(v) for v in np.percentile(finite, [5, 95]))
    total = nulls.sum(axis=0)
    r_mean = float(standardize(total / k) @ z_observed)
    result["r_mean_map"] = r_mean if np.isfinite(r_mean) else None
    if k >= 2 and result["r_mean_map"] is not None:
        # Leave-one-out: similarity of each null map to the mean of the others
        loo = np.einsum("ij,ij->i", z_nulls, standardize(total[np.newaxis] - nulls))
        loo = loo[np.isfinite(loo)]
        if loo.size:
            result["percentile"] = float(100.0 * np.count_nonzero(loo <= r_mean) / loo.size)
    return result


def _finite_or_none(value):
    return float(value) if value is not None and np.isfinite(value) else None


def _reference_maps(reference):
    """The {key: (V,) vector} maps of a reference (a ``reference.Reference`` or a plain dict of maps)."""
    maps = getattr(reference, "maps", reference)
    if not isinstance(maps, dict) or maps.get("mu_r") is None:
        raise ValueError("The reference has no mu_r map")
    return maps


def _selected(values, voxels, n_voxels, name="map"):
    """``values`` on the voxel selection: (V,) values are indexed by ``voxels``, values already of the selection's size are kept."""
    values = np.asarray(values, dtype=np.float64).ravel()
    if voxels is None:
        if values.size != n_voxels:
            raise ValueError(f"The {name} has {values.size} voxels and the reference maps {n_voxels}")
        return values
    voxels = np.asarray(voxels)
    n_selected = int(np.count_nonzero(voxels)) if voxels.dtype == bool else voxels.size
    if values.size == n_voxels:
        return values[voxels]
    if values.size == n_selected:
        return values
    raise ValueError(f"The {name} has {values.size} voxels; expected {n_voxels} or the {n_selected} selected voxels")


def reference_similarity(map_vec, reference, voxels=None):
    """Similarity of a map with the reference maps (addendum §3.7 QA card).

    All values are computed over the voxels where the map and every reference map used are finite.

    :param map_vec: (V,) map over the bundle voxels (or already restricted to ``voxels``).
    :param reference: ``reference.Reference`` (see :func:`load_reference`) or a dict of (V,) maps
      {"mu_r", "pc1".."pc3", "hub", ...}.
    :param voxels: optional voxel selection (indices or boolean mask over the V voxels).
    :return: dict with
      - ``mu_r``, ``pc1``, ``pc2``, ``pc3``, ``hub``: (Pearson r, Spearman rho) with each reference map
        ((None, None) when the reference has no such map);
      - ``r2_ref``: spatial R² of the map on [1, μC_r, PC1, PC2, PC3] (:data:`R2_REFERENCE_LABEL`;
        computed on the PCs the reference has);
      - ``partial_pc1_mu``: {"pc1_given_mu_r": partial r with PC1 given μC_r, "mu_r_given_pc1": partial r
        with μC_r given PC1} (each axis beyond the other).
      Undefined values are None.
    """
    maps = _reference_maps(reference)
    n_voxels = np.asarray(maps["mu_r"]).size
    values = _selected(map_vec, voxels, n_voxels)
    keys = [key for key in REFERENCE_KEYS if maps.get(key) is not None]
    rows = np.vstack([_selected(maps[key], voxels, n_voxels, f"reference map {key}") for key in keys])
    r, rho = similarity.pearson_spearman_rows(rows, values)
    out = dict.fromkeys(REFERENCE_KEYS, (None, None))
    for index, key in enumerate(keys):
        out[key] = (_finite_or_none(r[index]), _finite_or_none(rho[index]))
    valid = np.isfinite(values) & np.all(np.isfinite(rows), axis=0)
    if not valid.all():
        values, rows = values[valid], rows[:, valid]
    row_of = {key: rows[index] for index, key in enumerate(keys)}
    out["r2_ref"] = _finite_or_none(similarity.r2_explained(values, np.vstack([row_of[key] for key in R2_REFERENCE_KEYS if key in row_of])))
    partial = {"pc1_given_mu_r": None, "mu_r_given_pc1": None}
    if "pc1" in row_of:
        partial["pc1_given_mu_r"] = _finite_or_none(similarity.partial_spatial_correlation(values, row_of["pc1"], row_of["mu_r"]))
        partial["mu_r_given_pc1"] = _finite_or_none(similarity.partial_spatial_correlation(values, row_of["mu_r"], row_of["pc1"]))
    out["partial_pc1_mu"] = partial
    return out


def _resolution_text(k):
    resolution = 100.0 / k
    return f"{resolution:.0f}%" if resolution >= 1 else f"{resolution:.1g}%"


def calibrated_percentile(observed, null_values):
    """Percentile (0-100) of an observed value among null values; ties count half.

    percentile = 100 * (#{null < observed} + 0.5 * #{null == observed}) / k over the k finite null
    values (``scipy.stats.percentileofscore(..., kind="mean")``): 0 below every null, 100 above every
    null, 50 when all are tied. Its resolution is 100/k.

    :return: (percentile or None when the observed value is not finite or there is no finite null value, k).
    """
    nulls = np.asarray(null_values, dtype=np.float64).ravel()
    nulls = nulls[np.isfinite(nulls)]
    k = int(nulls.size)
    if k == 0 or observed is None or not np.isfinite(observed):
        return None, k
    below = np.count_nonzero(nulls < observed)
    ties = np.count_nonzero(nulls == observed)
    return float(100.0 * (below + 0.5 * ties) / k), k


def calibrated_level(percentile, k, nulls=RELOCATED_NULLS):
    """Calibrated QA chip of a percentile of r(map, μC_r) among k null maps.

    :param nulls: what the null maps are, for the text ("relocated lesions"; "null cohorts" for groups).
    :return: (level, text): ("below", "less degree-like than relocated lesions; ..."), ("amber",
      "indistinguishable from relocated lesions on this axis; ...") or ("red", "more degree-like than
      relocated lesions; ..."), the text ending with "percentile resolution 2% at K=50"; or
      (:data:`NOT_CALIBRATED`, ...) without a percentile.
    """
    if not k:
        return NOT_CALIBRATED, f"not calibrated (no {nulls} to compare with)"
    if percentile is None or not np.isfinite(percentile):
        return NOT_CALIBRATED, "not calibrated (the similarity with μC_r is undefined for this map)"
    level = "below" if percentile < CALIBRATED_LOW else "red" if percentile > CALIBRATED_HIGH else "amber"
    return level, f"{CALIBRATED_TEXTS[level].format(nulls=nulls)}; percentile resolution {_resolution_text(k)} at K={int(k)}"


def _calibrated_reference_qa(map_vec, reference, null_maps, null_voxels, null_kind, nulls):
    """The "reference" QA values of a map (see :func:`lesion_qa`), calibrated against null maps when given."""
    maps = _reference_maps(reference)
    values = similarity._vector(map_vec)
    sim = reference_similarity(values, maps)
    out = {}
    for key in REFERENCE_KEYS:
        name = "mu" if key == "mu_r" else key  # keys mu_r, mu_rho, pc1_r, pc1_rho, ...
        out[f"{name}_r"], out[f"{name}_rho"] = sim[key]
    out.update({"r2_ref": sim["r2_ref"], "partial_pc1_mu": sim["partial_pc1_mu"], "mu_r_sample": None, "percentile": None, "k": 0, "resolution": None})
    if null_maps is not None and len(null_maps):
        null_rows = np.asarray(null_maps, dtype=np.float64)
        null_rows = np.atleast_2d(null_rows)
        mu = _selected(maps["mu_r"], null_voxels, values.size, "reference map mu_r")
        observed = _selected(values, null_voxels, values.size)
        if null_rows.shape[1] != observed.size:
            raise ValueError(f"The null maps have {null_rows.shape[1]} voxels and the map {observed.size} at the null voxels")
        # Observed and null maps on the same voxels (the null voxel sample, finite in every map)
        r = similarity.pearson_rows(np.vstack((observed, null_rows)), mu)
        out["mu_r_sample"] = _finite_or_none(r[0])
        out["percentile"], out["k"] = calibrated_percentile(r[0], r[1:])
        out["resolution"] = 100.0 / out["k"] if out["k"] else None
    out["level"], out["text"] = calibrated_level(out["percentile"], out["k"], nulls)
    out["null_kind"] = null_kind
    return out


def lesion_qa(map_vec, degree_vec=None, null_maps=None, null_voxels=None, reference=None):
    """QA values of one map: degree similarity (when a degree map is given) and the null summary.

    :param null_voxels: optional indices (into the V voxels) at which ``null_maps`` were computed.
    :param reference: optional reference maps (``reference.Reference`` from :func:`load_reference`,
      or a dict of (V,) maps) for the reference similarity and the calibrated chip.
    :return: dict {degree_r, degree_rho, degree_level, null (see :func:`null_summary`) or None, null_level},
      and with a reference also "reference": {
      ``mu_r``, ``mu_rho``, ``pc1_r``, ``pc1_rho``, ``pc2_r``, ``pc2_rho``, ``pc3_r``, ``pc3_rho``, ``hub_r``,
      ``hub_rho``: r and rho with each reference map over all voxels; ``r2_ref`` (:data:`R2_REFERENCE_LABEL`);
      ``partial_pc1_mu`` (see :func:`reference_similarity`); ``mu_r_sample``: r(map, μC_r) on the null
      voxel sample; ``percentile`` of ``mu_r_sample`` among r(null map, μC_r) on the same voxels;
      ``k`` null maps used; ``resolution`` (100/k, %); ``level`` and ``text`` (:func:`calibrated_level`;
      "not_calibrated" without null maps); ``null_kind``: "relocated"}.
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
    if reference is not None:
        out["reference"] = _calibrated_reference_qa(map_vec, reference, null_maps, null_voxels, "relocated", RELOCATED_NULLS)
    return out


def group_qa(group_map, reference, null_group_maps=None, null_voxels=None, null_kind="null_cohort"):
    """Reference QA of a group map (mean T, GLM statistic, sLNM R, ...): the "reference" values of :func:`lesion_qa`.

    :param null_group_maps: optional (K, V) maps of K null cohorts computed like the group map (or on
      ``null_voxels``); without them the level is "not_calibrated".
    :param null_kind: the kind of the null cohorts (recorded; e.g. "rl_independent").
    :return: the dict described under "reference" in :func:`lesion_qa`, or None without a reference.
    """
    if reference is None:
        return None
    return _calibrated_reference_qa(group_map, reference, null_group_maps, null_voxels, null_kind, "null cohorts")


def analytic_rl_zmap(avg_r_map, reference):
    """Approximate random-location null z-map: z = (observed - μC_r) / sigmaC_r per voxel (:data:`ANALYTIC_RL_LABEL`).

    Descriptive (van den Heuvel, Sep 2026). Use the map on the r scale (mean r over subjects, e.g. AvgR).

    :param avg_r_map: (V,) map of mean r.
    :param reference: ``reference.Reference`` or dict of (V,) maps with "mu_r" and "sigma_r".
    :return: (V,) float64 z (NaN where sigmaC_r is 0 or a value is not finite), or None when the
      reference has no sigma_r map.
    """
    if reference is None:
        return None
    maps = getattr(reference, "maps", reference)
    if not isinstance(maps, dict) or maps.get("sigma_r") is None or maps.get("mu_r") is None:
        return None
    mu = np.asarray(maps["mu_r"], dtype=np.float64).ravel()
    sigma = np.asarray(maps["sigma_r"], dtype=np.float64).ravel()
    observed = similarity._vector(avg_r_map, mu.size, "r map")
    z = np.full(mu.size, np.nan)
    np.divide(observed - mu, sigma, out=z, where=np.isfinite(observed) & np.isfinite(mu) & np.isfinite(sigma) & (sigma > 0))
    return z


def load_reference(bundle, subjects_sha=None, path=None, strict=False, verify=True):
    """The bundle's reference maps (``reference.load_reference``), or None when missing or stale; never builds them."""
    from .reference import load_reference as load

    return load(bundle, subjects_sha=subjects_sha, path=path, strict=strict, verify=verify)


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
    """Contents of ``derived/degree.json`` of a bundle, or None (missing, unreadable, or not describing
    the current ``derived/degree.npy``: its ``degree_sha256`` must match the file).
    """
    from .export import file_sha256

    derived = os.path.join(os.fspath(bundle_path), "derived")
    path = os.path.join(derived, DEGREE_JSON)
    degree_path = os.path.join(derived, "degree.npy")
    if not (os.path.isfile(path) and os.path.isfile(degree_path)):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            metadata = json.load(f)
        # A degree map recomputed after degree.json was written (or a job stopped between the two files)
        if not isinstance(metadata, dict) or metadata.get("degree_sha256") != file_sha256(degree_path):
            return None
        return metadata
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

    json_path = os.path.join(bundle.path, "derived", DEGREE_JSON)
    if os.path.exists(json_path):
        os.remove(json_path)  # never leave metadata of an older degree map next to a new one
    start = time.perf_counter()
    degree = compute_degree(bundle, n_seeds=n_seeds, subjects=ids, measure=args.measure, seed=args.seed, chunk=args.chunk, store=True, progress=on_progress)
    elapsed = time.perf_counter() - start
    degree_path = os.path.join(bundle.path, "derived", "degree.npy")
    metadata = {
        "K": n_seeds,
        "subjects": len(ids),
        "subject_ids_sha256": subjects_sha256(ids),
        "seed": args.seed,
        "measure": args.measure,
        "manifest_sha256": file_sha256(os.path.join(bundle.path, "connectome.json")),
        "created": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "elapsed": round(elapsed, 3),
        "degree_sha256": file_sha256(degree_path),
    }
    tmp_path = f"{json_path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    os.replace(tmp_path, json_path)
    _emit({"event": "done", "path": degree_path, "json": json_path, "elapsed": round(elapsed, 3), "min": float(degree.min()), "max": float(degree.max()), **metadata})


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

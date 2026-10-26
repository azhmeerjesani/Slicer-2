"""Per-lesion circuit scores (SPEC_LNM_ADDENDUM §3.8): how strongly one lesion engages a circuit map.

Pure numpy (scipy.sparse rows are accepted as occupancy); no Slicer imports. Maps are (V,) vectors over the
connectome voxels (the voxel order of cohortmaps' weights.npz and of the masked map files).

Single-lesion scores:

- :func:`damage_score`: sum over the voxels of occupancy x circuit (the lesion's fractional occupancy,
  as stored by cohortmaps in weights.npz);
- :func:`mean_in_lesion`: the occupancy-weighted mean circuit value inside the lesion;
- :func:`pct_lesion_in_circuit`: the percentage of the lesion's occupancy where |circuit| (or the signed
  circuit) reaches a threshold;
- :func:`similarity_score`: the spatial Pearson r between the lesion's network map (AvgFz) and the circuit,
  its Fisher z, and the same partial r given covariate maps such as the reference maps [μC_r, PC1]
  (``similarity.partial_spatial_correlation``, addendum §3.7);
- :func:`roi_connectivity`: the mean network-map value inside an ROI (a binary mask, or signed weights such
  as the top-x% ROI of :func:`top_pct_roi`, where the negative part counts with its sign flipped).

Batched scores of many lesions against many maps (the folds of validate.py), equal to the single-lesion
functions: :func:`similarity_matrix` (from the sufficient terms of :func:`lesion_terms` and
:func:`map_terms`), :func:`damage_matrix` and :func:`roi_matrix`.

Occupancy is given as an (indices, values) pair, a dense (V,) vector or a 1 x V scipy.sparse row.
"""

import collections
import math

import numpy as np

from . import similarity

#: r is clipped to this magnitude before the Fisher transform (as engine.RMAX)
RMAX = 0.999999
#: Voxels per chunk of the batched scores (float64 temporaries of n x CHUNK_VOXELS)
CHUNK_VOXELS = 16384
SIGNS = ("both", "pos", "neg")

#: :func:`similarity_score` result: r, its Fisher z, and the partial r and z given covariate maps (NaN without them)
SimilarityScore = collections.namedtuple("SimilarityScore", ("r", "z", "partial_r", "partial_z"))


def fisher_z(r):
    """Fisher z = atanh(r), with |r| clipped to :data:`RMAX` (NaN stays NaN); a float for a scalar."""
    out = np.arctanh(np.clip(np.asarray(r, dtype=np.float64), -RMAX, RMAX))
    return float(out) if out.ndim == 0 else out


def _vector(values, name="map"):
    out = np.asarray(values, dtype=np.float64).ravel()
    if out.size == 0:
        raise ValueError(f"The {name} is empty")
    return out


def occupancy_pair(occupancy, n_voxels):
    """(indices int64, values float64) of a lesion occupancy given as a pair, a dense (V,) vector or a sparse row.

    Zero values are dropped. :raises ValueError: an index outside [0, n_voxels) or a dense vector of another length.
    """
    if isinstance(occupancy, tuple) and len(occupancy) == 2:
        indices = np.asarray(occupancy[0], dtype=np.int64).ravel()
        values = np.asarray(occupancy[1], dtype=np.float64).ravel()
        if indices.size != values.size:
            raise ValueError(f"The occupancy has {indices.size} indices and {values.size} values")
    elif hasattr(occupancy, "tocoo"):  # scipy.sparse row
        coo = occupancy.tocoo()
        if coo.shape[-1] != n_voxels or (coo.ndim == 2 and coo.shape[0] != 1):
            raise ValueError(f"The occupancy row has shape {coo.shape}, expected (1, {n_voxels})")
        indices = np.asarray(coo.col if coo.ndim == 2 else coo.coords[0], dtype=np.int64)
        values = np.asarray(coo.data, dtype=np.float64)
    else:
        dense = _vector(occupancy, "occupancy")
        if dense.size != n_voxels:
            raise ValueError(f"The occupancy has {dense.size} voxels, the circuit {n_voxels}")
        indices = np.flatnonzero(dense)
        values = dense[indices]
    if indices.size and (indices.min() < 0 or indices.max() >= n_voxels):
        raise ValueError(f"The occupancy indexes voxels outside [0, {n_voxels})")
    keep = values != 0
    return indices[keep], values[keep]


def _lesion_values(occupancy, circuit):
    """(occupancy values, circuit values at the lesion voxels) over the lesion voxels where the circuit is finite."""
    circuit = _vector(circuit, "circuit")
    indices, values = occupancy_pair(occupancy, circuit.size)
    inside = circuit[indices]
    finite = np.isfinite(inside) & np.isfinite(values)
    return values[finite], inside[finite]


def damage_score(occupancy, circuit):
    """Sum over the lesion voxels of occupancy x circuit (voxels where either is not finite are left out)."""
    values, inside = _lesion_values(occupancy, circuit)
    return float(values @ inside)


def mean_in_lesion(occupancy, circuit):
    """Occupancy-weighted mean of the circuit inside the lesion (NaN for an empty lesion)."""
    values, inside = _lesion_values(occupancy, circuit)
    total = float(values.sum())
    return float(values @ inside) / total if total > 0 else float("nan")


def _in_circuit(values, threshold, sign):
    if sign not in SIGNS:
        raise ValueError(f"sign must be one of {', '.join(SIGNS)}, got {sign!r}")
    if sign == "pos":
        return values >= threshold
    if sign == "neg":
        return values <= -threshold
    return np.abs(values) >= threshold


def pct_lesion_in_circuit(occupancy, circuit, threshold, sign="both"):
    """Percentage (0-100) of the lesion's occupancy inside the circuit thresholded at ``threshold``.

    :param threshold: a voxel is inside when |circuit| >= threshold (``sign`` "both"), circuit >= threshold
      ("pos") or circuit <= -threshold ("neg"). A binary ROI with threshold 1 gives the % of the lesion in the ROI.
    :return: NaN for an empty lesion.
    """
    values, inside = _lesion_values(occupancy, circuit)
    total = float(values.sum())
    if total <= 0:
        return float("nan")
    return 100.0 * float(values[_in_circuit(inside, float(threshold), sign)].sum()) / total


def similarity_score(lesion_fz, circuit, partial=None):
    """Spatial similarity of a lesion's network map with a circuit.

    :param lesion_fz: (V,) network map of the lesion (AvgFz).
    :param circuit: (V,) circuit map.
    :param partial: (k, V) covariate maps (e.g. ``reference.Reference.covariates(("mu_r", "pc1"))``) or None.
    :return: :class:`SimilarityScore` (r, Fisher z, partial r, partial z; the partial values are NaN without ``partial``).
    """
    r = similarity.pearson(lesion_fz, circuit)
    partial_r = float("nan") if partial is None else similarity.partial_spatial_correlation(lesion_fz, circuit, partial)
    return SimilarityScore(r, fisher_z(r), partial_r, fisher_z(partial_r))


def top_pct_roi(map_vec, pct=10.0, sign="both"):
    """Signed ROI of the top ``pct``% of a map (the rule of thresholds' ``top_pct``, applied within the voxels given).

    "both": the top pct% of |map| with the sign of the map (+1/-1); "pos": the top pct% of the map (+1);
    "neg": the bottom pct% (-1). Ties at the threshold are inside. Non-finite voxels are outside.

    :return: (V,) float32 with values in {-1, 0, 1}.
    """
    if not 0.0 < float(pct) < 100.0:
        raise ValueError(f"The top-x% value must be between 0 and 100 (exclusive), got {pct}")
    if sign not in SIGNS:
        raise ValueError(f"sign must be one of {', '.join(SIGNS)}, got {sign!r}")
    values = _vector(map_vec)
    out = np.zeros(values.size, dtype=np.float32)
    finite = np.isfinite(values)
    ranked = {"both": np.abs(values), "pos": values, "neg": -values}[sign]
    pool = ranked[finite]
    if pool.size == 0:
        return out
    count = max(1, math.ceil(float(pct) / 100.0 * pool.size))
    threshold = np.partition(pool, pool.size - count)[pool.size - count]
    inside = finite & (ranked >= threshold)
    if sign == "both":
        out[inside] = np.sign(values[inside])
    else:
        out[inside] = 1.0 if sign == "pos" else -1.0
    return out


def roi_connectivity(lesion_fz, roi_mask):
    """Mean network-map value inside an ROI: sum(roi x map) / sum(|roi|) over the voxels where the map is finite.

    :param roi_mask: (V,) binary mask, or signed weights (a voxel with weight -1 counts with its sign flipped).
    :return: NaN when the ROI holds no finite voxel.
    """
    values = _vector(lesion_fz, "lesion map")
    roi = _vector(roi_mask, "ROI")
    if roi.size != values.size:
        raise ValueError(f"The ROI has {roi.size} voxels, the map {values.size}")
    valid = np.isfinite(values) & np.isfinite(roi) & (roi != 0)
    total = float(np.abs(roi[valid]).sum())
    return float(roi[valid] @ values[valid]) / total if total > 0 else float("nan")


# Batched scores


def basis(covariates, n_voxels):
    """Orthonormal (V x m) basis of [1, covariates] over the voxels (float64; the first column is 1/sqrt(V)).

    Residualising two maps on [1, covariates] and correlating them (``similarity.partial_spatial_correlation``)
    equals correlating their projections off this basis. A covariate in the span of the others is left out.
    """
    columns = [np.full(n_voxels, 1.0 / math.sqrt(n_voxels))]
    if covariates is not None:
        rows = np.atleast_2d(np.asarray(covariates, dtype=np.float64))
        if rows.size:
            if rows.shape[1] != n_voxels:
                raise ValueError(f"The covariates have shape {rows.shape}, expected (k, {n_voxels})")
            if not np.all(np.isfinite(rows)):
                raise ValueError("The covariate maps are not finite everywhere")
            for row in rows:
                residual = row - np.column_stack(columns) @ (np.column_stack(columns).T @ row)
                norm = float(np.sqrt(residual @ residual))
                if norm > 1e-10 * max(float(np.sqrt(row @ row)), np.finfo(float).tiny):
                    columns.append(residual / norm)
    return np.column_stack(columns)


def _chunks(n_voxels, chunk):
    return [slice(start, min(start + chunk, n_voxels)) for start in range(0, n_voxels, chunk)]


def lesion_terms(Y, bases, chunk=CHUNK_VOXELS):
    """Sufficient terms of many network maps for :func:`similarity_matrix`: (sum of squares (n,), [Bᵀy (n x m) per basis]).

    :param Y: (n, V) maps (finite), any float dtype (memmaps are read in voxel chunks, in float64).
    :param bases: list of (V x m) bases (:func:`basis`).
    """
    n, n_voxels = Y.shape
    squares = np.zeros(n)
    projections = [np.zeros((n, b.shape[1])) for b in bases]
    for part in _chunks(n_voxels, chunk):
        block = np.asarray(Y[:, part], dtype=np.float64)
        squares += np.einsum("nv,nv->n", block, block)
        for out, b in zip(projections, bases, strict=True):
            out += block @ b[part]
    return squares, projections


def map_terms(C, bases):
    """The terms of :func:`lesion_terms` for (F, V) circuit maps held in memory."""
    C = np.asarray(C, dtype=np.float64)
    return np.einsum("fv,fv->f", C, C), [C @ b for b in bases]


def similarity_from_terms(cross, lesion, maps, index=0):
    """Pearson r (n x F) from the cross products Y Cᵀ and the terms of :func:`lesion_terms` / :func:`map_terms`.

    :param index: which basis of the terms to project off (0: [1] for the plain r; 1: [1, covariates] for the partial r).
    :return: (n x F) float64; NaN where a residual vanishes (a constant map, or a map in the covariates' span).
    """
    yy, y_proj = lesion[0], lesion[1][index]
    cc, c_proj = maps[0], maps[1][index]
    numerator = cross - y_proj @ c_proj.T
    y_res = np.maximum(yy - np.einsum("nm,nm->n", y_proj, y_proj), 0.0)
    c_res = np.maximum(cc - np.einsum("fm,fm->f", c_proj, c_proj), 0.0)
    denominator = np.sqrt(np.outer(y_res, c_res))
    # A residual at rounding level has no defined correlation (as similarity.partial_spatial_correlation)
    tolerance = 1e-12
    valid = (y_res > tolerance * np.maximum(yy, np.finfo(float).tiny))[:, None] & (c_res > tolerance * np.maximum(cc, np.finfo(float).tiny))[None, :]
    out = np.full(numerator.shape, np.nan)
    np.divide(numerator, denominator, out=out, where=valid & (denominator > 0))
    return np.clip(out, -1.0, 1.0, out=out)


def cross_products(Y, C, chunk=CHUNK_VOXELS):
    """Y Cᵀ (n x F) in float64, reading Y in voxel chunks."""
    C = np.atleast_2d(np.asarray(C))
    n, n_voxels = Y.shape
    if C.shape[1] != n_voxels:
        raise ValueError(f"The maps have {C.shape[1]} voxels, the lesion maps {n_voxels}")
    out = np.zeros((n, C.shape[0]))
    for part in _chunks(n_voxels, chunk):
        out += np.asarray(Y[:, part], dtype=np.float64) @ np.asarray(C[:, part], dtype=np.float64).T
    return out


def similarity_matrix(Y, C, covariates=None, chunk=CHUNK_VOXELS):
    """Spatial Pearson r of every lesion map with every circuit, and the partial r given covariate maps.

    Equal to :func:`similarity_score` per pair (the values must be finite; the voxels are all used).

    :param Y: (n, V) lesion network maps.
    :param C: (F, V) circuits.
    :param covariates: (k, V) covariate maps for the partial r, or None.
    :return: (r (n x F), partial r (n x F) or None).
    """
    C = np.atleast_2d(np.asarray(C, dtype=np.float64))
    n_voxels = C.shape[1]
    bases = [basis(None, n_voxels)] + ([basis(covariates, n_voxels)] if covariates is not None else [])
    lesion = lesion_terms(Y, bases, chunk)
    maps = map_terms(C, bases)
    cross = cross_products(Y, C, chunk)
    r = similarity_from_terms(cross, lesion, maps, 0)
    partial = similarity_from_terms(cross, lesion, maps, 1) if covariates is not None else None
    return r, partial


def damage_matrix(occupancy, C):
    """Damage scores (n x F): occupancy (n x V, scipy.sparse or dense) times the circuits (F x V) transposed."""
    C = np.atleast_2d(np.asarray(C, dtype=np.float64))
    if occupancy.shape[1] != C.shape[1]:
        raise ValueError(f"The occupancy has {occupancy.shape[1]} voxels, the circuits {C.shape[1]}")
    out = occupancy @ C.T
    return np.asarray(out, dtype=np.float64)


def roi_matrix(Y, R, chunk=CHUNK_VOXELS):
    """ROI connectivity (n x F) of every lesion map with every ROI (F x V signed weights): (Y Rᵀ) / sum |R| per ROI."""
    R = np.atleast_2d(np.asarray(R, dtype=np.float64))
    totals = np.abs(R).sum(axis=1)
    out = np.full((Y.shape[0], R.shape[0]), np.nan)
    np.divide(cross_products(Y, R, chunk), totals[None, :], out=out, where=totals[None, :] > 0)
    return out

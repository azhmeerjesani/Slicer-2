"""Spatial similarity between maps over the connectome voxels: the one implementation used for every map comparison.

- :func:`pearson`, :func:`spearman` (and :func:`pearson_spearman` for both in one pass): spatial r and
  rho over the voxels finite in both maps;
- :func:`partial_spatial_correlation`: Pearson r of two maps after both are residualised on
  [1, covariates], e.g. the reference maps [μC_r, PC1] (addendum §3.7: sLNM control maps, validation
  scores, the comparison matrix and Lead-DBS parity all compare maps this way);
- :func:`residual_map`: the OLS residual of a map on [1, covariates], the "debiased view" (descriptive;
  not an inferential correction);
- :func:`r2_explained`: the spatial R² of a map on [1, covariates].

Maps are (V,) vectors over the same voxels; covariates are (k, V) (one row per covariate map, as
``reference.Reference.covariates``). Every function uses the voxels where all of its inputs are
finite. Undefined values (fewer voxels than needed, constant maps) are NaN. Pure numpy.
"""

import collections

import numpy as np

MIN_VOXELS = 3
RANK_CACHE_SIZE = 16  # ranks of fixed maps (e.g. the reference maps compared with every lesion map)

DEBIASED_LABEL = "descriptive; not an inferential correction"


def ranks(values):
    """Ranks (1..n) with ties sharing their average rank, as scipy.stats.rankdata (which is slow to import)."""
    order = np.argsort(values, kind="stable")
    ordered = values[order]
    starts = np.flatnonzero(np.r_[True, ordered[1:] != ordered[:-1]])
    ends = np.r_[starts[1:], len(values)]
    out = np.empty(len(values), dtype=np.float64)
    out[order] = np.repeat((starts + ends + 1) / 2.0, ends - starts)
    return out


_rank_cache = collections.OrderedDict()


def _cached_ranks(values):
    """:func:`ranks` of a float64 vector, cached by content (for maps compared again and again, such as the reference maps)."""
    key = (values.size, hash(values.tobytes()))
    cached = _rank_cache.get(key)
    if cached is not None:
        _rank_cache.move_to_end(key)
        return cached
    out = ranks(values)
    out.setflags(write=False)
    _rank_cache[key] = out
    while len(_rank_cache) > RANK_CACHE_SIZE:
        _rank_cache.popitem(last=False)
    return out


def _pearson(a, b):
    """Pearson r of two finite float64 vectors (no voxel selection); NaN for a constant vector."""
    a = a - a.mean()
    b = b - b.mean()
    denominator = np.sqrt((a @ a) * (b @ b))
    return float((a @ b) / denominator) if denominator > 0 else float("nan")


def _vector(values, n_voxels=None, name="map"):
    out = np.asarray(values, dtype=np.float64).ravel()
    if n_voxels is not None and out.size != n_voxels:
        raise ValueError(f"The {name} has {out.size} voxels, expected {n_voxels}")
    return out


def _covariate_rows(covariates, n_voxels):
    """(k, V) float64 covariate matrix (a single (V,) map is one row; None or an empty matrix: no covariates)."""
    if covariates is None:
        return np.zeros((0, n_voxels))
    rows = np.asarray(covariates, dtype=np.float64)
    if rows.size == 0:
        return np.zeros((0, n_voxels))
    rows = np.atleast_2d(rows)
    if rows.ndim != 2 or rows.shape[1] != n_voxels:
        raise ValueError(f"The covariates have shape {rows.shape}, expected (k, {n_voxels})")
    return rows


def _pair(a, b):
    """The two maps over the voxels finite in both."""
    a = _vector(a)
    b = _vector(b, a.size, "second map")
    valid = np.isfinite(a) & np.isfinite(b)
    return a[valid], b[valid]


def pearson(a, b):
    """Spatial Pearson r between two maps over the voxels finite in both (NaN with fewer than 3 voxels or a constant map)."""
    a, b = _pair(a, b)
    return _pearson(a, b) if a.size >= MIN_VOXELS else float("nan")


def spearman(a, b):
    """Spatial Spearman rho (Pearson r of the ranks, ties averaged) between two maps over the voxels finite in both."""
    a, b = _pair(a, b)
    return _pearson(ranks(a), ranks(b)) if a.size >= MIN_VOXELS else float("nan")


def pearson_spearman(a, b):
    """(Pearson r, Spearman rho) between two maps over the voxels finite in both (one voxel selection for both)."""
    a, b = _pair(a, b)
    if a.size < MIN_VOXELS:
        return float("nan"), float("nan")
    return _pearson(a, b), _pearson(ranks(a), ranks(b))


def pearson_rows(rows, b):
    """Pearson r of each row of a (k, V) matrix with a (V,) map, over the voxels finite in ``b`` and in all rows.

    The rows share one voxel selection (as null maps compared with a reference map must); NaN per row
    when it is constant, all NaN with fewer than 3 common voxels.
    """
    b = _vector(b)
    rows = np.asarray(rows, dtype=np.float64)
    rows = rows.reshape(0, b.size) if rows.size == 0 else np.atleast_2d(rows)
    if rows.shape[1] != b.size:
        raise ValueError(f"The rows have {rows.shape[1]} voxels and the map {b.size}")
    valid = np.isfinite(b) & np.all(np.isfinite(rows), axis=0)
    out = np.full(rows.shape[0], np.nan)
    if np.count_nonzero(valid) < MIN_VOXELS or rows.shape[0] == 0:
        return out
    rows = rows[:, valid] - rows[:, valid].mean(axis=1, keepdims=True)
    b = b[valid] - b[valid].mean()
    denominator = np.sqrt(np.einsum("ij,ij->i", rows, rows) * (b @ b))
    np.divide(rows @ b, denominator, out=out, where=denominator > 0)
    return out


def pearson_spearman_rows(rows, b):
    """(Pearson r, Spearman rho) of each row of a (k, V) matrix with a (V,) map, over the voxels finite in ``b`` and in all rows.

    The rows are meant to be fixed maps (e.g. the reference maps): their ranks are cached by content.

    :return: two (k,) arrays (NaN where undefined).
    """
    b = _vector(b)
    rows = np.asarray(rows, dtype=np.float64)
    rows = rows.reshape(0, b.size) if rows.size == 0 else np.atleast_2d(rows)
    if rows.shape[1] != b.size:
        raise ValueError(f"The rows have {rows.shape[1]} voxels and the map {b.size}")
    valid = np.isfinite(b) & np.all(np.isfinite(rows), axis=0)
    r = np.full(rows.shape[0], np.nan)
    rho = np.full(rows.shape[0], np.nan)
    if np.count_nonzero(valid) < MIN_VOXELS:
        return r, rho
    if not valid.all():
        b, rows = b[valid], rows[:, valid]
    b_ranks = ranks(b)
    for index, row in enumerate(rows):
        r[index] = _pearson(row, b)
        rho[index] = _pearson(_cached_ranks(row), b_ranks)
    return r, rho


def _fit(maps, covariates):
    """OLS fit of the rows of ``maps`` (m, V) on [1, covariates] over the voxels finite in every input.

    :return: (residuals (m, n), valid (V,) boolean mask of the n voxels used, number of design columns).
    """
    n_voxels = maps.shape[1]
    rows = _covariate_rows(covariates, n_voxels)
    valid = np.all(np.isfinite(maps), axis=0) & np.all(np.isfinite(rows), axis=0)
    if valid.all():  # copies (centred in place below); a plain copy is much faster than boolean indexing
        y, x = np.array(maps), np.array(rows)
    else:
        y, x = maps[:, valid], rows[:, valid]
    if valid.any():
        y -= y.mean(axis=1, keepdims=True)
        # Centre and scale the covariates (same column space with the intercept, better conditioned);
        # a constant covariate (zero up to rounding once centred) is the intercept itself and is left out
        raw = np.sqrt(np.einsum("ij,ij->i", x, x))
        x -= x.mean(axis=1, keepdims=True)
        scale = np.sqrt(np.einsum("ij,ij->i", x, x))
        keep = scale > 1e-12 * raw
        x = x[keep] / scale[keep, np.newaxis]
    else:
        x = x[:0]
    if len(x):
        coefficients = np.linalg.lstsq(x.T, y.T, rcond=None)[0]  # (k, m)
        y -= coefficients.T @ x
    return y, valid, 1 + len(x)


def partial_spatial_correlation(a, b, covariates):
    """Partial spatial correlation of two maps given covariate maps.

    Both maps are residualised on [1, covariates] by least squares over the voxels finite in every
    input, then correlated (Pearson). With no covariates this is the Pearson r.

    :param a: (V,) map.
    :param b: (V,) map.
    :param covariates: (k, V) covariate maps (e.g. ``reference.covariates(("mu_r", "pc1"))``), or one (V,) map.
    :return: partial r; NaN when undefined (fewer than k + 3 voxels, or a map explained exactly).
    """
    a = _vector(a)
    b = _vector(b, a.size, "second map")
    residuals, valid, n_columns = _fit(np.vstack((a, b)), covariates)
    if np.count_nonzero(valid) < max(MIN_VOXELS, n_columns + 2):
        return float("nan")
    ra, rb = residuals
    # A residual at rounding level (the map lies in the covariates' span) has no defined correlation
    tolerance = 1e-12
    if ra @ ra <= tolerance * max(_sum_squares(a[valid]), np.finfo(float).tiny) or rb @ rb <= tolerance * max(_sum_squares(b[valid]), np.finfo(float).tiny):
        return float("nan")
    return _pearson(ra, rb)


def _sum_squares(values):
    centred = values - values.mean()
    return float(centred @ centred)


def residual_map(map_vec, covariates):
    """OLS residual of a map on [1, covariates]: the "debiased view" (descriptive; not an inferential correction).

    :param map_vec: (V,) map.
    :param covariates: (k, V) covariate maps (e.g. [μC_r, PC1]), or one (V,) map.
    :return: (V,) float64 residual (mean 0 over the voxels used); NaN where any input is not finite.
    """
    values = _vector(map_vec)
    out = np.full(values.size, np.nan)
    residuals, valid, n_columns = _fit(values[np.newaxis], covariates)
    if np.count_nonzero(valid) >= n_columns:
        out[valid] = residuals[0]
    return out


def r2_explained(map_vec, covariates):
    """Spatial R² of a map on [1, covariates] (1 - residual / total sum of squares over the voxels used).

    :return: R² in [0, 1]; NaN for a constant map or fewer than k + 2 voxels.
    """
    values = _vector(map_vec)
    residuals, valid, n_columns = _fit(values[np.newaxis], covariates)
    if np.count_nonzero(valid) < max(MIN_VOXELS, n_columns + 1):
        return float("nan")
    total = _sum_squares(values[valid])
    if total <= 0:
        return float("nan")
    residual = float(residuals[0] @ residuals[0])
    return float(min(1.0, max(0.0, 1.0 - residual / total)))

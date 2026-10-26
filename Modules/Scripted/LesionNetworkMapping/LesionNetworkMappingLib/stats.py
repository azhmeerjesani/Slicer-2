"""Statistics for lesion network mapping: thresholds, overlap, specificity tests and QA.

Maps are numpy arrays; cohort functions take stacks of shape (n_maps, V) where V is the number of
voxels (masked vectors or flattened volumes).
"""

import numpy as np
from scipy import stats as scipy_stats


def t_to_p(t, df, two_sided=True):
    """p-value of a t statistic (two-sided by default)."""
    p = scipy_stats.t.sf(np.abs(t) if two_sided else t, df)
    return 2.0 * p if two_sided else p


def p_to_t(p, df, two_sided=True):
    """t threshold for a p-value (two-sided by default)."""
    return scipy_stats.t.isf(np.asarray(p) / 2.0 if two_sided else p, df)


def bonferroni_t_threshold(n_voxels, df, alpha=0.05):
    """Two-sided |t| threshold controlling the family-wise error over n_voxels tests (Bonferroni)."""
    return float(p_to_t(alpha / n_voxels, df))


def _suprathreshold(values, threshold, sign):
    values = np.asarray(values)
    if sign == "both":
        return np.abs(values) >= threshold
    if sign == "pos":
        return values >= threshold
    if sign == "neg":
        return values <= -threshold
    raise ValueError("sign must be 'both', 'pos' or 'neg'")


def threshold_map(values, threshold, sign="both"):
    """Copy of a map with sub-threshold values set to 0 (``|x| >= t``, ``x >= t`` or ``x <= -t``)."""
    values = np.asarray(values)
    return np.where(_suprathreshold(values, threshold, sign), values, 0).astype(values.dtype)


def binarize(values, threshold=None, sign="both"):
    """Boolean map: supra-threshold voxels, or non-zero voxels when threshold is None."""
    if threshold is None:
        return np.asarray(values) != 0
    return _suprathreshold(values, threshold, sign)


def overlap(maps_binary):
    """Overlap of binary maps (n, V): (count per voxel, percent of maps per voxel)."""
    maps = np.asarray(maps_binary) != 0
    count = maps.sum(axis=0).astype(np.int32)
    return count, 100.0 * count / max(maps.shape[0], 1)


def sensitivity_map(percent, min_percent=90.0):
    """Voxels shared by at least ``min_percent`` percent of the maps."""
    return np.asarray(percent) >= min_percent


def conjunction(*binary_maps):
    """Voxels that are non-zero in all maps."""
    if not binary_maps:
        raise ValueError("conjunction needs at least one map")
    return np.logical_and.reduce([np.asarray(m) != 0 for m in binary_maps])


def welch_t(cases, controls):
    """Voxelwise Welch two-sample t test (unequal variances): cases (n1, V) vs controls (n2, V).

    :return: (t, df) arrays of shape (V,); t is 0 where both groups have zero variance.
    """
    a = np.asarray(cases, dtype=np.float64)
    b = np.asarray(controls, dtype=np.float64)
    n1, n2 = a.shape[0], b.shape[0]
    if n1 < 2 or n2 < 2:
        raise ValueError("Each group needs at least 2 maps")
    va = a.var(axis=0, ddof=1) / n1
    vb = b.var(axis=0, ddof=1) / n2
    se2 = va + vb
    t = np.zeros_like(se2)
    np.divide(a.mean(axis=0) - b.mean(axis=0), np.sqrt(se2), out=t, where=se2 > 0)
    denominator = va**2 / (n1 - 1) + vb**2 / (n2 - 1)
    df = np.full_like(se2, n1 + n2 - 2.0)
    np.divide(se2**2, denominator, out=df, where=denominator > 0)
    return t, df


def liebermeister(case_bin, control_bin, min_fraction=0.1):
    """Voxelwise one-sided Liebermeister test (cases more often affected than controls).

    The Liebermeister quasi-exact test is Fisher's exact test on the 2x2 table with one observation
    added to the concordant cells: [[a + 1, b], [c, d + 1]] (a/b: cases with/without the voxel,
    c/d: controls with/without). Computed with the hypergeometric distribution, once per distinct table.

    :param case_bin: (n1, V) binary maps of cases; control_bin: (n2, V) binary maps of controls.
    :param min_fraction: voxels present in fewer than this fraction of all maps get p = 1.
    :return: (V,) p-values.
    """
    cases = np.asarray(case_bin) != 0
    controls = np.asarray(control_bin) != 0
    n1, n2 = cases.shape[0], controls.shape[0]
    a = cases.sum(axis=0).astype(np.int64)
    c = controls.sum(axis=0).astype(np.int64)
    p = np.ones(a.shape, dtype=np.float64)
    valid = (a + c) >= max(min_fraction * (n1 + n2), 1e-12)
    if not valid.any():
        return p
    keys, inverse = np.unique(a[valid] * (n2 + 1) + c[valid], return_inverse=True)
    ua, uc = keys // (n2 + 1), keys % (n2 + 1)
    # X = cases in the "present" column ~ Hypergeom(total, present, cases); p = P(X >= a + 1)
    p[valid] = scipy_stats.hypergeom.sf(ua, n1 + n2 + 2, ua + 1 + uc, n1 + 1)[inverse]
    return p


def _tail(values, tail):
    if tail == "both":
        return np.abs(values)
    if tail == "pos":
        return values
    if tail == "neg":
        return -values
    raise ValueError("tail must be 'both', 'pos' or 'neg'")


def permutation_maxstat_fwe(cases, controls, n_perm=1000, seed=0, stat="welch", tail="both", batch=None):
    """Family-wise error corrected p-values by label permutation with the maximum statistic.

    :param cases: (n1, V) maps; controls: (n2, V) maps (e.g. Fz or T maps).
    :param stat: "welch" (Welch t) or "meandiff" (difference of means).
    :param tail: "both" (max |stat|), "pos" (cases > controls) or "neg".
    :return: (V,) FWE-corrected p-values ``(1 + #{max_null >= observed}) / (n_perm + 1)``.
    """
    if stat not in ("welch", "meandiff"):
        raise ValueError("stat must be 'welch' or 'meandiff'")
    data = np.vstack([np.asarray(cases, dtype=np.float64), np.asarray(controls, dtype=np.float64)])
    n1 = np.asarray(cases).shape[0]
    n, n_voxels = data.shape
    n2 = n - n1
    if n1 < 2 or n2 < 2:
        raise ValueError("Each group needs at least 2 maps")
    data -= data.mean(axis=0)  # does not change the statistics; improves the sum-of-squares accuracy
    squares = data**2
    total, total_sq = data.sum(axis=0), squares.sum(axis=0)

    def statistic(groups):
        """Statistic for each row of a (B, n) group-1 indicator matrix."""
        s1 = groups @ data
        mean1 = s1 / n1
        mean2 = (total - s1) / n2
        if stat == "meandiff":
            return mean1 - mean2
        q1 = groups @ squares
        var1 = np.maximum(q1 - n1 * mean1**2, 0) / (n1 - 1)
        var2 = np.maximum((total_sq - q1) - n2 * mean2**2, 0) / (n2 - 1)
        se = np.sqrt(var1 / n1 + var2 / n2)
        out = np.zeros_like(se)
        np.divide(mean1 - mean2, se, out=out, where=se > 0)
        return out

    labels = np.zeros((1, n))
    labels[0, :n1] = 1.0
    observed = _tail(statistic(labels)[0], tail)
    rng = np.random.default_rng(seed)
    batch = batch or max(1, 2_000_000 // max(n_voxels, 1))
    max_null = np.empty(n_perm)
    for start in range(0, n_perm, batch):
        size = min(batch, n_perm - start)
        groups = np.zeros((size, n))
        for row in range(size):
            groups[row, rng.permutation(n)[:n1]] = 1.0
        max_null[start:start + size] = _tail(statistic(groups), tail).max(axis=1)
    max_null.sort()
    exceed = n_perm - np.searchsorted(max_null, observed, side="left")
    return (1.0 + exceed) / (n_perm + 1.0)


def symptom_correlation(maps, scores, method="pearson"):
    """Voxelwise correlation between maps (n, V) and symptom scores (n,), as in symptom LNM.

    :param method: "pearson" or "spearman".
    :return: (r, p) arrays of shape (V,), two-sided p with n - 2 degrees of freedom.
    """
    x = np.asarray(maps, dtype=np.float64)
    y = np.asarray(scores, dtype=np.float64).ravel()
    n = x.shape[0]
    if y.shape[0] != n or n < 3:
        raise ValueError("Need at least 3 maps and one score per map")
    if method == "spearman":
        x = scipy_stats.rankdata(x, axis=0)
        y = scipy_stats.rankdata(y)
    elif method != "pearson":
        raise ValueError("method must be 'pearson' or 'spearman'")
    x = x - x.mean(axis=0)
    y = y - y.mean()
    denominator = np.sqrt((x**2).sum(axis=0) * (y**2).sum())
    r = np.zeros(x.shape[1])
    np.divide(y @ x, denominator, out=r, where=denominator > 0)
    r = np.clip(r, -1.0, 1.0)
    with np.errstate(divide="ignore"):
        t = r * np.sqrt((n - 2) / np.maximum(1.0 - r**2, 0.0))
    return r, t_to_p(t, n - 2)


def spatial_correlation(a, b, mask=None):
    """Pearson correlation between two maps over the finite voxels (within mask if given); NaN if undefined."""
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    valid = np.isfinite(a) & np.isfinite(b)
    if mask is not None:
        valid &= np.asarray(mask).ravel() != 0
    if np.count_nonzero(valid) < 2:
        return float("nan")
    a = a[valid] - a[valid].mean()
    b = b[valid] - b[valid].mean()
    denominator = np.sqrt((a**2).sum() * (b**2).sum())
    return float((a @ b) / denominator) if denominator > 0 else float("nan")


def peak_table(values, grid, top_k=10, min_distance_mm=10.0, mask_indices=None, sign="both"):
    """Strongest peaks of a map, at least ``min_distance_mm`` apart.

    :param values: masked vector (with ``mask_indices``) or volume indexed [i, j, k] on ``grid``.
    :param grid: :class:`geometry.Grid` of the map.
    :param sign: rank by |value| ("both"), value ("pos") or -value ("neg").
    :return: list of dicts {"value", "ijk", "world"} sorted by decreasing strength (world = RAS mm).
    """
    values = np.asarray(values, dtype=np.float64).ravel()
    positions = np.arange(values.size) if mask_indices is None else np.asarray(mask_indices)
    if positions.size != values.size:
        raise ValueError("values and mask_indices have different lengths")
    score = _tail(values, sign)
    candidates = np.flatnonzero(np.isfinite(score) & (score > 0))
    ijk = np.stack(np.unravel_index(positions[candidates], grid.shape), axis=1)
    world = grid.voxel_to_world(ijk)
    score = score[candidates]
    active = np.ones(len(candidates), dtype=bool)
    peaks = []
    while len(peaks) < top_k and active.any():
        best = int(np.argmax(np.where(active, score, -np.inf)))
        peaks.append({"value": float(values[candidates[best]]), "ijk": tuple(int(v) for v in ijk[best]), "world": tuple(float(v) for v in world[best])})
        active &= ((world - world[best]) ** 2).sum(axis=1) >= min_distance_mm**2
        active[best] = False
    return peaks

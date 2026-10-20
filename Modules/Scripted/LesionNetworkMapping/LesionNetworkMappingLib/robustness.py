"""Robustness of a lesion network map to the threshold rule and the overlap criterion.

The primary threshold and overlap criterion are fixed by the recipe or the locked plan. The sweep
re-thresholds the existing per-lesion maps over published alternatives (Cohen & Fox 2020; vdH 2026)
and reports, for every setting, how far the result moves from the primary: voxels and volume, Dice
of the binary maps, Pearson r of the overlap maps, whether the primary peak persists within 10 mm and
whether a region of interest still survives. :func:`conclusion_flips` names the first setting where
the conclusion changes. The sweep is secondary: it never replaces the primary and never selects a
"best" setting.

Settings::

    FWE (Bonferroni)                    always
    |T| >= 5, 7, 9, 12 (reference N 1000)  only for N >= 900; listed as disabled otherwise
    top 5%, 10%, 25% of |map|           always (rank rules, applied to each lesion's map)
    overlap >= 50, 75, 90, 95, 100% of lesions
"""

import csv
import math

from . import thresholds

DEFAULT_OVERLAPS = (50, 75, 90, 95, 100)
SWEEP_T_VALUES = (5, 7, 9, 12)
SWEEP_TOP_PCT = (5, 10, 25)
SWEEP_REFERENCE_N = 1000
PEAK_RADIUS_MM = 10.0
TSV_COLUMNS = (
    "setting",
    "overlap_pct",
    "threshold",
    "voxels",
    "ml",
    "dice_vs_primary",
    "r_vs_primary",
    "peak_persists",
    "roi_survives",
    "primary",
    "note",
)


def sweep_label(rule):
    """Short label of a sweep setting, e.g. "FWE (Bonferroni)", "|T| ≥ 7 (N≈1000)" or "Top 10%"."""
    rule = thresholds.normalize_rule(rule)
    kind = rule["kind"]
    if kind == "fwe_bonferroni":
        return thresholds.RULE_LABELS[kind] if rule["alpha"] == 0.05 else f"FWE (Bonferroni, alpha={rule['alpha']:g})"
    if kind == "top_pct":
        sign = {"both": "", "pos": " (positive)", "neg": " (negative)"}[rule["sign"]]
        return f"Top {rule['value']:g}%{sign}"
    if kind == "fz_abs":
        return f"|Fz| ≥ {rule['value']:g}"
    reference = f" (N≈{rule['reference_n']})" if rule.get("reference_n") else ""
    return f"|T| ≥ {rule['value']:g}{reference}"


def DEFAULT_RULES(n, statistic="T", sign="both"):  # noqa: N802 (a computed constant)
    """The default sweep settings for a map with ``n`` subjects.

    :param statistic: statistic of the swept maps ("T" or "Fz"); T rules need T maps.
    :param sign: sign of the top-x% rules ("both", "pos" or "neg").
    :return: list of {"rule", "label", "enabled", "reason"}; disabled entries carry the reason.
    """
    n = int(n)
    entries = [_entry({"kind": "fwe_bonferroni", "alpha": 0.05}, statistic)]
    for value in SWEEP_T_VALUES:
        entry = _entry({"kind": "custom_t", "value": float(value), "reference_n": SWEEP_REFERENCE_N}, statistic)
        if entry["enabled"] and n < thresholds.T_CONVENTION_MIN_N:
            entry.update(enabled=False, reason=f"|T| ≥ {value} needs N ≥ {thresholds.T_CONVENTION_MIN_N}; this map has N={n}")
        entries.append(entry)
    for value in SWEEP_TOP_PCT:
        entries.append(_entry({"kind": "top_pct", "value": float(value), "statistic": statistic, "sign": sign}, statistic))
    return entries


def _entry(rule, statistic):
    rule = thresholds.normalize_rule(rule)
    enabled, reason = True, None
    if _rule_statistic(rule) != statistic:
        enabled, reason = False, f"needs {_rule_statistic(rule)} maps; these are {statistic} maps"
    return {"rule": rule, "label": sweep_label(rule), "enabled": enabled, "reason": reason}


def _rule_statistic(rule):
    if rule["kind"] == "top_pct":
        return rule["statistic"]
    return "Fz" if rule["kind"] == "fz_abs" else "T"


def _scores(maps, sign):
    import numpy as np

    if sign == "pos":
        return maps
    if sign == "neg":
        return np.negative(maps)
    return np.abs(maps)


def _threshold_maps(maps, rule, n, v, sign, gsr):
    """Binary per-lesion maps (L, V) of a rule and the resolved threshold(s).

    Fixed rules resolve once; top-x% rules resolve on each lesion's map (a tuple of Resolved).
    Comparisons are made in float64, one lesion at a time (as :func:`thresholds.fraction_connected`).
    """
    import numpy as np

    binary = np.zeros(maps.shape, dtype=bool)
    if rule["kind"] == "top_pct":
        resolved = tuple(thresholds.resolve(rule, n, v, gsr=gsr, values=row) for row in maps)
        cuts = [r.value for r in resolved]
        sign = rule["sign"]
    else:
        resolved = thresholds.resolve(rule, n, v, gsr=gsr)
        cuts = [resolved.value] * maps.shape[0]
    with np.errstate(invalid="ignore"):
        for index, cut in enumerate(cuts):
            if not math.isnan(cut):
                binary[index] = _scores(maps[index].astype(np.float64), sign) >= cut
    return binary, resolved


def _overlap_binary(counts, n_lesions, overlap_pct):
    """Voxels where at least ``overlap_pct`` % of the lesions pass."""
    return counts * 100.0 >= overlap_pct * n_lesions - 1e-9


def _dice(a, b):
    total = int(a.sum()) + int(b.sum())
    return math.nan if total == 0 else 2.0 * int((a & b).sum()) / total


def _pearson(a, b):
    import numpy as np

    a = a - a.mean()
    b = b - b.mean()
    denominator = math.sqrt(float(a @ a) * float(b @ b))
    return math.nan if denominator == 0.0 else float(np.clip(float(a @ b) / denominator, -1.0, 1.0))


def _voxel_world(grid, mask_indices, v):
    """World coordinates (V, 3) of the swept voxels (flat C-order indices into the grid)."""
    import numpy as np

    from .geometry import Grid

    if not isinstance(grid, Grid):
        grid = Grid(grid.shape, grid.affine)
    if mask_indices is None:
        if grid.n_voxels != v:
            raise ValueError(f"The maps have {v} voxels but the grid has {grid.n_voxels}; pass mask_indices")
        mask_indices = np.arange(v)
    mask_indices = np.asarray(mask_indices).reshape(-1)
    if mask_indices.size != v:
        raise ValueError(f"The maps have {v} voxels but there are {mask_indices.size} mask indices")
    ijk = np.stack(np.unravel_index(mask_indices, grid.shape), axis=1)
    return grid.voxel_to_world(ijk)


def _roi_vector(roi, v):
    import numpy as np

    roi = np.asarray(roi)
    if roi.dtype == bool:
        if roi.size != v:
            raise ValueError(f"The ROI mask has {roi.size} voxels; the maps have {v}")
        return roi.reshape(-1)
    out = np.zeros(v, dtype=bool)
    out[roi.reshape(-1).astype(np.int64)] = True
    return out


def threshold_sweep(
    maps,
    n,
    v,
    primary_rule,
    primary_overlap,
    rules=None,
    overlaps=None,
    sign="both",
    voxel_volume_ml=None,
    grid=None,
    mask_indices=None,
    roi=None,
    peak_world=None,
    statistic="T",
    gsr=None,
):
    """Re-threshold existing per-lesion maps over threshold rules and overlap criteria.

    For each rule, every lesion's map is thresholded and the overlap map (% of lesions that pass at
    each voxel) is the mean of the binary maps: O(L·V) per rule. Each overlap criterion then gives
    the binary map of voxels where at least that % of lesions pass.

    :param maps: per-lesion maps, shape (L, V) (T maps by default, see ``statistic``), in-mask voxels.
    :param n: number of connectome subjects of the maps (df = n - 1).
    :param v: number of voxels tested (for the FWE rule; usually ``maps.shape[1]``).
    :param primary_rule: the fixed primary threshold rule.
    :param primary_overlap: the fixed primary overlap criterion, in % of lesions.
    :param rules: rule dicts or :func:`DEFAULT_RULES` entries; default: ``DEFAULT_RULES(n, statistic, sign)``.
    :param overlaps: overlap criteria in %; default :data:`DEFAULT_OVERLAPS`.
    :param sign: sign used by fixed (T/Fz) rules: "both" (|map| ≥ t), "pos" or "neg". Top-x% rules
      use their own sign.
    :param voxel_volume_ml: volume of one voxel in ml; default from ``grid`` (else ``ml`` is None).
    :param grid: ``geometry.Grid`` of the maps, needed for peak persistence.
    :param mask_indices: flat C-order grid indices of the V voxels (default: the maps cover the grid).
    :param roi: optional region of interest, a boolean vector of length V or voxel indices into V.
    :param peak_world: RAS mm position of the primary peak; default: the voxel with the highest
      primary overlap (ties broken by the largest mean score across lesions).
    :param statistic: statistic of ``maps`` ("T" or "Fz"); rules for the other statistic are disabled.
    :param gsr: the connectome's ``preprocessing.gsr`` (passed to :func:`thresholds.resolve`).
    :return: list of rows (dicts), the primary first, then each enabled rule and overlap (overlaps
      ascending within a rule), then one row per disabled rule. Row keys: ``rule_label``, ``rule``,
      ``resolved`` (a ``thresholds.Resolved``, or a tuple of one per lesion for top-x% rules),
      ``overlap_pct``, ``voxels``, ``ml``, ``dice_vs_primary``, ``r_vs_primary`` (overlap maps),
      ``peak_persists`` and ``roi_survives`` (None when not computable), ``is_primary``, ``enabled``
      and ``reason`` (why a rule is disabled).
    """
    import numpy as np

    maps = np.asarray(maps, dtype=np.float32)
    if maps.ndim != 2 or maps.shape[0] < 1:
        raise ValueError(f"maps must have shape (L, V) with L >= 1, got {maps.shape}")
    if sign not in thresholds.SIGNS:
        raise ValueError(f"sign must be one of {', '.join(thresholds.SIGNS)}, got {sign!r}")
    n_lesions, n_vox = maps.shape
    overlaps = sorted(float(p) for p in (DEFAULT_OVERLAPS if overlaps is None else overlaps))
    for pct in [*overlaps, primary_overlap]:
        if not 0.0 < float(pct) <= 100.0:
            raise ValueError(f"Overlap criteria must be in (0, 100] %, got {pct}")
    entries = DEFAULT_RULES(n, statistic, sign) if rules is None else [_as_entry(rule, statistic) for rule in rules]
    if voxel_volume_ml is None and grid is not None:
        voxel_volume_ml = float(abs(np.linalg.det(np.asarray(grid.affine, dtype=np.float64)[:3, :3]))) / 1000.0
    roi_vector = None if roi is None else _roi_vector(roi, n_vox)

    # Primary
    primary_rule = thresholds.normalize_rule(primary_rule)
    binary, primary_resolved = _threshold_maps(maps, primary_rule, n, v, sign, gsr)
    counts = binary.sum(axis=0)
    primary_overlap_map = counts * (100.0 / n_lesions)
    primary_binary = _overlap_binary(counts, n_lesions, float(primary_overlap))
    near_peak = None
    if grid is not None:
        world = _voxel_world(grid, mask_indices, n_vox)
        if peak_world is None and primary_binary.any():
            primary_sign = primary_rule.get("sign", sign)
            mean_score = np.zeros(n_vox, dtype=np.float64)
            for lesion_map in maps:
                mean_score += np.nan_to_num(_scores(lesion_map.astype(np.float64), primary_sign), nan=-np.inf)
            candidates = np.flatnonzero(primary_overlap_map == primary_overlap_map[primary_binary].max())
            peak_world = world[candidates[np.argmax(mean_score[candidates])]]
        if peak_world is not None:
            distance = np.linalg.norm(world - np.asarray(peak_world, dtype=np.float64).reshape(1, 3), axis=1)
            near_peak = distance <= PEAK_RADIUS_MM

    def row(entry, resolved, overlap_pct, overlap_map, binary_map, is_primary=False):
        voxels = int(binary_map.sum())
        return {
            "rule_label": entry["label"],
            "rule": entry["rule"],
            "resolved": resolved,
            "overlap_pct": overlap_pct,
            "voxels": voxels,
            "ml": None if voxel_volume_ml is None else voxels * voxel_volume_ml,
            "dice_vs_primary": _dice(binary_map, primary_binary),
            "r_vs_primary": _pearson(overlap_map, primary_overlap_map),
            "peak_persists": None if near_peak is None else bool((binary_map & near_peak).any()),
            "roi_survives": None if roi_vector is None else bool((binary_map & roi_vector).any()),
            "is_primary": is_primary,
            "enabled": True,
            "reason": None,
        }

    primary_entry = {"rule": primary_rule, "label": f"Primary: {sweep_label(primary_rule)}"}
    rows = [row(primary_entry, primary_resolved, float(primary_overlap), primary_overlap_map, primary_binary, is_primary=True)]
    disabled = []
    for entry in entries:
        if not entry["enabled"]:
            disabled.append(entry)
            continue
        binary, resolved = _threshold_maps(maps, entry["rule"], n, v, sign, gsr)
        counts = binary.sum(axis=0)
        overlap_map = counts * (100.0 / n_lesions)
        for pct in overlaps:
            rows.append(row(entry, resolved, pct, overlap_map, _overlap_binary(counts, n_lesions, pct)))
    for entry in disabled:
        rows.append(
            {
                "rule_label": entry["label"],
                "rule": entry["rule"],
                "resolved": None,
                "overlap_pct": None,
                "voxels": None,
                "ml": None,
                "dice_vs_primary": None,
                "r_vs_primary": None,
                "peak_persists": None,
                "roi_survives": None,
                "is_primary": False,
                "enabled": False,
                "reason": entry["reason"],
            },
        )
    return rows


def _as_entry(rule, statistic):
    if isinstance(rule, dict) and "rule" in rule and "kind" not in rule:
        entry = {"label": None, "enabled": True, "reason": None, **rule}
        entry["rule"] = thresholds.normalize_rule(entry["rule"])
        entry["label"] = entry["label"] or sweep_label(entry["rule"])
        if entry["enabled"] and _rule_statistic(entry["rule"]) != statistic:
            entry.update(enabled=False, reason=f"needs {_rule_statistic(entry['rule'])} maps; these are {statistic} maps")
        return entry
    return _entry(rule, statistic)


def conclusion_flips(rows):
    """The first sweep setting (in row order) where the ROI or the primary peak stops surviving.

    Only criteria that hold for the primary row are checked (a peak or ROI that does not survive
    the primary cannot "stop" surviving).

    :return: {"rule_label", "overlap_pct", "lost": ["ROI", "peak"], "text"}, or None when no
      conclusion flips (or nothing can be checked).
    """
    primary = next((r for r in rows if r.get("is_primary")), None)
    checks = [("roi_survives", "ROI"), ("peak_persists", "peak")]
    if primary is not None:
        checks = [(key, name) for key, name in checks if primary.get(key) is True]
    for row in rows:
        if row.get("is_primary") or not row.get("enabled", True):
            continue
        lost = [name for key, name in checks if row.get(key) is False]
        if lost:
            what = " and ".join({"ROI": "the ROI no longer survives", "peak": "the peak no longer persists within 10 mm"}[name] for name in lost)
            text = f"Conclusion flips at {row['rule_label']}, ≥ {row['overlap_pct']:g}% overlap ({what})."
            return {"rule_label": row["rule_label"], "overlap_pct": row["overlap_pct"], "lost": lost, "text": text}
    return None


def _threshold_text(resolved):
    if resolved is None:
        return "n/a"
    if isinstance(resolved, tuple):
        values = [r.value for r in resolved if math.isfinite(r.value)]
        if not values:
            return "n/a"
        statistic = resolved[0].statistic
        return f"per map {statistic} {min(values):.4g} to {max(values):.4g}" if len(values) > 1 else f"{statistic} {values[0]:.4g}"
    if not math.isfinite(resolved.value):
        return "n/a"
    return f"{resolved.statistic} {resolved.value:.4g}"


def _cell(value):
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return "n/a" if not math.isfinite(value) else f"{value:.6g}"
    return str(value)


def write_sweep_tsv(rows, path):
    """Write sweep rows as a tab-separated table (UTF-8, "n/a" for missing values, BIDS style).

    Columns: :data:`TSV_COLUMNS`. ``threshold`` is the resolved value (the per-lesion range for
    top-x% rules) and ``note`` the reason a setting is disabled, or the threshold banner.
    """
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(TSV_COLUMNS)
        for row in rows:
            resolved = row.get("resolved")
            banners = []
            if isinstance(resolved, tuple):
                banners = sorted({r.banner for r in resolved if r.banner})
            elif resolved is not None and resolved.banner:
                banners = [resolved.banner]
            note = row.get("reason") or " ".join(banners)
            writer.writerow(
                [
                    row["rule_label"],
                    _cell(row.get("overlap_pct")),
                    _threshold_text(resolved),
                    _cell(row.get("voxels")),
                    _cell(row.get("ml")),
                    _cell(row.get("dice_vs_primary")),
                    _cell(row.get("r_vs_primary")),
                    _cell(row.get("peak_persists")),
                    _cell(row.get("roi_survives")),
                    _cell(bool(row.get("is_primary"))),
                    note.replace("\t", " ").replace("\n", " ") or "n/a",
                ],
            )
    return path

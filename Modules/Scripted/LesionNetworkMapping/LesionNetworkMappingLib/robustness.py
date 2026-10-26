"""Robustness of a lesion network map: threshold rules, connectomes, lesion resampling and seed definitions.

Every analysis here is secondary: it never replaces the primary map and never selects a "best" setting.

**Threshold sweep** (:func:`threshold_sweep`). The primary threshold and overlap criterion are fixed by the
recipe or the locked plan. The sweep re-thresholds the existing per-lesion maps over published alternatives
(Cohen & Fox 2020; vdH 2026) and reports, for every setting, how far the result moves from the primary:
voxels and volume, Dice of the binary maps, Pearson r of the overlap maps, whether the primary peak persists
within 10 mm and whether a region of interest still survives. :func:`conclusion_flips` names the first
setting where the conclusion changes. Settings::

    FWE (Bonferroni)                    always
    |T| >= 5, 7, 9, 12 (reference N 1000)  only for N >= 900; listed as disabled otherwise
    top 5%, 10%, 25% of |map|           always (rank rules, applied to each lesion's map)
    overlap >= 50, 75, 90, 95, 100% of lesions

**Replication on another connectome** (:func:`replicate_on`, addendum §3.9). The same lesions are mapped on
connectome B (cohortmaps). Each lesion map and the group maps are compared by the unthresholded spatial r,
by Dice where each connectome's maps are thresholded at the rule **re-resolved for that connectome's own N
and V** (an FWE threshold for N = 1000 is not the one for N = 100), and by the persistence of the top 3
peaks within 10 mm. r < 0.7 is flagged next to the reference values (:data:`CROSS_CONNECTOME_REFERENCE`).
Group-PCA maps compare |r| only (a component's sign and scale are arbitrary). Cross-connectome r measures
how much the conclusion depends on the connectome; it is not split-half sampling noise.

**Lesion split-half** (:func:`lesion_split_half`; Ferguson 2019). The lesions are split at random into two
halves (``n_splits`` times, seeded, optionally within strata); each split gives the spatial r of the two
halves' overlap maps, the Dice of their sensitivity maps and, with a design, the r of their GLM t maps,
computed from sufficient statistics (XᵀX, XᵀY, Σy²) of each half.

**Leave-one-lesion-out influence** (:func:`leave_one_lesion_out`). Removing one lesion is a downdate of the
overlap counts and of the GLM's sufficient statistics (no refit): the sensitivity map's Dice against the
full map and the change of the GLM peak; the top-k influential lesions.

**Seed variants** (:func:`seed_variants`) and **hemisphere consolidation** (:func:`consolidate_hemisphere`)
write derived lesion files to a run folder (never in ``lesions/``; the traced masks are never modified),
with a manifest, and map them with cohortmaps for comparison. Mirroring is approximate
(``lesionops.MIRROR_NOTE``); consolidation is a recorded analysis option, never automatic.

**Connectome split-half** (:func:`connectome_split_half`): the odd/even-subject split of an engine run with
``--split-half`` is labelled :data:`CONNECTOME_SPLIT_HALF_LABEL`; it is reported with the connectome
results (connectomes.tsv), never next to the lesion split-half.

Outputs of a run folder: sweep.tsv, connectomes.tsv, splithalf.tsv (+ splithalf.npz), influence.tsv,
seeds.tsv (+ variants.json), consolidation.json (+ lesions.tsv) and ``<analysis>.json``. Paths in them are
relative to the run folder (SPEC 9.7 rule 10). Each run is registered (kind "robustness") with a summary that
the reporting checklist reads ("conclusion_flips", "connectomes", "cross_connectome_r", "split_half").

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage, 3 cancelled)::

    python -m LesionNetworkMappingLib.robustness sweep --maps maps.tsv --out DIR [--rule JSON] [--overlap 75] [--sign S]
    python -m LesionNetworkMappingLib.robustness replicate --maps maps.tsv --bundle-b B --out DIR [--rule JSON]
        [--sign S] [--overlap 90] [--cache DIR] [--budget-gb G]
    python -m LesionNetworkMappingLib.robustness splithalf (--maps maps.tsv | --connectome-run DIR) --out DIR
        [--n-splits 1000] [--seed 0] [--rule JSON] [--overlap 90] [--study DIR --design design.json] [--strata COLUMN]
    python -m LesionNetworkMappingLib.robustness influence --maps maps.tsv --out DIR [--rule JSON] [--overlap 90]
        [--study DIR --design design.json] [--top-k 5]
    python -m LesionNetworkMappingLib.robustness seeds --study DIR --bundle B --out DIR [--variants a,b,...]
        [--coordinates coords.tsv] [--rule JSON] [--overlap 90] [--no-maps]
    python -m LesionNetworkMappingLib.robustness consolidate --study DIR --side L|R --out DIR

(all with ``--threads``, ``--cancel-file``, ``--resume``, ``--run-root`` and ``--parent-run``). A cancel file or
SIGTERM stops the job between stages and inside the cohortmaps mapping (exit code 3, a "cancelled" event, no run
registered as done); ``--resume`` is forwarded to the cohortmaps stages of replicate and seeds. ``coords.tsv`` has the
columns participant_id, x, y, z (RAS mm; several rows per participant make a union of spheres).
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

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


def _resolve(rule, n, v, gsr, values=None):
    """``thresholds.resolve`` for a map of ``n`` subjects, or ``resolve_unknown_n`` when ``n`` is None."""
    if n is None:
        return thresholds.resolve_unknown_n(rule, v, values=values)
    return thresholds.resolve(rule, n, v, gsr=gsr, values=values)


def _threshold_maps(maps, rule, n, v, sign, gsr):
    """Binary per-lesion maps (L, V) of a rule and the resolved threshold(s).

    Fixed rules resolve once; top-x% rules resolve on each lesion's map (a tuple of Resolved).
    Comparisons are made in float64, one lesion at a time (as :func:`thresholds.fraction_connected`).
    """
    import numpy as np

    binary = np.zeros(maps.shape, dtype=bool)
    if rule["kind"] == "top_pct":
        resolved = tuple(_resolve(rule, n, v, gsr, values=row) for row in maps)
        cuts = [r.value for r in resolved]
        sign = rule["sign"]
    else:
        resolved = _resolve(rule, n, v, gsr)
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


#
# Shared: constants, cohorts, comparisons
#

FORMAT_NAME = "slicer-lnm-robustness"
FORMAT_VERSION = 1
RUN_KIND = "robustness"
CROSS_CONNECTOME_R_FLAG = 0.7
CROSS_CONNECTOME_REFERENCE = "Cotovio 2022: 0.86\u20130.90; deep lesions 0.5\u20130.7"
CROSS_CONNECTOME_NOTE = "Cross-connectome r measures how much the map depends on the connectome; it is not split-half sampling noise."
CONNECTOME_SPLIT_HALF_LABEL = "connectome sampling stability (≈0.99 expected for ≥ 500 per half; not evidence of robustness)"
CONNECTOME_SPLIT_HALF_MIN_HALF = 500
LESION_SPLIT_HALF_LABEL = "lesion split-half (stability of the group result under lesion resampling)"
INFLUENCE_LABEL = "leave-one-lesion-out influence"
PEAK_TOP = 3
GROUP_PCA_KINDS = ("pca", "pc1", "group_pca")
DEFAULT_SPLITS = 1000
DEFAULT_SENSITIVITY_PCT = 90.0
DEFAULT_TOP_K = 5
SWEEP_TSV = "sweep.tsv"
CONNECTOMES_TSV = "connectomes.tsv"
SPLITHALF_TSV = "splithalf.tsv"
SPLITHALF_NPZ = "splithalf.npz"
INFLUENCE_TSV = "influence.tsv"
SEEDS_TSV = "seeds.tsv"
VARIANTS_JSON = "variants.json"
CONSOLIDATION_JSON = "consolidation.json"
LESIONS_TSV = "lesions.tsv"
NA = "n/a"

#: Seed variants (addendum §3.9): name -> (label, description)
VARIANTS = {
    "binarized": ("Binarized", "the lesion thresholded at >= 0.5 and mapped as a binary seed (the primary, when not probabilistic, counts every non-zero voxel)"),
    "probabilistic": ("Probabilistic", "the fractional lesion (desc-lesionProb when present) mapped with fractional weights"),
    "extrude2": ("Extrusion 2 mm", "desc-traced extruded to an exact total thickness of 2 mm (fractional)"),
    "extrude4": ("Extrusion 4 mm", "desc-traced extruded to an exact total thickness of 4 mm (fractional)"),
    "sphere2": ("Sphere r = 2 mm", "coordinate seeds as spheres of radius 2 mm"),
    "sphere4": ("Sphere r = 4 mm", "coordinate seeds as spheres of radius 4 mm"),
    "sphere6": ("Sphere r = 6 mm", "coordinate seeds as spheres of radius 6 mm"),
    "flipped": ("Flipped L-R", "the lesion mirrored in world x (approximate: templates are asymmetric)"),
}
DEFAULT_VARIANTS = tuple(VARIANTS)
#: How the primary seeds were binarized (variants.json "primary_binarization"; cohortmaps' probabilistic flag)
PRIMARY_NONZERO = "binary: every non-zero voxel is lesion"
PRIMARY_FRACTIONAL = "fractional weights (probabilistic)"
CHANGED_ACTIONS = ("derived", "referenced", "reweighted")
CONSOLIDATION_CAVEAT = (
    "Hemisphere consolidation is an analysis option: lesions of the other hemisphere are replaced by derived, mirrored "
    "seeds in this run folder; the traced masks are never modified. Templates are asymmetric, so mirrored lesions do not "
    "sit on exactly homologous anatomy."
)

CONNECTOMES_COLUMNS = ("map", "kind", "participant_id", "connectome_a", "connectome_b", "n_a", "n_b", "v_a", "v_b", "threshold_a", "threshold_b", "r", "dice", "peaks_persist", "flag", "note")
SPLITHALF_COLUMNS = ("analysis", "metric", "n_splits", "half_size", "median", "p2_5", "p97_5", "mean", "min", "max", "undefined", "seed", "label")
INFLUENCE_COLUMNS = ("rank", "participant_id", "influence", "dice_vs_full", "voxels_changed", "t_peak_full", "t_at_peak", "delta_t_at_peak", "loo_peak_t", "peak_shift_mm", "top_k")
SEEDS_COLUMNS = ("variant", "map", "kind", "changed", "r", "dice", "peaks_persist", "note")


def _np():
    import numpy as np

    return np


def _relative(path, folder):
    from .export import _portable_file

    return _portable_file(path, folder) if path else None


def _now_iso():
    from .study import now_iso

    return now_iso()


def _rule_text(resolved):
    return _threshold_text(resolved)


class Cohort:
    """Per-lesion maps of one connectome: ``maps`` (L, V) in-mask values of ``ids``, the number of connectome
    subjects ``n`` (None when unknown) and voxels ``v``, ``gsr``, the grid and the mask's flat indices, and a label.
    """

    def __init__(self, maps, ids, n, grid, mask_indices, gsr=None, label="A", info=None, statistic="T", run_id=None):
        np = _np()
        from .geometry import Grid

        self.maps = np.asarray(maps, dtype=np.float32)
        if self.maps.ndim != 2:
            raise ValueError(f"maps must have shape (L, V), got {self.maps.shape}")
        self.ids = [str(sid) for sid in ids]
        if len(self.ids) != self.maps.shape[0]:
            raise ValueError(f"{len(self.ids)} ids for {self.maps.shape[0]} maps")
        self.n = None if n is None else int(n)
        self.grid = grid if isinstance(grid, Grid) else Grid(grid[0], grid[1])
        self.mask_indices = np.asarray(mask_indices, dtype=np.int64).reshape(-1)
        if self.mask_indices.size != self.maps.shape[1]:
            raise ValueError(f"The maps have {self.maps.shape[1]} voxels but the mask has {self.mask_indices.size}")
        self.v = int(self.mask_indices.size)
        self.gsr = gsr
        self.label = label
        self.info = info or {}
        self.statistic = statistic
        self.run_id = run_id

    def subset(self, ids):
        index = {sid: row for row, sid in enumerate(self.ids)}
        rows = [index[sid] for sid in ids]
        return Cohort(self.maps[rows], ids, self.n, self.grid, self.mask_indices, self.gsr, self.label, self.info, self.statistic, self.run_id)


def bundle_cohort_info(bundle, n=None):
    """{"label", "name", "manifest_sha256", "n", "v", "gsr", "grid", "mask_indices"} of a connectome bundle
    (a ``ConnectomeBundle`` or its folder); ``n`` defaults to all its subjects.
    """
    from . import connectome, export

    if isinstance(bundle, (str, os.PathLike)):
        bundle = connectome.ConnectomeBundle.open(os.fspath(bundle))
    info = export.bundle_info(bundle)
    return {
        "label": info.get("label") or export.connectome_label(bundle.name),
        "name": bundle.name,
        "manifest_sha256": info.get("manifest_sha256"),
        "n": bundle.n_subjects if n is None else int(n),
        "v": bundle.n_voxels,
        "gsr": info.get("gsr"),
        "grid": bundle.grid,
        "mask_indices": bundle.mask_indices,
        "info": info,
    }


def cohort_from(maps, info, ids, label=None):
    """A :class:`Cohort` from (L, V) maps and a bundle description (:func:`bundle_cohort_info` dict, a bundle or its folder)."""
    if not isinstance(info, dict):
        info = bundle_cohort_info(info)
    return Cohort(maps, ids, info.get("n"), info["grid"], info["mask_indices"], gsr=info.get("gsr"), label=label or info.get("label") or "A",
                  info=info.get("info") or {k: v for k, v in info.items() if k not in ("grid", "mask_indices")})


def load_cohort(maps_tsv, column="T", label=None):
    """The :class:`Cohort` of a cohortmaps run (its maps.tsv, with cohortmaps.json beside it)."""
    from . import cohort as cohort_lib
    from . import cohortmaps
    from .geometry import Grid

    rows = cohortmaps.read_maps_tsv(maps_tsv)
    if not rows:
        raise ValueError(f"{maps_tsv} has no maps")
    record = cohortmaps.read_record(os.path.join(os.path.dirname(os.path.abspath(os.fspath(maps_tsv))), cohortmaps.COHORTMAPS_JSON)) or {}
    bundle = record.get("bundle") or {}
    bundle_path = bundle.get("path")
    if not bundle_path or not os.path.isdir(bundle_path):
        raise ValueError(f"The connectome of {maps_tsv} is not available ({bundle_path or 'not recorded'})")
    indices, shape, affine = cohort_lib.mask_geometry(os.path.join(bundle_path, "mask.nii.gz"))
    maps = cohort_lib.load_masked([row[column] for row in rows], indices, shape)
    n = record.get("n")
    if n is None:
        n = cohort_lib._n_from_header(rows[0]["T"]) if "T" in rows[0] else None
    return Cohort(maps, [row["participant_id"] for row in rows], n, Grid(shape, affine), indices, gsr=bundle.get("gsr"),
                  label=label or bundle.get("label") or "A", info=bundle, statistic=column, run_id=record.get("run_id"))


def _pearson_rows(a, b):
    """Pearson r of matching rows of two (k, V) arrays (NaN where a row is constant)."""
    np = _np()
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    a = a - a.mean(axis=1, keepdims=True)
    b = b - b.mean(axis=1, keepdims=True)
    numerator = np.einsum("kv,kv->k", a, b)
    denominator = np.sqrt(np.einsum("kv,kv->k", a, a) * np.einsum("kv,kv->k", b, b))
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(denominator > 0, numerator / np.where(denominator > 0, denominator, 1.0), np.nan)
    return np.clip(r, -1.0, 1.0)


def _dice_rows(a, b):
    np = _np()
    a = np.asarray(a, dtype=bool)
    b = np.asarray(b, dtype=bool)
    total = a.sum(axis=1).astype(np.float64) + b.sum(axis=1)
    both = (a & b).sum(axis=1).astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(total > 0, 2.0 * both / np.where(total > 0, total, 1.0), np.nan)


def _finite_or_none(value):
    return None if value is None or not math.isfinite(float(value)) else float(value)


def _distribution(values):
    """Summary of a distribution (NaN values counted as undefined): median, 2.5 and 97.5 percentiles, mean, min, max."""
    np = _np()
    values = np.asarray(values, dtype=np.float64)
    finite = values[np.isfinite(values)]
    out = {"n": int(values.size), "undefined": int(values.size - finite.size)}
    if finite.size == 0:
        out.update(median=None, p2_5=None, p97_5=None, mean=None, min=None, max=None)
        return out
    out.update(
        median=float(np.median(finite)),
        p2_5=float(np.percentile(finite, 2.5)),
        p97_5=float(np.percentile(finite, 97.5)),
        mean=float(finite.mean()),
        min=float(finite.min()),
        max=float(finite.max()),
    )
    return out


def _align(cohort_a, cohort_b):
    """Common voxels of two cohorts on A's grid: (columns of A, function mapping a (k, V_b) array of B to them).

    On the same grid the common voxels are the intersection of the masks. On another grid B's rows are
    resampled onto A's grid (trilinear for values, nearest neighbour for binary maps) and the common voxels
    are A's mask voxels inside B's mask.
    """
    np = _np()
    from . import geometry

    if cohort_a.grid.matches(cohort_b.grid):
        _common, cols_a, cols_b = np.intersect1d(cohort_a.mask_indices, cohort_b.mask_indices, assume_unique=True, return_indices=True)
        return cols_a, lambda rows: np.asarray(rows)[:, cols_b]

    def resample(rows, mode):
        rows = np.atleast_2d(np.asarray(rows))
        out = np.empty((rows.shape[0], cohort_a.v), dtype=np.float32)
        for index, row in enumerate(rows):
            volume = np.zeros(cohort_b.grid.n_voxels, dtype=np.float32)
            volume[cohort_b.mask_indices] = row
            moved = geometry.resample_to_grid(volume.reshape(cohort_b.grid.shape), cohort_b.grid.affine, cohort_a.grid, mode=mode)
            out[index] = moved.reshape(-1)[cohort_a.mask_indices]
        return out

    inside = resample(np.ones((1, cohort_b.v), dtype=np.float32), "nearest")[0] > 0.5
    cols_a = np.flatnonzero(inside)

    def to_common(rows):
        rows = np.asarray(rows)
        if rows.dtype == bool:
            return resample(rows.astype(np.float32), "nearest")[:, cols_a] > 0.5
        return resample(rows, "linear")[:, cols_a]

    return cols_a, to_common


def _peaks(values, grid, positions, sign):
    from . import stats

    return stats.peak_table(values, grid, top_k=PEAK_TOP, min_distance_mm=PEAK_RADIUS_MM, mask_indices=positions, sign=sign)


def peak_persistence(values_a, values_b, grid, positions, sign="both"):
    """How many of A's top-3 peaks (at least 10 mm apart) have a peak of B's top 3 within 10 mm (of the same
    sign for ``sign`` "both"): (persisting, total), (0, 0) when A has no peak.
    """
    np = _np()
    peaks_a = _peaks(values_a, grid, positions, sign)
    peaks_b = _peaks(values_b, grid, positions, sign)
    persisting = 0
    for peak in peaks_a:
        for other in peaks_b:
            same_sign = sign != "both" or (peak["value"] > 0) == (other["value"] > 0)
            if same_sign and np.linalg.norm(np.subtract(peak["world"], other["world"])) <= PEAK_RADIUS_MM:
                persisting += 1
                break
    return persisting, len(peaks_a)


def _persist_text(pair):
    persisting, total = pair
    return f"{persisting}/{total}" if total else NA


def _sensitivity(counts, n_lesions, overlap_pct):
    return _overlap_binary(counts, n_lesions, overlap_pct)


def _flag(r):
    if r is None or not math.isfinite(r) or r >= CROSS_CONNECTOME_R_FLAG:
        return ""
    return f"r < {CROSS_CONNECTOME_R_FLAG:g} (reference: {CROSS_CONNECTOME_REFERENCE})"


def compare_cohorts(cohort_a, cohort_b, rule, sign="both", overlap_pct=DEFAULT_SENSITIVITY_PCT, group_maps=None, per_lesion=True):
    """Compare the maps of the same lesions in two cohorts (connectomes or seed variants).

    Each cohort's maps are thresholded at the rule resolved for its **own** N and V (top-x% rules per map), then
    compared on the common voxels: per lesion and for the group maps (mean map; overlap % and the sensitivity map
    at ``overlap_pct``), by the unthresholded spatial r, the Dice of the binary maps and the persistence of the top
    3 peaks within 10 mm. ``group_maps`` = {name: {"a": vector over A's mask, "b": vector over B's mask, "kind"}}
    adds other group maps (GLM t, ...); kinds in :data:`GROUP_PCA_KINDS` compare |r| only.

    :return: {"rows": [...], "resolved_a", "resolved_b", "ids", "common_voxels", "median_lesion_r", "group_r"}.
      Row keys: map, kind, participant_id, r, dice, peaks_persist ((k, total) or None), threshold_a, threshold_b, flag, note.
    """
    np = _np()
    rule = thresholds.normalize_rule(rule)
    ids_b = set(cohort_b.ids)
    ids = [sid for sid in cohort_a.ids if sid in ids_b]
    if not ids:
        raise ValueError("The two cohorts have no lesion in common")
    a = cohort_a.subset(ids)
    b = cohort_b.subset(ids)
    binary_a, resolved_a = _threshold_maps(a.maps, rule, a.n, a.v, sign, a.gsr)
    binary_b, resolved_b = _threshold_maps(b.maps, rule, b.n, b.v, sign, b.gsr)
    cols_a, to_common = _align(a, b)
    if cols_a.size == 0:
        raise ValueError("The two connectomes have no voxel in common")
    positions = a.mask_indices[cols_a]
    values_a = a.maps[:, cols_a]
    values_b = to_common(b.maps)
    bin_a = binary_a[:, cols_a]
    bin_b = to_common(binary_b)

    def cut(resolved, row):
        item = resolved[row] if isinstance(resolved, tuple) else resolved
        return _threshold_text(item)

    rows = []
    lesion_r = _pearson_rows(values_a, values_b)
    lesion_dice = _dice_rows(bin_a, bin_b)
    if per_lesion:
        for row, sid in enumerate(ids):
            r = _finite_or_none(lesion_r[row])
            rows.append({
                "map": sid, "kind": "lesion", "participant_id": sid, "r": r, "dice": _finite_or_none(lesion_dice[row]),
                "peaks_persist": peak_persistence(values_a[row], values_b[row], a.grid, positions, sign),
                "threshold_a": cut(resolved_a, row), "threshold_b": cut(resolved_b, row), "flag": _flag(r), "note": "",
            })
    n_lesions = len(ids)
    mean_a, mean_b = values_a.astype(np.float64).mean(axis=0), values_b.astype(np.float64).mean(axis=0)
    counts_a, counts_b = bin_a.sum(axis=0), bin_b.sum(axis=0)
    sens_a, sens_b = _sensitivity(counts_a, n_lesions, overlap_pct), _sensitivity(counts_b, n_lesions, overlap_pct)
    threshold_a = _rule_text(resolved_a)
    threshold_b = _rule_text(resolved_b)
    group_r = _finite_or_none(_pearson(mean_a, mean_b))
    rows.append({"map": "group mean", "kind": "group_mean", "participant_id": None, "r": group_r, "dice": None,
                 "peaks_persist": peak_persistence(mean_a, mean_b, a.grid, positions, sign), "threshold_a": None, "threshold_b": None, "flag": _flag(group_r), "note": "mean of the lesion maps (unthresholded)"})
    overlap_r = _finite_or_none(_pearson(counts_a.astype(np.float64), counts_b.astype(np.float64)))
    rows.append({"map": f"overlap ≥ {overlap_pct:g}%", "kind": "group_sensitivity", "participant_id": None, "r": overlap_r, "dice": _finite_or_none(_dice(sens_a, sens_b)),
                 "peaks_persist": None, "threshold_a": threshold_a, "threshold_b": threshold_b, "flag": _flag(overlap_r),
                 "note": "r of the overlap % maps; Dice of the sensitivity maps, each thresholded at its own rule"})
    for name, item in (group_maps or {}).items():
        kind = str(item.get("kind") or "group")
        map_a = np.asarray(item["a"], dtype=np.float64).reshape(1, -1)[:, cols_a][0]
        map_b = np.asarray(to_common(np.asarray(item["b"], dtype=np.float32).reshape(1, -1)), dtype=np.float64)[0]
        r = _finite_or_none(_pearson(map_a, map_b))
        if kind.lower() in GROUP_PCA_KINDS:
            r = None if r is None else abs(r)
            rows.append({"map": name, "kind": kind, "participant_id": None, "r": r, "dice": None, "peaks_persist": None, "threshold_a": None, "threshold_b": None,
                         "flag": _flag(r), "note": "group PCA: |r| only (component sign and scale are arbitrary)"})
        else:
            rows.append({"map": name, "kind": kind, "participant_id": None, "r": r, "dice": None, "peaks_persist": peak_persistence(map_a, map_b, a.grid, positions, sign),
                         "threshold_a": None, "threshold_b": None, "flag": _flag(r), "note": ""})
    finite = lesion_r[np.isfinite(lesion_r)]
    return {
        "rows": rows,
        "ids": ids,
        "resolved_a": resolved_a,
        "resolved_b": resolved_b,
        "common_voxels": int(cols_a.size),
        "median_lesion_r": float(np.median(finite)) if finite.size else None,
        "group_r": group_r,
    }


#
# Run records
#


def _write_json(path, record):
    from . import jobs

    jobs.write_json_atomic(path, record)
    return path


def _check_cancel(cancel, done=None):
    """Raise ``jobs.Cancelled`` when ``cancel`` (a ``jobs.CancelToken``, Event or callable; None: never) is set."""
    if cancel is None:
        return
    from . import jobs
    from .connectome import _is_cancelled

    if _is_cancelled(cancel):
        raise jobs.Cancelled(done=done, reason=getattr(cancel, "reason", None))


def _register(run_root, out_dir, analysis, params, parents=(), summary=None, outputs=None, seeds=None):
    """Register a finished robustness run (kind "robustness"); returns its id."""
    from . import runs

    run_id = runs.register(run_root, RUN_KIND, out_dir, parents=[p for p in parents if p], params={"analysis": analysis, **(params or {})}, seeds=seeds, status="running")
    runs.update(run_root, run_id, status="done", ended=_now_iso(), summary={"analysis": analysis, **(summary or {})}, outputs=list(outputs or []))
    return run_id


def _record(analysis, out_dir, run_id, **fields):
    from . import export

    return {"format": FORMAT_NAME, "version": FORMAT_VERSION, "analysis": analysis, "run_id": run_id, "created": _now_iso(), "versions": export.versions(), **fields}


def _tsv_cell(value):
    if value is None:
        return NA
    if isinstance(value, tuple) and len(value) == 2:
        return _persist_text(value)
    text = _cell(value)
    return text.replace("\t", " ").replace("\n", " ") or NA


def _write_tsv(path, columns, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_tsv_cell(row.get(column)) for column in columns])
    return path


#
# (1) Replication on another connectome
#


def write_connectomes_tsv(rows, path):
    """Write connectome comparison rows (:func:`replicate_on`, :func:`connectome_split_half`); columns :data:`CONNECTOMES_COLUMNS`."""
    return _write_tsv(path, CONNECTOMES_COLUMNS, rows)


def _cohortmaps_cohort(lesions, bundle, out_dir, label, emit, run_root, parents, options, cancel=None):
    """Map ``lesions`` on ``bundle`` with cohortmaps into ``out_dir``; return (Cohort, cohortmaps run id)."""
    from . import cohort as cohort_lib
    from . import cohortmaps

    def inner(event, **fields):
        # cohortmaps is a stage of this job: its start and done events are not this job's
        if event == "start":
            return None
        if event == "done":
            return emit("result", kind="mapped", label=label, path=os.path.join(out_dir, cohortmaps.MAPS_TSV), n=fields.get("n"),
                        cache_hits=fields.get("cache_hits"), computed=fields.get("computed"))
        return emit(event, **fields)

    _check_cancel(cancel)
    record = cohortmaps.run(lesions, bundle, out_dir, emit=inner, run_root=run_root, parent_runs=parents, cancel=cancel, **(options or {}))
    rows = cohortmaps.read_maps_tsv(record["maps_tsv"])
    info = bundle_cohort_info(bundle, n=record.get("n"))
    maps = cohort_lib.load_masked([row["T"] for row in rows], info["mask_indices"], info["grid"].shape)
    cohort = Cohort(maps, [row["participant_id"] for row in rows], record.get("n"), info["grid"], info["mask_indices"], gsr=info.get("gsr"), label=label or info["label"], info=info["info"], run_id=record.get("run_id"))
    return cohort, record.get("run_id")


def replicate_on(maps_a, info_a, bundle_b, lesions, rule, out, ids=None, sign="both", overlap_pct=DEFAULT_SENSITIVITY_PCT, maps_b=None, info_b=None, group_maps=None,
                 cohortmaps_options=None, emit=None, run_root=None, parent_runs=(), register=True, cancel=None):
    """Replicate the lesion maps of connectome A on connectome B (addendum §3.9).

    :param maps_a: (L, V_a) per-lesion maps on A (T maps; a :class:`Cohort` is also accepted).
    :param info_a: A's description: a :func:`bundle_cohort_info` dict (``n`` = the subjects of the maps), a bundle or its folder.
    :param bundle_b: connectome B (a bundle or its folder).
    :param lesions: [(participant_id, path[, sha256])] in the order of the rows of ``maps_a``.
    :param rule: the threshold rule, re-resolved for each connectome's own N and V.
    :param out: run folder; B's maps go to ``<out>/cohortmaps_conn-<label B>`` (cohortmaps, cached).
    :param ids: participant ids of the rows of ``maps_a`` (default: those of ``lesions``).
    :param maps_b, info_b: B's maps when already computed (then nothing is mapped).
    :param group_maps: extra group maps to compare, see :func:`compare_cohorts`.
    :param cancel: optional ``jobs.CancelToken`` (or Event/callable); a stop raises ``jobs.Cancelled`` and nothing is registered.
    :return: the record written to ``<out>/replicate.json`` (rows also in connectomes.tsv).
    """
    from . import cohortmaps, jobs

    emit = emit or jobs.emit
    out = os.path.abspath(os.fspath(out))
    os.makedirs(out, exist_ok=True)
    run_root = os.path.abspath(os.fspath(run_root or out))
    lesions = cohortmaps._normalize_lesions(lesions)
    if isinstance(maps_a, Cohort):
        cohort_a = maps_a
    else:
        cohort_a = cohort_from(maps_a, info_a, ids or [sid for sid, _path, _sha in lesions])
    parents = list(parent_runs or ()) + ([cohort_a.run_id] if cohort_a.run_id else [])
    if maps_b is not None:
        cohort_b = maps_b if isinstance(maps_b, Cohort) else cohort_from(maps_b, info_b or bundle_b, [sid for sid, _path, _sha in lesions])
        run_b = cohort_b.run_id
    else:
        label_b = bundle_cohort_info(bundle_b)["label"]
        cohort_b, run_b = _cohortmaps_cohort(lesions, bundle_b, os.path.join(out, f"cohortmaps_conn-{label_b}"), label_b, emit, run_root, parents, cohortmaps_options, cancel)
    if cohort_a.label == cohort_b.label:
        # a relabelled copy: the caller's Cohort is not modified
        cohort_b = Cohort(cohort_b.maps, cohort_b.ids, cohort_b.n, cohort_b.grid, cohort_b.mask_indices, gsr=cohort_b.gsr, label=f"{cohort_b.label}-B",
                          info=cohort_b.info, statistic=cohort_b.statistic, run_id=cohort_b.run_id)
    _check_cancel(cancel)
    rule = thresholds.normalize_rule(rule)
    result = compare_cohorts(cohort_a, cohort_b, rule, sign=sign, overlap_pct=overlap_pct, group_maps=group_maps)
    for row in result["rows"]:
        row.update(connectome_a=cohort_a.label, connectome_b=cohort_b.label, n_a=cohort_a.n, n_b=cohort_b.n, v_a=cohort_a.v, v_b=cohort_b.v)
    flagged = [row["map"] for row in result["rows"] if row["flag"]]
    tsv = write_connectomes_tsv(result["rows"], os.path.join(out, CONNECTOMES_TSV))
    summary = {
        "connectomes": [cohort_a.label, cohort_b.label],
        "n_lesions": len(result["ids"]),
        "cross_connectome_r": result["median_lesion_r"],
        "group_r": result["group_r"],
        "flagged": len(flagged),
        "reference": CROSS_CONNECTOME_REFERENCE,
        "rule_resolution": "per connectome (own N and V)",
        "thresholds": {cohort_a.label: _rule_text(result["resolved_a"]), cohort_b.label: _rule_text(result["resolved_b"])},
    }
    run_id = _register(run_root, out, "replicate", {"rule": rule, "sign": sign, "overlap_pct": overlap_pct, "connectomes": summary["connectomes"]},
                       parents=[*parents, run_b], summary=summary, outputs=[CONNECTOMES_TSV, "replicate.json"]) if register else None
    record = _record("replicate", out, run_id, rule=rule, sign=sign, overlap_pct=overlap_pct, note=CROSS_CONNECTOME_NOTE, flag_below=CROSS_CONNECTOME_R_FLAG,
                     connectome_a={"label": cohort_a.label, "n": cohort_a.n, "v": cohort_a.v, "manifest_sha256": (cohort_a.info or {}).get("manifest_sha256"), "run_id": cohort_a.run_id},
                     connectome_b={"label": cohort_b.label, "n": cohort_b.n, "v": cohort_b.v, "manifest_sha256": (cohort_b.info or {}).get("manifest_sha256"), "run_id": run_b},
                     resolved_a=_resolved_json(result["resolved_a"]), resolved_b=_resolved_json(result["resolved_b"]), common_voxels=result["common_voxels"],
                     summary=summary, flagged=flagged, outputs={"connectomes": _relative(tsv, out)})
    _write_json(os.path.join(out, "replicate.json"), record)
    record["rows"] = result["rows"]
    return record


def _resolved_json(resolved):
    if resolved is None:
        return None
    if isinstance(resolved, tuple):
        return {"per_map": True, "values": [item.value if math.isfinite(item.value) else None for item in resolved], "kind": resolved[0].kind if resolved else None}
    return resolved.to_dict()


#
# (2) Lesion split-half and (3) leave-one-lesion-out influence
#


def _design_arrays(design, n_lesions):
    """(X (L, p) float64, c (p,) float64) of a one-row contrast from a ``design.Design``, a dict {"X", "C"} or a tuple."""
    np = _np()
    if design is None:
        return None, None
    if isinstance(design, dict):
        X, C = design["X"], design.get("C", design.get("contrast"))
    elif isinstance(design, (tuple, list)):
        X, C = design
    else:
        X, C = design.X, design.C
    X = np.asarray(X, dtype=np.float64)
    C = np.atleast_2d(np.asarray(C, dtype=np.float64))
    if X.ndim != 2 or X.shape[0] != n_lesions:
        raise ValueError(f"The design has {X.shape[0] if X.ndim == 2 else '?'} rows for {n_lesions} maps")
    if C.shape[0] != 1 or C.shape[1] != X.shape[1]:
        raise ValueError("Split-half and influence need a one-row (t) contrast with one weight per design column")
    return X, C[0]


def glm_t(XtX, XtY, yy, n, c):
    """t map of a one-row contrast from sufficient statistics (XᵀX, XᵀY, Σy², n rows); None when not estimable.

    beta = (XᵀX)⁺ XᵀY, SSE = Σy² - Σ beta ⊙ XᵀY, df = n - rank, t = cᵀbeta / sqrt(SSE / df · cᵀ(XᵀX)⁺c),
    the same estimate as ``glm.fit`` (voxels with SSE = 0 get t = 0).
    """
    np = _np()
    pinv = np.linalg.pinv(XtX)
    rank = int(np.linalg.matrix_rank(XtX))
    df = int(n) - rank
    if df < 1:
        return None
    if np.abs(c @ (np.eye(len(c)) - pinv @ XtX)).max() > 1e-8 * max(1.0, np.abs(c).max()):
        return None  # the contrast is not estimable in this subset
    beta = pinv @ XtY
    sse = np.maximum(yy - np.einsum("pv,pv->v", beta, XtY), 0.0)
    variance = float(c @ pinv @ c)
    scale = np.sqrt(sse / df * variance)
    t = np.zeros(XtY.shape[1])
    np.divide(c @ beta, scale, out=t, where=sse > 1e-12 * np.maximum(yy, 1e-300))  # a perfect fit (SSE = 0 up to rounding) gives 0
    return t


class SufficientStatistics:
    """XᵀX, XᵀY and Σy² of a GLM over all the lesions, with downdates that leave one lesion out (no refit)."""

    def __init__(self, Y, X, c):
        np = _np()
        self.Y = np.asarray(Y, dtype=np.float64)
        self.X = np.asarray(X, dtype=np.float64)
        self.c = np.asarray(c, dtype=np.float64)
        self.n = self.X.shape[0]
        self.XtX = self.X.T @ self.X
        self.XtY = self.X.T @ self.Y
        self.yy = np.einsum("lv,lv->v", self.Y, self.Y)

    def t(self):
        return glm_t(self.XtX, self.XtY, self.yy, self.n, self.c)

    def t_without(self, row):
        """t map without lesion ``row``: XᵀX - x xᵀ, XᵀY - x yᵀ, Σy² - y²."""
        np = _np()
        x = self.X[row]
        y = self.Y[row]
        return glm_t(self.XtX - np.outer(x, x), self.XtY - np.outer(x, y), self.yy - y * y, self.n - 1, self.c)


def split_indices(n_lesions, n_splits=DEFAULT_SPLITS, seed=0, strata=None):
    """Random half splits of ``n_lesions`` lesions: (first half (S, h), second half (S, h)) index arrays.

    Each stratum (``strata``: one label per lesion) is split in half; a stratum of odd size leaves one of its
    lesions out of each split. Deterministic for a seed (``numpy.random.default_rng(seed)``).
    """
    np = _np()
    if n_lesions < 2:
        raise ValueError("A split-half needs at least 2 lesions")
    labels = ["all"] * n_lesions if strata is None else [str(label) for label in strata]
    if len(labels) != n_lesions:
        raise ValueError(f"{len(labels)} strata labels for {n_lesions} lesions")
    groups = {}
    for index, label in enumerate(labels):
        groups.setdefault(label, []).append(index)
    groups = [np.asarray(members) for _label, members in sorted(groups.items())]
    half = sum(len(members) // 2 for members in groups)
    if half < 1:
        raise ValueError("Every stratum has a single lesion: no split-half is possible")
    rng = np.random.default_rng(seed)
    first = np.empty((n_splits, half), dtype=np.int64)
    second = np.empty((n_splits, half), dtype=np.int64)
    for split in range(n_splits):
        a, b = [], []
        for members in groups:
            order = rng.permutation(members)
            k = len(members) // 2
            a.append(order[:k])
            b.append(order[k:2 * k])
        first[split] = np.sort(np.concatenate(a))
        second[split] = np.sort(np.concatenate(b))
    return first, second


def lesion_split_half(maps, n_splits=DEFAULT_SPLITS, seed=0, rule=None, n=None, v=None, gsr=None, sign="both", overlap_pct=DEFAULT_SENSITIVITY_PCT, design=None, strata=None, batch=None, cancel=None):
    """Lesion split-half stability (Ferguson 2019): distributions over ``n_splits`` random half splits.

    :param maps: (L, V) per-lesion maps (T maps for T rules) or a :class:`Cohort` (then ``n``, ``v`` and ``gsr`` come from it).
    :param rule: threshold rule of the binary per-lesion maps (default FWE, which needs ``n``).
    :param n, v, gsr: connectome subjects, voxels tested (default V) and GSR of the maps, for the rule.
    :param overlap_pct: sensitivity criterion (% of the half's lesions).
    :param design: optional GLM (``design.Design``, {"X", "C"} or (X, C), rows in map order, one-row contrast):
      the t maps of the two halves are computed from their sufficient statistics and correlated.
    :param strata: optional label per lesion; splits are made within strata (e.g. groups of a two-group design).
    :param cancel: optional ``jobs.CancelToken`` (or Event/callable), checked before each batch of splits.
    :return: {"label", "n_splits", "seed", "half_size", "left_out", "r_overlap", "dice_sensitivity", "r_glm_t"
      (arrays of length n_splits; NaN where undefined; r_glm_t None without a design), "summary" ({metric: distribution}),
      "rule", "overlap_pct"}.
    """
    np = _np()
    if isinstance(maps, Cohort):
        n, v, gsr, maps = maps.n, maps.v, maps.gsr, maps.maps
    maps = np.asarray(maps, dtype=np.float32)
    n_lesions, n_vox = maps.shape
    v = n_vox if v is None else int(v)
    rule = thresholds.normalize_rule(rule)
    binary, _resolved = _threshold_maps(maps, rule, n, v, sign, gsr)
    binary = binary.astype(np.float32)
    first, second = split_indices(n_lesions, n_splits, seed, strata)
    half = first.shape[1]
    X, c = _design_arrays(design, n_lesions)
    batch = batch or max(1, min(64, int(64_000_000 // max(n_vox, 1))))
    r_overlap = np.full(n_splits, np.nan)
    dice = np.full(n_splits, np.nan)
    r_glm = np.full(n_splits, np.nan) if X is not None else None
    Y = maps.astype(np.float64) if X is not None else None
    Y2 = Y * Y if X is not None else None
    for start in range(0, n_splits, batch):
        _check_cancel(cancel, done=start)
        stop = min(start + batch, n_splits)
        size = stop - start
        S1 = np.zeros((size, n_lesions), dtype=np.float32)
        S2 = np.zeros((size, n_lesions), dtype=np.float32)
        S1[np.repeat(np.arange(size), half), first[start:stop].reshape(-1)] = 1.0
        S2[np.repeat(np.arange(size), half), second[start:stop].reshape(-1)] = 1.0
        counts1 = S1 @ binary
        counts2 = S2 @ binary
        r_overlap[start:stop] = _pearson_rows(counts1, counts2)
        dice[start:stop] = _dice_rows(_sensitivity(counts1, half, overlap_pct), _sensitivity(counts2, half, overlap_pct))
        if X is not None:
            yy1 = S1.astype(np.float64) @ Y2
            yy2 = S2.astype(np.float64) @ Y2
            for offset in range(size):
                halves = []
                for weights, yy in ((S1[offset], yy1[offset]), (S2[offset], yy2[offset])):
                    Wt = X.T * weights.astype(np.float64)  # Xᵀ diag(s): the half's rows without copying Y
                    halves.append(glm_t(Wt @ X, Wt @ Y, yy, half, c))
                if halves[0] is not None and halves[1] is not None:
                    r_glm[start + offset] = _pearson_rows(halves[0][None], halves[1][None])[0]
    summary = {"r_overlap": _distribution(r_overlap), "dice_sensitivity": _distribution(dice)}
    if r_glm is not None:
        summary["r_glm_t"] = _distribution(r_glm)
    return {
        "label": LESION_SPLIT_HALF_LABEL,
        "n_splits": int(n_splits),
        "seed": seed,
        "half_size": int(half),
        "left_out": int(n_lesions - 2 * half),
        "stratified": strata is not None,
        "r_overlap": r_overlap,
        "dice_sensitivity": dice,
        "r_glm_t": r_glm,
        "summary": summary,
        "rule": rule,
        "overlap_pct": float(overlap_pct),
        "sign": sign,
    }


METRIC_LABELS = {
    "r_overlap": "r of the halves' overlap maps",
    "dice_sensitivity": "Dice of the halves' sensitivity maps",
    "r_glm_t": "r of the halves' GLM t maps",
}


def write_splithalf_tsv(result, path):
    """Write the lesion split-half distributions (one row per metric); columns :data:`SPLITHALF_COLUMNS`."""
    rows = []
    for metric, values in result["summary"].items():
        rows.append({"analysis": "lesion_split_half", "metric": METRIC_LABELS.get(metric, metric), "n_splits": result["n_splits"], "half_size": result["half_size"],
                     "seed": result["seed"], "label": result["label"], **{key: values.get(key) for key in ("median", "p2_5", "p97_5", "mean", "min", "max", "undefined")}})
    return _write_tsv(path, SPLITHALF_COLUMNS, rows)


def _peak_index(t, sign):
    np = _np()
    if sign == "pos":
        return int(np.argmax(t))
    if sign == "neg":
        return int(np.argmin(t))
    return int(np.argmax(np.abs(t)))


def leave_one_lesion_out(maps, design=None, ids=None, rule=None, n=None, v=None, gsr=None, sign="both", overlap_pct=DEFAULT_SENSITIVITY_PCT, top_k=DEFAULT_TOP_K, grid=None, mask_indices=None, cancel=None):
    """Leave-one-lesion-out influence, by downdates (no refit).

    For each lesion i: the sensitivity map of the other L - 1 lesions (overlap counts minus lesion i's binary map)
    against the full sensitivity map (Dice, voxels that change) and, with a design, the GLM t map without row i
    (:meth:`SufficientStatistics.t_without`): t at the full peak, the change, the new peak value and, with a grid,
    how far the peak moved.

    :return: {"rows" (one per lesion, in map order; keys of :data:`INFLUENCE_COLUMNS`), "top" (the ``top_k`` most
      influential participant ids, ranked by |Δt at the peak| with a design, else by 1 - Dice), "peak" (full GLM peak)}.
    """
    np = _np()
    if isinstance(maps, Cohort):
        ids = ids or maps.ids
        n, v, gsr, grid, mask_indices, maps = maps.n, maps.v, maps.gsr, maps.grid, maps.mask_indices, maps.maps
    maps = np.asarray(maps, dtype=np.float32)
    n_lesions, n_vox = maps.shape
    if n_lesions < 2:
        raise ValueError("Leave-one-out needs at least 2 lesions")
    ids = list(ids) if ids is not None else [str(index) for index in range(n_lesions)]
    v = n_vox if v is None else int(v)
    rule = thresholds.normalize_rule(rule)
    binary, _resolved = _threshold_maps(maps, rule, n, v, sign, gsr)
    counts = binary.sum(axis=0)
    full = _sensitivity(counts, n_lesions, overlap_pct)
    X, c = _design_arrays(design, n_lesions)
    world = None
    if grid is not None:
        world = _voxel_world(grid, mask_indices, n_vox)
    stats_ = t_full = None
    peak = None
    if X is not None:
        stats_ = SufficientStatistics(maps, X, c)
        t_full = stats_.t()
        if t_full is None:
            raise ValueError("The design has no residual degrees of freedom or the contrast is not estimable")
        index = _peak_index(t_full, sign)
        peak = {"index": index, "t": float(t_full[index]), "world": None if world is None else [float(x) for x in world[index]]}
    rows = []
    for row in range(n_lesions):
        _check_cancel(cancel, done=row)
        loo = _sensitivity(counts - binary[row], n_lesions - 1, overlap_pct)
        item = {"participant_id": ids[row], "dice_vs_full": _finite_or_none(_dice(loo, full)), "voxels_changed": int((loo ^ full).sum())}
        if stats_ is not None:
            t_loo = stats_.t_without(row)
            if t_loo is None:
                item.update(t_peak_full=peak["t"], t_at_peak=None, delta_t_at_peak=None, loo_peak_t=None, peak_shift_mm=None)
            else:
                new = _peak_index(t_loo, sign)
                item.update(
                    t_peak_full=peak["t"],
                    t_at_peak=float(t_loo[peak["index"]]),
                    delta_t_at_peak=float(t_loo[peak["index"]] - peak["t"]),
                    loo_peak_t=float(t_loo[new]),
                    peak_shift_mm=None if world is None else float(np.linalg.norm(world[new] - world[peak["index"]])),
                )
        dice_value = item["dice_vs_full"]
        if stats_ is not None:
            item["influence"] = None if item.get("delta_t_at_peak") is None else abs(item["delta_t_at_peak"])
        else:
            item["influence"] = None if dice_value is None else 1.0 - dice_value
        rows.append(item)
    order = sorted(range(n_lesions), key=lambda index: -(rows[index]["influence"] if rows[index]["influence"] is not None else -1.0))
    for rank, index in enumerate(order, start=1):
        rows[index]["rank"] = rank
        rows[index]["top_k"] = rank <= top_k
    return {"label": INFLUENCE_LABEL, "rows": rows, "top": [rows[index]["participant_id"] for index in order[:top_k]], "peak": peak, "rule": rule,
            "overlap_pct": float(overlap_pct), "influence": "|Δt at the GLM peak|" if stats_ is not None else "1 - Dice of the sensitivity map"}


def write_influence_tsv(result, path):
    """Write the leave-one-lesion-out rows, most influential first; columns :data:`INFLUENCE_COLUMNS`."""
    rows = sorted(result["rows"], key=lambda row: row["rank"])
    return _write_tsv(path, INFLUENCE_COLUMNS, rows)


#
# (4) Seed variants and (5) hemisphere consolidation
#


def _study(study):
    from .study import Study

    return Study.open(study) if isinstance(study, (str, os.PathLike)) else study


def _check_out(study, out):
    """``out`` must not be inside the study's lesions/ folder (derived files never go there)."""
    from .study import LESIONS_DIR

    lesions_dir = os.path.realpath(os.path.join(study.path, LESIONS_DIR))
    real = os.path.realpath(out)
    if real == lesions_dir or real.startswith(lesions_dir + os.sep):
        raise ValueError(f"Derived lesion files are never written in {LESIONS_DIR}/: choose a run folder (e.g. derivatives/lnm/robustness)")


def _sibling(path, desc):
    """The file next to ``path`` with its desc entity replaced by ``desc`` (desc-lesion -> desc-traced), or None."""
    import re

    folder, name = os.path.split(path)
    if not re.search(r"_desc-[A-Za-z0-9]+", name):
        return None
    return os.path.join(folder, re.sub(r"_desc-[A-Za-z0-9]+", f"_desc-{desc}", name, count=1))


def _stem(path):
    name = os.path.basename(path)
    for extension in (".nii.gz", ".nii"):
        if name.endswith(extension):
            return name[: -len(extension)]
    return os.path.splitext(name)[0]


def _variant_name(sid, desc):
    return f"{sid}_desc-{desc}_mask.nii.gz"


def _is_binary(values):
    np = _np()
    return bool(np.all((values == 0) | (values == 1)))


def _sphere_volume(centres, radius, spacing=1.0, margin=2.0):
    """A union of spheres on a local 1 mm grid around them: (uint8 volume, affine)."""
    np = _np()
    centres = np.atleast_2d(np.asarray(centres, dtype=np.float64))
    low = centres.min(axis=0) - radius - margin
    high = centres.max(axis=0) + radius + margin
    shape = tuple(math.ceil((h - lo) / spacing) + 1 for lo, h in zip(low, high, strict=True))
    affine = np.diag([spacing, spacing, spacing, 1.0])
    affine[:3, 3] = low
    world = np.indices(shape).reshape(3, -1).T * spacing + low
    inside = np.zeros(world.shape[0], dtype=bool)
    for centre in centres:
        inside |= np.linalg.norm(world - centre, axis=1) <= radius
    return inside.reshape(shape).astype(np.uint8), affine


def read_coordinates(path):
    """{participant_id: [(x, y, z), ...]} from a TSV with participant_id, x, y, z (RAS mm)."""
    from .study import read_tsv

    columns, rows = read_tsv(path)
    for column in ("participant_id", "x", "y", "z"):
        if column not in columns:
            raise ValueError(f"{path} has no {column} column")
    out = {}
    for row in rows:
        out.setdefault(row["participant_id"], []).append(tuple(float(row[axis]) for axis in "xyz"))
    return out


def _build_variant(variant, sid, source, folder, coordinates, primary_probabilistic):
    """Write one variant file of a lesion into ``folder``; return (entry, path or None for the unchanged source, probabilistic)."""
    np = _np()
    from . import lesionops, nifti

    entry = {"participant_id": sid, "action": "derived", "note": None}
    if variant.startswith("sphere"):
        centres = (coordinates or {}).get(sid)
        if not centres:
            entry.update(action="kept", note="no coordinate seed")
            return entry, None, primary_probabilistic
        radius = float(variant[len("sphere"):])
        volume, affine = _sphere_volume(centres, radius)
        path = os.path.join(folder, _variant_name(sid, variant))
        nifti.write_nifti(path, volume, affine)
        entry["note"] = f"{len(centres)} coordinate(s), radius {radius:g} mm"
        return entry, path, False
    if variant.startswith("extrude"):
        traced = _sibling(source, "traced")
        if traced is None or not os.path.isfile(traced):
            entry.update(action="kept", note="no desc-traced tracing")
            return entry, None, primary_probabilistic
        data, affine, _header = nifti.read_nifti(traced)
        try:
            values, record = lesionops.extrude(np.asarray(data), affine, total_mm=float(variant[len("extrude"):]), mode=lesionops.EXTRUDE_FRACTIONAL)
        except ValueError as exc:
            entry.update(action="kept", note=str(exc))
            return entry, None, primary_probabilistic
        path = os.path.join(folder, _variant_name(sid, variant))
        nifti.write_nifti(path, values.astype(np.float32), affine)
        entry.update(source_traced=traced, note=record["description"])
        return entry, path, True
    data, affine, _header = nifti.read_nifti(source)
    values = np.nan_to_num(np.asarray(data, dtype=np.float64))
    if values.ndim > 3:
        values = values.reshape(values.shape[:3])
    if variant == "binarized":
        if _is_binary(values):
            entry.update(action="unchanged", note="already binary")
            return entry, None, False
        path = os.path.join(folder, _variant_name(sid, variant))
        nifti.write_nifti(path, (values >= lesionops.LESION_THRESHOLD).astype(np.uint8), affine)
        entry["note"] = f"fraction >= {lesionops.LESION_THRESHOLD:g}"
        return entry, path, False
    if variant == "probabilistic":
        prob = _sibling(source, "lesionProb")
        if prob is not None and os.path.isfile(prob) and os.path.abspath(prob) != os.path.abspath(source):
            entry.update(action="referenced", note="desc-lesionProb")
            return entry, prob, True
        if not primary_probabilistic and not _is_binary(values):
            entry.update(action="reweighted", note="no desc-lesionProb; the fractional lesion mapped with fractional weights")
        else:
            entry.update(action="unchanged", note="no desc-lesionProb and the primary seed already has the same weights")
        return entry, None, True
    if variant == "flipped":
        fractional, binary, note = lesionops.mirror_world(values, affine)
        path = os.path.join(folder, _variant_name(sid, variant))
        if primary_probabilistic or not _is_binary(values):
            nifti.write_nifti(path, fractional.astype(np.float32), affine)
        else:
            nifti.write_nifti(path, binary, affine)
        entry["note"] = note
        return entry, path, primary_probabilistic or not _is_binary(values)
    raise ValueError(f"Unknown seed variant {variant!r} (known: {', '.join(VARIANTS)})")


def _lesion_rows(lesions, out):
    from .export import file_sha256

    return [{"participant_id": sid, "lesion": _relative(path, out), "lesion_sha256": file_sha256(path)} for sid, path in lesions]


def _write_lesions_tsv(path, rows):
    return _write_tsv(path, ("participant_id", "lesion", "lesion_sha256"), rows)


def seed_variants(study, variants=DEFAULT_VARIANTS, out=None, bundle=None, lesions=None, coordinates=None, rule=None, sign="both", overlap_pct=DEFAULT_SENSITIVITY_PCT,
                  probabilistic=False, compare=True, cohortmaps_options=None, emit=None, run_root=None, parent_runs=(), register=True, cancel=None):
    """Build seed-variant lesion files in a run folder (never in ``lesions/``) and compare their maps with the primary.

    Variants (:data:`VARIANTS`): binarized vs probabilistic; extrusion 2 vs 4 mm from ``desc-traced``
    (``lesionops.extrude``, fractional); spheres of radius 2/4/6 mm around coordinate seeds (``coordinates``:
    {participant_id: [(x, y, z), ...]}); flipped (``lesionops.mirror_world``, approximate). A lesion a variant does
    not apply to (no desc-traced tracing, a 3D tracing, no coordinate) keeps its primary seed and is listed as "kept".

    :param study: ``Study`` or its folder (the lesions default to its analysable rows).
    :param out: run folder (default <study>/derivatives/lnm/robustness); files go to ``<out>/variants/<variant>/``.
    :param bundle: connectome for the comparison (needed when ``compare``).
    :param probabilistic: how the primary seeds are mapped (cohortmaps' ``probabilistic``: False maps every non-zero
      voxel of the primary file as lesion, recorded as ``primary_binarization`` in variants.json).
    :param cancel: optional ``jobs.CancelToken`` (or Event/callable), checked before each variant and inside the mappings.
    :return: the record written to ``<out>/variants.json``; with ``compare`` its "rows" are in seeds.tsv.
    """
    from . import cohortmaps, jobs
    from .export import file_sha256

    emit = emit or jobs.emit
    study = _study(study)
    out = os.path.abspath(os.fspath(out or os.path.join(study.path, "derivatives", "lnm", "robustness")))
    _check_out(study, out)
    unknown = [name for name in variants if name not in VARIANTS]
    if unknown:
        raise ValueError(f"Unknown seed variant(s) {', '.join(unknown)} (known: {', '.join(VARIANTS)})")
    if compare and bundle is None:
        raise ValueError("Comparing the variants needs the connectome bundle")
    if lesions is None:
        lesions, _excluded = cohortmaps.study_lesions(study)
    lesions = [(sid, path) for sid, path, *_rest in cohortmaps._normalize_lesions(lesions)]
    run_root = os.path.abspath(os.fspath(run_root or study.path))
    os.makedirs(out, exist_ok=True)
    manifest = {}
    variant_lesions = {}
    for variant in variants:
        _check_cancel(cancel)
        folder = os.path.join(out, "variants", variant)
        os.makedirs(folder, exist_ok=True)
        entries, seeds, kinds = [], [], []
        for sid, source in lesions:
            entry, path, prob = _build_variant(variant, sid, source, folder, coordinates, probabilistic)
            used = path or source
            entry.update(source=_relative(source, out), source_sha256=file_sha256(source), output=_relative(used, out), probabilistic=prob)
            if entry.get("source_traced"):
                entry["source_traced"] = _relative(entry["source_traced"], out)
            entries.append(entry)
            seeds.append((sid, used))
            kinds.append(prob)
        changed = sum(1 for entry in entries if entry["action"] in CHANGED_ACTIONS)
        _write_lesions_tsv(os.path.join(folder, LESIONS_TSV), _lesion_rows(seeds, folder))
        manifest[variant] = {"label": VARIANTS[variant][0], "description": VARIANTS[variant][1], "probabilistic": any(kinds), "lesions": entries, "changed": changed,
                             "applicable": changed > 0, "lesions_tsv": _relative(os.path.join(folder, LESIONS_TSV), out)}
        variant_lesions[variant] = seeds
    rows, parents, comparisons = [], list(parent_runs or ()), {}
    rule = thresholds.normalize_rule(rule)
    if compare:
        maps_dir = os.path.join(out, "maps")
        options = dict(cohortmaps_options or {})
        options.setdefault("cache_root", cohortmaps.default_cache_root(study.path))
        primary, primary_run = _cohortmaps_cohort([(sid, path) for sid, path in lesions], bundle, os.path.join(maps_dir, "primary"), "primary", emit, run_root, parents, {**options, "probabilistic": probabilistic}, cancel)
        parents.append(primary_run)
        for variant in variants:
            if not manifest[variant]["applicable"]:
                rows.append({"variant": variant, "map": "all", "kind": "variant", "changed": 0, "r": None, "dice": None, "peaks_persist": None, "note": "not applicable: no lesion changed"})
                continue
            # Fractional files are mapped with fractional weights; for binary files the weights are the same either way
            cohort_v, run_v = _cohortmaps_cohort(variant_lesions[variant], bundle, os.path.join(maps_dir, variant), variant, emit, run_root, [primary_run],
                                                 {**options, "probabilistic": manifest[variant]["probabilistic"]}, cancel)
            parents.append(run_v)
            result = compare_cohorts(primary, cohort_v, rule, sign=sign, overlap_pct=overlap_pct)
            changed = {entry["participant_id"]: entry["action"] in CHANGED_ACTIONS for entry in manifest[variant]["lesions"]}
            for row in result["rows"]:
                rows.append({"variant": variant, "map": row["map"], "kind": row["kind"], "changed": changed.get(row["participant_id"]) if row["participant_id"] else manifest[variant]["changed"],
                             "r": row["r"], "dice": row["dice"], "peaks_persist": row["peaks_persist"], "note": row["note"] or ("approximate (mirror)" if variant == "flipped" and row["kind"] == "lesion" else "")})
            comparisons[variant] = {"median_lesion_r": result["median_lesion_r"], "group_r": result["group_r"], "run_id": run_v}
        write_seeds_tsv(rows, os.path.join(out, SEEDS_TSV))
    _check_cancel(cancel)
    summary = {"seed_variants": list(variants), "comparisons": comparisons}
    run_id = _register(run_root, out, "seeds", {"variants": list(variants), "rule": rule, "sign": sign, "overlap_pct": overlap_pct, "probabilistic": probabilistic},
                       parents=parents, summary=summary, outputs=[VARIANTS_JSON] + ([SEEDS_TSV] if compare else [])) if register else None
    record = _record("seeds", out, run_id, study=_relative(study.path, out), rule=rule, sign=sign, overlap_pct=overlap_pct, primary_probabilistic=probabilistic,
                     primary_binarization=PRIMARY_FRACTIONAL if probabilistic else PRIMARY_NONZERO,
                     note="Derived seed files only; the study's lesions/ folder is never modified.", mirror_note=None if "flipped" not in variants else _mirror_caveat(),
                     variants=manifest, comparisons=comparisons)
    _write_json(os.path.join(out, VARIANTS_JSON), record)
    record["rows"] = rows
    return record


def _mirror_caveat():
    from .lesionops import MIRROR_NOTE

    return MIRROR_NOTE


def write_seeds_tsv(rows, path):
    """Write seed-variant comparison rows; columns :data:`SEEDS_COLUMNS`."""
    return _write_tsv(path, SEEDS_COLUMNS, rows)


def consolidate_hemisphere(study, side, out=None, lesions=None, run_root=None, parent_runs=(), register=True, cancel=None):
    """Derived seeds with every lesion on one hemisphere: lesions traced on the other side are mirrored (world x -> -x).

    An analysis option, recorded: the mirrored seeds are written to ``<out>/consolidated/`` with
    ``consolidation.json`` (side, caveat, one entry per lesion: traced side, action, source and output paths
    relative to ``out``, sha256) and ``lesions.tsv`` (participant_id, lesion, lesion_sha256; for cohortmaps
    ``--lesions-tsv``). Lesions already on ``side`` are referenced unchanged; bilateral and midline lesions are
    kept and listed. The traced masks are never modified.

    :param side: "L" or "R" (any form ``lesionops.normalize_side`` accepts).
    :param cancel: optional ``jobs.CancelToken`` (or Event/callable), checked before each lesion.
    :return: the consolidation.json record.
    """
    np = _np()
    from . import cohortmaps, lesionops, nifti
    from .export import file_sha256

    side = lesionops.normalize_side(side)
    if side not in ("L", "R"):
        raise ValueError("The consolidation side must be L or R")
    study = _study(study)
    out = os.path.abspath(os.fspath(out or os.path.join(study.path, "derivatives", "lnm", "robustness")))
    _check_out(study, out)
    if lesions is None:
        lesions, _excluded = cohortmaps.study_lesions(study)
    lesions = [(sid, path) for sid, path, *_rest in cohortmaps._normalize_lesions(lesions)]
    folder = os.path.join(out, "consolidated")
    os.makedirs(folder, exist_ok=True)
    run_root = os.path.abspath(os.fspath(run_root or study.path))
    entries, seeds, counts = [], [], {"mirrored": 0, "kept": 0, "bilateral_or_midline": 0, "empty": 0}
    for index, (sid, source) in enumerate(lesions):
        _check_cancel(cancel, done=index)
        data, affine, _header = nifti.read_nifti(source)
        values = np.nan_to_num(np.asarray(data, dtype=np.float64))
        if values.ndim > 3:
            values = values.reshape(values.shape[:3])
        traced = lesionops.side_check(values, affine, None)["traced_side"]
        entry = {"participant_id": sid, "traced_side": traced, "source": _relative(source, out), "source_sha256": file_sha256(source), "note": None}
        used = source
        if traced in ("L", "R") and traced != side:
            fractional, binary, note = lesionops.mirror_world(values, affine)
            used = os.path.join(folder, f"{sid}_hemi-{side}_desc-mirrored_mask.nii.gz")
            if _is_binary(values):
                nifti.write_nifti(used, binary, affine)
            else:
                nifti.write_nifti(used, fractional.astype(np.float32), affine)
            entry.update(action="mirrored", note=note, probabilistic=not _is_binary(values))
            counts["mirrored"] += 1
        elif traced == side:
            entry["action"] = "kept"
            counts["kept"] += 1
        elif traced is None:
            entry.update(action="kept", note="empty lesion: not mirrored")
            counts["empty"] += 1
        else:
            entry.update(action="kept", note=f"{traced} lesion: not mirrored")
            counts["bilateral_or_midline"] += 1
        entry["output"] = _relative(used, out)
        entry["output_sha256"] = file_sha256(used)
        entries.append(entry)
        seeds.append((sid, used))
    _check_cancel(cancel, done=len(lesions))
    tsv = _write_lesions_tsv(os.path.join(out, LESIONS_TSV), _lesion_rows(seeds, out))
    summary = {"consolidation": {"side": side, **counts}}
    run_id = _register(run_root, out, "consolidate", {"side": side}, parents=parent_runs, summary=summary, outputs=[CONSOLIDATION_JSON, LESIONS_TSV]) if register else None
    record = _record("consolidate", out, run_id, side=side, analysis_option=True, caveat=CONSOLIDATION_CAVEAT, mirror_note=_mirror_caveat(), study=_relative(study.path, out),
                     counts=counts, lesions=entries, lesions_tsv=_relative(tsv, out), traced_masks_modified=False)
    _write_json(os.path.join(out, CONSOLIDATION_JSON), record)
    return record


#
# (6) Connectome split-half
#


def connectome_split_half(run):
    """Summary of the connectome split-half of an engine run with ``--split-half`` (odd vs even subjects).

    :param run: the engine's output folder (its qa.json), qa.json or run.json, or their dict (or the "done" event).
    :return: {"label": :data:`CONNECTOME_SPLIT_HALF_LABEL`, "n_odd", "n_even", "r" ({lesion: r}), "median_r",
      "min_r", "small_halves" (fewer than 500 subjects per half), "rows" (connectomes.tsv rows)}.
    """
    import json

    data = run
    if isinstance(run, (str, os.PathLike)):
        path = os.fspath(run)
        if os.path.isdir(path):
            path = os.path.join(path, "qa.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    split = data.get("split_half") if isinstance(data, dict) else None
    if not isinstance(split, dict) or "r" not in split:
        raise ValueError("This run has no connectome split-half (run the engine with --split-half)")
    values = {name: _finite_or_none(r) for name, r in (split.get("r") or {}).items()}
    import statistics

    finite = sorted(r for r in values.values() if r is not None)
    n_odd, n_even = split.get("n_odd"), split.get("n_even")
    small = n_odd is not None and n_even is not None and min(n_odd, n_even) < CONNECTOME_SPLIT_HALF_MIN_HALF
    label = data.get("bundle") if isinstance(data.get("bundle"), str) else None
    note = CONNECTOME_SPLIT_HALF_LABEL + ("; fewer than 500 subjects per half" if small else "")
    rows = [{"map": name, "kind": "connectome_split_half", "participant_id": name, "connectome_a": label, "connectome_b": label, "n_a": n_odd, "n_b": n_even,
             "r": r, "dice": None, "peaks_persist": None, "flag": "", "note": note} for name, r in values.items()]
    return {
        "label": CONNECTOME_SPLIT_HALF_LABEL,
        "n_odd": n_odd,
        "n_even": n_even,
        "r": values,
        "median_r": float(statistics.median(finite)) if finite else None,
        "min_r": finite[0] if finite else None,
        "small_halves": small,
        "rows": rows,
    }


#
# Command line
#


def _parse_rule(text):
    from . import jobs

    if not text:
        return thresholds.normalize_rule(None)
    try:
        return thresholds.rule_from_json(text)
    except ValueError as exc:
        raise jobs.JobError(f"Invalid --rule: {exc}", code="usage", exit_code=jobs.EXIT_USAGE) from None


def build_parser():
    from . import jobs

    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.robustness", description="Robustness of lesion network maps: thresholds, connectomes, lesion resampling and seeds.")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, help_text, maps=True):
        sub = commands.add_parser(name, help=help_text, allow_abbrev=False)
        jobs.add_common_arguments(sub)
        sub.add_argument("--out", required=True, help="run folder (written files: see the module docstring)")
        if maps:
            sub.add_argument("--rule", default=None, help="threshold rule as JSON (default FWE)")
            sub.add_argument("--sign", choices=thresholds.SIGNS, default="both", help="both (|map| >= t), pos or neg (default: both)")
        return sub

    sweep = command("sweep", "threshold sweep of a cohortmaps run")
    sweep.add_argument("--maps", required=True, help="maps.tsv of a cohortmaps run")
    sweep.add_argument("--overlap", type=float, default=75.0, help="primary overlap criterion in %% (default 75)")
    replicate = command("replicate", "replicate a cohortmaps run on another connectome")
    replicate.add_argument("--maps", required=True, help="maps.tsv of the cohortmaps run on connectome A")
    replicate.add_argument("--bundle-b", required=True, help="connectome B (.lnmc)")
    replicate.add_argument("--overlap", type=float, default=DEFAULT_SENSITIVITY_PCT, help=f"sensitivity criterion in %% (default {DEFAULT_SENSITIVITY_PCT:g})")
    replicate.add_argument("--cache", default=None, help="cohortmaps cache folder for connectome B")
    replicate.add_argument("--budget-gb", type=float, default=None, help="memory budget of one engine pass in GB")
    splithalf = command("splithalf", "lesion split-half, or the summary of a connectome split-half")
    source = splithalf.add_mutually_exclusive_group(required=True)
    source.add_argument("--maps", default=None, help="maps.tsv of a cohortmaps run (lesion split-half)")
    source.add_argument("--connectome-run", default=None, help="output folder of an engine run with --split-half (connectome sampling stability)")
    splithalf.add_argument("--n-splits", type=jobs.positive_int, default=DEFAULT_SPLITS, help=f"random splits (default {DEFAULT_SPLITS})")
    splithalf.add_argument("--seed", type=int, default=0, help="random seed of the splits")
    splithalf.add_argument("--overlap", type=float, default=DEFAULT_SENSITIVITY_PCT, help=f"sensitivity criterion in %% (default {DEFAULT_SENSITIVITY_PCT:g})")
    splithalf.add_argument("--study", default=None, help="study folder (with --design or --strata)")
    splithalf.add_argument("--design", default=None, help="design.json: also the r of the halves' GLM t maps")
    splithalf.add_argument("--strata", default=None, help="participants.tsv column to split within (e.g. group)")
    influence = command("influence", "leave-one-lesion-out influence")
    influence.add_argument("--maps", required=True, help="maps.tsv of a cohortmaps run")
    influence.add_argument("--overlap", type=float, default=DEFAULT_SENSITIVITY_PCT, help=f"sensitivity criterion in %% (default {DEFAULT_SENSITIVITY_PCT:g})")
    influence.add_argument("--study", default=None, help="study folder (with --design)")
    influence.add_argument("--design", default=None, help="design.json: also the change of the GLM peak")
    influence.add_argument("--top-k", type=jobs.positive_int, default=DEFAULT_TOP_K, help=f"influential lesions to list (default {DEFAULT_TOP_K})")
    seeds = command("seeds", "seed variants: build derived lesion files and compare their maps")
    seeds.add_argument("--study", required=True, help="study folder")
    seeds.add_argument("--bundle", default=None, help="connectome (.lnmc); required unless --no-maps")
    seeds.add_argument("--variants", default=",".join(DEFAULT_VARIANTS), help=f"comma-separated variants (default: {','.join(DEFAULT_VARIANTS)})")
    seeds.add_argument("--coordinates", default=None, help="TSV of coordinate seeds: participant_id, x, y, z (RAS mm)")
    seeds.add_argument("--overlap", type=float, default=DEFAULT_SENSITIVITY_PCT, help=f"sensitivity criterion in %% (default {DEFAULT_SENSITIVITY_PCT:g})")
    seeds.add_argument("--probabilistic", action="store_true", help="the primary seeds are mapped with fractional weights")
    seeds.add_argument("--no-maps", action="store_true", help="only build the variant files and the manifest")
    seeds.add_argument("--cache", default=None, help="cohortmaps cache (default: the study's)")
    seeds.add_argument("--budget-gb", type=float, default=None, help="memory budget of one engine pass in GB")
    consolidate = command("consolidate", "derived seeds with every lesion on one hemisphere (mirrored)", maps=False)
    consolidate.add_argument("--study", required=True, help="study folder")
    consolidate.add_argument("--side", required=True, help="hemisphere to consolidate on: L or R")
    return parser


def _design_for(study_path, design_path, ids):
    """(X, C) of a design.json built on the study, with its rows in the order of ``ids`` (maps without a design row are dropped)."""
    from . import design as design_lib

    built = design_lib.build_design(study_path, design_lib.DesignSpec.load(design_path))
    if built.blocking:
        raise ValueError("The design is blocked: " + "; ".join(check.message for check in built.blocking))
    rows = {sid: index for index, sid in enumerate(built.sids)}
    keep = [sid for sid in ids if sid in rows]
    return built.X[[rows[sid] for sid in keep]], built.C, keep


def _strata_for(study_path, column, ids):
    from .study import Study

    study = Study.open(study_path)
    if column not in study.columns:
        raise ValueError(f"The study has no column {column!r}")
    missing = [sid for sid in ids if sid not in study]
    if missing:
        raise ValueError(f"Strata: {len(missing)} map(s) have no row in the study ({', '.join(missing[:5])})")
    return [study.value(sid, column) for sid in ids]


def _run_root(args, out, study=None):
    return os.path.abspath(args.run_root or study or out)


def main(argv=None):
    """Command-line entry point (see the module docstring); returns the exit code."""
    np = _np()
    from . import cohortmaps, jobs

    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    out = os.path.abspath(args.out)
    if args.command not in ("seeds", "consolidate"):
        # seeds and consolidate create their folder themselves, after checking that it is not inside lesions/
        os.makedirs(out, exist_ok=True)
    rule = _parse_rule(getattr(args, "rule", None))
    parents = list(args.parent_run or [])
    cancel = jobs.CancelToken(args.cancel_file)
    try:
        if args.command == "sweep":
            cohort = load_cohort(args.maps)
            cancel.raise_if_cancelled()
            rows = threshold_sweep(cohort.maps, cohort.n, cohort.v, rule, args.overlap, sign=args.sign, grid=cohort.grid, mask_indices=cohort.mask_indices, gsr=cohort.gsr)
            write_sweep_tsv(rows, os.path.join(out, SWEEP_TSV))
            flip = conclusion_flips(rows)
            summary = {"conclusion_flips": flip["text"] if flip else None, "settings": len(rows)}
            run_id = _register(_run_root(args, out), out, "sweep", {"rule": rule, "overlap_pct": args.overlap, "sign": args.sign}, parents=[*parents, cohort.run_id], summary=summary, outputs=[SWEEP_TSV])
            _write_json(os.path.join(out, "sweep.json"), _record("sweep", out, run_id, rule=rule, overlap_pct=args.overlap, sign=args.sign, summary=summary))
            outputs = [os.path.join(out, SWEEP_TSV)]
        elif args.command == "replicate":
            cohort = load_cohort(args.maps)
            rows = cohortmaps.read_maps_tsv(args.maps)
            lesions = [(row["participant_id"], row["lesion"], row.get("lesion_sha256")) for row in rows]
            options = {"budget_bytes": cohortmaps._budget_bytes(args.budget_gb), "threads": args.threads, "resume": args.resume}
            if args.cache:
                options["cache_root"] = args.cache
            record = replicate_on(cohort, None, args.bundle_b, lesions, rule, out, sign=args.sign, overlap_pct=args.overlap, cohortmaps_options=options,
                                  run_root=_run_root(args, out), parent_runs=parents, cancel=cancel)
            run_id, summary = record["run_id"], record["summary"]
            outputs = [os.path.join(out, CONNECTOMES_TSV), os.path.join(out, "replicate.json")]
        elif args.command == "splithalf" and args.connectome_run:
            cancel.raise_if_cancelled()
            result = connectome_split_half(args.connectome_run)
            write_connectomes_tsv(result["rows"], os.path.join(out, CONNECTOMES_TSV))
            summary = {"split_half": {key: result[key] for key in ("label", "n_odd", "n_even", "median_r", "min_r", "small_halves")}}
            run_id = _register(_run_root(args, out), out, "connectome_split_half", {}, parents=parents, summary=summary, outputs=[CONNECTOMES_TSV])
            _write_json(os.path.join(out, "connectome_split_half.json"), _record("connectome_split_half", out, run_id, **{k: v for k, v in result.items() if k != "rows"}))
            outputs = [os.path.join(out, CONNECTOMES_TSV)]
        elif args.command in ("splithalf", "influence"):
            cohort = load_cohort(args.maps)
            design = None
            if args.design:
                if not args.study:
                    raise jobs.JobError("--design needs --study", code="usage", exit_code=jobs.EXIT_USAGE)
                X, C, keep = _design_for(args.study, args.design, cohort.ids)
                cohort = cohort.subset(keep)
                design = (X, C)
            if args.command == "splithalf":
                strata = _strata_for(args.study, args.strata, cohort.ids) if args.strata and args.study else None
                result = lesion_split_half(cohort, n_splits=args.n_splits, seed=args.seed, rule=rule, sign=args.sign, overlap_pct=args.overlap, design=design, strata=strata, cancel=cancel)
                write_splithalf_tsv(result, os.path.join(out, SPLITHALF_TSV))
                arrays = {key: result[key] for key in ("r_overlap", "dice_sensitivity", "r_glm_t") if result[key] is not None}
                np.savez_compressed(os.path.join(out, SPLITHALF_NPZ), **arrays)
                summary = {"lesion_split_half": {metric: values.get("median") for metric, values in result["summary"].items()}, "label": result["label"]}
                params = {"rule": rule, "sign": args.sign, "overlap_pct": args.overlap, "n_splits": args.n_splits, "design": bool(design), "strata": args.strata}
                outputs = [SPLITHALF_TSV, SPLITHALF_NPZ]
                fields = {k: v for k, v in result.items() if k not in arrays}
            else:
                result = leave_one_lesion_out(cohort, design=design, rule=rule, sign=args.sign, overlap_pct=args.overlap, top_k=args.top_k, cancel=cancel)
                write_influence_tsv(result, os.path.join(out, INFLUENCE_TSV))
                summary = {"influence_top": result["top"], "influence": result["influence"]}
                params = {"rule": rule, "sign": args.sign, "overlap_pct": args.overlap, "design": bool(design), "top_k": args.top_k}
                outputs = [INFLUENCE_TSV]
                fields = {k: v for k, v in result.items() if k != "rows"}
            run_id = _register(_run_root(args, out, args.study), out, args.command, params, parents=[*parents, cohort.run_id], summary=summary, outputs=outputs,
                               seeds={"splits": args.seed} if args.command == "splithalf" else None)
            _write_json(os.path.join(out, f"{args.command}.json"), _record(args.command, out, run_id, **fields))
            outputs = [os.path.join(out, name) for name in outputs]
        elif args.command == "seeds":
            variants = [name.strip() for name in args.variants.split(",") if name.strip()]
            if not args.no_maps and not args.bundle:
                raise jobs.JobError("seeds needs --bundle (or --no-maps)", code="usage", exit_code=jobs.EXIT_USAGE)
            options = {"budget_bytes": cohortmaps._budget_bytes(args.budget_gb), "threads": args.threads, "resume": args.resume}
            if args.cache:
                options["cache_root"] = args.cache
            coordinates = read_coordinates(args.coordinates) if args.coordinates else None
            record = seed_variants(args.study, variants, out, bundle=args.bundle, coordinates=coordinates, rule=rule, sign=args.sign, overlap_pct=args.overlap,
                                   probabilistic=args.probabilistic, compare=not args.no_maps, cohortmaps_options=options, run_root=args.run_root, parent_runs=parents, cancel=cancel)
            run_id, summary = record["run_id"], {"seed_variants": variants}
            outputs = [os.path.join(out, VARIANTS_JSON)] + ([] if args.no_maps else [os.path.join(out, SEEDS_TSV)])
        else:
            record = consolidate_hemisphere(args.study, args.side, out, run_root=args.run_root, parent_runs=parents, cancel=cancel)
            run_id, summary = record["run_id"], {"consolidation": record["counts"]}
            outputs = [os.path.join(out, CONSOLIDATION_JSON), os.path.join(out, LESIONS_TSV)]
    except (jobs.Cancelled, jobs.JobError):
        raise
    except (OSError, ValueError, KeyError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None
    jobs.emit("done", outputs=outputs, elapsed=round(jobs.elapsed(), 3), run_id=run_id, summary=summary)
    return jobs.EXIT_OK


if __name__ == "__main__":
    from LesionNetworkMappingLib import jobs as _jobs

    _jobs.run_job(main, job="robustness")

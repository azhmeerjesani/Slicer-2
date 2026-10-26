"""Cluster and peak tables of thresholded maps: the manuscript table (cluster, sign, extent, peak, MNI, labels).

:func:`cluster_table` follows the semantics of nilearn's ``get_clusters_table``:

- supra-threshold voxels (``x >= t`` for positive clusters, ``x <= -t`` for negative ones, the convention of
  ``stats.threshold_map``) are grouped into connected clusters (26-connected by default) separately for each sign,
  so a two-sided map gives positive and negative clusters that never merge;
- clusters smaller than ``min_extent_mm3`` are dropped; the others are sorted by the strength of their peak;
- each cluster reports its peak and up to ``max_subpeaks`` sub-peaks (rows "1a", "1b", ...): local maxima of the
  cluster (no stronger voxel among their 26 neighbours in the cluster) more than ``subpeak_min_mm`` (8 mm) away
  from every stronger peak kept.

Unlike nilearn, cluster numbers continue across signs (positive clusters first) so that every id is unique, the
table gives the value-weighted centre of gravity and the hemisphere of each peak, and the values of other maps at
each peak (``extra_maps``, e.g. Fz, PFwe, Hedges g). A plateau of equal maxima gives one peak: its voxel nearest to
the plateau's centre.

Extents are descriptive: there is no cluster-extent inference (addendum §3.4, §9), and the caption says so.

Labels are injected: ``labeler(mask, grid)`` returns [(atlas, label, pct)] (or (atlas, label, pct, network)) for the
boolean cluster volume; :func:`atlas_labeler` wraps ``atlases.label_cluster``. :func:`pair_homologues` pairs
left/right mirror peaks (Ferguson 2019 Table 1) and :func:`write_clusters` writes the TSV and HTML tables.
"""

import html
import math
import os
import string
import tempfile

import numpy as np
from scipy import ndimage

from . import thresholds

SIGNS = ("pos", "neg")
CONNECTIVITY_RANK = {6: 1, 18: 2, 26: 3}  # ndimage.generate_binary_structure(3, rank)
MIDLINE_MM = 0.5  # |x| below this is "midline"
DESCRIPTIVE_NOTE = "Extents are descriptive (no cluster-extent inference)."
SIGN_LABELS = {"pos": "+", "neg": "-"}  # ASCII, so that the TSV reads anywhere


class ClusterTable(list):
    """The rows of :func:`cluster_table` (dicts, see there) with how they were made.

    :ivar statistic: name of the thresholded map ("T", "Fz", ...).
    :ivar threshold: absolute threshold value; ``resolved`` is the :class:`thresholds.Resolved` when one was given.
    :ivar sign: "both", "pos" or "neg"; ``connectivity``, ``min_extent_mm3``, ``subpeak_min_mm`` and
      ``max_subpeaks`` as given.
    :ivar voxel_mm3: voxel volume of the grid.
    :ivar atlases: ids of the atlases that labelled the clusters (in order of first use).
    :ivar notices: notices of the labeler (e.g. atlases skipped for their space family).
    :ivar cluster_vector: int32 vector aligned with ``values``: the cluster number of each voxel (0 outside).
    :ivar dropped: number of clusters smaller than ``min_extent_mm3``.
    """

    def __init__(self, rows=()):
        super().__init__(rows)
        self.statistic = None
        self.threshold = None
        self.resolved = None
        self.sign = "both"
        self.connectivity = 26
        self.min_extent_mm3 = 0.0
        self.subpeak_min_mm = 8.0
        self.max_subpeaks = 3
        self.voxel_mm3 = None
        self.atlases = []
        self.notices = []
        self.cluster_vector = None
        self.dropped = 0

    @property
    def clusters(self):
        """The cluster rows (one per cluster, without the sub-peak rows)."""
        return [row for row in self if not row["subpeak"]]


def _threshold_of(threshold, statistic):
    """(value, Resolved or None, statistic) of a threshold given as a Resolved, its dict or a number."""
    resolved = None
    if isinstance(threshold, dict):
        threshold = thresholds.Resolved.from_dict(threshold)
    if isinstance(threshold, thresholds.Resolved):
        resolved = threshold
        value = float(threshold.value)
        statistic = statistic or threshold.statistic
    else:
        value = float(threshold)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"The cluster-forming threshold must be a finite number > 0, got {value}")
    return value, resolved, statistic or "T"


def _as_volume(values, grid, mask_indices):
    """(volume of grid.shape with NaN outside the mask, flat positions of the values)."""
    values = np.asarray(values, dtype=np.float64)
    if mask_indices is None:
        if values.size != grid.n_voxels:
            raise ValueError(f"The map has {values.size} values but the grid {grid.shape} has {grid.n_voxels} voxels (pass mask_indices for a masked vector)")
        return values.reshape(grid.shape), np.arange(grid.n_voxels)
    positions = np.asarray(mask_indices)
    values = values.ravel()
    if positions.size != values.size:
        raise ValueError("values and mask_indices have different lengths")
    volume = np.full(grid.n_voxels, np.nan)
    volume[positions] = values
    return volume.reshape(grid.shape), positions


def _hemisphere(x):
    if abs(x) < MIDLINE_MM:
        return "midline"
    return "R" if x > 0 else "L"


def _local_maxima(score, cluster):
    """Flat indices (in ``score``) of the local maxima of a cluster: one voxel per plateau of equal maxima."""
    inside = np.where(cluster, score, -np.inf)
    neighbourhood = ndimage.maximum_filter(inside, size=3, mode="constant", cval=-np.inf)
    maxima = cluster & (inside >= neighbourhood)
    plateaus, count = ndimage.label(maxima, structure=np.ones((3, 3, 3), dtype=bool))
    if count == len(np.flatnonzero(maxima)):  # no plateau: every maximum is its own voxel
        return np.flatnonzero(maxima)
    peaks = []
    for index, region in enumerate(ndimage.find_objects(plateaus), start=1):
        local = np.argwhere(plateaus[region] == index) + [s.start for s in region]
        centre = local.mean(axis=0)
        nearest = local[np.argmin(((local - centre) ** 2).sum(axis=1))]  # argwhere is in C order: ties go to the first
        peaks.append(np.ravel_multi_index(tuple(nearest), score.shape))
    return np.asarray(peaks, dtype=np.int64)


def _peaks(score, cluster, offset, grid, min_mm, max_subpeaks):
    """Peak and sub-peaks of a cluster as (ijk (n, 3) in the grid, world (n, 3)), strongest first."""
    flat = _local_maxima(score, cluster)
    ijk = np.stack(np.unravel_index(flat, score.shape), axis=1) + offset
    strength = score.reshape(-1)[flat]
    order = np.lexsort((np.ravel_multi_index(tuple(ijk.T), grid.shape), -strength))  # strongest first, ties by index
    ijk = ijk[order]
    world = grid.voxel_to_world(ijk)
    kept = []
    for index in range(len(ijk)):
        # The tolerance keeps the boundary grid-independent: on an oblique affine 10 mm computes as 10.000000000000002
        if all(np.sqrt(((world[index] - world[other]) ** 2).sum()) > min_mm + 1e-9 for other in kept):
            kept.append(index)
            if len(kept) > max_subpeaks:
                break
    return ijk[kept], world[kept]


def _label_rows(labels, table):
    """Normalize labeler output to [(atlas, label, pct)] and the network of its largest overlap."""
    rows, network = [], None
    for notice in getattr(labels, "notices", None) or []:
        if notice not in table.notices:  # one notice per atlas, not one per cluster
            table.notices.append(notice)
    for item in labels or []:
        atlas, label, pct = item[0], item[1], float(item[2])
        rows.append((atlas, label, pct))
        if atlas not in table.atlases:
            table.atlases.append(atlas)
        if network is None and len(item) > 3 and isinstance(item[3], str) and item[3]:
            network = item[3]
    return rows, network


def cluster_table(values, grid, mask_indices, threshold, sign="both", connectivity=26, min_extent_mm3=0.0, subpeak_min_mm=8.0, max_subpeaks=3, labeler=None, extra_maps=None, statistic=None):
    """Cluster and peak table of a map (descriptive extents; no cluster-extent inference).

    :param values: masked vector (with ``mask_indices``), or a flat or 3D volume on ``grid`` (``mask_indices`` None).
      NaNs never cluster.
    :param grid: :class:`geometry.Grid` of the map (world coordinates are RAS mm, i.e. MNI for template grids).
    :param threshold: a :class:`thresholds.Resolved` (or its dict) or an absolute number t > 0.
    :param sign: "both" (positive and negative clusters, split by sign), "pos" or "neg".
    :param connectivity: 6, 18 or 26 (faces, edges, corners).
    :param min_extent_mm3: clusters smaller than this volume are dropped (counted in ``table.dropped``).
    :param subpeak_min_mm: sub-peaks closer than or at this distance to a stronger peak are not reported.
    :param max_subpeaks: sub-peaks reported per cluster after its peak.
    :param labeler: ``labeler(mask, grid)`` -> [(atlas, label, pct[, network])] for a boolean volume of
      ``grid.shape`` (e.g. :func:`atlas_labeler`); None leaves the labels empty.
    :param extra_maps: {name: map laid out like ``values``}: their values at each peak (e.g. Fz, PFwe, HedgesG).
    :param statistic: name of the thresholded map (default: the Resolved's statistic, else "T").
    :return: :class:`ClusterTable` of dicts, cluster rows followed by their sub-peak rows::

        {"id": "1" or "1a", "cluster": 1, "subpeak": False, "sign": "pos" or "neg",
         "voxels": 120, "mm3": 960.0 (None on sub-peak rows), "peak": 6.1, "extra": {"Fz": 0.21},
         "ijk": (i, j, k), "xyz": (x, y, z), "hemisphere": "L", "R" or "midline",
         "cog": (x, y, z) value-weighted centre of gravity (None on sub-peak rows),
         "labels": [(atlas, label, pct)], "network": "Default" or None}
    """
    if sign not in ("both", *SIGNS):
        raise ValueError("sign must be 'both', 'pos' or 'neg'")
    if connectivity not in CONNECTIVITY_RANK:
        raise ValueError("connectivity must be 6, 18 or 26")
    if not 0 <= int(max_subpeaks) <= len(string.ascii_lowercase):
        raise ValueError("max_subpeaks must be between 0 and 26")
    value, resolved, statistic = _threshold_of(threshold, statistic)
    volume, positions = _as_volume(values, grid, mask_indices)
    extras = {}
    for name, extra in (extra_maps or {}).items():
        extras[name] = _as_volume(extra, grid, mask_indices)[0]

    table = ClusterTable()
    table.statistic, table.threshold, table.resolved, table.sign = statistic, value, resolved, sign
    table.connectivity, table.min_extent_mm3 = connectivity, float(min_extent_mm3)
    table.subpeak_min_mm, table.max_subpeaks = float(subpeak_min_mm), int(max_subpeaks)
    table.voxel_mm3 = round(grid.voxel_volume, 9)  # 8.0, not 7.999999999999998, for a 2 mm grid
    numbers = np.zeros(grid.shape, dtype=np.int32)
    structure = ndimage.generate_binary_structure(3, CONNECTIVITY_RANK[connectivity])
    finite = np.isfinite(volume)
    number = 0
    for current in SIGNS if sign == "both" else (sign,):
        score = np.where(finite, volume if current == "pos" else -volume, -np.inf)
        labels, _count = ndimage.label(score >= value, structure=structure)
        found = []
        for index, region in enumerate(ndimage.find_objects(labels), start=1):
            cluster = labels[region] == index
            voxels = int(cluster.sum())
            if voxels * table.voxel_mm3 < table.min_extent_mm3:
                table.dropped += 1
                continue
            offset = np.array([s.start for s in region])
            ijk, world = _peaks(score[region], cluster, offset, grid, table.subpeak_min_mm, table.max_subpeaks)
            first = np.ravel_multi_index(tuple(np.argwhere(cluster)[0] + offset), grid.shape)
            found.append((-float(score[tuple(ijk[0])]), -voxels, first, region, cluster, ijk, world))
        for _strength, _voxels, _first, region, cluster, ijk, world in sorted(found, key=lambda item: item[:3]):
            number += 1
            numbers[region][cluster] = number
            local = np.argwhere(cluster) + [s.start for s in region]
            weights = np.abs(volume[tuple(local.T)])
            cog = grid.voxel_to_world((local * weights[:, None]).sum(axis=0) / weights.sum() if weights.sum() > 0 else local.mean(axis=0))
            labels_of, network = [], None
            if labeler is not None:
                mask = np.zeros(grid.shape, dtype=bool)
                mask[region][cluster] = True
                labels_of, network = _label_rows(labeler(mask, grid), table)
            for peak, (voxel, xyz) in enumerate(zip(ijk, world, strict=True)):
                index = tuple(int(v) for v in voxel)
                main = peak == 0
                table.append(
                    {
                        "id": str(number) if main else f"{number}{string.ascii_lowercase[peak - 1]}",
                        "cluster": number,
                        "subpeak": not main,
                        "sign": current,
                        "voxels": len(local) if main else None,
                        "mm3": len(local) * table.voxel_mm3 if main else None,
                        "peak": float(volume[index]),
                        "extra": {name: float(extra[index]) for name, extra in extras.items()},
                        "ijk": index,
                        "xyz": tuple(float(v) for v in xyz),
                        "hemisphere": _hemisphere(float(xyz[0])),
                        "cog": tuple(float(v) for v in cog) if main else None,
                        "labels": labels_of if main else [],
                        "network": network if main else None,
                    },
                )
    table.cluster_vector = numbers.reshape(-1)[positions].astype(np.int32)
    return table


def atlas_labeler(atlases, top=3, min_pct=0.0, space=None, allow_cross_family=False):
    """A ``labeler`` for :func:`cluster_table` that labels each cluster with ``atlases.label_cluster``.

    Rows are (atlas id, label, pct of the cluster, network); the atlases' notices (e.g. a space-family mismatch)
    reach ``table.notices``.
    """
    from . import atlases as atlases_module

    def labeler(mask, grid):
        result = atlases_module.label_cluster(mask, grid, atlases, allow_cross_family=allow_cross_family, space=space, top=top, min_pct=min_pct)
        rows = atlases_module.LabelResult((detail["atlas"], detail["label"], detail["pct_of_mask"], detail["network"]) for detail in result.details)
        rows.notices = result.notices
        return rows

    return labeler


def pair_homologues(rows, max_mm=10.0):
    """Pair left and right peaks that mirror each other (Ferguson 2019 Table 1: region | MNI L | MNI R).

    A left peak (x < 0) and a right peak (x > 0) of the same sign pair when the mirror (-x, y, z) of one lies within
    ``max_mm`` of the other; pairs are formed closest first and every row is used at most once.

    :return: list of {"left": row or None, "right": row or None, "distance_mm": float or None}, one entry per pair
      and per unpaired row (midline peaks go to "left"), sorted by the strongest |peak| of the entry.
    """
    rows = list(rows)
    candidates = []
    for i, left in enumerate(rows):
        for j, right in enumerate(rows):
            if left["hemisphere"] == "L" and right["hemisphere"] == "R" and left["sign"] == right["sign"]:
                mirror = np.array(left["xyz"]) * [-1.0, 1.0, 1.0]
                distance = float(np.sqrt(((mirror - np.array(right["xyz"])) ** 2).sum()))
                if distance <= max_mm:
                    candidates.append((distance, i, j))
    used, entries = set(), []
    for distance, i, j in sorted(candidates):
        if i not in used and j not in used:
            used.update((i, j))
            entries.append({"left": rows[i], "right": rows[j], "distance_mm": distance})
    for i, row in enumerate(rows):
        if i not in used:
            side = "right" if row["hemisphere"] == "R" else "left"
            entries.append({"left": row if side == "left" else None, "right": row if side == "right" else None, "distance_mm": None})

    def strength(entry):
        return max(abs(entry[side]["peak"]) for side in ("left", "right") if entry[side] is not None)

    return sorted(entries, key=lambda entry: -strength(entry))


def _format_number(value, digits=3):
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.{digits}g}"


def cluster_caption(table, n=None, df=None, atlases=None, space="MNI"):
    """Caption of a cluster table: threshold, rule, N, df, connectivity, extent, label atlases and the
    statement that the extents are descriptive (no cluster-extent inference).

    :param n: number of subjects (default: the Resolved's N); ``df`` likewise.
    :param atlases: label atlas names (default: ``table.atlases``).
    """
    resolved = table.resolved
    # A rule resolved for a map of unknown N (thresholds.resolve_unknown_n) records n = df = 0: "N unknown"
    n_unknown = n is None and resolved is not None and thresholds.N_UNKNOWN_NOTE in (resolved.notes or ())
    n = n if n is not None or n_unknown else (resolved.n if resolved is not None else None)
    df = df if df is not None or n_unknown else (resolved.df if resolved is not None else None)
    quantity = {"both": f"|{table.statistic}|", "pos": table.statistic, "neg": f"-{table.statistic}"}[table.sign]
    parts = [f"Clusters of the {table.statistic} map at {quantity} ≥ {_format_number(table.threshold, 4)}"]
    if resolved is not None:
        rule = thresholds.rule_label(resolved.rule)
        alpha = resolved.rule.get("alpha")
        rule += f", α = {alpha:g}" if alpha is not None else ""  # noqa: RUF001
        rule += f", p_unc ≤ {thresholds.format_p(resolved.p_unc)}" if resolved.p_unc is not None else ""
        parts[0] += f" (rule: {rule})"
    counts = ["N unknown" if n_unknown else f"N = {n}" if n is not None else None, f"df = {df}" if df is not None else None]
    if any(counts):
        parts.append(", ".join(text for text in counts if text))
    sides = {"both": "two-sided, positive and negative clusters split by sign", "pos": "positive clusters", "neg": "negative clusters"}[table.sign]
    parts.append(f"{sides}; {table.connectivity}-connected; minimum extent {table.min_extent_mm3:g} mm³")
    parts.append(f"sub-peaks (up to {table.max_subpeaks} per cluster) more than {table.subpeak_min_mm:g} mm from a stronger peak")
    parts.append(f"x, y, z: {space} coordinates (mm) of the peak voxel; CoG: value-weighted centre of gravity")
    names = list(atlases) if atlases is not None else list(table.atlases)
    parts.append(f"labels: {', '.join(names)} (% of the cluster)" if names else "no atlas labels")
    return "; ".join(parts) + ". " + DESCRIPTIVE_NOTE


def _extra_names(rows):
    names = []
    for row in rows:
        for name in row["extra"]:
            if name not in names:
                names.append(name)
    return names


def _cells(row, extras, digits=4):
    def coord(values):
        return [f"{v:.1f}" for v in values] if values is not None else ["", "", ""]

    labels = "; ".join(f"{atlas}: {label} ({pct:.1f}%)" for atlas, label, pct in row["labels"])
    return [
        row["id"],
        SIGN_LABELS[row["sign"]],
        "" if row["voxels"] is None else str(row["voxels"]),
        "" if row["mm3"] is None else f"{row['mm3']:.10g}",  # 1225043 and 12345.75, never 1.22504e+06
        _format_number(row["peak"], digits),
        *[_format_number(row["extra"].get(name), digits) for name in extras],
        *coord(row["xyz"]),
        row["hemisphere"],
        *coord(row["cog"]),
        labels,
        row["network"] or "",
    ]


def _header(extras):
    return ["cluster_id", "sign", "voxels", "volume_mm3", "peak", *extras, "x", "y", "z", "hemisphere", "cog_x", "cog_y", "cog_z", "labels", "network"]


def _write_atomic(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".clusters-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        umask = os.umask(0)  # mkstemp creates 0600: give the file the mode open() would (as the other LNM writers do)
        os.umask(umask)
        os.chmod(temporary, 0o666 & ~umask)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


def write_clusters(rows, tsv_path=None, html_path=None, caption=None):
    """Write a cluster table as TSV (one header line, tab-separated, no comments) and as an HTML table.

    :param rows: a :class:`ClusterTable` (or its rows).
    :param caption: caption text; by default :func:`cluster_caption` of ``rows`` (a :class:`ClusterTable`).
      The HTML table carries it; the TSV has data only.
    :return: the caption.
    """
    if caption is None:
        if not isinstance(rows, ClusterTable):
            raise ValueError("A caption is needed for rows that are not a ClusterTable")
        caption = cluster_caption(rows)
    extras = _extra_names(rows)
    header = _header(extras)
    if tsv_path:
        clean = [[cell.replace("\t", " ").replace("\n", " ") for cell in _cells(row, extras)] for row in rows]
        _write_atomic(tsv_path, "\n".join("\t".join(line) for line in [header, *clean]) + "\n")
    if html_path:
        head = "".join(f"<th>{html.escape(name)}</th>" for name in header)
        body = "\n".join(
            f"<tr class=\"{'subpeak' if row['subpeak'] else 'cluster'}\">" + "".join(f"<td>{html.escape(cell)}</td>" for cell in _cells(row, extras)) + "</tr>"
            for row in rows
        )
        page = (
            '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<title>Cluster table</title>\n'
            "<style>table{border-collapse:collapse;font:13px sans-serif}caption{caption-side:bottom;text-align:left;"
            "padding-top:6px;max-width:60em}th,td{padding:2px 8px;border-bottom:1px solid #ccc;text-align:right}"
            "td:nth-last-child(-n+2),th:nth-last-child(-n+2){text-align:left}tr.subpeak td{color:#555}</style>\n"
            f"</head>\n<body>\n<table>\n<caption>{html.escape(caption)}</caption>\n<thead><tr>{head}</tr></thead>\n"
            f"<tbody>\n{body}\n</tbody>\n</table>\n</body>\n</html>\n"
        )
        _write_atomic(html_path, page)
    return caption

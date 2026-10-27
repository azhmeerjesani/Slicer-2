"""Networks card of the Map module (order 115): which networks a map falls in, with a null or "not tested" (L-32).

For every network of Yeo-7 or Yeo-17 the table gives, for the map and threshold chosen in the card (the same
``SourceModel`` as the Clusters card):

- the **percentage of the suprathreshold voxels** that lie in the network, positive and negative separately;
- the **percentage of the network** that is suprathreshold;
- the **mean and median** T (or Fz) of the map in the network;
- the **spatial r** of the map with the network's binary mask (over every in-mask voxel).

The cortex comes from the Schaefer names (``7Networks_LH_Vis_1`` is Vis), the cerebellum from the Buckner networks and the
subcortex from the Tian structures; an atlas that is not cached is named, and offered (never downloaded implicitly). Atlases
of another template family than the map are not used (no silent mix).

**Significance is null-referenced or "not tested" and never omitted.**

- A single lesion is compared with its relocated-lesion null maps when the run kept them (the engine's ``--write-null``
  files ``*_desc-RelocatedT_funcmap.nii.gz`` / ``RelocatedFz``): the percentile of the network's mean value (and of its spatial
  r) among the same value in each relocated lesion's map. "Test against relocated lesions" runs the engine once to make them.
  The resolution is 1/(K + 1) and the percentile is not corrected for the number of networks.
- A group map is compared with the null cohorts' mean maps (``null_group_maps`` of a Nulls run: a voxel sample) when the map
  is the mean map of the statistic the Nulls run used.
- Otherwise every row says "not tested", and the card says why.

**Regions of interest** (a NIfTI file or a segment) are added to the table, and, for a cohort, give the Cohen 2019 Figure 7
chart: the percentage of lesions whose map is connected to each ROI (a lesion is connected when at least the chosen share of
the ROI's voxels pass that lesion's threshold), made with ``LesionNetworkMappingLib.figures`` (``Figure`` and ``write_figure``).

**Exports**: ``networks.tsv`` and ``networks.json`` (atlases, threshold, null status and caption; no local paths), PNG and SVG
of the charts, and the volumetric files of the Network Correspondence Toolbox on the FSL MNI 2 mm grid (the grid of the
GSP connectome): the binary suprathreshold masks and the thresholded map.

Works in Classic and without a main window.
"""

import json
import logging
import math
import os
import tempfile

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_NETWORKS, widgets
from .card_clusters import (
    LAST_FOLDER_SETTING,
    TABLE_FONT_PX,
    UNLABELLED_FAMILIES,
    SourceBar,
    SourceError,
    _label,
    _setAlignment,
    _settings,
    confirmFetch,
    mapWidget,
    shortName,
    sharedAtlases,
    sharedModel,
)
from .cohort_contrast import _idClickedSignal

#: Fork-only settings (SPEC Appendix B)
SCHEME_SETTING = "Studio/LNM/Networks/Scheme"
ROI_FRACTION_SETTING = "Studio/LNM/Networks/RoiMinPercent"
#: Atlases of each network scheme: cortex (Schaefer names), cerebellum (Buckner) and subcortex (Tian)
SCHEMES = {
    "yeo7": {"title": "Yeo-7", "cortex": "schaefer2018-400-7n-2mm", "cerebellum": "buckner2011-cerebellum-7n-1mm", "subcortex": "tian-s1-2mm"},
    "yeo17": {"title": "Yeo-17", "cortex": "schaefer2018-400-17n-2mm", "cerebellum": "buckner2011-cerebellum-17n-1mm", "subcortex": "tian-s1-2mm"},
}
SYSTEMS = ("cortex", "cerebellum", "subcortex")
SYSTEM_TEXT = {"cortex": _("Cortex"), "cerebellum": _("Cerebellum"), "subcortex": _("Subcortex"), "roi": _("ROI"), "other": _("Other")}
#: A network with fewer voxels than this in the null voxel sample is not tested
MIN_NULL_VOXELS = 10
#: Relocated null maps larger than this (bytes of float32) are not loaded
MAX_NULL_BYTES = 1.0e9
#: Relocations of "Test against relocated lesions"
NULL_RELOCATIONS = 50
DEFAULT_ROI_PERCENT = 50.0
NULL_FOLDER = "networks-null"
NOT_TESTED = _("not tested")
ROW_HEIGHT = 22
VISIBLE_ROWS = 22
#: Widest first column of the network table (longer ROI names are elided; the tooltip has the name)
NETWORK_COLUMN_MAX_PX = 150
CAPTION_NOTE = _(
    "Descriptive. A percentile among relocated lesions or null cohorts is shown only when those null maps exist; it has the resolution 1/(K + 1) and is not corrected for the number of networks. Without them every network is 'not tested'.",
)


#
# Networks of an atlas
#


def labelVoxels(atlas, grid, indices):
    """Label value of ``atlas`` at each in-mask voxel of ``grid`` (nearest voxel through the affines; 0 outside)."""
    import numpy as np

    ijk = np.stack(np.unravel_index(np.asarray(indices), grid.shape), axis=1)
    return atlas.values_at(grid.voxel_to_world(ijk))


def networkGroups(atlas):
    """[(network name, [label values])] of an atlas in the order of its label numbers (hemispheres of a network are one network)."""
    groups = {}
    for value in sorted(atlas.labels):
        name = atlas.network(value) if atlas.networks else None
        name = name or atlas.label_name(value)
        if name:
            groups.setdefault(name, []).append(int(value))
    return list(groups.items())


def buildUnits(schemeId, atlasList, grid, indices, allowCrossFamily=False):
    """The network units of a scheme for the voxels ``indices`` of ``grid``.

    :return: ``(units, notices)``: units are dicts ``{"system", "name", "atlas", "positions"}`` (``positions``: positions
      into the in-mask vector); notices say which system is missing and why.
    """
    import numpy as np

    from LesionNetworkMappingLib import atlases as atlasLib

    scheme = SCHEMES[schemeId]
    byId = {atlas.id: atlas for atlas in atlasList}
    family, _space = atlasLib.mask_family(grid.shape, grid.affine)
    units, notices = [], []
    for system in SYSTEMS:
        atlasId = scheme[system]
        atlas = byId.get(atlasId)
        if atlas is None:
            notices.append(_("{system}: {atlas} is not on this computer.").format(system=SYSTEM_TEXT[system], atlas=shortName(atlasId)))
            continue
        known = family not in UNLABELLED_FAMILIES and atlas.family not in UNLABELLED_FAMILIES
        if not (known and family == atlas.family) and not allowCrossFamily:
            reason = _("the map is in the {family} template family, the atlas in the {atlasFamily} family") if known else _("the template space of the map could not be told")
            notices.append(_("{system}: {atlas} is not used: {reason} (the labels would differ by millimetres).").format(system=SYSTEM_TEXT[system], atlas=shortName(atlasId), reason=reason.format(family=family, atlasFamily=atlas.family)))
            continue
        labels = labelVoxels(atlas, grid, indices)
        for name, values in networkGroups(atlas):
            positions = np.flatnonzero(np.isin(labels, values))
            if positions.size:
                units.append({"system": system, "name": name, "atlas": atlasId, "positions": positions})
    return units, notices


def _signs(sign):
    return sign in ("both", "pos"), sign in ("both", "neg")


def networkStatistics(values, units, threshold, sign="both"):
    """One row per unit, plus the voxels that are in no unit, with the statistics of the card (see the module docstring).

    :param values: in-mask values of the map (float, NaN ignored).
    :param threshold: absolute threshold: voxels ``>= t`` are positive, ``<= -t`` negative (as the sign allows).
    :return: list of dicts: system, name, voxels, pctPos, pctNeg (percentage of the suprathreshold voxels in the unit;
      None when there is none), pctNet (percentage of the unit that is suprathreshold, by the sign), pctNetPos, pctNetNeg,
      mean, median and r (None for "other"); ``supraPos`` and ``supraNeg`` totals are in ``rows[0]["totals"]``.
    """
    import numpy as np

    values = np.asarray(values, dtype=np.float64)
    finite = np.isfinite(values)
    wantPos, wantNeg = _signs(sign)
    pos = finite & (values >= threshold) if wantPos else np.zeros(values.shape, dtype=bool)
    neg = finite & (values <= -threshold) if wantNeg else np.zeros(values.shape, dtype=bool)
    supra = pos | neg
    totalPos, totalNeg = int(pos.sum()), int(neg.sum())
    count = int(finite.sum())
    v = np.where(finite, values, 0.0)
    meanAll = float(v.sum() / count) if count else math.nan
    sdAll = math.sqrt(max(float((v * v).sum() / count) - meanAll * meanAll, 0.0)) if count else math.nan
    totals = {"supraPos": totalPos, "supraNeg": totalNeg, "voxels": count}
    covered = np.zeros(values.shape, dtype=bool)

    def percent(part, whole):
        return 100.0 * part / whole if whole else None

    rows = []
    for unit in units:
        positions = unit["positions"]
        covered[positions] = True
        inside = finite[positions]
        k = int(inside.sum())
        mine = values[positions][inside]
        row = {"system": unit["system"], "name": unit["name"], "atlas": unit.get("atlas"), "voxels": k}
        row["pctPos"] = percent(int(pos[positions].sum()), totalPos) if wantPos else None
        row["pctNeg"] = percent(int(neg[positions].sum()), totalNeg) if wantNeg else None
        row["pctNet"] = percent(int(supra[positions].sum()), k)
        row["pctNetPos"] = percent(int(pos[positions].sum()), k) if wantPos else None
        row["pctNetNeg"] = percent(int(neg[positions].sum()), k) if wantNeg else None
        row["mean"] = float(mine.mean()) if k else None
        row["median"] = float(np.median(mine)) if k else None
        row["r"] = _binaryR(float(mine.sum()), k, count, meanAll, sdAll)
        rows.append(row)
    other = finite & ~covered
    k = int(other.sum())
    if units and k:
        mine = values[other]
        rows.append({"system": "other", "name": _("not in these networks"), "atlas": None, "voxels": k,
                     "pctPos": percent(int((pos & other).sum()), totalPos) if wantPos else None, "pctNeg": percent(int((neg & other).sum()), totalNeg) if wantNeg else None,
                     "pctNet": percent(int((supra & other).sum()), k), "pctNetPos": percent(int((pos & other).sum()), k) if wantPos else None,
                     "pctNetNeg": percent(int((neg & other).sum()), k) if wantNeg else None, "mean": float(mine.mean()), "median": float(np.median(mine)), "r": None})
    if rows:
        rows[0]["totals"] = totals
    return rows


def _binaryR(sumInside, k, n, meanAll, sdAll):
    """Pearson r between a map and the binary mask of k of its n voxels (``sumInside``: the map's sum over the k voxels)."""
    if not n or not 0 < k < n or not sdAll or not math.isfinite(sdAll) or sdAll <= 0:
        return None
    mu = k / n
    covariance = sumInside / n - meanAll * mu
    return covariance / (sdAll * math.sqrt(mu * (1.0 - mu)))


def percentileOf(observed, nullValues):
    """Percentile (0-100) of ``observed`` among ``nullValues`` (ties count half): 97.5 means 97.5 % of the nulls are lower."""
    import numpy as np

    nulls = np.asarray(nullValues, dtype=np.float64)
    nulls = nulls[np.isfinite(nulls)]
    if not nulls.size or not math.isfinite(observed):
        return None
    return 100.0 * (float((nulls < observed).sum()) + 0.5 * float((nulls == observed).sum())) / nulls.size


def nullPercentiles(values, units, nullMaps, nullVoxels=None):
    """Percentile of each unit's mean value, and of its spatial r, among the same values of the null maps.

    :param values: in-mask values of the observed map (V,).
    :param nullMaps: (K, V') null maps of the same statistic over the in-mask voxels ``nullVoxels`` (sorted positions into
      the V in-mask voxels; None: all of them). The observed values are taken at the same voxels.
    :return: list aligned with ``units``: ``{"k", "voxels", "pctMean", "pctR"}`` or ``{"reason": text}`` when the unit has
      fewer than ``MIN_NULL_VOXELS`` voxels in the sample.
    """
    import numpy as np

    values = np.asarray(values, dtype=np.float64)
    nullMaps = np.asarray(nullMaps, dtype=np.float32)
    k = nullMaps.shape[0]
    sample = np.arange(values.size) if nullVoxels is None else np.asarray(nullVoxels, dtype=np.int64)
    if nullMaps.shape[1] != sample.size:
        raise ValueError(f"The null maps have {nullMaps.shape[1]} voxels but the sample has {sample.size}")
    column = np.full(values.size, -1, dtype=np.int64)
    column[sample] = np.arange(sample.size)
    observed = values[sample]
    finiteObserved = np.isfinite(observed)
    observed = np.where(finiteObserved, observed, 0.0)
    size = sample.size
    meanO, sdO = float(observed.mean()), float(observed.std())
    # Per null map: mean, sd and the sums over each unit, taken a few maps at a time in float64 (no float64 copy of the whole array).
    unitColumns = []
    for unit in units:
        columns = column[unit["positions"]]
        columns = columns[columns >= 0]
        unitColumns.append(columns if MIN_NULL_VOXELS <= columns.size < size else None)
    meanN, sdN = np.empty(k), np.empty(k)
    sums = [None if columns is None else np.empty(k) for columns in unitColumns]
    chunk = max(1, int(2.0e7 // max(size, 1)))
    for start in range(0, k, chunk):
        block = np.nan_to_num(nullMaps[start : start + chunk].astype(np.float64))
        meanN[start : start + chunk] = block.mean(axis=1)
        sdN[start : start + chunk] = block.std(axis=1)
        for columns, total in zip(unitColumns, sums, strict=True):
            if columns is not None:
                total[start : start + chunk] = block[:, columns].sum(axis=1)
    out = []
    for columns, total in zip(unitColumns, sums, strict=True):
        if columns is None:
            out.append({"reason": _("fewer than {n} voxels in the null voxel sample").format(n=MIN_NULL_VOXELS)})
            continue
        observedMean = float(observed[columns].mean())
        nullMean = total / columns.size
        mu = columns.size / size
        scale = math.sqrt(mu * (1.0 - mu))
        observedR = (float(observed[columns].sum()) / size - meanO * mu) / (sdO * scale) if sdO > 0 else math.nan
        with np.errstate(divide="ignore", invalid="ignore"):
            nullR = (total / size - meanN * mu) / (sdN * scale)
        out.append({"k": k, "voxels": int(columns.size), "pctMean": percentileOf(observedMean, nullMean), "pctR": percentileOf(observedR, nullR)})
    return out


def ordinal(value):
    """1 -> "1st", 22 -> "22nd", 97.4 -> "97th" (the percentile of the cells)."""
    value = round(value)
    suffix = "th" if 10 <= value % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


#
# Null maps
#


class NullMaps:
    """Relocated-lesion or null-cohort maps for a source: ``maps`` (K, V'), ``voxels`` (positions or None), text for the card."""

    def __init__(self, kind, maps, voxels, statistic, description):
        self.kind = kind  # "relocated lesions" or "null cohorts"
        self.maps = maps
        self.voxels = voxels
        self.statistic = statistic
        self.description = description

    @property
    def k(self):
        return int(self.maps.shape[0])


def _sameGrid(grid, affine, shape):
    import numpy as np

    return tuple(shape) == tuple(grid.shape) and np.allclose(np.asarray(affine), grid.affine, atol=1e-4)


def findLesionNull(source):
    """The relocated-lesion null maps of a single-lesion source: ``(NullMaps, None)`` or ``(None, reason)``.

    The engine keeps them with ``--write-null``: a 4D ``<lesion>_conn-<label>_desc-RelocatedT_funcmap.nii.gz`` (or
    ``RelocatedFz``) next to the run's maps, or in the ``networks-null`` folder made by "Test against relocated lesions".
    """
    import glob

    import numpy as np

    from LesionNetworkMappingLib.nifti import read_nifti

    if source.key != "result" or not source.runDir:
        return None, _("A map without its run (a loaded file, a partial run or a group map) has no relocated null maps.")
    if source.statistic not in ("T", "Fz"):
        return None, _("The null maps are of T and Fz; this is the {statistic} map.").format(statistic=source.statistic)
    desc = "RelocatedT" if source.statistic == "T" else "RelocatedFz"
    found = []
    for folder in (source.runDir, os.path.join(source.runDir, NULL_FOLDER)):
        found += glob.glob(os.path.join(folder, f"*_desc-{desc}*_funcmap.nii.gz"))
    if not found:
        return None, _("This run kept no relocated null maps.")
    path = max(found, key=lambda item: os.stat(item).st_mtime_ns)
    try:
        data, affine, _header = read_nifti(path)
    except Exception as exc:
        return None, _("The null maps could not be read: {error}").format(error=exc)
    data = np.asarray(data)
    if data.ndim != 4 or not _sameGrid(source.grid, affine, data.shape[:3]):
        return None, _("The null maps are not on the grid of this map.")
    k = data.shape[3]
    if k * source.indices.size * 4 > MAX_NULL_BYTES:
        return None, _("The null maps are too large to load ({k} maps).").format(k=k)
    maps = np.ascontiguousarray(data.reshape(-1, k)[source.indices].T, dtype=np.float32)
    return NullMaps("relocated lesions", maps, None, source.statistic, _("{k} relocated lesions, on all {v:,} voxels").format(k=k, v=source.indices.size)), None


def findGroupNull(source):
    """The null-cohort mean maps of a group source: ``(NullMaps, None)`` or ``(None, reason)``.

    A Nulls run (``nulls.json``) keeps the mean map of the rule's statistic of each null cohort on a voxel sample. It applies to
    the mean map of that statistic (``desc-meanT`` / ``meanFz``) of the same cohort, found next to the map or in a ``nulls``
    folder beside it.
    """
    import glob

    import numpy as np

    if source.key == "result":
        return None, _("Null cohorts exist for group maps; this is a single-lesion map.")
    if source.desc not in ("meanT", "meanFz"):
        return None, _("The null cohorts are of the mean map of the statistic ({desc} is not).").format(desc=source.desc or source.name)
    if not source.path:
        return None, _("A group map is compared with null cohorts only when it is a file of the cohort's analysis folder.")
    folder = os.path.dirname(os.path.abspath(source.path))
    candidates = []
    for base in (folder, os.path.dirname(folder)):
        candidates += [os.path.join(base, "nulls.json")] + glob.glob(os.path.join(base, "nulls*", "nulls.json"))
    candidates = [path for path in dict.fromkeys(candidates) if os.path.isfile(path)]
    if not candidates:
        return None, _("No Nulls run was found next to this map.")
    from LesionNetworkMappingLib import nulls

    for path in sorted(candidates, key=lambda item: -os.stat(item).st_mtime_ns):
        try:
            maps, voxels, statistic = nulls.load_null_group_maps(os.path.dirname(path))
        except (OSError, ValueError, KeyError):
            continue
        if statistic and str(statistic).replace("approximate ", "").strip() != source.statistic:
            continue
        if maps.shape[1] != len(voxels) or (len(voxels) and int(np.max(voxels)) >= source.indices.size):
            continue
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        kind = (record.get("design") or {}).get("kind") or record.get("kind") or "rl_independent"
        return NullMaps("null cohorts", maps, np.asarray(voxels, dtype=np.int64), source.statistic, _("{k} null cohorts ({kind}), on a sample of {v:,} voxels").format(k=maps.shape[0], kind=kind, v=len(voxels))), None
    return None, _("No Nulls run of the mean {statistic} map was found next to this map.").format(statistic=source.statistic)


#
# Regions of interest
#


def roiMask(entry, grid):
    """Boolean mask [i, j, k] on ``grid`` of an ROI entry: ``{"kind": "file", "path"}`` or ``{"kind": "segment", "node", "segment"}``."""
    import numpy as np

    from LesionNetworkMappingLib.geometry import resample_to_grid
    from LesionNetworkMappingLib.nifti import read_nifti

    if entry["kind"] == "file":
        data, affine, _header = read_nifti(entry["path"])
        data = np.asarray(data)
        if data.ndim > 3:
            data = data.reshape(data.shape[:3])
        occupancy = (np.nan_to_num(data.astype(np.float64)) != 0).astype(np.float64)
        if _sameGrid(grid, affine, occupancy.shape):
            return occupancy > 0
        return resample_to_grid(occupancy, affine, grid, mode="fraction") >= 0.5
    import vtk

    node = slicer.mrmlScene.GetNodeByID(entry["node"])
    if node is None:
        raise ValueError(_("The segmentation of this ROI is not in the scene any more."))
    reference = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "LNM ROI reference")
    try:
        image = vtk.vtkImageData()
        image.SetDimensions(*grid.shape)
        image.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
        image.GetPointData().GetScalars().Fill(0)
        reference.SetAndObserveImageData(image)
        matrix = vtk.vtkMatrix4x4()
        for row in range(4):
            for column in range(4):
                matrix.SetElement(row, column, float(grid.affine[row, column]))
        reference.SetIJKToRASMatrix(matrix)
        array = slicer.util.arrayFromSegmentBinaryLabelmap(node, entry["segment"], reference)
        return np.transpose(np.asarray(array), (2, 1, 0)) > 0
    finally:
        slicer.mrmlScene.RemoveNode(reference)


def lesionConnectivity(paths, indices, shape, roiPositions, thresholdFor, sign="both", minPercent=DEFAULT_ROI_PERCENT, progress=None, cancelled=None):
    """Which lesions' maps are connected to each ROI.

    :param paths: NIfTI map of each lesion (on the grid ``shape``); ``indices`` the in-mask voxels (flat C order).
    :param roiPositions: list of arrays of positions (into ``indices``) of each ROI.
    :param thresholdFor: ``thresholdFor(values, row) -> t`` the absolute threshold of a lesion's map.
    :param minPercent: a lesion is connected to an ROI when at least this percentage of the ROI's voxels pass (and at least one).
    :return: ``{"connected": (L, R) bool, "percent": (L, R) float}``; stops early (fewer rows) when ``cancelled()`` is true.
    """
    import numpy as np

    from LesionNetworkMappingLib.nifti import read_nifti

    wantPos, wantNeg = _signs(sign)
    connected, percent = [], []
    for row, path in enumerate(paths):
        if cancelled is not None and cancelled():
            break
        data, _affine, _header = read_nifti(path)
        data = np.asarray(data)
        if tuple(data.shape[:3]) != tuple(shape):
            raise ValueError(f"The map {os.path.basename(path)} is not on the grid of the maps")
        values = data.reshape(-1)[indices].astype(np.float64)
        threshold = thresholdFor(values, row)
        finite = np.isfinite(values)
        supra = (finite & (values >= threshold) if wantPos else np.zeros(values.shape, dtype=bool)) | (finite & (values <= -threshold) if wantNeg else np.zeros(values.shape, dtype=bool))
        share = [100.0 * float(supra[positions].sum()) / positions.size if positions.size else 0.0 for positions in roiPositions]
        percent.append(share)
        connected.append([value > 0 and value >= minPercent for value in share])
        if progress is not None:
            progress(row + 1, len(paths))
    return {"connected": np.array(connected, dtype=bool).reshape(len(connected), len(roiPositions)), "percent": np.array(percent, dtype=np.float64).reshape(len(percent), len(roiPositions))}


#
# Figures (the Figure of LesionNetworkMappingLib.figures, drawn here)
#


def barFigure(items, title, caption, axisLabel, preset, params=None, dpi=300, widthMm=89.0, color=None):
    """A horizontal bar chart as a ``figures.Figure`` (so ``figures.write_figure`` writes PNG, TIFF, SVG, PDF, figure.json,
    caption.txt and the source data).

    :param items: dicts ``{"label", "value" (0-100), "text" (shown after the bar), ...extra columns for the source data}``.
    :param preset: name recorded in figure.json ("roi-connectivity", "network-profile").
    """
    from PIL import Image

    from LesionNetworkMappingLib import colormaps, figures

    color = color or colormaps.POSITIVE_COLORS[0]
    fonts = figures.Fonts(None)
    px = dpi / 72.0
    text, small, big = round(figures.TEXT_PT * px), round(figures.SMALL_PT * px), round(figures.LETTER_PT * px)
    margin = round(6 * px)
    width = round(widthMm / 25.4 * dpi)
    labelWidth = max([fonts.width(item["label"], text) for item in items] or [0])
    valueWidth = max([fonts.width(item["text"], small) for item in items] or [0])
    left = margin + labelWidth + margin
    right = max(width - margin - valueWidth - margin, left + 10)
    rowHeight = round(text * 1.9)
    top = margin + (round(big * 1.7) if title else 0)
    plotHeight = rowHeight * len(items)
    height = top + plotHeight + round(small * 3.4) + margin
    base = Image.new("RGB", (width, height), "white")
    annotations = []
    if title:
        annotations.append(figures.Annotation("text", margin, margin, text=title, size=big, color="#000000", anchor="lt", bold=True))
    for tick in (0, 25, 50, 75, 100):
        x = left + (right - left) * tick / 100.0
        annotations.append(figures.Annotation("line", x, top, w=x, h=top + plotHeight, size=max(1, round(px / 3)), color="#C8C8C8"))
        annotations.append(figures.Annotation("text", x, top + plotHeight + round(small * 0.4), text=str(tick), size=small, color="#000000", anchor="mt"))
    annotations.append(figures.Annotation("text", (left + right) / 2, top + plotHeight + round(small * 1.9), text=axisLabel, size=small, color="#000000", anchor="mt"))
    sourcedata = []
    for index, item in enumerate(items):
        y = top + index * rowHeight
        value = 0.0 if item["value"] is None or not math.isfinite(item["value"]) else max(0.0, min(100.0, float(item["value"])))
        barHeight = round(rowHeight * 0.62)
        barTop = y + (rowHeight - barHeight) / 2
        annotations.append(figures.Annotation("text", margin, y + rowHeight / 2, text=item["label"], size=text, color="#000000", anchor="lm"))
        length = (right - left) * value / 100.0
        if length >= 1:
            annotations.append(figures.Annotation("rect", left, barTop, w=length, h=barHeight, color=color))
        annotations.append(figures.Annotation("text", left + length + round(px * 2), y + rowHeight / 2, text=item["text"], size=small, color="#000000", anchor="lm"))
        row = {"label": item["label"], "percent": "" if item["value"] is None else f"{item['value']:.4g}"}
        row.update({key: value for key, value in item.items() if key not in ("label", "value", "text")})
        sourcedata.append(row)
    meta = {
        "format": figures.FORMAT,
        "version": figures.VERSION,
        "preset": preset,
        "title": title,
        "params": dict(params or {}),
        "inputs": [],
        "convention": None,
        "panels": [],
        "tiles": [],
        "colorbars": [],
        "dpi": int(dpi),
        "width_mm": float(widthMm),
        "size_px": [width, height],
        "font": "Inter" if not fonts.default else "Pillow default",
        "watermark": None,
        "caption": caption,
    }
    return figures.Figure(base, annotations, dpi, fonts, meta, caption + "\n", sourcedata)


#
# Volumetric export for the Network Correspondence Toolbox
#


def nctGrid():
    """The FSL MNI 2 mm grid (91 x 109 x 91; the grid of the GSP connectome) as a ``geometry.Grid``."""
    from LesionNetworkMappingLib import geometry

    spec = next(spec for spec in geometry.GRID_REGISTRY if spec.grid_id == "NLin6-2mm")
    return spec.grid


def nctVolumes(source):
    """``{"binary", "pos", "neg", "map"}`` arrays on the FSL MNI 2 mm grid for the Network Correspondence Toolbox.

    The binary masks are the suprathreshold voxels (by the sign), ``map`` the map with the sub-threshold voxels set to 0.
    A map on another grid of the FSL family is resampled (nearest voxel for the map); a map of another template family is refused.
    """
    import numpy as np

    from LesionNetworkMappingLib import atlases as atlasLib
    from LesionNetworkMappingLib.geometry import resample_to_grid

    target = nctGrid()
    family, _space = atlasLib.mask_family(source.grid.shape, source.grid.affine)
    if not target.matches(source.grid):
        if family != "fsl6":
            raise ValueError(_("The Network Correspondence Toolbox takes maps on the FSL MNI 2 mm grid; this map is on another grid ({family} family): warp it to MNI152NLin6Asym first.").format(family=family))
    volume = np.where(np.isfinite(source.volume), source.volume, 0.0)
    wantPos, wantNeg = _signs(source.sign)
    pos = volume >= source.value if wantPos else np.zeros(volume.shape, dtype=bool)
    neg = volume <= -source.value if wantNeg else np.zeros(volume.shape, dtype=bool)
    thresholded = np.where(pos | neg, volume, 0.0)
    if target.matches(source.grid):
        return {"binary": (pos | neg).astype(np.uint8), "pos": pos.astype(np.uint8), "neg": neg.astype(np.uint8), "map": thresholded.astype(np.float32)}
    out = {}
    for name, array in (("pos", pos), ("neg", neg), ("binary", pos | neg)):
        out[name] = (resample_to_grid(array.astype(np.float64), source.grid.affine, target, mode="fraction") >= 0.5).astype(np.uint8)
    out["map"] = resample_to_grid(thresholded, source.grid.affine, target, mode="nearest").astype(np.float32)
    return out


def writeNct(source, folder, prefix="networks_nct"):
    """Write the NCT-compatible volumes and ``<prefix>.json`` into ``folder``; return the written paths."""
    from LesionNetworkMappingLib import nifti

    volumes = nctVolumes(source)
    target = nctGrid()
    os.makedirs(folder, exist_ok=True)
    written = []
    wantPos, wantNeg = _signs(source.sign)
    for name, desc in (("binary", "supra"), ("pos", "suprapos"), ("neg", "supraneg"), ("map", "thresholded")):
        if (name == "pos" and not wantPos) or (name == "neg" and not wantNeg) or (name in ("pos", "neg") and source.sign != "both"):
            continue
        path = os.path.join(folder, f"{prefix}_desc-{desc}.nii.gz")
        nifti.write_nifti(path, volumes[name], target.affine, dtype="uint8" if name != "map" else "float32", descrip=f"LNM {source.statistic} {desc}")
        written.append(path)
    sidecar = os.path.join(folder, f"{prefix}.json")
    record = {
        "format": "slicer-lnm-nct",
        "version": 1,
        "grid": {"id": "NLin6-2mm", "shape": list(target.shape), "space": "MNI152NLin6Asym (FSL MNI152, 2 mm)"},
        "statistic": source.statistic,
        "threshold": source.value,
        "sign": source.sign,
        "files": [os.path.basename(path) for path in written],
        "note": "Volumetric maps for the Network Correspondence Toolbox (NCT): the binary masks hold the voxels with the statistic beyond the threshold; the thresholded map keeps their values. Descriptive; no cluster-extent inference.",
    }
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=1, ensure_ascii=False)
        f.write("\n")
    written.append(sidecar)
    return written


#
# Table text and files
#


def _pct(value):
    return "–" if value is None else f"{value:.1f}"  # noqa: RUF001


def _num(value, digits=2):
    return "–" if value is None or not math.isfinite(value) else f"{value:.{digits}f}"  # noqa: RUF001


def nullText(null):
    """The cell of the "vs null" column: a percentile or "not tested" with the reason."""
    if null is None:
        return NOT_TESTED
    if "reason" in null:
        return _("{status} ({reason})").format(status=NOT_TESTED, reason=null["reason"])
    return ordinal(null["pctMean"]) if null["pctMean"] is not None else "–"  # noqa: RUF001


TSV_COLUMNS = ("system", "network", "voxels", "pct_of_supra_pos", "pct_of_supra_neg", "pct_of_network_supra", "pct_of_network_pos", "pct_of_network_neg", "mean", "median", "r_binary_network",
               "null_status", "null_kind", "null_k", "null_percentile_mean", "null_percentile_r", "null_note")


def tsvRows(rows, nullInfo):
    """Rows of ``networks.tsv`` as lists of strings: the statistics and, always, the null status ("tested" or "not tested")."""

    def cell(value, digits=4):
        return "" if value is None or (isinstance(value, float) and not math.isfinite(value)) else f"{value:.{digits}g}" if isinstance(value, float) else str(value)

    out = []
    for row in rows:
        null = row.get("null")
        tested = null is not None and "reason" not in null
        note = "" if tested else (null["reason"] if null is not None and "reason" in null else (nullInfo.get("reason") or ""))
        out.append([
            row["system"], row["name"], cell(row["voxels"]), cell(row["pctPos"]), cell(row["pctNeg"]), cell(row["pctNet"]), cell(row["pctNetPos"]), cell(row["pctNetNeg"]),
            cell(row["mean"]), cell(row["median"]), cell(row["r"]), "tested" if tested else "not tested", nullInfo.get("kind") or "" if tested else "", cell(null["k"]) if tested else "",
            cell(null["pctMean"], 4) if tested else "", cell(null["pctR"], 4) if tested else "", note.replace("\t", " ").replace("\n", " ")])
    return out


def _writeText(path, text):
    folder = os.path.dirname(os.path.abspath(path))
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=".networks-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(temporary, 0o666 & ~umask)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


#
# The card
#


class NetworksCard:
    """Controller of the Networks card (``widget`` is a collapsible card)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.model = sharedModel(ctx)
        self.atlases = sharedAtlases(ctx)
        self.scheme = self._storedScheme()
        self.source = None
        self.rows = []
        self.notices = []
        self.null = None
        self.nullInfo = {"reason": _("Nothing is computed yet.")}
        self.rois = []  # {"kind", "name", "path" | "node" + "segment", "voxels"}
        self.roiResult = None  # {"names", "connected", "percent", "n", "minPercent", "threshold"}
        self.roiJob = None
        self.nullJob = None
        self.lastFolder = None
        self.figureFiles = []
        self.roiFigure = None
        self._unitsCache = None
        self._nullCache = None
        self._shown = None
        self._observed = {}
        self.widget = widgets.card(_("Networks"), objectName="LNMNetworksCard", collapsed=True)
        layout = qt.QVBoxLayout(self.widget)
        self.sourceBar = SourceBar(self.model, "LNMNetworks")
        layout.addWidget(self.sourceBar.widget)

        schemeRow = qt.QHBoxLayout()
        schemeRow.addWidget(qt.QLabel(_("Networks")))
        self.schemeFrame, self.schemeGroup = widgets.segmented([SCHEMES[key]["title"] for key in SCHEMES])
        self.schemeFrame.setObjectName("LNMNetworksSchemeSegmented")
        self.schemeGroup.button(list(SCHEMES).index(self.scheme)).setChecked(True)
        self.schemeGroup.connect(_idClickedSignal(self.schemeGroup), self._onSchemeClicked)
        schemeRow.addWidget(self.schemeFrame)
        schemeRow.addStretch(1)
        layout.addLayout(schemeRow)
        self.atlasBanner = widgets.banner("", [(_("Get atlases"), self.getAtlases)], level="neutral")
        self.atlasBanner.setObjectName("LNMNetworksAtlasBanner")
        self.atlasBanner.hide()
        layout.addWidget(self.atlasBanner)

        actionRow = qt.QHBoxLayout()
        self.updateButton = qt.QPushButton(_("Make table"))
        self.updateButton.setObjectName("LNMNetworksUpdateButton")
        self.updateButton.setProperty("slicerRole", "secondary")
        self.updateButton.connect("clicked()", self.update)
        actionRow.addWidget(self.updateButton)
        self.staleChip = widgets.chip(_("Out of date"), "warn")
        self.staleChip.setObjectName("LNMNetworksStaleChip")
        self.staleChip.hide()
        actionRow.addWidget(self.staleChip)
        actionRow.addStretch(1)
        layout.addLayout(actionRow)
        self.headerLabel = _label("LNMNetworksHeader")
        layout.addWidget(self.headerLabel)
        self.messageLabel = _label("LNMNetworksMessage", "secondaryText")
        layout.addWidget(self.messageLabel)
        self.columns = [
            (_("Network"), _("Cortex (Schaefer names), cerebellum (Buckner) or subcortex (Tian structure); the tooltip of a row gives its voxels")),
            (_("vs null"), _("Percentile of the network's mean among the same value in the null maps; 'not tested' without null maps")),
            (_("+ %"), _("Percentage of the positive suprathreshold voxels that lie in the network")),
            (_("− %"), _("Percentage of the negative suprathreshold voxels that lie in the network")),  # noqa: RUF001
            (_("% net"), _("Percentage of the network's voxels that are suprathreshold")),
            (_("Mean"), _("Mean of the map in the network")),
            (_("Median"), _("Median of the map in the network")),
            ("r", _("Spatial correlation of the map with the binary mask of the network, over every in-mask voxel")),
        ]
        self.table = qt.QTableWidget(0, len(self.columns))
        self.table.setObjectName("LNMNetworksTable")
        self.table.setHorizontalHeaderLabels([name for name, _tip in self.columns])
        for column, (_name, tip) in enumerate(self.columns):
            item = self.table.horizontalHeaderItem(column)
            if item is not None:
                item.setToolTip(tip)
        self.table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.setWordWrap(False)
        self.table.setHorizontalScrollMode(qt.QAbstractItemView.ScrollPerPixel)
        widgets._font(self.table, TABLE_FONT_PX)
        self.table.hide()
        layout.addWidget(self.table)
        self.nullChip = widgets.chip(NOT_TESTED, "neutral")
        self.nullChip.setObjectName("LNMNetworksNullChip")
        nullRow = qt.QHBoxLayout()
        nullRow.addWidget(self.nullChip, 0, qt.Qt.AlignTop)
        self.nullLabel = _label("LNMNetworksNullText")
        nullRow.addWidget(self.nullLabel, 1)
        layout.addLayout(nullRow)
        self.nullButton = qt.QPushButton(_("Test against relocated lesions"))
        self.nullButton.setObjectName("LNMNetworksNullTestButton")
        self.nullButton.setProperty("slicerRole", "secondary")
        self.nullButton.setToolTip(_("Map the lesion again with {k} relocated copies of it and keep their maps (one engine run, as long as the map)").format(k=NULL_RELOCATIONS))
        self.nullButton.connect("clicked()", self.testAgainstRelocated)
        self.nullButton.hide()
        layout.addWidget(self.nullButton)
        self.nullProgress = widgets.progressRow(objectNamePrefix="LNMNetworksNull")
        self.nullProgress.widget.hide()
        layout.addWidget(self.nullProgress.widget)
        self.noticeLabel = _label("LNMNetworksNotices", "secondaryText")
        self.noticeLabel.hide()
        layout.addWidget(self.noticeLabel)
        self.noteLabel = _label("LNMNetworksNote", "secondaryText")
        self.noteLabel.setText(CAPTION_NOTE)
        layout.addWidget(self.noteLabel)

        # Regions of interest
        self.roiTitle = qt.QLabel(_("Regions of interest"))
        self.roiTitle.setProperty("slicerRole", "secondaryText")
        layout.addWidget(self.roiTitle)
        roiRow = qt.QHBoxLayout()
        self.roiFileButton = qt.QPushButton(_("Add file…"))
        self.roiFileButton.setObjectName("LNMNetworksAddRoiFileButton")
        self.roiFileButton.setProperty("slicerRole", "secondary")
        self.roiFileButton.setToolTip(_("Add a NIfTI mask as a region of interest (it is not added to the scene)"))
        self.roiFileButton.connect("clicked()", self.addRoiFile)
        roiRow.addWidget(self.roiFileButton)
        self.roiSegmentButton = qt.QToolButton()
        self.roiSegmentButton.setObjectName("LNMNetworksAddRoiSegmentButton")
        self.roiSegmentButton.setText(_("Add segment…"))
        self.roiSegmentButton.setProperty("slicerRole", "secondary")
        self.roiSegmentButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.roiSegmentMenu = qt.QMenu(self.roiSegmentButton)
        self.roiSegmentMenu.connect("aboutToShow()", self._fillSegmentMenu)
        self.roiSegmentButton.setMenu(self.roiSegmentMenu)
        roiRow.addWidget(self.roiSegmentButton)
        self.roiRemoveButton = qt.QToolButton()
        self.roiRemoveButton.setObjectName("LNMNetworksRemoveRoiButton")
        self.roiRemoveButton.setText(_("Remove"))
        self.roiRemoveButton.setProperty("slicerRole", "ghost")
        self.roiRemoveButton.connect("clicked()", self.removeSelectedRoi)
        roiRow.addWidget(self.roiRemoveButton)
        roiRow.addStretch(1)
        layout.addLayout(roiRow)
        self.roiList = qt.QListWidget()
        self.roiList.setObjectName("LNMNetworksRoiList")
        self.roiList.setMaximumHeight(ROW_HEIGHT * 4)
        self.roiList.hide()
        layout.addWidget(self.roiList)
        percentRow = qt.QHBoxLayout()
        self.roiPercentLabel = qt.QLabel(_("Connected when at least"))
        percentRow.addWidget(self.roiPercentLabel)
        self.roiPercentSpin = qt.QDoubleSpinBox()
        self.roiPercentSpin.setObjectName("LNMNetworksRoiPercentSpinBox")
        self.roiPercentSpin.setRange(0, 100)
        self.roiPercentSpin.setDecimals(0)
        self.roiPercentSpin.setSuffix(_(" % of the ROI"))
        self.roiPercentSpin.setValue(self._storedRoiPercent())
        self.roiPercentSpin.setToolTip(_("A lesion's map is connected to an ROI when at least this share of the ROI's voxels pass the lesion's threshold (and at least one voxel)"))
        self.roiPercentSpin.connect("valueChanged(double)", self._onRoiPercent)
        percentRow.addWidget(self.roiPercentSpin)
        percentRow.addStretch(1)
        layout.addLayout(percentRow)
        self.roiRunButton = qt.QPushButton(_("Count connected lesions"))
        self.roiRunButton.setObjectName("LNMNetworksRoiRunButton")
        self.roiRunButton.setProperty("slicerRole", "secondary")
        self.roiRunButton.setToolTip(_("Read each lesion's map of the cohort and count the lesions connected to each ROI"))
        self.roiRunButton.connect("clicked()", self.runRoiChart)
        layout.addWidget(self.roiRunButton)
        self.roiProgress = widgets.progressRow(objectNamePrefix="LNMNetworksRoi")
        self.roiProgress.widget.hide()
        layout.addWidget(self.roiProgress.widget)
        self.roiLabel = _label("LNMNetworksRoiText", "secondaryText")
        layout.addWidget(self.roiLabel)
        self.roiImage = qt.QLabel("")
        self.roiImage.setObjectName("LNMNetworksRoiChart")
        self.roiImage.setAlignment(qt.Qt.AlignCenter)
        self.roiImage.hide()
        layout.addWidget(self.roiImage)

        # Export
        exportRow = qt.QHBoxLayout()
        self.exportButton = qt.QPushButton(_("Export…"))
        self.exportButton.setObjectName("LNMNetworksExportButton")
        self.exportButton.setProperty("slicerRole", "secondary")
        self.exportButton.setToolTip(_("Write networks.tsv, networks.json, the chart as PNG and SVG, and the volumes for the Network Correspondence Toolbox"))
        self.exportButton.connect("clicked()", self.export)
        self.exportButton.setEnabled(False)
        exportRow.addWidget(self.exportButton)
        self.showFolderButton = qt.QPushButton(_("Show folder"))
        self.showFolderButton.setObjectName("LNMNetworksShowFolderButton")
        self.showFolderButton.setProperty("slicerRole", "ghost")
        self.showFolderButton.connect("clicked()", self.showFolder)
        self.showFolderButton.hide()
        exportRow.addWidget(self.showFolderButton)
        exportRow.addStretch(1)
        layout.addLayout(exportRow)
        self.exportLabel = _label("LNMNetworksExportResult", "secondaryText")
        self.exportLabel.hide()
        layout.addWidget(self.exportLabel)

        self.timer = qt.QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.connect("timeout()", self._autoUpdate)
        try:
            self.widget.connect("contentsCollapsed(bool)", self._onCollapsed)
        except Exception:
            logging.debug("Lesion network mapping: the Networks card cannot follow its expansion", exc_info=True)
        for name in ("runChanged", "resultChanged", "ruleChanged", "bundleChanged", "seedChanged"):
            getattr(ctx, name).append(self.schedule)
        self.model.listeners.append(self.schedule)
        self.atlases.listeners.append(self._onAtlasesChanged)
        self._refreshAtlasBanner()
        self._refreshRoiControls()
        self._refreshStatus()

    #
    # Card protocol
    #

    def onRunEvent(self, event):
        if event.get("event") == "start":
            self._clearTable()
            self.schedule()

    def onRunDone(self, job=None):
        self.schedule()

    def onCohortMapsChanged(self, path=None):
        self.roiResult = None
        self._showRoiChart()
        self.schedule()

    def onStudyChanged(self):
        self.schedule()

    def onRunSettingsChanged(self):
        self.schedule()

    def cleanup(self):
        for name in ("runChanged", "resultChanged", "ruleChanged", "bundleChanged", "seedChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self.schedule in callbacks:
                callbacks.remove(self.schedule)
        if self.schedule in self.model.listeners:
            self.model.listeners.remove(self.schedule)
        if self._onAtlasesChanged in self.atlases.listeners:
            self.atlases.listeners.remove(self._onAtlasesChanged)
        self.sourceBar.cleanup()
        self.timer.stop()
        for job in (self.roiJob, self.nullJob):
            if job is not None and hasattr(job, "isFinished") and not job.isFinished():
                job.spec.onDone = None
                job.spec.onEvent = None
        self.roiJob = None

    #
    # Settings
    #

    @staticmethod
    def _storedScheme():
        value = str(_settings().value(SCHEME_SETTING) or "")
        return value if value in SCHEMES else "yeo7"

    @staticmethod
    def _storedRoiPercent():
        try:
            value = float(str(_settings().value(ROI_FRACTION_SETTING)).strip())
        except (TypeError, ValueError):
            return DEFAULT_ROI_PERCENT
        return value if 0 <= value <= 100 else DEFAULT_ROI_PERCENT

    def setScheme(self, scheme):
        """Choose "yeo7" or "yeo17"."""
        if scheme not in SCHEMES or scheme == self.scheme:
            return
        self.scheme = scheme
        _settings().setValue(SCHEME_SETTING, scheme)
        self.schemeGroup.button(list(SCHEMES).index(scheme)).setChecked(True)
        self._refreshAtlasBanner()
        self.schedule()

    def _onSchemeClicked(self, index):
        self.setScheme(list(SCHEMES)[index])

    def _onRoiPercent(self, value):
        _settings().setValue(ROI_FRACTION_SETTING, f"{value:g}")
        self.roiResult = None
        self._showRoiChart()

    #
    # Update scheduling
    #

    def expanded(self):
        try:
            return not bool(self.widget.collapsed)
        except (ValueError, RuntimeError):
            return False

    def atlasIds(self):
        scheme = SCHEMES[self.scheme]
        return [scheme[system] for system in SYSTEMS]

    def signature(self, source):
        return (source.stamp(), self.scheme, tuple(self.atlases.isCached(atlasId) for atlasId in self.atlasIds()), self.atlases.allowCrossFamily,
                tuple((roi["kind"], roi.get("path") or roi.get("node"), roi.get("segment")) for roi in self.rois))

    def _currentSignature(self):
        try:
            return self.signature(self.model.source())
        except SourceError:
            return None
        except Exception:
            logging.exception("Lesion network mapping: the map of the Networks card could not be read")
            return None

    def schedule(self, *args):
        self._refreshRoiVisibility()
        self._refreshStatus()
        if self.expanded() and self._isStale():
            self.timer.start()

    def _isStale(self):
        return self._shown is None or self._currentSignature() != self._shown

    def _autoUpdate(self):
        try:
            self.widget.objectName
        except (ValueError, RuntimeError):
            return
        if self._isStale():
            self.update()

    def _onCollapsed(self, collapsed):
        if not collapsed and self._isStale():
            self.timer.start()

    def _refreshStatus(self):
        self.staleChip.setVisible(bool(self.rows and self._isStale()))
        self.updateButton.setText(_("Update table") if self.rows else _("Make table"))

    def _onAtlasesChanged(self):
        self._refreshAtlasBanner()
        if not self.atlases.busy():
            self.schedule()

    #
    # Atlases
    #

    def getAtlases(self):
        """Download the atlases of the scheme after the user confirmed (an explicit action; never implicit)."""
        ids = self.atlases.missing(self.atlasIds())
        if ids and confirmFetch(self.atlases, ids):
            self.atlases.fetch(ids)

    def _refreshAtlasBanner(self):
        missing = self.atlases.missing(self.atlasIds())
        busy = self.atlases.busy()
        self.atlasBanner.setVisible(bool(missing) or busy)
        if busy:
            widgets.setBannerText(self.atlasBanner, self.atlases.message or _("Downloading…"))
        elif missing:
            names = ", ".join(shortName(atlasId) for atlasId in missing)
            widgets.setBannerText(self.atlasBanner, self.atlases.message or _("Not on this computer: {names} ({size}). Networks of the other atlases are listed.").format(names=names, size=self.atlases.sizeText(missing)))
        for button in self.atlasBanner.findChildren(qt.QToolButton):
            button.setEnabled(not busy)

    #
    # The table
    #

    def _clearTable(self):
        self.rows = []
        self._shown = None
        self.table.setRowCount(0)
        self.table.hide()
        self.headerLabel.setText("")
        self.exportButton.setEnabled(False)
        self.nullButton.hide()
        self._refreshStatus()

    def _showMessage(self, text):
        self._clearTable()
        self.messageLabel.setText(text)
        self.messageLabel.show()

    def _unitsFor(self, source, atlasList):
        key = (self.scheme, source.grid.shape, tuple(source.grid.affine.ravel().round(4)), hash(source.indices.tobytes()), tuple(atlas.id for atlas in atlasList), self.atlases.allowCrossFamily)
        if self._unitsCache is not None and self._unitsCache[0] == key:
            return self._unitsCache[1]
        result = buildUnits(self.scheme, atlasList, source.grid, source.indices, self.atlases.allowCrossFamily)
        self._unitsCache = (key, result)
        return result

    def _findNull(self, source):
        key = (source.key, source.token, source.statistic, source.runDir, source.path)
        if self._nullCache is not None and self._nullCache[0] == key:
            return self._nullCache[1]
        if source.key == "result":
            found = findLesionNull(source)
        else:
            found = findGroupNull(source)
        self._nullCache = (key, found)
        return found

    def roiUnits(self, source):
        """ROI entries as units on the map's grid (``{"system": "roi", "name", "positions"}``); an ROI that cannot be read is skipped with a notice."""
        import numpy as np

        units, notices = [], []
        position = np.full(source.grid.n_voxels, -1, dtype=np.int64)
        position[source.indices] = np.arange(source.indices.size)
        for roi in self.rois:
            try:
                mask = roiMask(roi, source.grid)
            except Exception as exc:
                notices.append(_("ROI {name}: {error}").format(name=roi["name"], error=exc))
                continue
            positions = position[np.flatnonzero(mask.reshape(-1))]
            positions = positions[positions >= 0]
            roi["voxels"] = int(positions.size)
            if positions.size:
                units.append({"system": "roi", "name": roi["name"], "atlas": None, "positions": positions})
            else:
                notices.append(_("ROI {name} has no voxel inside the map.").format(name=roi["name"]))
        return units, notices

    def update(self):
        """Make the network table of the current map and threshold; return the rows (None with a message when there are none)."""
        self.timer.stop()
        self.messageLabel.hide()
        try:
            source = self.model.source()
        except SourceError as exc:
            self._showMessage(str(exc))
            return None
        try:
            with slicer.util.WaitCursor():
                atlasList = self.atlases.load(self.atlasIds())
                units, notices = self._unitsFor(source, atlasList)
                roiUnits, roiNotices = self.roiUnits(source)
                notices = [*notices, *roiNotices, *source.notes]
                values = source.vector
                rows = networkStatistics(values, [*units, *roiUnits], source.value, source.sign)
                null, reason = self._findNull(source)
                nullInfo = {"reason": reason}
                if null is not None:
                    try:
                        results = nullPercentiles(values, [*units, *roiUnits], null.maps, null.voxels)
                    except ValueError as exc:
                        null, reason = None, _("The null maps do not fit this map: {error}").format(error=exc)
                        nullInfo = {"reason": reason}
                    else:
                        nullInfo = {"kind": null.kind, "k": null.k, "description": null.description, "reason": None}
                        for row, result in zip((row for row in rows if row["system"] != "other"), results, strict=True):
                            row["null"] = result
        except Exception as exc:
            logging.exception("Lesion network mapping: the network table could not be made")
            self._showMessage(_("The network table could not be made: {error}").format(error=exc))
            return None
        self.source, self.rows, self.notices, self.null, self.nullInfo = source, rows, notices, null, nullInfo
        self._shown = self.signature(source)
        self._fillTable()
        self._showHeader()
        self._showNull()
        self.exportButton.setEnabled(bool(rows))
        self._refreshStatus()
        return rows

    def _showHeader(self):
        source = self.source
        parts = [source.thresholdText(), SCHEMES[self.scheme]["title"]]
        totals = (self.rows[0].get("totals") if self.rows else None) or {}
        if source.sign in ("both", "pos"):
            parts.append(_("{n:,} voxels positive").format(n=totals.get("supraPos", 0)))
        if source.sign in ("both", "neg"):
            parts.append(_("{n:,} negative").format(n=totals.get("supraNeg", 0)))
        self.headerLabel.setText(" · ".join(parts))
        notices = list(self.notices)
        if not self.rows:
            notices.insert(0, _("No network could be listed."))
        elif not any(row["system"] in SYSTEMS for row in self.rows):
            notices.insert(0, _("No network atlas is available: get the atlases (Get atlases) to list the networks."))
        self.noticeLabel.setText(" ".join(notices))
        self.noticeLabel.setVisible(bool(notices))

    def _showNull(self):
        info = self.nullInfo
        if self.null is not None:
            widgets.setChipLevel(self.nullChip, "ok", _("Null: {kind}").format(kind=info["kind"]))
            self.nullLabel.setText(_("{description}. The percentile compares the network's mean {statistic} with the same value in each null map; it has the resolution 1/(K + 1) and is not corrected for the number of networks.").format(description=info["description"], statistic=self.source.statistic))
        else:
            widgets.setChipLevel(self.nullChip, "neutral", NOT_TESTED)
            self.nullLabel.setText(_("Not tested: {reason}").format(reason=info.get("reason") or ""))
        self.nullButton.setVisible(self.nullTestPossible())

    def nullTestPossible(self):
        """True when "Test against relocated lesions" can make the null of the shown single-lesion map."""
        source, run, bundle = self.source, self.ctx.currentRun, self.ctx.bundle
        if source is None or source.key != "result" or self.null is not None or run is None or bundle is None or not source.runDir:
            return False
        if getattr(run, "partial", False) or (run.runJson.get("desc_suffix") or "") or (run.n and run.n != bundle.n_subjects):
            return False
        return source.statistic in ("T", "Fz") and self._lesionPath() is not None

    def _lesionPath(self):
        run = self.ctx.currentRun
        if run is None or not run.runDir:
            return None
        import glob

        found = sorted(glob.glob(os.path.join(run.runDir, "lesion", "*.nii.gz")))
        return found[0] if found else None

    def _cell(self, row, column):
        """(text, alignment) of the table cell of a row (columns: network, vs null, +%, -%, % of network, mean, median, r)."""
        system = row["system"]
        if column == 0:
            return row["name"], "l"
        if column == 1:
            return ("–", "l") if system == "other" else (nullText(row.get("null")), "l")  # noqa: RUF001
        if column == 2:
            return _pct(row["pctPos"]), "r"
        if column == 3:
            return _pct(row["pctNeg"]), "r"
        if column == 4:
            return _pct(row["pctNet"]), "r"
        if column == 5:
            return _num(row["mean"]), "r"
        if column == 6:
            return _num(row["median"]), "r"
        return _num(row["r"]), "r"

    def _sections(self):
        """The table as ``[("section", system, text) | ("row", row)]``: a heading row for each system, then its networks."""
        entries, last = [], None
        for row in self.rows:
            if row["system"] != last:
                last = row["system"]
                atlasId = row.get("atlas")
                title = SYSTEM_TEXT[last] + (f" · {shortName(atlasId)}" if atlasId else "")
                if last == "other":
                    title = _("Voxels in none of these networks")
                entries.append(("section", last, title))
            entries.append(("row", row))
        return entries

    def _fillTable(self):
        table = self.table
        entries = self._sections()
        table.clearSpans()
        table.setRowCount(len(entries))
        columns = len(self.columns)
        for rowIndex, entry in enumerate(entries):
            if entry[0] == "section":
                item = qt.QTableWidgetItem(entry[2])
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                table.setItem(rowIndex, 0, item)
                for column in range(1, columns):
                    table.setItem(rowIndex, column, qt.QTableWidgetItem(""))
                table.setSpan(rowIndex, 0, 1, columns)
                continue
            row = entry[1]
            for column in range(columns):
                text, align = self._cell(row, column)
                item = qt.QTableWidgetItem(text)
                _setAlignment(item, {"l": qt.Qt.AlignLeft, "r": qt.Qt.AlignRight}[align] | qt.Qt.AlignVCenter)
                null = row.get("null")
                if column == 1 and null is not None and "reason" in null:
                    item.setToolTip(null["reason"])
                elif column == 1 and null is not None:
                    item.setToolTip(_("Mean: {mean} percentile; spatial r: {r} percentile; among {k} {kind}").format(
                        mean=_num(null["pctMean"], 1), r=_num(null["pctR"], 1), k=null["k"], kind=self.nullInfo.get("kind", "")))
                elif column == 0:
                    item.setToolTip(_("{voxels:,} voxels").format(voxels=row["voxels"]) + (f" · {self.atlases.title(row['atlas'])}" if row.get("atlas") else ""))
                table.setItem(rowIndex, column, item)
        header = table.horizontalHeader()
        for column in range(1, columns):
            header.setSectionResizeMode(column, qt.QHeaderView.ResizeToContents)
        # The first column is sized from the network rows only: a heading row spans every column and must not widen it
        metrics = qt.QFontMetrics(table.font)
        advance = metrics.horizontalAdvance if hasattr(metrics, "horizontalAdvance") else metrics.width  # Qt 5.11+, Qt 6
        widest = max([advance(self._cell(entry[1], 0)[0]) for entry in entries if entry[0] == "row"] + [advance(self.columns[0][0])])
        header.setSectionResizeMode(0, qt.QHeaderView.Interactive)
        table.setColumnWidth(0, min(widest + 16, NETWORK_COLUMN_MAX_PX))
        header.setStretchLastSection(True)
        table.setFixedHeight(ROW_HEIGHT * (min(len(entries), VISIBLE_ROWS) + 1) + 8)
        table.setVisible(bool(self.rows))

    #
    # Relocated-lesion test
    #

    def testAgainstRelocated(self, confirm=True):
        """Map the lesion again with relocated copies (``--write-null``), into ``networks-null`` of the run; return the job or None."""
        if not self.nullTestPossible():
            return None
        from . import jobrunner
        from .card_qa import nullCostText

        run, bundle = self.ctx.currentRun, self.ctx.bundle
        if confirm:
            text = _("Map this lesion again with {k} relocated copies of it, keeping their maps? The run takes about as long as the map ({cost} more).").format(k=NULL_RELOCATIONS, cost=nullCostText(bundle))
            if qt.QMessageBox.question(slicer.util.mainWindow(), _("Relocated-lesion test"), text, qt.QMessageBox.Ok | qt.QMessageBox.Cancel) != qt.QMessageBox.Ok:
                return None
        out = os.path.join(run.runDir, NULL_FOLDER)
        os.makedirs(out, exist_ok=True)
        args = ["--bundle", bundle.path, "--lesion", self._lesionPath(), "--out", out, "--null-relocations", str(NULL_RELOCATIONS), "--write-null"]
        spec = jobrunner.JobSpec(
            "LesionNetworkMapping", ["job", "LesionNetworkMappingLib.engine", *args], title=_("Relocated-lesion test"), heavy=True, kind="netnull", runRoot=os.path.dirname(run.runDir),
            onEvent=lambda event: self.nullProgress.update(), onDone=self._onNullDone, name="netnull",
        )
        self.nullJob = self.ctx.submit(spec)
        self.nullProgress.setJob(self.nullJob)
        self.nullProgress.widget.show()
        self.nullButton.setEnabled(False)
        return self.nullJob

    def _onNullDone(self, job):
        self.nullProgress.update()
        self.nullButton.setEnabled(True)
        self._nullCache = None
        if job.state == "failed":
            self.nullLabel.setText(_("The relocated-lesion test failed: {reason}").format(reason=job.failureReason))
        self._shown = None
        self.schedule()
        if self.expanded():
            self.update()

    #
    # ROIs
    #

    def _refreshRoiControls(self):
        self.roiList.clear()
        for roi in self.rois:
            self.roiList.addItem(f"{roi['name']} · {roi['voxels']:,} voxels" if roi.get("voxels") else roi["name"])
        self.roiList.setVisible(bool(self.rois))
        self.roiRemoveButton.setEnabled(bool(self.rois))
        self._refreshRoiVisibility()

    def _refreshRoiVisibility(self):
        """The count of connected lesions is for a cohort: its controls follow the seed mode."""
        cohort = self.model.seedMode() == "cohort"
        for widget in (self.roiPercentLabel, self.roiPercentSpin, self.roiRunButton):
            widget.setVisible(cohort and bool(self.rois))

    def addRoiFile(self, path=None):
        """Add a NIfTI mask as an ROI (a dialog asks for the file when ``path`` is None); return the entry."""
        if not path:
            start = str(_settings().value(LAST_FOLDER_SETTING) or "")
            path = qt.QFileDialog.getOpenFileName(slicer.util.mainWindow(), _("Add a region of interest"), start, "NIfTI (*.nii *.nii.gz);;All files (*)")
        if not path:
            return None
        name = os.path.basename(path)
        for suffix in (".nii.gz", ".nii"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        return self._addRoi({"kind": "file", "name": name, "path": os.path.abspath(path)})

    def addRoiSegment(self, node, segmentId):
        """Add a segment of a segmentation node as an ROI; return the entry."""
        segment = node.GetSegmentation().GetSegment(segmentId)
        return self._addRoi({"kind": "segment", "name": f"{node.GetName()} / {segment.GetName()}", "node": node.GetID(), "segment": segmentId})

    def _addRoi(self, entry):
        self.rois.append(entry)
        self.roiResult = None
        self._refreshRoiControls()
        self._showRoiChart()
        self.schedule()
        return entry

    def _fillSegmentMenu(self):
        menu = self.roiSegmentMenu
        menu.clear()
        found = False
        for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
            segmentation = node.GetSegmentation()
            for index in range(segmentation.GetNumberOfSegments()):
                segmentId = segmentation.GetNthSegmentID(index)
                action = menu.addAction(f"{node.GetName()} / {segmentation.GetSegment(segmentId).GetName()}")
                action.connect("triggered()", lambda n=node, s=segmentId: self.addRoiSegment(n, s))
                found = True
        if not found:
            menu.addAction(_("No segmentation in the scene")).setEnabled(False)

    def removeSelectedRoi(self):
        row = self.roiList.currentRow
        if 0 <= row < len(self.rois):
            del self.rois[row]
            self.roiResult = None
            self._refreshRoiControls()
            self._showRoiChart()
            self.schedule()

    def cohortMapPaths(self):
        """(rows, statistic column, maps.tsv path) of the cohort maps in use, or ``(None, None, None)``."""
        path = getattr(self.ctx, "cohortMaps", None)
        if not path or not os.path.isfile(path):
            return None, None, None
        from LesionNetworkMappingLib import cohort, cohortmaps

        rule = self._rule()
        column = cohort.MAP_COLUMNS[cohort.rule_statistic(rule)] if rule is not None else cohort.MAP_COLUMNS["T"]
        rows = [row for row in cohortmaps.read_maps_tsv(path) if row.get(column) and row.get(column) != "n/a" and os.path.isfile(row[column])]
        return rows, column, path

    def _rule(self):
        widget = mapWidget()
        try:
            return widget.currentRule() if widget is not None else None
        except Exception:
            return None

    def runRoiChart(self):
        """Count the cohort's lesions whose maps are connected to each ROI, step by step on the event loop; return the number of lesions or 0."""
        if not self.rois:
            return 0
        try:
            source = self.model.source()
        except SourceError as exc:
            self.roiLabel.setText(str(exc))
            return 0
        rows, column, path = self.cohortMapPaths()
        if not rows:
            self.roiLabel.setText(_("The chart needs the cohort's lesion maps: map the lesions in the Compute card first."))
            return 0
        import numpy as np

        units, notices = self.roiUnits(source)
        if not units:
            self.roiLabel.setText(" ".join(notices) or _("No ROI could be used."))
            return 0
        bundle = self.ctx.bundle
        if bundle is None or not np.array_equal(np.asarray(bundle.mask_indices), source.indices):
            self.roiLabel.setText(_("The lesion maps are read on the connectome grid: choose the Result or group map of the open connectome."))
            return 0
        from LesionNetworkMappingLib import cohort

        rule = self._rule()
        n = (cohortRecordN(path) or source.n or bundle.n_subjects)
        widget = mapWidget()
        try:
            gsr = widget.logic.gsr(bundle) if widget is not None else None
        except Exception:
            gsr = None
        fixed = None if source.fromRule or rule is None else source.value

        def thresholdFor(values, row):
            if fixed is not None:
                return fixed
            return cohort.resolve_rules(values[None, :], rule, n, bundle.n_voxels, gsr)[0].value

        state = {"paths": [row[column] for row in rows], "ids": [row.get("participant_id", str(index + 1)) for index, row in enumerate(rows)], "index": 0, "connected": [], "percent": [], "stop": False}
        self._roiState = state
        positions = [unit["positions"] for unit in units]
        names = [unit["name"] for unit in units]
        minPercent = float(self.roiPercentSpin.value)
        self.roiProgress.widget.show()
        self.roiProgress.stopButton.setEnabled(True)
        try:
            self.roiProgress.stopButton.disconnect("clicked()")
        except Exception:
            pass
        self.roiProgress.stopButton.connect("clicked()", lambda: state.update(stop=True))
        self.roiRunButton.setEnabled(False)
        total = len(state["paths"])

        def step():
            try:
                self.widget.objectName
            except (ValueError, RuntimeError):
                return
            if state["stop"] or state["index"] >= total:
                self._finishRoi(state, names, minPercent, source, notices)
                return
            try:
                result = lesionConnectivity([state["paths"][state["index"]]], source.indices, bundle.shape, positions, lambda values, _row: thresholdFor(values, state["index"]), source.sign, minPercent)
            except Exception as exc:
                logging.exception("Lesion network mapping: a lesion map could not be read for the ROI chart")
                state["error"] = str(exc)
                self._finishRoi(state, names, minPercent, source, notices)
                return
            state["connected"].append(result["connected"][0])
            state["percent"].append(result["percent"][0])
            state["index"] += 1
            self.roiProgress.setProgress(state["index"] / total, _("{done} / {total} lesions").format(done=state["index"], total=total))
            qt.QTimer.singleShot(0, step)

        qt.QTimer.singleShot(0, step)
        return total

    def _finishRoi(self, state, names, minPercent, source, notices):
        import numpy as np

        self.roiRunButton.setEnabled(True)
        self.roiProgress.widget.hide()
        done = len(state["connected"])
        if not done:
            self.roiLabel.setText(_("No lesion map was read.") if not state.get("error") else _("The ROI chart failed: {error}").format(error=state["error"]))
            return
        self.roiResult = {"names": names, "connected": np.array(state["connected"], dtype=bool), "percent": np.array(state["percent"], dtype=np.float64), "ids": state["ids"][:done],
                          "minPercent": minPercent, "threshold": source.thresholdText(), "partial": done < len(state["paths"]), "notices": notices}
        self._showRoiChart()

    def roiItems(self):
        """Chart items of the ROI result: label, percentage of lesions connected and its text."""
        result = self.roiResult
        if result is None:
            return []
        n = result["connected"].shape[0]
        items = []
        for index, name in enumerate(result["names"]):
            count = int(result["connected"][:, index].sum())
            percent = 100.0 * count / n if n else None
            items.append({"label": name, "value": percent, "text": f"{percent:.0f}% ({count}/{n})" if percent is not None else "–", "n_lesions": n, "n_connected": count})  # noqa: RUF001
        return items

    def roiCaption(self):
        result = self.roiResult
        if result is None:
            return ""
        n = result["connected"].shape[0]
        text = _("Percentage of the {n} lesions whose network map is connected to each region of interest: a lesion is connected when at least {percent:g} % of the region's voxels (and at least one) pass the lesion's threshold ({threshold}). Descriptive: no test.").format(
            n=n, percent=result["minPercent"], threshold=result["threshold"])
        if result.get("partial"):
            text += " " + _("Stopped early: only the first {n} lesions are counted.").format(n=n)
        return text

    def roiFigureObject(self):
        """The ROI bar chart as a ``figures.Figure`` (None without a result)."""
        items = self.roiItems()
        if not items:
            return None
        params = {"min_percent_of_roi": self.roiResult["minPercent"], "threshold": self.roiResult["threshold"], "lesions": self.roiResult["connected"].shape[0]}
        return barFigure(items, _("Lesions connected to each region"), self.roiCaption(), _("% of lesions connected"), "roi-connectivity", params)

    def _showRoiChart(self):
        figure = self.roiFigureObject()
        self.roiFigure = figure
        if figure is None:
            self.roiImage.hide()
            if self.rois and self.model.seedMode() == "cohort":
                self.roiLabel.setText(_("Count the lesions connected to each region with the button above."))
            else:
                self.roiLabel.setText(_("The regions are added to the network table; the chart of connected lesions needs a cohort."))
            return
        handle, path = tempfile.mkstemp(suffix=".png")
        os.close(handle)
        try:
            with open(path, "wb") as f:
                f.write(figure.png_bytes())
            pixmap = qt.QPixmap(path)
        finally:
            os.remove(path)
        self.roiImage.setPixmap(pixmap.scaledToWidth(min(pixmap.width(), 420), qt.Qt.SmoothTransformation))
        self.roiImage.setToolTip(self.roiCaption())
        self.roiImage.show()
        self.roiLabel.setText(self.roiCaption())

    #
    # Export
    #

    def caption(self):
        """Caption of the network table: map, threshold, scheme, atlases, the null status and the descriptive note."""
        if self.source is None:
            return ""
        source = self.source
        atlasNames = sorted({shortName(row["atlas"]) for row in self.rows if row.get("atlas")})
        text = _("Networks of the {statistic} map ({name}) at {threshold}: {scheme}; cortex from the Schaefer names, cerebellum from Buckner, subcortex from Tian structures; atlases: {atlases}. ").format(
            statistic=source.statistic, name=source.name, threshold=source.thresholdText(), scheme=SCHEMES[self.scheme]["title"], atlases=", ".join(atlasNames) or _("none"))
        if self.null is not None:
            text += _("Null: {description}. ").format(description=self.nullInfo["description"])
        else:
            text += _("Null: not tested ({reason}). ").format(reason=(self.nullInfo.get("reason") or "").rstrip("."))
        return text + CAPTION_NOTE

    def networkFigure(self):
        """The network profile as a ``figures.Figure``: the percentage of each network that is suprathreshold."""
        items = []
        for row in self.rows:
            if row["system"] == "other" or row.get("pctNet") is None:
                continue
            label = f"{SYSTEM_TEXT[row['system']]} · {row['name']}"
            null = row.get("null")
            suffix = f" ({ordinal(null['pctMean'])} pct)" if null is not None and "reason" not in null and null.get("pctMean") is not None else ""
            share = f"{row['pctNet']:.1f}" if row["pctNet"] < 10 else f"{row['pctNet']:.0f}"
            items.append({"label": label, "value": row["pctNet"], "text": f"{share}%{suffix}", "voxels": row["voxels"], "mean": row["mean"]})
        if not items:
            return None
        source = self.source
        params = {"scheme": self.scheme, "threshold": source.value, "sign": source.sign, "statistic": source.statistic, "null": self.nullInfo.get("kind") or "not tested"}
        return barFigure(items, _("Suprathreshold share of each network"), self.caption(), _("% of the network's voxels suprathreshold"), "network-profile", params)

    def export(self, folder=None):
        """Write the exports into ``folder`` (asked when None); return the written paths or None."""
        if not self.rows:
            return None
        if not folder:
            start = str(_settings().value(LAST_FOLDER_SETTING) or "")
            folder = qt.QFileDialog.getExistingDirectory(slicer.util.mainWindow(), _("Export the networks into…"), start)
            if not folder:
                return None
            _settings().setValue(LAST_FOLDER_SETTING, folder)
        try:
            return self.exportTo(folder)
        except Exception:  # exportTo has logged it and shown "Export failed" in the card; the button slot must not print a traceback
            return None

    def exportTo(self, folder):
        """Write networks.tsv, networks.json, the chart PNG/SVG (and the ROI chart) and the NCT volumes into ``folder``; return the paths."""
        from LesionNetworkMappingLib import figures

        written = []
        try:
            os.makedirs(folder, exist_ok=True)
            tsv = os.path.join(folder, "networks.tsv")
            lines = ["\t".join(TSV_COLUMNS), *["\t".join(cells) for cells in tsvRows(self.rows, self.nullInfo)]]
            _writeText(tsv, "\n".join(lines) + "\n")
            written.append(tsv)
            sidecar = os.path.join(folder, "networks.json")
            source = self.source
            record = {
                "format": "slicer-lnm-networks", "version": 1, "scheme": self.scheme, "statistic": source.statistic, "threshold": source.value, "sign": source.sign, "n": source.n,
                "map": source.name,
                "atlases": [{"id": atlasId, "title": self.atlases.title(atlasId)} for atlasId in self.atlasIds() if any(row.get("atlas") == atlasId for row in self.rows)],
                "null": {"status": "tested" if self.null is not None else "not tested", "kind": self.nullInfo.get("kind"), "k": self.nullInfo.get("k"), "description": self.nullInfo.get("description"),
                         "reason": self.nullInfo.get("reason")},
                "rois": [{"name": roi["name"], "kind": roi["kind"], "voxels": roi.get("voxels")} for roi in self.rois],
                "caption": self.caption(),
            }
            _writeText(sidecar, json.dumps(record, indent=1, ensure_ascii=False) + "\n")
            written.append(sidecar)
            self.figureFiles = []
            for name, figure in (("network-profile", self.networkFigure()), ("roi-connectivity", self.roiFigureObject())):
                if figure is not None:
                    result = figures.write_figure(figure, folder, name, formats=("png", "svg"))
                    self.figureFiles.extend(os.path.join(result["dir"], item) for item in result["files"])
            written.extend(self.figureFiles)
            try:
                written.extend(writeNct(source, folder))
            except ValueError as exc:
                self.exportLabel.setText(_("Exported the table and the charts. The volumes for the Network Correspondence Toolbox were not written: {error}").format(error=exc))
                self.exportLabel.show()
                self.lastFolder = folder
                self.showFolderButton.show()
                return written
        except Exception as exc:
            logging.exception("Lesion network mapping: the networks could not be exported")
            self.exportLabel.setText(_("Export failed: {error}").format(error=exc))
            self.exportLabel.show()
            raise
        self.lastFolder = folder
        self.exportLabel.setText(_("Exported networks.tsv, networks.json, the charts (PNG, SVG) and the Network Correspondence Toolbox volumes to {path}").format(path=folder))
        self.exportLabel.show()
        self.showFolderButton.show()
        return written

    def showFolder(self):
        if self.lastFolder and os.path.isdir(self.lastFolder):
            widgets.showInFileManager(self.lastFolder)


def cohortRecordN(mapsTsv):
    """The number of connectome subjects of the cohort maps (``cohortmaps.json`` next to maps.tsv), or None."""
    from LesionNetworkMappingLib import cohortmaps

    record = cohortmaps.read_record(os.path.join(os.path.dirname(os.path.abspath(mapsTsv)), "cohortmaps.json")) or {}
    n = record.get("n")
    return int(n) if isinstance(n, (int, float)) and n else None


CARD = {
    "id": "networks",
    "title": "Networks",
    "order": ORDER_NETWORKS,
    "modes": ("lesion", "cohort"),
    "factory": NetworksCard,
}

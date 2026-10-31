"""Headless publication figures of lesion network maps (numpy, scipy.ndimage and Pillow; no matplotlib).

The same code runs in the Slicer GUI, the command line, Docker and the browser build: no Slicer, VTK or
Qt imports and no screen grabs (addendum §2, outputs research §2.1-2.2, decision D-A25).

Display grid
------------

Every input goes onto one RAS display grid (:class:`DisplayGrid`: axes +R, +A, +S, isotropic spacing,
the template's field of view by default) with ``scipy.ndimage.map_coordinates`` through the voxel-to-world
affines, never by index (:func:`reslice`: nearest for labels and counts, linear for maps). Tiles are
then sampled at the figure's pixel size (linear for maps and the template, nearest for labels), so
thresholds and contours are decided at the final resolution.

Orientation
-----------

**Neurological** convention is the default: the left hemisphere is on the image left. The
**radiological** toggle mirrors every tile and swaps the stamps. Every axial and coronal tile carries
L and R stamps; sagittal tiles carry P and A (anterior on the image right in the neurological
convention, on the image left in the radiological one) and their slice label names the hemisphere.

Display modes (:data:`MODES`)
-----------------------------

- ``hard``: voxels below the threshold are hidden;
- ``transparent``: below the threshold, alpha = clip((|v| / thr)², 0, 1), and a 1 px contour outlines
  the suprathreshold voxels (Taylor et al. 2023, NeuroImage 274:120138);
- ``unthresholded``: every non-zero voxel is shown.

Thresholded panels get an **unthresholded companion** by default (``companion=True``). The colour
window is symmetric (±W, W = the 99.5th percentile of |value| over every map of the figure) and is
**shared by all panels**; the colour bar carries the units and a tick at ±threshold. Diverging maps use
:func:`colormaps.lnm_diverging`, counts the sequential stops of SPEC §4.12 (:data:`SEQUENTIAL_COLORS`)
with exact integer bins.

Presets (:data:`PRESETS`)
-------------------------

:func:`gallery` (one tile per lesion at its maximum-area axial slice, pages of N), :func:`overlap`
(integer bins, maximum stated), :func:`montage` (``"A -20 -10 0 10 20 30"`` or automatic slices from
peaks), :func:`pair` (thresholded and unthresholded), :func:`triptych` (sensitivity | specificity |
conjunction on one scale), :func:`replication` (two maps with their spatial r), :func:`glass` (3-view
maximum-|value| projection with the brain outline from the mask edge) and :func:`ortho` (three
planes through a point with a 1 px crosshair). The schematic preset is not implemented.

In-process API
--------------

Each preset returns a :class:`Figure` (:func:`gallery` a list of pages): ``image()`` gives a PIL RGB
image, ``rgba()`` a uint8 array, ``png_bytes()`` deterministic PNG bytes, ``svg()`` the SVG text, and
``meta``/``caption``/``sourcedata`` the records. Lower-level pieces for report builders:
:func:`load_volume`, :func:`display_grid`, :func:`reslice`, :func:`render_slice` (one tile as a PIL
image), :func:`diverging_rgba`, :func:`sequential_rgba`, :func:`symmetric_window` and
:func:`parse_slices`.

Outputs (:func:`write_figure`)
------------------------------

``<out>/figures/<name>/``: ``<name>.png`` and ``.tiff`` (with dpi), ``<name>.svg`` (the raster as an
embedded base64 PNG, vector text and colour bar), ``<name>.pdf`` (Pillow), ``figure.json`` (every
parameter, input sha256s, no local paths), ``caption.txt`` (orientation, statistic, rule ⇒ |T| and p,
N, df, connectome, colour range, slice coordinates) and ``<name>_sourcedata.csv``. Tutorial studies get
a ``TUTORIAL DATA`` watermark and synthetic bundles ``SYNTHETIC`` (:func:`watermark_for`).

Command line::

    python -m LesionNetworkMappingLib.figures --preset montage --map T.nii.gz --template tpl.nii.gz \
        --rule '{"kind": "fwe_bonferroni", "alpha": 0.05}' --n 1000 --out DIR [--slices "A -20 0 20"]

It writes ``DIR/figures/<name>/`` and registers the run (kind ``figure``) in the study's run registry.
"""

import base64
import copy
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field, fields
from xml.sax.saxutils import escape as _xml_escape

import numpy as np

from . import colormaps, jobs, nifti, thresholds

FORMAT = "slicer-lnm-figure"
VERSION = 1
JOB = "figures"
PRESETS = ("gallery", "overlap", "montage", "pair", "triptych", "replication", "glass", "ortho")
MODES = ("hard", "transparent", "unthresholded")
CONVENTIONS = ("neurological", "radiological")
DEFAULT_CONVENTION = "neurological"  # D-A25
PLANES = ("sagittal", "coronal", "axial")
PLANE_AXIS = {"sagittal": 0, "coronal": 1, "axial": 2}
PLANE_LETTERS = {"S": "sagittal", "C": "coronal", "A": "axial"}
AXIS_NAMES = "xyz"
#: Journal column widths (mm) and resolutions (dpi)
JOURNAL_WIDTHS_MM = {"single": 89.0, "double": 183.0}
DPIS = (300, 450, 600)
DEFAULT_WIDTH_MM = JOURNAL_WIDTHS_MM["double"]
DEFAULT_DPI = 300
FORMATS = ("png", "tiff", "svg", "pdf")
#: Sequential colour stops of count and percent maps (SPEC §4.12)
SEQUENTIAL_COLORS = ("#3B0F70", "#8C2981", "#DE4968", "#FE9F6D", "#FCFDBF")
LESION_COLOR = "#F2545B"  # SPEC §4.12
LUT_ENTRIES = colormaps.DEFAULT_ENTRIES
WINDOW_PERCENTILE = 99.5  # SPEC §4.12 (as display.WINDOW_PERCENTILE)
WIDEN_FACTOR = 1.1  # a threshold at or above W widens W to at least this multiple of the threshold
WATERMARKS = {"tutorial": "TUTORIAL DATA", "synthetic": "SYNTHETIC"}
#: Colour and opacity of the watermark (figures here and the report figures of :mod:`reportfigs`)
WATERMARK_COLOR = "#808080"
WATERMARK_OPACITY = 0.25
#: Font sizes in points (converted to pixels at the figure's dpi)
TEXT_PT = 7.0
LETTER_PT = 9.0
SMALL_PT = 6.0
FONT_FAMILY_SVG = "Inter, Helvetica, Arial, sans-serif"
TRANSPARENT_NOTE = "below the threshold, opacity = (|value| / threshold)²; suprathreshold voxels outlined (transparent thresholding, Taylor et al. 2023)"
SLAB_POINTS = 2_000_000  # display-grid points resliced per map_coordinates call (bounds memory)

_ASCII = {"≥": ">=", "≤": "<=", "\u2212": "-", "\u2013": "-", "—": "-", "±": "+/-", "·": "|", "\u00d7": "x", "µ": "u", "⇒": "=>", "\u03b1": "alpha", "…": "...", "²": "^2", "°": " deg", "\u2019": "'"}


# Inputs


@dataclass(frozen=True)
class Volume:
    """A 3D image: ``data`` [i, j, k], ``affine`` voxel-to-RAS (4x4), a display ``name`` and the sha256
    of the source file (or of the array bytes).
    """

    data: object
    affine: object
    name: str = "image"
    sha256: str | None = None
    source: str | None = None  # file name only (never a local path)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _array_sha256(data, affine):
    digest = hashlib.sha256()
    digest.update(np.ascontiguousarray(data).tobytes())
    digest.update(np.ascontiguousarray(affine, dtype=np.float64).tobytes())
    return digest.hexdigest()


def _strip_nii(name):
    return re.sub(r"\.nii(\.gz)?$", "", name)


def load_volume(source, name=None):
    """A :class:`Volume` from a NIfTI path, a ``(data, affine[, name])`` tuple or a Volume.

    A file with several volumes (4D, or a vector image) is refused: a figure shows one 3D volume, and drawing only the
    first one would silently drop the others. A 4D file with a single volume is read as 3D. Non-finite values are kept
    (they are never shown).
    """
    if source is None:
        return None
    if isinstance(source, Volume):
        return source if name is None else Volume(source.data, source.affine, name, source.sha256, source.source)
    if isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)
        data, affine, _ = nifti.read_nifti(path)
        base = os.path.basename(path)
        if data.ndim > 3:
            count = int(np.prod(data.shape[3:]))
            if count != 1:
                raise ValueError(f"{base} holds {count} volumes (shape {tuple(int(n) for n in data.shape)}); a figure shows one 3D volume. Save the volume to show as a 3D NIfTI file (for example with fslroi, or Slicer's Volumes module) and give that file.")
            data = data.reshape(data.shape[:3])
        return Volume(np.asarray(data), np.asarray(affine, dtype=np.float64), name or _strip_nii(base), sha256_file(path), base)
    if isinstance(source, (tuple, list)) and len(source) in (2, 3):
        data = np.asarray(source[0])
        affine = np.asarray(source[1], dtype=np.float64)
        if data.ndim != 3 or affine.shape != (4, 4):
            raise ValueError("A (data, affine) input needs a 3D array and a 4x4 affine")
        label = name or (source[2] if len(source) == 3 else "image")
        return Volume(data, affine, str(label), _array_sha256(data, affine), None)
    raise TypeError(f"Unsupported image input: {type(source).__name__}")


# Display grid


@dataclass(frozen=True)
class DisplayGrid:
    """An RAS-aligned isotropic grid: index (i, j, k) -> world origin + (i, j, k) x spacing (mm)."""

    origin: tuple
    spacing: float
    shape: tuple

    @property
    def affine(self):
        affine = np.diag([self.spacing, self.spacing, self.spacing, 1.0])
        affine[:3, 3] = self.origin
        return affine

    def coordinate(self, axis, index):
        """World coordinate (mm) of plane ``index`` along ``axis`` (0 = x, 1 = y, 2 = z)."""
        return float(self.origin[axis] + index * self.spacing)

    def index(self, axis, coordinate):
        """Nearest plane index of a world coordinate along ``axis`` (clipped to the grid)."""
        index = round((float(coordinate) - self.origin[axis]) / self.spacing)
        return min(max(index, 0), self.shape[axis] - 1)

    def contains(self, axis, coordinate):
        position = (float(coordinate) - self.origin[axis]) / self.spacing
        return -0.5 <= position <= self.shape[axis] - 0.5

    def to_dict(self):
        return {"origin": [round(float(value), 6) for value in self.origin], "spacing": float(self.spacing), "shape": [int(value) for value in self.shape]}


def _spacing(affine):
    return np.linalg.norm(np.asarray(affine, dtype=np.float64)[:3, :3], axis=0)


def display_grid(reference, spacing=None, crop=True, margin_mm=4.0):
    """The display grid covering ``reference`` (a :class:`Volume`, or a list of them: the grid then
    covers the union of their boxes).

    :param spacing: grid spacing in mm (default: the smallest voxel spacing of the references).
    :param crop: cover only the finite non-zero voxels of each reference (its whole field of view when
      it has none).
    :param margin_mm: margin added around the covered box.
    Plane coordinates are multiples of the spacing, so 0 mm is a plane.
    """
    references = [reference] if isinstance(reference, Volume) else [volume for volume in reference if volume is not None]
    if not references:
        raise ValueError("A display grid needs at least one image")
    if spacing is None:
        spacing = min(float(round(_spacing(np.asarray(volume.affine, dtype=np.float64)).min(), 4)) for volume in references)
    spacing = float(spacing)
    if not spacing > 0:
        raise ValueError(f"The display spacing must be positive, got {spacing}")
    boxes = np.array([_world_box(volume, crop) for volume in references])
    low = np.floor((boxes[:, 0].min(axis=0) - margin_mm) / spacing) * spacing
    high = np.ceil((boxes[:, 1].max(axis=0) + margin_mm) / spacing) * spacing
    shape = tuple(round((hi_ - lo_) / spacing) + 1 for lo_, hi_ in zip(low, high, strict=False))
    return DisplayGrid(tuple(float(value) for value in low), spacing, shape)


def _world_box(volume, crop):
    """(low, high) world corners of a volume's voxel box (of its finite non-zero voxels with ``crop``)."""
    data = np.asarray(volume.data)
    affine = np.asarray(volume.affine, dtype=np.float64)
    lo = np.zeros(3)
    hi = np.asarray(data.shape[:3], dtype=np.float64) - 1
    if crop:
        nonzero = np.isfinite(data) & (data != 0)
        if nonzero.any():
            hits = [np.flatnonzero(nonzero.any(axis=tuple(b for b in range(3) if b != a))) for a in range(3)]
            lo = np.array([h[0] for h in hits], dtype=np.float64)
            hi = np.array([h[-1] for h in hits], dtype=np.float64)
    corners = np.array([[x, y, z, 1.0] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
    world = (affine @ corners.T)[:3].T
    return world.min(axis=0), world.max(axis=0)


def inside_fraction(volume, grid):
    """Share (0..1) of the finite non-zero voxels of ``volume`` whose centres lie inside ``grid``'s box
    (None when the volume has no such voxel).
    """
    data = np.asarray(volume.data)
    if data.dtype.kind not in "fiub":
        return None
    nonzero = np.isfinite(data) & (data != 0)
    ijk = np.argwhere(nonzero)
    if not len(ijk):
        return None
    affine = np.asarray(volume.affine, dtype=np.float64)
    world = ijk.astype(np.float64) @ affine[:3, :3].T + affine[:3, 3]
    position = (world - np.asarray(grid.origin, dtype=np.float64)) / grid.spacing
    inside = np.all((position >= -0.5) & (position <= np.asarray(grid.shape, dtype=np.float64) - 0.5), axis=1)
    return float(inside.mean())


def _percent_text(share):
    """``share`` (0..1) as a percentage for a caption note, never rounded to 0% or 100% unless exact."""
    percent = 100.0 * share
    if 0 < percent < 0.1:
        return "less than 0.1%"
    if 99.9 < percent < 100:
        return "more than 99.9%"
    return f"{percent:.1f}%"


def reslice(volume, grid, order=1, cval=0.0, dtype=np.float32):
    """``volume`` resampled onto ``grid`` through the affines (``order`` 0 = nearest, 1 = linear).

    Non-finite source values become ``cval``; points outside the source field of view get ``cval``.
    """
    from scipy import ndimage

    data = np.asarray(volume.data)
    if data.dtype.kind not in "fiub":
        raise ValueError(f"Unsupported image data type {data.dtype}")
    data = data.astype(np.float64 if order == 0 else np.float32, copy=False)
    data = np.where(np.isfinite(data), data, cval).astype(data.dtype, copy=False)
    to_source = np.linalg.inv(np.asarray(volume.affine, dtype=np.float64)) @ grid.affine
    nx, ny, nz = grid.shape
    out = np.empty(grid.shape, dtype=dtype)
    ii, jj = np.meshgrid(np.arange(nx, dtype=np.float64), np.arange(ny, dtype=np.float64), indexing="ij")
    plane = np.stack([ii.ravel(), jj.ravel()])
    slab = max(1, SLAB_POINTS // max(nx * ny, 1))
    for k0 in range(0, nz, slab):
        ks = np.arange(k0, min(k0 + slab, nz), dtype=np.float64)
        points = np.concatenate([np.tile(plane, (1, len(ks))), np.repeat(ks, nx * ny)[None]], axis=0)
        source = to_source[:3, :3] @ points + to_source[:3, 3:4]
        if order == 0:
            source = np.floor(source + 0.5)  # round half up (map_coordinates' own rounding is not specified)
        values = ndimage.map_coordinates(data, source, order=order, mode="constant", cval=cval, prefilter=False)
        out[:, :, k0:k0 + len(ks)] = values.reshape(len(ks), nx, ny).transpose(1, 2, 0)
    return out


def template_gray(values, low_pct=1.0, high_pct=99.5):
    """Template intensities scaled to 0..1 between two percentiles of its non-zero voxels."""
    finite = values[np.isfinite(values) & (values != 0)]
    if finite.size == 0:
        return np.zeros(values.shape, dtype=np.float32)
    lo, hi = np.percentile(finite, [low_pct, high_pct])
    if hi <= lo:
        hi = lo + 1.0
    return np.clip((values - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)


def symmetric_window(arrays, percentile=WINDOW_PERCENTILE):
    """W of a symmetric window shared by ``arrays``: the ``percentile`` of |value| over all their finite
    non-zero values (1.0 when there are none).
    """
    pooled = [np.abs(np.asarray(a, dtype=np.float64).ravel()) for a in arrays if a is not None]
    values = np.concatenate(pooled) if pooled else np.zeros(0)
    values = values[np.isfinite(values) & (values > 0)]
    if values.size == 0:
        return 1.0
    window = float(np.percentile(values, percentile))
    return window if window > 0 and math.isfinite(window) else float(values.max())


# Slices


def parse_slices(text, default_plane="axial"):
    """Parse a slice specification: ``"A -20 -10 0 10 20 30"``, ``"C 0"``, ``"S -40 40"``, ``"z=-20,0,20"``,
    ``"auto"`` or ``"A auto 6"``.

    :return: (plane, coordinates or None for automatic, count for automatic).
    """
    if text is None:
        return default_plane, None, None
    if isinstance(text, (list, tuple)):
        return default_plane, [float(value) for value in text], None
    tokens = [token for token in re.split(r"[\s,;]+", str(text).strip()) if token]
    plane = default_plane
    if tokens:
        head = tokens[0]
        match = re.match(r"^([xyzXYZ])=(.*)$", head)
        if match:
            plane = {"x": "sagittal", "y": "coronal", "z": "axial"}[match.group(1).lower()]
            tokens = [match.group(2)] + tokens[1:] if match.group(2) else tokens[1:]
        elif head.upper() in PLANE_LETTERS:
            plane = PLANE_LETTERS[head.upper()]
            tokens = tokens[1:]
        elif head.lower() in PLANE_AXIS:
            plane = head.lower()
            tokens = tokens[1:]
    if not tokens or tokens[0].lower() == "auto":
        count = int(tokens[1]) if len(tokens) > 1 else None
        if count is not None and count < 1:
            raise ValueError(f"The number of automatic slices must be at least 1, got {count}")
        return plane, None, count
    try:
        return plane, [float(token) for token in tokens], None
    except ValueError:
        raise ValueError(f"Cannot read the slice specification {text!r} (expected e.g. 'A -20 -10 0 10 20 30')") from None


def auto_slices(values, grid, plane, count=6, threshold=None, sign="both", min_separation_mm=8.0):
    """Plane coordinates (mm, ascending) through the strongest peaks of ``values`` (on ``grid``).

    Planes are ranked by their maximum score (|value|, value or -value by ``sign``) and kept when they
    are at least ``min_separation_mm`` apart; when fewer qualify (above ``threshold`` when given) the rest
    are spread evenly between the planes whose non-zero area is at least 30% of the largest.
    """
    axis = PLANE_AXIS[plane]
    scores = _scores(values, sign)
    other = tuple(a for a in range(3) if a != axis)
    profile = scores.max(axis=other) if scores.size else np.zeros(grid.shape[axis])
    order = np.argsort(-profile, kind="stable")
    chosen = []
    floor = 0.0 if threshold is None else float(threshold)
    for index in order:
        if profile[index] <= 0 or profile[index] < floor or len(chosen) >= count:
            break
        if all(abs(int(index) - c) * grid.spacing >= min_separation_mm for c in chosen):
            chosen.append(int(index))
    if len(chosen) < count:
        areas = (np.abs(values) > 0).sum(axis=other)
        present = np.flatnonzero(areas >= 0.3 * areas.max()) if areas.max() > 0 else np.zeros(0, dtype=int)
        lo, hi = (present[0], present[-1]) if present.size else (0, grid.shape[axis] - 1)
        for index in np.linspace(lo, hi, count + 2)[1:-1]:
            index = round(index)
            if len(chosen) >= count:
                break
            if all(abs(index - c) * grid.spacing >= min_separation_mm / 2 for c in chosen):
                chosen.append(index)
    return sorted(grid.coordinate(axis, index) for index in chosen)


def _scores(values, sign):
    values = np.asarray(values, dtype=np.float32)
    if sign == "pos":
        return np.where(values > 0, values, 0)
    if sign == "neg":
        return np.where(values < 0, -values, 0)
    return np.abs(values)


def orient(slice2d, convention=DEFAULT_CONVENTION):
    """Image array (rows top to bottom, columns left to right) of a 2D section ``slice2d[a, b]`` whose
    first axis is the horizontal world axis (x for axial and coronal, y for sagittal) and second the
    vertical one (y for axial, z otherwise), both ascending. Neurological: +x (right) on the image right
    and anterior on the right of sagittal tiles; radiological mirrors the columns.
    """
    image = np.asarray(slice2d)[:, ::-1].swapaxes(0, 1)
    if convention == "radiological":
        image = image[:, ::-1]
    return image


def side_labels(plane, convention=DEFAULT_CONVENTION):
    """(left stamp, right stamp) of a tile: L/R for axial and coronal, P/A for sagittal."""
    if plane == "sagittal":
        pair = ("P", "A")
    else:
        pair = ("L", "R")
    return pair if convention == "neurological" else pair[::-1]


def section(volume3d, plane, index, sign="both"):
    """The 2D section of a display-grid volume at plane ``index`` (``index`` None: a projection; for maps
    the signed value of maximum |value|, or with ``sign`` "pos"/"neg" the maximum/minimum value, so a
    voxel of the excluded sign never hides one of the shown sign).
    """
    axis = PLANE_AXIS[plane]
    if index is None:
        values = np.asarray(volume3d)
        if values.dtype == bool:
            return values.any(axis=axis)
        if sign == "pos":
            return values.max(axis=axis)
        if sign == "neg":
            return values.min(axis=axis)
        where = np.argmax(np.abs(values), axis=axis)
        return np.take_along_axis(values, np.expand_dims(where, axis), axis=axis).squeeze(axis)
    return np.take(volume3d, index, axis=axis)


def _tile_mm(grid, plane):
    nx, ny, nz = grid.shape
    width, height = {"axial": (nx, ny), "coronal": (nx, nz), "sagittal": (ny, nz)}[plane]
    return width * grid.spacing, height * grid.spacing


# Colours


def _hex(color):
    return np.asarray(colormaps.hex_to_rgb(color), dtype=np.float64)


_DIVERGING = {}


def diverging_lut(entries=LUT_ENTRIES):
    if entries not in _DIVERGING:
        _DIVERGING[entries] = np.asarray(colormaps.lnm_diverging(entries), dtype=np.float64)
    return _DIVERGING[entries]


def sequential_colors(t):
    """RGB (0..1) of the sequential map at ``t`` in 0..1 (piecewise linear through :data:`SEQUENTIAL_COLORS`)."""
    stops = np.stack([_hex(color) for color in SEQUENTIAL_COLORS])
    t = np.clip(np.asarray(t, dtype=np.float64), 0.0, 1.0) * (len(stops) - 1)
    index = np.minimum(t.astype(int), len(stops) - 2)
    weight = (t - index)[..., None]
    return stops[index] * (1 - weight) + stops[index + 1] * weight


def count_color(count, max_count):
    """RGB (0..1) of the integer bin ``count`` (1..max) of an overlap map: t = count / max."""
    return sequential_colors(np.asarray(count, dtype=np.float64) / max(int(max_count), 1))


def diverging_rgba(values, window, threshold=None, mode="hard", sign="both"):
    """(rgb float [..., 3] in 0..1, alpha [...], suprathreshold mask) of map ``values``.

    The colour of a value is the LNM diverging entry whose bin contains it in [-window, +window]
    (values beyond the window take the end colours). Alpha follows ``mode`` (see the module docstring);
    zero and non-finite values are transparent.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
    values = np.asarray(values, dtype=np.float64)
    finite = np.isfinite(values) & (values != 0)
    values = np.where(finite, values, 0.0)
    lut = diverging_lut()
    n = len(lut)
    window = float(window) if window and window > 0 else 1.0
    index = np.clip(np.floor((values + window) / (2.0 * window) * n), 0, n - 1).astype(np.int64)
    rgb = lut[index]
    scores = _scores(values, sign).astype(np.float64)
    if threshold is None or not math.isfinite(threshold) or mode == "unthresholded":
        supra = finite if threshold is None or not math.isfinite(threshold) else finite & (scores >= threshold)
        alpha = finite.astype(np.float64)
        return rgb, alpha, supra
    supra = finite & (scores >= threshold) & (scores > 0)
    if mode == "hard":
        alpha = supra.astype(np.float64)
    else:
        alpha = np.where(finite, np.clip((scores / threshold) ** 2 if threshold > 0 else 1.0, 0.0, 1.0), 0.0)
        alpha[supra] = 1.0
    return rgb, alpha, supra


def sequential_rgba(values, vmin=0.0, vmax=1.0, discrete=False):
    """(rgb, alpha) of a count or percent map: alpha 1 above ``vmin``. With ``discrete`` the values are
    integer bins 1..vmax coloured by :func:`count_color` (exact; no interpolation between bins).
    """
    values = np.asarray(values, dtype=np.float64)
    values = np.where(np.isfinite(values), values, 0.0)
    if discrete:
        bins = np.rint(values)
        alpha = (bins >= 1).astype(np.float64)
        return count_color(np.clip(bins, 1, max(vmax, 1)), vmax), alpha
    span = max(float(vmax) - float(vmin), 1e-12)
    alpha = (values > vmin).astype(np.float64)
    return sequential_colors((values - vmin) / span), alpha


# Layers and tiles


@dataclass
class Layer:
    """One overlay on the display grid.

    ``kind``: "diverging" (maps), "sequential" (counts/percent; ``discrete`` for integer bins),
    "binary" (filled where value >= 0.5, in ``color``) or "outline" (the 1 px edge of value >= 0.5).
    """

    values: object
    kind: str = "diverging"
    window: float = 1.0
    threshold: float | None = None
    mode: str = "hard"
    sign: str = "both"
    vmin: float = 0.0
    vmax: float = 1.0
    discrete: bool = False
    color: str = LESION_COLOR
    opacity: float = 1.0
    contour_color: str = "#000000"


@dataclass
class Scene:
    """What a tile shows: the grid, a template (0..1 gray, or None), layers, and for glass views the
    brain mask whose projected edge is drawn.
    """

    grid: DisplayGrid
    background: object = None
    layers: list = field(default_factory=list)
    outline_mask: object = None
    background_color: str = "#000000"
    outline_color: str = "#8C8C8C"


def _resample2d(array, shape, order):
    from scipy import ndimage

    array = np.asarray(array, dtype=np.float64 if order else array.dtype)
    rows = (np.arange(shape[0]) + 0.5) * array.shape[0] / shape[0] - 0.5
    cols = (np.arange(shape[1]) + 0.5) * array.shape[1] / shape[1] - 0.5
    if order == 0:
        rows, cols = np.floor(rows + 0.5), np.floor(cols + 0.5)
        rows = np.clip(rows, 0, array.shape[0] - 1).astype(np.intp)
        cols = np.clip(cols, 0, array.shape[1] - 1).astype(np.intp)
        return array[rows[:, None], cols[None, :]]
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    return ndimage.map_coordinates(array.astype(np.float64), [rr, cc], order=1, mode="nearest", prefilter=False)


def _edge(mask):
    from scipy import ndimage

    mask = np.asarray(mask, dtype=bool)
    return mask & ~ndimage.binary_erosion(mask, border_value=0)


def render_slice(scene, plane, index, size, convention=DEFAULT_CONVENTION, crosshair=None):
    """One tile as a PIL RGB image of ``size`` (width, height) pixels.

    :param index: plane index on the scene's grid, or None for a maximum-|value| projection.
    :param crosshair: optional world point (x, y, z) marked with 1 px lines.
    """
    from PIL import Image

    width, height = int(size[0]), int(size[1])
    shape = (height, width)
    rgb = np.empty(shape + (3,), dtype=np.float64)
    rgb[:] = _hex(scene.background_color)
    if scene.background is not None and index is not None:
        gray = _resample2d(orient(section(scene.background, plane, index), convention), shape, 1)
        rgb = np.repeat(np.clip(gray, 0, 1)[..., None], 3, axis=2)
    if scene.outline_mask is not None:
        projected = orient(section(np.asarray(scene.outline_mask, dtype=bool), plane, index), convention)
        projected = _resample2d(projected, shape, 0).astype(bool)
        rgb[_edge(projected)] = _hex(scene.outline_color)
    for layer in scene.layers:
        label = layer.kind in ("binary", "outline") or (layer.kind == "sequential" and layer.discrete)
        values = section(layer.values, plane, index, layer.sign if layer.kind == "diverging" else "both")
        pixels = _resample2d(orient(values, convention), shape, 0 if label else 1)
        contour = None
        if layer.kind == "diverging":
            colors, alpha, supra = diverging_rgba(pixels, layer.window, layer.threshold, layer.mode, layer.sign)
            if layer.mode == "transparent" and layer.threshold is not None:
                contour = _edge(supra)
        elif layer.kind == "sequential":
            colors, alpha = sequential_rgba(pixels, layer.vmin, layer.vmax, layer.discrete)
        elif layer.kind == "binary":
            alpha = (pixels >= 0.5).astype(np.float64)
            colors = np.broadcast_to(_hex(layer.color), shape + (3,))
        elif layer.kind == "outline":
            alpha = _edge(pixels >= 0.5).astype(np.float64)
            colors = np.broadcast_to(_hex(layer.color), shape + (3,))
        else:
            raise ValueError(f"Unknown layer kind {layer.kind!r}")
        alpha = (alpha * layer.opacity)[..., None]
        rgb = rgb * (1.0 - alpha) + colors * alpha
        if contour is not None:
            rgb[contour] = _hex(layer.contour_color)
    if crosshair is not None:
        _draw_crosshair(rgb, scene.grid, plane, crosshair, convention)
    return Image.fromarray(np.rint(np.clip(rgb, 0, 1) * 255).astype(np.uint8), "RGB")


def _draw_crosshair(rgb, grid, plane, point, convention, color="#33FF66"):
    height, width = rgb.shape[:2]
    horizontal_axis, vertical_axis = {"axial": (0, 1), "coronal": (0, 2), "sagittal": (1, 2)}[plane]
    n_h, n_v = grid.shape[horizontal_axis], grid.shape[vertical_axis]
    u = (point[horizontal_axis] - grid.origin[horizontal_axis]) / grid.spacing  # grid index along the image columns
    v = (point[vertical_axis] - grid.origin[vertical_axis]) / grid.spacing
    column = (u + 0.5) * width / n_h - 0.5
    if convention == "radiological":
        column = width - 1 - column
    row = (n_v - 1 - v + 0.5) * height / n_v - 0.5
    column, row = round(column), round(row)
    if 0 <= column < width:
        rgb[:, column] = _hex(color)
    if 0 <= row < height:
        rgb[row, :] = _hex(color)


# Fonts and annotations


class Fonts:
    """Pillow fonts at pixel sizes: an Inter TTF (``path``; a SemiBold file next to it is used for bold)
    or Pillow's default font, whose text is made ASCII (it lacks ≥ - ± µ ·).
    """

    def __init__(self, path=None, bold_path=None):
        self.path = os.fspath(path) if path and os.path.isfile(os.fspath(path)) else None
        bold = bold_path
        if bold is None and self.path:
            for candidate in ("SemiBold", "Bold", "Medium"):
                guess = re.sub(r"(Regular)?(\.[ot]tf)$", f"{candidate}\\2", self.path, flags=re.IGNORECASE)
                if guess != self.path and os.path.isfile(guess):
                    bold = guess
                    break
        self.bold_path = os.fspath(bold) if bold and os.path.isfile(os.fspath(bold)) else self.path
        self.default = self.path is None
        self._cache = {}

    def get(self, size, bold=False):
        from PIL import ImageFont

        size = max(round(size), 6)
        key = (size, bool(bold))
        if key not in self._cache:
            path = self.bold_path if bold else self.path
            if path:
                self._cache[key] = ImageFont.truetype(path, size)
            else:
                try:
                    self._cache[key] = ImageFont.load_default(size=size)
                except TypeError:  # Pillow < 10.1 or no FreeType
                    self._cache[key] = ImageFont.load_default()
        return self._cache[key]

    def text(self, text):
        """``text`` as drawn into the raster (ASCII with the default font)."""
        if not self.default:
            return text
        for symbol, replacement in _ASCII.items():
            text = text.replace(symbol, replacement)
        return text.encode("ascii", "replace").decode("ascii")

    def width(self, text, size, bold=False):
        return self.get(size, bold).getlength(self.text(text))


@dataclass
class Annotation:
    """A vector element drawn over the raster (and written as SVG): "text", "rect", "line" or "bar"."""

    kind: str
    x: float
    y: float
    w: float = 0.0
    h: float = 0.0
    text: str = ""
    size: float = 10.0
    color: str = "#000000"
    opacity: float = 1.0
    anchor: str = "ls"
    bold: bool = False
    colors: tuple = ()
    role: str = ""


def _pt(points, dpi):
    return points / 72.0 * dpi


# Figure


class Figure:
    """A composed figure: the raster ``base`` (tiles only), vector ``annotations``, ``meta`` (every
    parameter, inputs, panels, tiles), ``caption`` and ``sourcedata`` rows.
    """

    def __init__(self, base, annotations, dpi, fonts, meta, caption, sourcedata):
        self.base = base
        self.annotations = annotations
        self.dpi = int(dpi)
        self.fonts = fonts
        self.meta = meta
        self.caption = caption
        self.sourcedata = sourcedata
        self._image = None

    @property
    def size(self):
        return self.base.size

    def image(self):
        """The full figure (raster plus annotations) as a PIL RGB image."""
        if self._image is None:
            from PIL import Image, ImageDraw

            canvas = self.base.convert("RGBA")
            overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            for item in self.annotations:
                fill = _rgba(item.color, item.opacity)
                if item.kind == "rect":
                    draw.rectangle([item.x, item.y, item.x + item.w - 1, item.y + item.h - 1], fill=fill)
                elif item.kind == "line":
                    draw.line([(item.x, item.y), (item.w, item.h)], fill=fill, width=max(1, round(item.size)))
                elif item.kind == "bar":
                    for x0, x1, color in _bar_segments(item):
                        draw.rectangle([x0, item.y, max(x0, x1 - 1), item.y + item.h - 1], fill=_rgba(color, 1.0))
                elif item.kind == "text":
                    draw.text((item.x, item.y), self.fonts.text(item.text), font=self.fonts.get(item.size, item.bold), fill=fill, anchor=item.anchor)
            self._image = Image.alpha_composite(canvas, overlay).convert("RGB")
        return self._image

    def rgba(self):
        """The full figure as a uint8 array [rows, columns, 4]."""
        return np.asarray(self.image().convert("RGBA"))

    def png_bytes(self):
        """Deterministic PNG bytes of the figure (with its dpi)."""
        buffer = io.BytesIO()
        self.image().save(buffer, "PNG", dpi=(self.dpi, self.dpi))
        return buffer.getvalue()

    def svg(self):
        """SVG text: the raster (tiles only) embedded as a base64 PNG, with vector text and colour bar."""
        width, height = self.base.size
        buffer = io.BytesIO()
        self.base.save(buffer, "PNG")
        data = base64.b64encode(buffer.getvalue()).decode("ascii")
        mm = 25.4 / self.dpi
        parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{width * mm:.2f}mm" height="{height * mm:.2f}mm" viewBox="0 0 {width} {height}" role="img" aria-label="{_attr(self.meta.get("title") or self.meta.get("preset", "figure"))}">',
            f'<image x="0" y="0" width="{width}" height="{height}" preserveAspectRatio="none" style="image-rendering:pixelated" href="data:image/png;base64,{data}" xlink:href="data:image/png;base64,{data}"/>',
        ]
        for item in self.annotations:
            opacity = "" if item.opacity >= 1 else f' fill-opacity="{item.opacity:.3g}" stroke-opacity="{item.opacity:.3g}"'
            if item.kind == "rect":
                parts.append(f'<rect x="{item.x:g}" y="{item.y:g}" width="{item.w:g}" height="{item.h:g}" fill="{item.color}"{opacity}/>')
            elif item.kind == "line":
                parts.append(f'<line x1="{item.x:g}" y1="{item.y:g}" x2="{item.w:g}" y2="{item.h:g}" stroke="{item.color}" stroke-width="{max(1, round(item.size))}"{opacity}/>')
            elif item.kind == "bar":
                parts.append(f'<g shape-rendering="crispEdges" aria-label="{_attr(item.role or "colour bar")}">')
                parts.extend(f'<rect x="{x0:g}" y="{item.y:g}" width="{max(x1 - x0, 1):g}" height="{item.h:g}" fill="{color}"/>' for x0, x1, color in _bar_segments(item))
                parts.append("</g>")
            elif item.kind == "text":
                horizontal = {"l": "start", "m": "middle", "r": "end"}[item.anchor[0]]
                baseline = {"s": "alphabetic", "t": "hanging", "m": "central", "a": "hanging", "b": "text-after-edge", "d": "text-after-edge"}[item.anchor[1]]
                weight = ' font-weight="600"' if item.bold else ""
                parts.append(
                    f'<text x="{item.x:g}" y="{item.y:g}" font-family="{FONT_FAMILY_SVG}" font-size="{item.size:.2f}"{weight} fill="{item.color}"{opacity} text-anchor="{horizontal}" dominant-baseline="{baseline}">{_xml_escape(item.text)}</text>',
                )
        parts.append("</svg>")
        return "\n".join(parts) + "\n"


def _rgba(color, opacity):
    rgb = tuple(round(c * 255) for c in colormaps.hex_to_rgb(color))
    return (*rgb, round(255 * max(0.0, min(1.0, opacity))))


def _attr(text):
    return _xml_escape(str(text), {'"': "&quot;"})


def _bar_segments(item):
    count = len(item.colors)
    edges = [item.x + item.w * index / count for index in range(count + 1)]
    return [(round(edges[index]), round(edges[index + 1]), item.colors[index]) for index in range(count)]


def _to_hex(rgb):
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02X}" for c in rgb)


# Options


@dataclass
class Options:
    """Options shared by the presets (keyword arguments of every preset function).

    Thresholds: ``rule`` (rule dict or JSON, see thresholds.normalize_rule) with ``n`` connectome subjects
    and ``v`` tested voxels (default: the in-mask voxels of each map), or a plain ``threshold`` (a custom
    |T|). ``mode`` applies to thresholded panels; ``companion`` adds unthresholded panels.
    """

    rule: object = None
    n: int | None = None
    v: int | None = None
    threshold: float | None = None
    gsr: bool | None = None
    sign: str | None = None
    statistic: str | None = None
    units: str | None = None
    mode: str = "hard"
    companion: bool = True
    window: float | None = None
    mask: object = None
    convention: str = DEFAULT_CONVENTION
    width_mm: float = DEFAULT_WIDTH_MM
    dpi: int = DEFAULT_DPI
    font: object = None
    spacing: float | None = None
    connectome: str | None = None
    space: str | None = None
    watermark: str | None = None
    title: str | None = None
    columns: int | None = None
    opacity: float = 1.0

    def check(self):
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}, got {self.mode!r}")
        if self.convention not in CONVENTIONS:
            raise ValueError(f"convention must be neurological or radiological, got {self.convention!r}")
        if self.sign is not None and self.sign not in thresholds.SIGNS:
            raise ValueError(f"sign must be one of {', '.join(thresholds.SIGNS)}, got {self.sign!r}")
        if not self.width_mm > 0 or int(self.dpi) < 30:
            raise ValueError("width_mm must be positive and dpi at least 30")
        if self.watermark is not None and self.watermark not in WATERMARKS.values():
            self.watermark = WATERMARKS.get(str(self.watermark).lower(), str(self.watermark))
        return self

    def params(self):
        out = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "mask":
                value = None if value is None else "mask"
            elif item.name == "font":
                value = None if value is None else os.path.basename(os.fspath(value))
            elif item.name == "rule" and value is not None:
                value = thresholds.normalize_rule(value)
            out[item.name] = value
        return out


def _options(kwargs):
    names = {item.name for item in fields(Options)}
    unknown = sorted(set(kwargs) - names)
    if unknown:
        raise TypeError(f"Unknown figure option(s): {', '.join(unknown)}")
    return Options(**kwargs).check()


# Map panels: threshold resolution


@dataclass
class MapInfo:
    volume: Volume
    title: str
    grid_values: object = None
    resolved: object = None
    fraction: float | None = None
    v: int = 0
    key: str = ""  # unique within a figure (the role, else the name, de-duplicated)
    values: object = None  # finite in-mask values of the native map (for the shown-sign share)


def _in_mask_values(volume, mask):
    data = np.asarray(volume.data, dtype=np.float64)
    if mask is not None:
        if np.shape(mask.data) == data.shape and np.allclose(mask.affine, volume.affine, atol=1e-4):
            inside = np.asarray(mask.data) > 0
        else:
            inside = reslice(mask, _NativeGrid(volume), order=0) > 0
        values = data[inside]
    else:
        values = data[np.isfinite(data) & (data != 0)]
    return values[np.isfinite(values)]


def _resolve(info, options):
    """Resolve the threshold of one map (None when the figure is unthresholded)."""
    values = _in_mask_values(info.volume, options.mask)
    info.values = values
    info.v = int(options.v or values.size or 1)
    rule = options.rule
    if rule is None and options.threshold is not None:
        rule = {"kind": "custom_t", "value": float(options.threshold)}
    if rule is None:
        return None
    if options.n:
        resolved = thresholds.resolve(rule, int(options.n), info.v, gsr=options.gsr, values=values)
    else:
        resolved = thresholds.resolve_unknown_n(rule, info.v, values=values)
    info.resolved = resolved
    info.fraction = resolved.fraction
    return resolved


# Composition


@dataclass
class TileSpec:
    plane: str
    index: int | None  # None: projection
    top_left: str = ""
    top_right: str = ""
    crosshair: tuple | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class PanelSpec:
    scene: Scene
    tiles: list
    columns: int
    title: str = ""
    letter: str = ""
    meta: dict = field(default_factory=dict)


def _letters(count):
    return [chr(ord("A") + index) if index < 26 else f"A{index - 25}" for index in range(count)]


def _compose(panels, options, fonts, colorbars=(), panel_columns=1, heading=None):
    """Lay out panels (each a grid of tiles at one mm-to-pixel scale) at the figure width and return
    (base image, annotations, tile records).
    """
    from PIL import Image

    dpi = int(options.dpi)
    width_px = round(options.width_mm / 25.4 * dpi)
    margin = round(_pt(4, dpi))
    gap = max(2, round(dpi / 150))
    panel_gap = round(_pt(6, dpi))
    text_px = _pt(TEXT_PT, dpi)
    small_px = _pt(SMALL_PT, dpi)
    letter_px = _pt(LETTER_PT, dpi)
    has_header = any(panel.title or panel.letter for panel in panels)
    header_h = round(letter_px * 1.5) if has_header else 0

    # Panel sizes in mm (rows of tiles), then one pixels-per-mm scale for the whole figure
    layouts = []
    for panel in panels:
        rows = [panel.tiles[start:start + panel.columns] for start in range(0, len(panel.tiles), panel.columns)]
        sizes = [[_tile_mm(panel.scene.grid, tile.plane) for tile in row] for row in rows]
        layouts.append((rows, sizes))
    panel_rows = [list(range(start, min(start + panel_columns, len(panels)))) for start in range(0, len(panels), panel_columns)]
    available = width_px - 2 * margin
    scale = math.inf
    for members in panel_rows:
        mm = sum(max(sum(w for w, _ in row) for row in layouts[p][1]) for p in members)
        gaps = sum((max(len(row) for row in layouts[p][0]) - 1) * gap for p in members) + (len(members) - 1) * panel_gap
        scale = min(scale, (available - gaps) / mm)
    scale = max(scale, 0.05)

    # Heading line
    y = margin
    annotations = []
    if heading:
        annotations.append(Annotation("text", margin, y, text=heading, size=text_px, anchor="lt", bold=True))
        y += round(text_px * 1.6)
    tiles_out = []
    placements = []
    for members in panel_rows:
        x = margin
        row_height = 0
        for p in members:
            panel = panels[p]
            rows, sizes = layouts[p]
            panel_w = 0
            ty = y + header_h
            for row, row_sizes in zip(rows, sizes, strict=False):
                tx = x
                tallest = 0
                for tile, (w_mm, h_mm) in zip(row, row_sizes, strict=False):
                    w_px, h_px = max(1, round(w_mm * scale)), max(1, round(h_mm * scale))
                    placements.append((panel, tile, tx, ty, w_px, h_px))
                    tx += w_px + gap
                    tallest = max(tallest, h_px)
                panel_w = max(panel_w, tx - gap - x)
                ty += tallest + gap
            panel_h = ty - gap - y
            if panel.letter or panel.title:
                annotations.append(Annotation("text", x, y + header_h * 0.82, text=panel.letter, size=letter_px, anchor="ls", bold=True, role="panel letter"))
                if panel.title:
                    offset = fonts.width(panel.letter + "  ", letter_px, bold=True) if panel.letter else 0
                    annotations.append(Annotation("text", x + offset, y + header_h * 0.82, text=panel.title, size=text_px, anchor="ls", role="panel title"))
            panel.meta["bbox"] = [x, y, x + panel_w, y + panel_h]
            x += panel_w + panel_gap
            row_height = max(row_height, panel_h)
        y += row_height + panel_gap
    y -= panel_gap

    # Colour bars
    bar_h = round(_pt(5, dpi))
    for bar in colorbars:
        y += round(text_px * 1.8)
        inset = round(fonts.width("\u221200.0", small_px) / 2)  # room for the first tick value
        annotations.extend(_colorbar(bar, margin + inset, y, round(available * (0.45 if options.width_mm > 120 else 0.8)), bar_h, fonts, text_px, small_px))
        y += bar_h + round(small_px * 1.8)
    height = y + margin

    base = Image.new("RGB", (width_px, height), (255, 255, 255))
    for panel, tile, tx, ty, w_px, h_px in placements:
        image = render_slice(panel.scene, tile.plane, tile.index, (w_px, h_px), options.convention, tile.crosshair)
        base.paste(image, (tx, ty))
        on_dark = _is_dark(panel.scene.background_color)
        ink = "#FFFFFF" if on_dark else "#000000"
        pad = max(2, round(small_px * 0.35))
        left, right = side_labels(tile.plane, options.convention)
        annotations.append(Annotation("text", tx + pad, ty + h_px - pad, text=left, size=small_px, color=ink, anchor="lb", bold=True, role="stamp"))
        annotations.append(Annotation("text", tx + w_px - pad, ty + h_px - pad, text=right, size=small_px, color=ink, anchor="rb", bold=True, role="stamp"))
        if tile.top_left:
            annotations.append(Annotation("text", tx + pad, ty + pad, text=tile.top_left, size=small_px, color=ink, anchor="lt", role="slice"))
        if tile.top_right:
            annotations.append(Annotation("text", tx + w_px - pad, ty + pad, text=tile.top_right, size=small_px, color=ink, anchor="rt", role="case"))
        record = {"panel": panel.letter, "plane": tile.plane, "bbox": [tx, ty, tx + w_px, ty + h_px], "stamps": {"left": left, "right": right}}
        record.update(tile.meta)
        tiles_out.append(record)
    if options.watermark:
        # First, so every label and stamp is drawn over it and stays readable
        annotations.insert(0, Annotation("text", width_px / 2, height / 2, text=options.watermark, size=min(width_px / max(len(options.watermark), 1) * 1.2, height * 0.3), color=WATERMARK_COLOR, opacity=WATERMARK_OPACITY, anchor="mm", bold=True, role="watermark"))
    return base, annotations, tiles_out


def _is_dark(color):
    return sum(colormaps.hex_to_rgb(color)) < 1.5


def _fmt(value):
    if value is None or not math.isfinite(value):
        return "n/a"
    text = f"{value:.3g}"
    return text.replace("-", "\u2212")


def _colorbar(bar, x, y, width, height, fonts, text_px, small_px):
    """Annotations of a horizontal colour bar: the label above, the bar, ticks and their values below."""
    items = [Annotation("text", x, y - text_px * 0.45, text=bar["label"], size=small_px, anchor="ls", role="colour bar label")]
    ticks = []
    if bar["kind"] == "diverging":
        window = bar["window"]
        colors = tuple(_to_hex(rgb) for rgb in diverging_lut())
        for value in (-window, 0.0, window):
            ticks.append((x + width * (value + window) / (2 * window), _fmt(value), False))
        threshold = bar.get("threshold")
        if threshold is not None and math.isfinite(threshold) and threshold > 0:
            sign = bar.get("sign", "both")
            for value, keep in ((-threshold, sign != "pos"), (threshold, sign != "neg")):
                if not keep:
                    continue
                # A threshold beyond a (user-set) window is marked at the bar end
                clamped = max(-window, min(window, value))
                label = _fmt(value) if clamped == value else ("\u2265 " if value > 0 else "\u2264 ") + _fmt(value)
                ticks.append((x + width * (clamped + window) / (2 * window), label, True))
            ticks.sort(key=lambda tick: tick[0])
    else:
        vmax = bar["vmax"]
        if bar.get("discrete"):
            colors = tuple(_to_hex(rgb) for rgb in count_color(np.arange(1, int(vmax) + 1), vmax))
            step = width / max(int(vmax), 1)
            labels = sorted({1, int(vmax)} | ({round(vmax / 2)} if vmax > 4 else set()))
            ticks = [(x + step * (value - 0.5), str(value), False) for value in labels]
        else:
            colors = tuple(_to_hex(rgb) for rgb in sequential_colors(np.linspace(0, 1, 128)))
            vmin = bar.get("vmin", 0.0)
            ticks = [(x, _fmt(vmin), False), (x + width, _fmt(vmax), False)]
    items.append(Annotation("bar", x, y, w=width, h=height, colors=colors, role=bar["label"]))
    items.append(Annotation("rect", x, y + height, w=width, h=1, color="#000000"))
    for tx, label, is_threshold in ticks:
        tick = height * (0.9 if is_threshold else 0.45)
        items.append(Annotation("line", tx, y + height, w=tx, h=y + height + tick, size=1, color="#000000", role="threshold tick" if is_threshold else "tick"))
        items.append(Annotation("text", tx, y + height + tick + small_px * 0.15, text=label, size=small_px, anchor="mt", role="threshold value" if is_threshold else "tick value"))
    return items


# Context: shared loading, display grid and caption pieces


class _Context:
    def __init__(self, preset, options, template=None, reference=None):
        """``reference``: the input volume(s) that set the display grid when there is no template (the
        grid then covers all of them).
        """
        self.preset = preset
        self.options = options
        self.fonts = Fonts(options.font)
        self.template = load_volume(template)
        self.mask = load_volume(options.mask)
        options.mask = self.mask
        if self.template is not None:
            anchor = self.template
        else:
            anchor = [reference] if isinstance(reference, Volume) else [volume for volume in (reference or ()) if volume is not None]
        if not anchor:
            raise ValueError("A figure needs a template or at least one image")
        self.grid = display_grid(anchor, spacing=options.spacing, crop=True)
        self.background = None if self.template is None else template_gray(reslice(self.template, self.grid, order=1))
        self.inputs = []
        if self.template is not None:
            self._input("template", self.template)
        if self.mask is not None:
            self._input("mask", self.mask)
        self.notes = []
        self.keys = set()

    def page(self):
        """A context for another page of the same figure: same grid and template, its own inputs."""
        other = copy.copy(self)
        other.inputs = [item for item in self.inputs if item["role"] in ("template", "mask")]
        other.notes = []
        other.keys = set()
        return other

    def _input(self, role, volume):
        self.inputs.append({"role": role, "name": volume.name, "file": volume.source, "sha256": volume.sha256})

    def check_coverage(self, volumes, what="map"):
        """Compare the input(s) with the template-based display grid, which silently drops whatever lies
        outside it. Raises when none of the (non-empty) inputs overlaps the grid; else returns the list of
        (volume, share inside) pairs for the inputs that are partly outside. Inputs without a template
        define the grid and are always covered.
        """
        if self.template is None:
            return []
        shares = [(volume, inside_fraction(volume, self.grid)) for volume in volumes]
        present = [(volume, share) for volume, share in shares if share is not None]
        if present and all(share == 0.0 for _, share in present):
            names = ", ".join(volume.name for volume, _ in present[:3]) + (", ..." if len(present) > 3 else "")
            raise ValueError(f"No voxel of the {what} ({names}) lies inside the template's field of view ({self.template.name}); nothing could be drawn. Is the {what} in the template's space (for example MNI)?")
        return [(volume, share) for volume, share in present if share < 1.0]

    def lesion_notes(self, volumes, labels=None):
        """Caption sentences for lesions that lie partly or wholly outside the template's field of view
        (raises when all of them do).
        """
        partial = self.check_coverage(volumes, "lesions")
        if not partial:
            return []
        labels = labels if labels is not None else [str(index + 1) for index in range(len(volumes))]
        by_id = {id(volume): label for volume, label in zip(volumes, labels, strict=False)}
        cases = [by_id[id(volume)] for volume, _ in partial]
        shown = ", ".join(cases[:8]) + (", ..." if len(cases) > 8 else "")
        worst = min(share for _, share in partial)
        noun = "lesion lies" if len(cases) == 1 else "lesions lie"
        return [f"Warning: {len(cases)} of {len(volumes)} {noun} partly or wholly outside the template's field of view (case {shown}; at worst {_percent_text(1.0 - worst)} of a lesion is not shown)."]

    def map(self, source, title, role="map"):
        volume = load_volume(source)
        for _, share in self.check_coverage([volume]):
            self.notes.append(f"Warning: {_percent_text(1.0 - share)} of the non-zero voxels of {volume.name} lie outside the template's field of view and are not shown; the statistics above describe the whole map.")
        self._input(role, volume)
        key = base = volume.name if role == "map" else role
        suffix = 2
        while key in self.keys:
            key = f"{base}-{suffix}"
            suffix += 1
        self.keys.add(key)
        info = MapInfo(volume, title, key=key)
        info.grid_values = reslice(volume, self.grid, order=1)
        return info

    def scene(self, layers=(), glass=False):
        if glass:
            outline = self.mask_on_grid()
            return Scene(self.grid, None, list(layers), outline, background_color="#FFFFFF")
        return Scene(self.grid, self.background, list(layers))

    def mask_on_grid(self):
        if self.mask is not None:
            return reslice(self.mask, self.grid, order=0) > 0
        if self.template is not None:
            return self.background > 0.05
        return None

    def sign(self, infos):
        if self.options.sign:
            return self.options.sign
        for info in infos:
            if info.resolved is not None:
                return info.resolved.rule.get("sign", "both")
        return "both"


def _statistic(options, infos):
    if options.statistic:
        return options.statistic
    for info in infos:
        if info.resolved is not None:
            return info.resolved.statistic
    return "value"


def _units(options, statistic):
    if options.units:
        return options.units
    return {"T": "T score", "Fz": "mean Fz", "r": "Pearson r", "AvgR": "mean r", "AvgFz": "mean Fz"}.get(statistic, statistic)


def _orientation_sentence(convention, planes):
    side = "left" if convention == "neurological" else "right"
    text = f"{convention.capitalize()} convention: the left hemisphere is on the image {side}; L/R stamps on every axial and coronal panel."
    if "sagittal" in planes:
        anterior = "right" if convention == "neurological" else "left"
        text += f" Sagittal views have anterior on the image {anterior} (P/A stamps)."
    return text


def _threshold_sentence(info, statistic):
    resolved = info.resolved
    if resolved is None:
        return f"{info.title}: unthresholded (no rule)."
    rule = resolved.rule
    parts = [f"{info.title}: rule {thresholds.rule_label(rule)}"]
    if rule.get("kind") == "fwe_bonferroni":
        parts[0] += f" (\u03b1 = {rule.get('alpha', 0.05):g}, two-sided)"
    describe = thresholds.describe(resolved, info.fraction)
    n_text = "N unknown, no df" if thresholds.N_UNKNOWN_NOTE in resolved.notes else f"N = {resolved.n}, df = {resolved.df}"
    sentence = f"{parts[0]} {describe}; {n_text}"
    if resolved.p_fwe is not None:
        sentence += f"; p_FWE ≤ {thresholds.format_p(resolved.p_fwe)}"
    sentence += "."
    if resolved.banner:
        sentence += f" Note: {resolved.banner}"
    for note in resolved.notes:
        if note != thresholds.N_UNKNOWN_NOTE:
            sentence += f" {note}"
    return sentence


def _mode_sentence(mode, companion):
    text = {"hard": "Thresholded panels hide voxels below the threshold.", "transparent": f"Thresholded panels use transparent thresholding: {TRANSPARENT_NOTE}.", "unthresholded": "Maps are shown unthresholded."}[mode]
    if companion and mode != "unthresholded":
        text += " Each thresholded panel has an unthresholded companion with the same colour range."
    return text


def _threshold_condition(statistic, sign, threshold):
    """The displayed threshold condition: "|T| ≥ 5.15", "T ≥ 5.15" or "T ≤ -5.15" for ``sign`` both/pos/neg (negative values in the Unicode minus sign, as :func:`_fmt` writes them)."""
    if sign == "neg":
        return f"{statistic} ≤ {_fmt(-threshold)}"
    if sign == "pos":
        return f"{statistic} ≥ {_fmt(threshold)}"
    return f"|{statistic}| ≥ {_fmt(threshold)}"


def _sign_sentence(infos, sign, mode, statistic):
    """Which tail the thresholded panels show when ``sign`` is "pos" or "neg" (empty for "both" or when
    nothing is thresholded): the other tail is hidden there, so the caption must say so, with the share of
    in-mask voxels of the shown tail (the rule line gives the two-sided |value| share).
    """
    if sign not in ("pos", "neg") or mode == "unthresholded":
        return ""
    shown, hidden = ("positive", "negative") if sign == "pos" else ("negative", "positive")
    parts = []
    resolved_infos = [info for info in infos if info.resolved is not None and math.isfinite(info.resolved.value)]
    for info in resolved_infos:
        text = _threshold_condition(statistic, sign, info.resolved.value)
        if info.values is not None:
            share = thresholds.fraction_connected(info.values, info.resolved, sign)
            if math.isfinite(share):
                text += f", {thresholds._format_fraction(share)} of in-mask voxels"
        parts.append(text if len(resolved_infos) == 1 else f"{info.title}: {text}")
    detail = f" ({'; '.join(parts)})" if parts else ""
    return f"Thresholded panels show only {shown} values{detail}; {hidden} values are hidden there."


def _slices_sentence(plane, coordinates):
    axis = AXIS_NAMES[PLANE_AXIS[plane]]
    values = ", ".join(_signed(c) for c in coordinates)
    return f"{plane.capitalize()} slices at {axis} = {values} mm (RAS world coordinates of the display grid)."


def _signed(value):
    value = float(value)
    text = f"{value:+g}" if value else "0"
    return text


def _slice_label(plane, coordinate):
    axis = AXIS_NAMES[PLANE_AXIS[plane]]
    text = f"{axis}={_signed(round(coordinate, 1))}"
    if plane == "sagittal" and coordinate:
        text += " (L)" if coordinate < 0 else " (R)"
    return text


def _context_sentences(ctx):
    options = ctx.options
    out = []
    out.append(f"Connectome: {options.connectome}." if options.connectome else "Connectome: not recorded.")
    if ctx.template is not None:
        space = f" ({options.space})" if options.space else ""
        out.append(f"Background: {ctx.template.name}{space}, display only.")
    out.append(f"Display grid: {ctx.grid.spacing:g} mm, resampled through the image affines (linear for maps, nearest for labels).")
    if options.watermark:
        out.append(f"Watermark: {options.watermark}.")
    return out


def _finish(ctx, panels, title, caption_lines, sourcedata, colorbars, panel_columns=1, extra_meta=None):
    options = ctx.options
    letters = _letters(len(panels)) if len(panels) > 1 else [""]
    for panel, letter in zip(panels, letters, strict=False):
        panel.letter = letter
    heading = options.title
    base, annotations, tiles = _compose(panels, options, ctx.fonts, colorbars, panel_columns, heading)
    meta = {
        "format": FORMAT,
        "version": VERSION,
        "preset": ctx.preset,
        "title": options.title or title,
        "params": options.params(),
        "inputs": ctx.inputs,
        "grid": ctx.grid.to_dict(),
        "convention": options.convention,
        "panels": [{"letter": panel.letter, "title": panel.title, **panel.meta} for panel in panels],
        "tiles": tiles,
        "colorbars": colorbars,
        "dpi": int(options.dpi),
        "width_mm": float(options.width_mm),
        "size_px": list(base.size),
        "font": "Inter" if not ctx.fonts.default else "Pillow default",
        "watermark": options.watermark,
    }
    meta.update(extra_meta or {})
    caption = " ".join(line for line in caption_lines if line) + "\n"
    meta["caption"] = caption.strip()
    return Figure(base, annotations, options.dpi, ctx.fonts, meta, caption, sourcedata)


def _map_panels(ctx, infos, tile_builder, columns, glass=False, titles=None):
    """Thresholded panels for each map (in ``options.mode``) plus their unthresholded companions."""
    options = ctx.options
    sign = ctx.sign(infos)
    window = options.window or _shared_window(ctx, infos, sign)
    thresholded = any(info.resolved is not None for info in infos)
    mode = options.mode if thresholded else "unthresholded"
    modes = [mode] + (["unthresholded"] if options.companion and mode != "unthresholded" else [])
    panels = []
    for display_mode in modes:
        for info in infos:
            threshold = None if info.resolved is None else info.resolved.value
            layer = Layer(info.grid_values, "diverging", window, threshold, display_mode, sign, opacity=options.opacity)
            label = {"hard": "thresholded", "transparent": "transparent threshold", "unthresholded": "unthresholded"}[display_mode]
            panel_title = f"{info.title}, {label}" if titles is None else titles(info, display_mode)
            panels.append(PanelSpec(ctx.scene([layer], glass=glass), tile_builder(info), columns, panel_title, meta={"map": info.volume.name, "key": info.key, "mode": display_mode, "window": float(window), "threshold": threshold, "sign": sign}))
    return panels, window, sign, mode


def _shared_window(ctx, infos, sign):
    """The symmetric window W shared by all panels: the 99.5th percentile of |value| pooled over the maps,
    widened (with a caption note) when a threshold is at or above it, so the threshold tick stays on the
    colour bar and suprathreshold voxels are not all drawn in the end colour.
    """
    window = symmetric_window([info.grid_values for info in infos])
    levels = [info.resolved.value for info in infos if info.resolved is not None and math.isfinite(info.resolved.value) and info.resolved.value > 0]
    if not levels or max(levels) < window:
        return window
    level = max(levels)
    supra = [scores[scores >= level] for scores in (_scores(info.grid_values, sign) for info in infos)]
    widened = max(level * WIDEN_FACTOR, symmetric_window(supra) if any(part.size for part in supra) else 0.0)
    ctx.notes.append(f"The colour range was widened from \u00b1{_fmt(window)} (the {WINDOW_PERCENTILE:g}th percentile of |value|) to \u00b1{_fmt(widened)} so that the threshold lies inside it.")
    return widened


def _companion_titles(options):
    """Panel titles: the map's title for the primary panels, ", unthresholded" for companions."""

    def title(info, display_mode):
        if display_mode == "unthresholded" and options.mode != "unthresholded":
            return f"{info.title}, unthresholded"
        return info.title

    return title


def _map_bar(infos, window, sign, options, statistic):
    threshold = next((info.resolved.value for info in infos if info.resolved is not None and math.isfinite(info.resolved.value)), None)
    label = f"{_units(options, statistic)} (shared range ±{_fmt(window)}"
    if threshold is not None:
        label += f"; threshold {_threshold_condition(statistic, sign, threshold)}"
    label += ")"
    return {"kind": "diverging", "window": float(window), "threshold": threshold, "sign": sign, "label": label}


def _map_rows(panels, infos, planes_coords):
    rows = []
    for panel in panels:
        info = next(i for i in infos if i.key == panel.meta["key"])
        for plane, coordinate, index in planes_coords:
            values = section(info.grid_values, plane, index, panel.meta["sign"])
            threshold = panel.meta["threshold"]
            scores = _scores(values, panel.meta["sign"])
            finite = values[np.isfinite(values) & (values != 0)]
            rows.append({
                "panel": panel.letter,
                "map": info.volume.name,
                "key": info.key,
                "sha256": info.volume.sha256,
                "mode": panel.meta["mode"],
                "plane": plane,
                "coordinate_mm": "projection" if index is None else round(coordinate, 3),
                "voxels_nonzero": int(finite.size),
                "voxels_suprathreshold": "" if threshold is None else int((scores >= threshold).sum()),
                "min": "" if finite.size == 0 else float(finite.min()),
                "max": "" if finite.size == 0 else float(finite.max()),
                "window": panel.meta["window"],
                "threshold": "" if threshold is None else threshold,
            })
    return rows


# Presets


def montage(map, template=None, slices="auto", **kwargs):  # noqa: A002 (the CLI's --map)
    """Slices of one map (``slices``: ``"A -20 -10 0 10 20 30"``, ``"auto"`` or ``"A auto 6"``); the
    thresholded row has an unthresholded companion row by default.
    """
    return _montage("montage", [map], template, slices, kwargs)


def pair(map, template=None, slices="auto", **kwargs):  # noqa: A002
    """The thresholded map (panel A) and its unthresholded companion (panel B) on the same slices."""
    kwargs = dict(kwargs)
    kwargs["companion"] = True
    if kwargs.get("mode") == "unthresholded":
        raise ValueError("The pair preset shows a thresholded map: choose mode hard or transparent")
    return _montage("pair", [map], template, slices, kwargs)


def _montage(preset, maps, template, slices, kwargs):
    options = _options(kwargs)
    first = load_volume(maps[0])
    ctx = _Context(preset, options, template, first)
    infos = [ctx.map(first, first.name)]
    for info in infos:
        _resolve(info, options)
    plane, coordinates, count = parse_slices(slices)
    if coordinates is None:
        threshold = infos[0].resolved.value if infos[0].resolved is not None else None
        coordinates = auto_slices(infos[0].grid_values, ctx.grid, plane, count or 6, threshold, ctx.sign(infos))
    axis = PLANE_AXIS[plane]
    planes = [(plane, ctx.grid.coordinate(axis, ctx.grid.index(axis, c)), ctx.grid.index(axis, c)) for c in coordinates]
    for c in coordinates:
        if not ctx.grid.contains(axis, c):
            ctx.notes.append(f"Slice {_signed(c)} mm is outside the display grid; the nearest plane is shown.")

    def build(info):
        return [TileSpec(p, index, _slice_label(p, c), meta={"coordinate_mm": c}) for p, c, index in planes]

    columns = options.columns or len(planes)
    panels, window, sign, mode = _map_panels(ctx, infos, build, columns)
    statistic = _statistic(options, infos)
    caption = [
        f"{'Thresholded and unthresholded map' if preset == 'pair' else 'Montage'} of {infos[0].volume.name} ({_units(options, statistic)}).",
        _orientation_sentence(options.convention, [plane]),
        *(_threshold_sentence(info, statistic) for info in infos),
        _mode_sentence(mode, options.companion),
        _sign_sentence(infos, sign, mode, statistic),
        f"Colour range symmetric ±{_fmt(window)} shared by all panels; warm colours are positive, cool colours negative.",
        _slices_sentence(plane, [c for _, c, _ in planes]),
        *ctx.notes,
        *_context_sentences(ctx),
    ]
    figure = _finish(ctx, panels, preset, caption, None, [_map_bar(infos, window, sign, options, statistic)], extra_meta=_threshold_meta(infos, window, planes))
    figure.sourcedata = _map_rows(panels, infos, planes)
    return figure


def _threshold_meta(infos, window, planes):
    return {
        "window": float(window),
        "thresholds": {info.key: None if info.resolved is None else info.resolved.to_dict() for info in infos},
        "fraction_above": {info.key: info.fraction for info in infos},
        "maps": {info.key: info.volume.name for info in infos},
        "slices": [{"plane": p, "coordinate_mm": c} for p, c, _ in planes],
    }


def triptych(sensitivity, specificity, conjunction, template=None, slices="A auto 3", titles=("Sensitivity", "Specificity", "Conjunction"), **kwargs):
    """Sensitivity | specificity | conjunction maps side by side on one shared colour range (and one
    threshold rule), each on the same slices; unthresholded companions below by default.
    """
    options = _options(kwargs)
    volumes = [load_volume(source) for source in (sensitivity, specificity, conjunction)]
    ctx = _Context("triptych", options, template, volumes)
    infos = [ctx.map(volume, title, role=title.lower()) for volume, title in zip(volumes, titles, strict=False)]
    for info in infos:
        _resolve(info, options)
    plane, coordinates, count = parse_slices(slices)
    if coordinates is None:
        threshold = infos[-1].resolved.value if infos[-1].resolved is not None else None
        combined = np.max(np.stack([np.abs(info.grid_values) for info in infos]), axis=0)
        coordinates = auto_slices(combined, ctx.grid, plane, count or 3, None if threshold is None else min(threshold, combined.max()), "both")
    axis = PLANE_AXIS[plane]
    planes = [(plane, ctx.grid.coordinate(axis, ctx.grid.index(axis, c)), ctx.grid.index(axis, c)) for c in coordinates]

    def build(info):
        return [TileSpec(p, index, _slice_label(p, c), meta={"coordinate_mm": c}) for p, c, index in planes]

    panels, window, sign, mode = _map_panels(ctx, infos, build, options.columns or 1, titles=_companion_titles(options))
    statistic = _statistic(options, infos)
    caption = [
        f"Sensitivity, specificity and conjunction maps ({_units(options, statistic)}) on one shared colour range.",
        _orientation_sentence(options.convention, [plane]),
        *(_threshold_sentence(info, statistic) for info in infos),
        _mode_sentence(mode, options.companion),
        _sign_sentence(infos, sign, mode, statistic),
        f"Colour range symmetric ±{_fmt(window)} shared by all panels; warm colours are positive, cool colours negative.",
        _slices_sentence(plane, [c for _, c, _ in planes]),
        *ctx.notes,
        *_context_sentences(ctx),
    ]
    figure = _finish(ctx, panels, "triptych", caption, None, [_map_bar(infos, window, sign, options, statistic)], panel_columns=3, extra_meta=_threshold_meta(infos, window, planes))
    figure.sourcedata = _map_rows(panels, infos, planes)
    return figure


def spatial_r(a, b, mask=None):
    """Pearson r between two arrays over the voxels finite in both (and inside ``mask`` when given,
    else where either is non-zero); returns (r, voxels).
    """
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    keep = np.isfinite(a) & np.isfinite(b)
    keep &= (np.asarray(mask).ravel() > 0) if mask is not None else ((a != 0) | (b != 0))
    if keep.sum() < 3:
        return math.nan, int(keep.sum())
    x, y = a[keep] - a[keep].mean(), b[keep] - b[keep].mean()
    denominator = math.sqrt(float((x * x).sum()) * float((y * y).sum()))
    return (float((x * y).sum()) / denominator if denominator > 0 else math.nan), int(keep.sum())


def replication(first, second, template=None, slices="auto", titles=("Discovery", "Replication"), **kwargs):
    """Two maps side by side (unthresholded by default) with their spatial correlation r, computed on
    the maps' own grid when they share it (else on the display grid) inside the mask when given.
    """
    kwargs = dict(kwargs)
    kwargs.setdefault("mode", "unthresholded")
    options = _options(kwargs)
    volumes = [load_volume(first), load_volume(second)]
    ctx = _Context("replication", options, template, volumes)
    infos = [ctx.map(volume, title, role=title.lower()) for volume, title in zip(volumes, titles, strict=False)]
    for info in infos:
        _resolve(info, options)
    a, b = infos[0].volume, infos[1].volume
    same_grid = np.shape(a.data) == np.shape(b.data) and np.allclose(a.affine, b.affine, atol=1e-4)
    if same_grid:
        mask = None
        if ctx.mask is not None:
            mask = reslice(ctx.mask, _NativeGrid(a), order=0) if not (np.shape(ctx.mask.data) == np.shape(a.data) and np.allclose(ctx.mask.affine, a.affine, atol=1e-4)) else ctx.mask.data
        r, voxels = spatial_r(a.data, b.data, mask)
        where = "on the maps' common grid"
    else:
        mask = ctx.mask_on_grid() if ctx.mask is not None else None
        r, voxels = spatial_r(infos[0].grid_values, infos[1].grid_values, mask)
        where = "on the display grid"
    plane, coordinates, count = parse_slices(slices)
    if coordinates is None:
        combined = np.abs(infos[0].grid_values) + np.abs(infos[1].grid_values)
        coordinates = auto_slices(combined, ctx.grid, plane, count or 4)
    axis = PLANE_AXIS[plane]
    planes = [(plane, ctx.grid.coordinate(axis, ctx.grid.index(axis, c)), ctx.grid.index(axis, c)) for c in coordinates]

    def build(info):
        return [TileSpec(p, index, _slice_label(p, c), meta={"coordinate_mm": c}) for p, c, index in planes]

    panels, window, sign, mode = _map_panels(ctx, infos, build, options.columns or len(planes), titles=_companion_titles(options))
    r_text = "n/a" if not math.isfinite(r) else f"{r:.2f}"
    statistic = _statistic(options, infos)
    options.title = options.title or f"Spatial correlation r = {r_text}"
    caption = [
        f"Replication: {a.name} and {b.name} ({_units(options, statistic)}); spatial correlation r = {r_text} (Pearson, {voxels:,} voxels {where}, {'inside the mask' if ctx.mask is not None else 'non-zero in either map'}).",
        _orientation_sentence(options.convention, [plane]),
        *(_threshold_sentence(info, statistic) for info in infos if info.resolved is not None),
        _mode_sentence(mode, options.companion),
        _sign_sentence(infos, sign, mode, statistic),
        f"Colour range symmetric ±{_fmt(window)} shared by both maps; warm colours are positive, cool colours negative.",
        _slices_sentence(plane, [c for _, c, _ in planes]),
        *ctx.notes,
        *_context_sentences(ctx),
    ]
    meta = _threshold_meta(infos, window, planes)
    meta.update({"spatial_r": None if not math.isfinite(r) else r, "r_voxels": voxels, "r_grid": "native" if same_grid else "display"})
    figure = _finish(ctx, panels, "replication", caption, None, [_map_bar(infos, window, sign, options, statistic)], extra_meta=meta)
    figure.sourcedata = _map_rows(panels, infos, planes) + [{"panel": "", "map": f"{a.name} vs {b.name}", "plane": "spatial_r", "coordinate_mm": "", "voxels_nonzero": voxels, "min": "", "max": "", "window": "", "threshold": "", "sha256": "", "mode": "", "voxels_suprathreshold": "", "r": r}]
    return figure


class _NativeGrid:
    """A grid-like view of a volume's own grid (for reslicing a mask onto it)."""

    def __init__(self, volume):
        self.affine = np.asarray(volume.affine, dtype=np.float64)
        self.shape = tuple(np.shape(volume.data)[:3])


def glass(map, mask=None, template=None, **kwargs):  # noqa: A002
    """Three-view maximum-|value| projection (sagittal, coronal, axial) on white, with the brain outline
    from the edge of the projected mask (``mask``, else the template above 5% of its range).
    """
    kwargs = dict(kwargs)
    if mask is not None:
        kwargs["mask"] = mask
    options = _options(kwargs)
    first = load_volume(map)
    ctx = _Context("glass", options, template, first)
    infos = [ctx.map(first, first.name)]
    for info in infos:
        _resolve(info, options)
    if ctx.mask_on_grid() is None:
        ctx.notes.append("No mask or template: the brain outline is not drawn.")

    def build(info):
        return [TileSpec(plane, None, f"{plane} projection", meta={"projection": True}) for plane in PLANES]

    panels, window, sign, mode = _map_panels(ctx, infos, build, 3, glass=True)
    statistic = _statistic(options, infos)
    caption = [
        f"Glass brain of {first.name} ({_units(options, statistic)}): signed value of maximum |value| along each viewing axis (sagittal, coronal, axial); outline from the edge of the projected brain mask.",
        _orientation_sentence(options.convention, PLANES),
        *(_threshold_sentence(info, statistic) for info in infos),
        _mode_sentence(mode, options.companion),
        _sign_sentence(infos, sign, mode, statistic),
        f"Colour range symmetric ±{_fmt(window)} shared by all panels; warm colours are positive, cool colours negative.",
        *ctx.notes,
        *_context_sentences(ctx),
    ]
    planes = [(plane, 0.0, None) for plane in PLANES]
    figure = _finish(ctx, panels, "glass", caption, None, [_map_bar(infos, window, sign, options, statistic)], extra_meta=_threshold_meta(infos, window, []))
    figure.sourcedata = _map_rows(panels, infos, planes)
    return figure


def ortho(map, point=None, template=None, **kwargs):  # noqa: A002
    """Sagittal, coronal and axial tiles through ``point`` (world mm; default: the voxel of maximum
    score) with a 1 px crosshair.
    """
    options = _options(kwargs)
    first = load_volume(map)
    ctx = _Context("ortho", options, template, first)
    infos = [ctx.map(first, first.name)]
    for info in infos:
        _resolve(info, options)
    if point is None:
        scores = _scores(infos[0].grid_values, ctx.sign(infos))
        peak = np.unravel_index(int(np.argmax(scores)), scores.shape)
        point = tuple(ctx.grid.coordinate(axis, peak[axis]) for axis in range(3))
    point = tuple(float(value) for value in point)
    indices = [ctx.grid.index(axis, point[axis]) for axis in range(3)]
    planes = [(plane, ctx.grid.coordinate(PLANE_AXIS[plane], indices[PLANE_AXIS[plane]]), indices[PLANE_AXIS[plane]]) for plane in PLANES]

    def build(info):
        return [TileSpec(p, index, _slice_label(p, c), crosshair=point, meta={"coordinate_mm": c}) for p, c, index in planes]

    panels, window, sign, mode = _map_panels(ctx, infos, build, 3)
    statistic = _statistic(options, infos)
    caption = [
        f"Orthogonal views of {first.name} ({_units(options, statistic)}) through ({', '.join(_signed(round(c, 1)) for c in point)}) mm (crosshair).",
        _orientation_sentence(options.convention, PLANES),
        *(_threshold_sentence(info, statistic) for info in infos),
        _mode_sentence(mode, options.companion),
        _sign_sentence(infos, sign, mode, statistic),
        f"Colour range symmetric ±{_fmt(window)} shared by all panels; warm colours are positive, cool colours negative.",
        *ctx.notes,
        *_context_sentences(ctx),
    ]
    meta = _threshold_meta(infos, window, planes)
    meta["point_mm"] = list(point)
    figure = _finish(ctx, panels, "ortho", caption, None, [_map_bar(infos, window, sign, options, statistic)], extra_meta=meta)
    figure.sourcedata = _map_rows(panels, infos, planes)
    return figure


def overlap(count=None, lesions=None, template=None, slices="auto", n_lesions=None, binarize_at=0.5, **kwargs):
    """Lesion overlap: colour = number of lesions covering each voxel, exact integer bins 1..max on the
    sequential map, maximum stated ("max 6/23 (26%)").

    :param count: an integer count map (e.g. lesionset's overlapCount), or
    :param lesions: lesion masks (a voxel counts for a lesion when its value >= ``binarize_at``); they are
      counted on their own grid when they share it (exactly as lesionset.overlap_maps), else on the display grid.
    :param n_lesions: number of lesions behind ``count`` (default: unknown, the legend then omits the %).
    """
    options = _options(kwargs)
    if (count is None) == (lesions is None):
        raise ValueError("Give either a count map or lesion masks")
    if lesions is not None:
        volumes = [load_volume(source) for source in lesions]
        if not volumes:
            raise ValueError("No lesions")
        n_lesions = len(volumes)
        ctx = _Context("overlap", options, template, volumes)
        for volume in volumes:
            ctx._input("lesion", volume)
        outside_notes = ctx.lesion_notes(volumes)
        first = volumes[0]
        if all(np.shape(v.data) == np.shape(first.data) and np.allclose(v.affine, first.affine, atol=1e-4) for v in volumes):
            native = np.zeros(np.shape(first.data), dtype=np.int32)
            for volume in volumes:
                native += (np.nan_to_num(np.asarray(volume.data, dtype=np.float64)) >= binarize_at).astype(np.int32)
            counted = Volume(native, first.affine, "lesion count", _array_sha256(native, first.affine))
            grid_count = np.rint(reslice(counted, ctx.grid, order=0)).astype(np.int32)
        else:
            native = None
            grid_count = np.zeros(ctx.grid.shape, dtype=np.int32)
            for volume in volumes:
                grid_count += (reslice(volume, ctx.grid, order=0) >= binarize_at).astype(np.int32)
        name = "lesion overlap"
    else:
        volume = load_volume(count)
        ctx = _Context("overlap", options, template, volume)
        ctx._input("count", volume)
        outside_notes = [f"Warning: {_percent_text(1.0 - share)} of the non-zero voxels of {volume.name} lie outside the template's field of view and are not shown." for _, share in ctx.check_coverage([volume], "count map")]
        native = np.rint(np.nan_to_num(np.asarray(volume.data, dtype=np.float64))).astype(np.int32)
        grid_count = np.rint(reslice(volume, ctx.grid, order=0)).astype(np.int32)
        name = volume.name
    max_count = int(native.max()) if native is not None and native.size else int(grid_count.max())
    legend = _overlap_legend(max_count, n_lesions)
    if outside_notes and int(grid_count.max()) < max_count:
        outside_notes.append(f"The maximum ({max_count}) is reached outside the displayed area; the largest count shown is {int(grid_count.max())}.")
    plane, coordinates, n_slices = parse_slices(slices)
    if coordinates is None:
        coordinates = auto_slices(grid_count.astype(np.float32), ctx.grid, plane, n_slices or 6)
    axis = PLANE_AXIS[plane]
    planes = [(plane, ctx.grid.coordinate(axis, ctx.grid.index(axis, c)), ctx.grid.index(axis, c)) for c in coordinates]
    layer = Layer(grid_count, "sequential", vmin=0, vmax=max(max_count, 1), discrete=True, opacity=1.0)
    tiles = [TileSpec(p, index, _slice_label(p, c), meta={"coordinate_mm": c}) for p, c, index in planes]
    panels = [PanelSpec(ctx.scene([layer]), tiles, options.columns or len(tiles), "", meta={"map": name, "mode": "count", "max_count": max_count, "legend": legend})]
    of = f" of {n_lesions} lesions ({round(100.0 * max_count / n_lesions):.0f}%)" if n_lesions else ""
    caption = [
        f"Lesion overlap: colour = number of lesions covering each voxel, exact integer bins 1\u2013{max(max_count, 1)} (sequential colour map); maximum {max_count}{of}.",
        _orientation_sentence(options.convention, [plane]),
        f"A voxel counts for a lesion when its value is ≥ {binarize_at:g}." if lesions is not None else "",
        _slices_sentence(plane, [c for _, c, _ in planes]),
        *outside_notes,
        *_context_sentences(ctx),
    ]
    bar = {"kind": "sequential", "vmin": 0, "vmax": max(max_count, 1), "discrete": True, "label": f"lesions per voxel ({legend})"}
    histogram_source = native if native is not None else grid_count
    values, counts = np.unique(histogram_source[histogram_source > 0], return_counts=True)
    rows = [{"kind": "histogram", "lesions": int(v), "voxels": int(c), "grid": "native" if native is not None else "display", "plane": "", "coordinate_mm": "", "max_in_slice": ""} for v, c in zip(values, counts, strict=False)]
    for p, c, index in planes:
        rows.append({"kind": "slice", "lesions": "", "voxels": int((section(grid_count, p, index) > 0).sum()), "grid": "display", "plane": p, "coordinate_mm": round(c, 3), "max_in_slice": int(section(grid_count, p, index).max())})
    meta = {"max_count": max_count, "n_lesions": n_lesions, "legend": legend, "slices": [{"plane": p, "coordinate_mm": c} for p, c, _ in planes], "bins": {int(v): int(c) for v, c in zip(values, counts, strict=False)}}
    return _finish(ctx, panels, "overlap", caption, rows, [bar], extra_meta=meta)


def _overlap_legend(max_count, n):
    if n:
        return f"max {int(max_count)}/{int(n)} ({round(100.0 * max_count / n):.0f}%)"
    return f"max {int(max_count)}"


def max_area_index(mask3d, axis=2):
    """Plane index (along ``axis``) where the mask has the largest area (the lowest index on ties)."""
    other = tuple(a for a in range(3) if a != axis)
    areas = np.asarray(mask3d, dtype=bool).sum(axis=other)
    return int(np.argmax(areas)), int(areas.max())


def gallery(lesions, template=None, labels=None, per_page=30, binarize_at=0.5, cancel=None, **kwargs):
    """One axial tile per lesion at its maximum-area slice: red fill on the template, the case number
    (supplementary-table order: ``labels`` or 1..N) at the top right, z at the top left, L/R stamps.

    :param cancel: optional :class:`jobs.CancelToken` (or any callable); checked between lesions and pages,
        it raises :class:`jobs.Cancelled` when the job is stopped.
    :return: a list of :class:`Figure` pages of ``per_page`` tiles (``columns`` per row, default 6).
    """
    options = _options(kwargs)
    volumes = [load_volume(source) for source in lesions]
    if not volumes:
        raise ValueError("No lesions")
    labels = [str(label) for label in labels] if labels is not None else [str(index + 1) for index in range(len(volumes))]
    if len(labels) != len(volumes):
        raise ValueError(f"{len(labels)} labels for {len(volumes)} lesions")
    per_page = max(1, int(per_page))
    pages = []
    ctx_all = _Context("gallery", options, template, volumes)
    outside_notes = ctx_all.lesion_notes(volumes, labels)
    entries = []
    for volume, label in zip(volumes, labels, strict=False):
        _check_cancel(cancel, len(entries))
        on_grid = reslice(volume, ctx_all.grid, order=0) >= binarize_at
        index, area = max_area_index(on_grid, 2)
        # Keep only the chosen axial section (a tile renders that plane alone): a read-only 3D view of it
        # costs no memory per lesion.
        chosen = np.broadcast_to(section(on_grid, "axial", index)[:, :, None], ctx_all.grid.shape)
        entries.append((volume, label, chosen, index, area, int(on_grid.sum())))
        del on_grid
    total_pages = (len(entries) + per_page - 1) // per_page
    for page in range(total_pages):
        _check_cancel(cancel, page)
        chunk = entries[page * per_page:(page + 1) * per_page]
        ctx = ctx_all.page() if page else ctx_all
        tiles = []
        panels = []
        rows = []
        for volume, label, chosen, index, area, voxels in chunk:
            ctx._input("lesion", volume)
            z = ctx.grid.coordinate(2, index)
            empty = area == 0
            scene = ctx.scene([Layer(chosen, "binary", color=LESION_COLOR, opacity=0.9)])
            tile = TileSpec("axial", index, "empty" if empty else _slice_label("axial", z), label, meta={"case": label, "lesion": volume.name, "coordinate_mm": z, "area_voxels": area})
            tiles.append(tile)
            panels.append(PanelSpec(scene, [tile], 1, "", meta={"case": label}))
            rows.append({"case": label, "lesion": volume.name, "sha256": volume.sha256, "plane": "axial", "coordinate_mm": round(z, 3), "area_mm2": round(area * ctx.grid.spacing**2, 3), "voxels_display_grid": voxels})
        columns = options.columns or 6
        caption = [
            f"Lesion gallery ({len(entries)} lesions{f', page {page + 1} of {total_pages}' if total_pages > 1 else ''}): each lesion (red) on the axial slice of its largest area; numbers match the case table; z in mm.",
            _orientation_sentence(options.convention, ["axial"]),
            *outside_notes,
            *_context_sentences(ctx),
        ]
        figure = _finish_gallery(ctx, panels, columns, caption, rows, page, total_pages)
        pages.append(figure)
    return pages


def _finish_gallery(ctx, panels, columns, caption, rows, page, total_pages):
    options = ctx.options
    base, annotations, tiles = _compose(panels, options, ctx.fonts, (), columns, options.title)
    meta = {
        "format": FORMAT,
        "version": VERSION,
        "preset": "gallery",
        "title": options.title or "gallery",
        "params": options.params(),
        "inputs": ctx.inputs,
        "grid": ctx.grid.to_dict(),
        "convention": options.convention,
        "panels": [panel.meta for panel in panels],
        "tiles": tiles,
        "colorbars": [],
        "dpi": int(options.dpi),
        "width_mm": float(options.width_mm),
        "size_px": list(base.size),
        "font": "Inter" if not ctx.fonts.default else "Pillow default",
        "watermark": options.watermark,
        "page": page + 1,
        "pages": total_pages,
    }
    text = " ".join(line for line in caption if line) + "\n"
    meta["caption"] = text.strip()
    return Figure(base, annotations, options.dpi, ctx.fonts, meta, text, rows)


# Watermarks


def watermark_for(study=None, bundle=None):
    """The watermark of a figure: "TUTORIAL DATA" for tutorial studies, "SYNTHETIC" for synthetic
    connectome bundles or studies, else None.

    :param study: a study folder, its study.json path or its metadata dictionary.
    :param bundle: connectome bundle metadata (dict with name/description/synthetic) or its name.
    """
    meta = study
    if isinstance(study, (str, os.PathLike)):
        path = os.fspath(study)
        if os.path.isdir(path):
            path = os.path.join(path, "study.json")
        try:
            with open(path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = None
    if isinstance(meta, dict):
        tags = [str(tag).lower() for tag in meta.get("tags", []) or []]
        if meta.get("tutorial") or "tutorial" in tags:
            return WATERMARKS["tutorial"]
        if meta.get("synthetic") or "synthetic" in tags:
            return WATERMARKS["synthetic"]
    if isinstance(bundle, dict):
        if bundle.get("synthetic") or "synthetic" in f"{bundle.get('name', '')} {bundle.get('description', '')}".lower():
            return WATERMARKS["synthetic"]
    elif isinstance(bundle, str) and "synthetic" in bundle.lower():
        return WATERMARKS["synthetic"]
    return None


# Outputs


def _json_ready(value):
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, np.ndarray):
        return _json_ready(value.tolist())
    return value


def _versions():
    import PIL
    import scipy

    try:
        library = jobs.library_version()
    except Exception:
        library = None
    return {"python": sys.version.split()[0], "numpy": np.__version__, "scipy": scipy.__version__, "pillow": PIL.__version__, "library": library}


def _write_bytes(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RESUME_NAME = ".figure-resume.json"  # in the figure folder: --resume skips a figure that is complete for the same inputs and options
_RESUME_IGNORED = ("cancel_file", "resume", "threads", "parent_run", "run_root")


def _check_cancel(cancel, done=None):
    """Raise ``jobs.Cancelled`` when ``cancel`` (a CancelToken or any callable) says the job was stopped."""
    if cancel is not None and cancel():
        raise jobs.Cancelled(done=done, reason=getattr(cancel, "reason", None))


def _sha256_file(path, block=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(block):
            digest.update(chunk)
    return digest.hexdigest()


def write_figure(figure, out_root, name, formats=FORMATS, cancel=None):
    """Write a figure (or a list of gallery pages) to ``<out_root>/figures/<name>/``.

    Files: ``<name>.png/.tiff/.svg/.pdf`` (pages: ``<name>_p01.png`` ... and one multi-page PDF),
    ``figure.json``, ``caption.txt`` and ``<name>_sourcedata.csv``.

    ``cancel`` (a :class:`jobs.CancelToken` or any callable) is checked while the files are encoded; a stopped
    job raises :class:`jobs.Cancelled` before the first file of this call is written (no partial figure).

    :return: dict {"dir": folder, "files": [file names], "figure_json": path}.
    """
    if not _NAME_PATTERN.match(name or ""):
        raise ValueError(f"Figure names use letters, digits, '.', '_' and '-': {name!r}")
    unknown = sorted(set(formats) - set(FORMATS))
    if unknown:
        raise ValueError(f"Unknown format(s): {', '.join(unknown)}")
    pages = list(figure) if isinstance(figure, (list, tuple)) else [figure]
    folder = os.path.join(os.fspath(out_root), "figures", name)
    files = []
    payloads = []  # (file name, bytes): everything is encoded before the first write, so a stop leaves no partial figure
    stems = [name] if len(pages) == 1 else [f"{name}_p{index + 1:02d}" for index in range(len(pages))]
    for page, stem in zip(pages, stems, strict=False):
        _check_cancel(cancel, len(files))
        image = page.image()
        if "png" in formats:
            payloads.append((stem + ".png", page.png_bytes()))
            files.append(stem + ".png")
            _check_cancel(cancel, len(files))
        if "tiff" in formats:
            buffer = io.BytesIO()
            image.save(buffer, "TIFF", dpi=(page.dpi, page.dpi), compression="tiff_lzw")
            payloads.append((stem + ".tiff", buffer.getvalue()))
            files.append(stem + ".tiff")
            _check_cancel(cancel, len(files))
        if "svg" in formats:
            payloads.append((stem + ".svg", page.svg().encode("utf-8")))
            files.append(stem + ".svg")
    if "pdf" in formats:
        _check_cancel(cancel, len(files))
        buffer = io.BytesIO()
        images = [page.image() for page in pages]
        images[0].save(buffer, "PDF", resolution=float(pages[0].dpi), save_all=len(images) > 1, append_images=images[1:], title=name, creator="Slicer Studio LNM figures")
        payloads.append((name + ".pdf", buffer.getvalue()))
        files.append(name + ".pdf")
    caption = "\n".join(page.caption.strip() for page in pages) + "\n"
    payloads.append(("caption.txt", caption.encode("utf-8")))
    files.append("caption.txt")
    rows = [row for page in pages for row in (page.sourcedata or [])]
    csv_name = f"{name}_sourcedata.csv"
    columns = []
    for row in rows:
        columns.extend(key for key in row if key not in columns)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns or ["note"], lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: ("" if value is None else value) for key, value in row.items()})
    payloads.append((csv_name, buffer.getvalue().encode("utf-8")))
    files.append(csv_name)
    first = pages[0].meta
    record = dict(first)
    if len(pages) > 1:
        record["pages"] = [{"page": page.meta.get("page"), "tiles": page.meta["tiles"], "inputs": page.meta["inputs"], "size_px": page.meta["size_px"]} for page in pages]
        record.pop("tiles", None)
        record["inputs"] = [item for page in pages for item in page.meta["inputs"] if item["role"] != "template"] + [item for item in first["inputs"] if item["role"] == "template"]
    record["name"] = name
    record["files"] = files + ["figure.json"]
    record["software"] = _versions()
    record["created"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payloads.append(("figure.json", (json.dumps(_json_ready(record), indent=1, ensure_ascii=False) + "\n").encode("utf-8")))
    _check_cancel(cancel, len(files))
    os.makedirs(folder, exist_ok=True)
    for file_name, data in payloads:
        _write_bytes(os.path.join(folder, file_name), data)
    return {"dir": folder, "files": record["files"], "figure_json": os.path.join(folder, "figure.json")}


# Command line


def _parser():
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.figures", description="Publication figures of lesion network maps (PNG, TIFF, SVG, PDF, caption and source data).")
    parser.add_argument("--preset", required=True, choices=PRESETS)
    parser.add_argument("--map", action="append", default=[], help="map NIfTI (repeat: replication takes 2, triptych 3 in the order sensitivity, specificity, conjunction)")
    parser.add_argument("--lesion", action="append", default=[], help="lesion mask NIfTI (gallery, overlap; repeatable)")
    parser.add_argument("--label", action="append", default=None, help="case label of each --lesion (gallery; default 1..N)")
    parser.add_argument("--count", default=None, help="integer overlap count map (overlap)")
    parser.add_argument("--n-lesions", type=int, default=None, help="number of lesions behind --count")
    parser.add_argument("--template", default=None, help="background template NIfTI")
    parser.add_argument("--mask", default=None, help="brain/connectome mask NIfTI (V, glass outline, replication r)")
    parser.add_argument("--rule", default=None, help="threshold rule as JSON, e.g. '{\"kind\": \"fwe_bonferroni\", \"alpha\": 0.05}'")
    parser.add_argument("--threshold", type=float, default=None, help="a plain |T| threshold (custom_t) instead of --rule")
    parser.add_argument("--n", type=int, default=None, help="connectome subjects N (df = N - 1)")
    parser.add_argument("--v", type=int, default=None, help="voxels tested V (default: in-mask voxels)")
    parser.add_argument("--slices", default=None, help="e.g. 'A -20 -10 0 10 20 30', 'C 0', 'auto' or 'A auto 6'")
    parser.add_argument("--point", default=None, help="x,y,z in mm (ortho; default: the peak)")
    parser.add_argument("--mode", choices=MODES, default=None, help="display mode of thresholded panels (default hard; replication: unthresholded)")
    parser.add_argument("--no-companion", action="store_true", help="omit the unthresholded companion panels")
    parser.add_argument("--sign", choices=thresholds.SIGNS, default=None)
    parser.add_argument("--statistic", default=None, help="statistic name on the colour bar (default: from the rule)")
    parser.add_argument("--units", default=None, help="units shown on the colour bar")
    parser.add_argument("--window", type=float, default=None, help="symmetric colour range W (default: 99.5th percentile of |value|)")
    parser.add_argument("--convention", choices=CONVENTIONS, default=DEFAULT_CONVENTION)
    parser.add_argument("--width-mm", type=float, default=DEFAULT_WIDTH_MM, help="figure width (journals: 89 or 183 mm)")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI, help="resolution (300, 450 or 600)")
    parser.add_argument("--font", default=None, help="Inter TTF file (default: Pillow's font)")
    parser.add_argument("--connectome", default=None, help="connectome name for the caption")
    parser.add_argument("--space", default=None, help="template space for the caption")
    parser.add_argument("--watermark", default="auto", help="auto (from --study / --connectome), none, tutorial or synthetic")
    parser.add_argument("--title", default=None)
    parser.add_argument("--columns", type=int, default=None)
    parser.add_argument("--per-page", type=int, default=30, help="gallery tiles per page")
    parser.add_argument("--formats", default=",".join(FORMATS), help="comma-separated: png,tiff,svg,pdf")
    parser.add_argument("--name", default=None, help="figure name (default: the preset)")
    parser.add_argument("--study", default=None, help="study folder (run registry and watermark)")
    parser.add_argument("--out", default=None, help="output root; files go to <out>/figures/<name>/ (default: <study>/derivatives/lnm)")
    jobs.add_common_arguments(parser, threads_default=1)
    return parser


def _find_study(folder):
    folder = os.path.abspath(folder)
    while True:
        if os.path.isfile(os.path.join(folder, "study.json")):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def _relative(root, path):
    if path is None:
        return None
    try:
        rel = os.path.relpath(os.path.abspath(path), root)
    except ValueError:
        return os.path.basename(path)
    return os.path.basename(path) if rel.startswith("..") else rel.replace(os.sep, "/")


def _resume_hash(args, formats):
    """Hash of everything that decides the figure (options and the bytes of the input files); no path is stored."""
    from . import runs

    values = {key: value for key, value in vars(args).items() if key not in _RESUME_IGNORED}
    values["formats"] = formats
    inputs = [*args.map, *args.lesion, args.count, args.template, args.mask]
    values["inputs_sha256"] = [_sha256_file(path) if path and os.path.isfile(path) else None for path in inputs]
    values["library"] = _versions()["library"]
    return runs.params_sha256(values)


def main(argv=None, cancel=None):
    """Command-line entry point (see the module docstring); returns the exit code.

    ``--cancel-file`` (or a ``cancel`` token given by an in-process caller) stops the job with exit code 3 before
    any file of the figure is written; ``--resume`` skips a figure that a previous run completed for the same
    inputs and options (its files are reported again and no run is registered a second time).
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    cancel = cancel or jobs.CancelToken(args.cancel_file)
    jobs.emit("start", job=JOB, argv=argv)
    if args.out is None and args.study is None:
        raise jobs.JobError("Give --out or --study", code="usage", exit_code=jobs.EXIT_USAGE)
    if args.rule is not None and args.threshold is not None:
        raise jobs.JobError("Give --rule or --threshold, not both", code="usage", exit_code=jobs.EXIT_USAGE)
    out_root = args.out or os.path.join(args.study, "derivatives", "lnm")
    run_root = args.run_root or args.study or _find_study(out_root) or out_root
    name = args.name or args.preset
    formats = [item.strip().lower() for item in args.formats.split(",") if item.strip()]
    watermark = args.watermark
    if watermark == "auto":
        watermark = watermark_for(args.study, args.connectome)
    elif watermark == "none":
        watermark = None
    options = {
        "rule": args.rule,
        "n": args.n,
        "v": args.v,
        "threshold": args.threshold,
        "sign": args.sign,
        "statistic": args.statistic,
        "units": args.units,
        "companion": not args.no_companion,
        "window": args.window,
        "mask": args.mask,
        "convention": args.convention,
        "width_mm": args.width_mm,
        "dpi": args.dpi,
        "font": args.font,
        "connectome": args.connectome,
        "space": args.space,
        "watermark": watermark,
        "title": args.title,
        "columns": args.columns,
    }
    if args.mode:
        options["mode"] = args.mode
    preset = args.preset
    cancel.raise_if_cancelled(done=0)
    state = None
    if _NAME_PATTERN.match(name or ""):
        state = jobs.ResumeState(os.path.join(out_root, "figures", name, RESUME_NAME))
        if not args.resume:
            state.clear()
    try:
        if args.resume and state is not None and state.done:
            key = _resume_hash(args, formats)
            folder = os.path.join(out_root, "figures", name)
            files = state.data.get("files") or []
            if state.data.get("params_sha256") == key and files and all(os.path.isfile(os.path.join(folder, item)) for item in files):
                for file_name in files:
                    jobs.emit("result", kind="figure", path=os.path.join(folder, file_name))
                jobs.emit("done", outputs=[os.path.join(folder, f) for f in files], elapsed=round(jobs.elapsed(), 3), run_id=state.data.get("run_id"), resumed=True)
                return 0
            state.clear()
        needs = {"montage": 1, "pair": 1, "glass": 1, "ortho": 1, "replication": 2, "triptych": 3}
        if preset in needs and len(args.map) != needs[preset]:
            raise ValueError(f"The {preset} preset needs {needs[preset]} --map")
        slices = args.slices
        if preset in ("montage", "pair"):
            figure = (montage if preset == "montage" else pair)(args.map[0], args.template, slices or "auto", **options)
        elif preset == "triptych":
            figure = triptych(*args.map, template=args.template, slices=slices or "A auto 3", **options)
        elif preset == "replication":
            figure = replication(*args.map, template=args.template, slices=slices or "auto", **options)
        elif preset == "glass":
            options.pop("mask")
            figure = glass(args.map[0], mask=args.mask, template=args.template, **options)
        elif preset == "ortho":
            point = None if args.point is None else tuple(float(v) for v in args.point.split(","))
            if point is not None and len(point) != 3:
                raise ValueError("--point needs x,y,z")
            figure = ortho(args.map[0], point, args.template, **options)
        elif preset == "overlap":
            for key in ("rule", "n", "v", "threshold", "sign", "statistic", "units", "companion", "window", "mask"):
                options.pop(key)
            figure = overlap(args.count, args.lesion or None, args.template, slices or "auto", n_lesions=args.n_lesions, **options)
        else:
            for key in ("rule", "n", "v", "threshold", "sign", "statistic", "units", "companion", "window", "mask"):
                options.pop(key)
            figure = gallery(args.lesion, args.template, args.label, args.per_page, cancel=cancel, **options)
        written = write_figure(figure, out_root, name, formats, cancel=cancel)
    except (ValueError, TypeError, OSError) as exc:
        raise jobs.JobError(str(exc), code="input") from None
    from . import runs

    params = {
        "preset": preset,
        "maps": [_relative(run_root, path) for path in args.map],
        "lesions": [_relative(run_root, path) for path in args.lesion],
        "count": _relative(run_root, args.count),
        "template": _relative(run_root, args.template),
        "mask": _relative(run_root, args.mask),
        "options": (figure[0] if isinstance(figure, list) else figure).meta["params"],
        "slices": args.slices,
        "formats": formats,
        "name": name,
    }
    run_id = runs.register(run_root, "figure", written["dir"], parents=args.parent_run, params=params, status="done", summary={"files": written["files"]})
    if state is not None:
        try:
            state.data = {"params_sha256": _resume_hash(args, formats), "files": written["files"], "run_id": run_id}
            state.mark_done("figure")
        except OSError:
            pass  # only --resume loses its shortcut
    for file_name in written["files"]:
        jobs.emit("result", kind="figure", path=os.path.join(written["dir"], file_name))
    jobs.emit("done", outputs=[os.path.join(written["dir"], f) for f in written["files"]], elapsed=round(jobs.elapsed(), 3), run_id=run_id)
    return 0


if __name__ == "__main__":
    from LesionNetworkMappingLib import jobs as _jobs

    _jobs.run_job(main, job=JOB)

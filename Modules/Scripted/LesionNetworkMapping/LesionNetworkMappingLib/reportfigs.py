"""Figures of the cohort report (report.html, A-04b): arrangements of L-23 tiles, SVG plots and PNG encoding.

Every pixel comes from the figure renderer (:mod:`figures`, L-23): the display grid (:func:`figures.display_grid`),
resampling through the affines (:func:`figures.reslice`), colour mapping and tile rendering
(:func:`figures.render_slice` with :class:`figures.Layer` and :class:`figures.Scene`), the diverging map of
:mod:`colormaps` and the sequential stops of SPEC §4.12. This module only chooses slices, arranges tiles into
sheets, stamps the slice text and the L/R letters (ASCII: Pillow's default font lacks the >=, minus, +/- and micro signs, so symbols go into
the HTML captions), encodes deterministic quantized PNGs and draws small SVG plots.

A report renders many figures over one template, so a :class:`Canvas` resamples the template once per slice and
each map only on the planes it shows (one-plane grids of the shared display grid), which keeps a 300-lesion
report within its time budget.

Orientation: neurological by default (the left hemisphere on the image left, decision D-A25) with L/R stamps on
every axial and coronal tile; ``convention="radiological"`` mirrors the tiles and swaps the stamps (L-23).

Watermark: a canvas made with ``watermark`` ("TUTORIAL DATA" for tutorial studies, "SYNTHETIC" for synthetic
connectomes: :func:`figures.watermark_for`) burns it into every figure it renders, drawn like the L-23 figure
watermark (centred, grey, 25 % opacity), and records it in the figure's ``meta``.
"""

import base64
import html
import io
import math
import os
import sys
from dataclasses import dataclass, field

import numpy as np

from . import colormaps, figures

TEMPLATE_FILE = "tpl-MNI152NLin2009cAsym_res-02_desc-brain_T1w.nii.gz"
TEMPLATE_SPACE = "MNI152NLin2009cAsym"
#: Default axial slices of a montage (world z, mm): -40 ... +72 in steps of 8 (15 tiles, 5 columns)
DEFAULT_SLICES = tuple(range(-40, 73, 8))
COLUMNS = 5
KINDS = ("diverging", "sequential", "binary", "lesion")
MONTAGE_COLORS = 192
STRIP_COLORS = 128
#: Tiles have one pixel per display-grid voxel (no resampling of the rendered tile); montages are shown at twice
#: that size in the page (CSS pixelated), strips at their own size
MONTAGE_DISPLAY_SCALE = 2
STRIP_DISPLAY_SCALE = 1
FONT_PX = 9
GAP_PX = 2
INK = (255, 255, 255)
SHEET = (0, 0, 0)
NO_TEMPLATE_NOTE = "no template found"


# Template


def template_space(path):
    """Space label of a template file name ("tpl-<space>_..."), or None."""
    name = os.path.basename(os.fspath(path)) if path else ""
    if name.startswith("tpl-"):
        return name[4:].split("_", 1)[0] or None
    return None


def find_template(explicit=None, bundle_info=None):
    """Path of the display template, or None (the figures then have a black background).

    Search order: 1. ``explicit``; 2. the template recorded in the bundle provenance (``bundle_info["template"]``);
    3. every ``sys.path`` entry ending in "qt-scripted-modules", joined with Resources/Data/<TEMPLATE_FILE>
    (installed builds and macOS bundles); 4. Resources/Data next to this library's folder (the same place, found
    without sys.path); 5. the source tree's SampleData/Resources/Data next to this module.
    """
    candidates = []
    if explicit:
        candidates.append(os.fspath(explicit))
    if isinstance(bundle_info, dict):
        recorded = bundle_info.get("template")
        if isinstance(recorded, dict):
            recorded = recorded.get("path")
        if isinstance(recorded, str) and recorded:
            candidates.append(recorded)
    for entry in sys.path:
        if isinstance(entry, str) and entry.rstrip("/\\").endswith("qt-scripted-modules"):
            candidates.append(os.path.join(entry, "Resources", "Data", TEMPLATE_FILE))
    library = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(os.path.dirname(library), "Resources", "Data", TEMPLATE_FILE))
    candidates.append(os.path.join(library, "..", "..", "SampleData", "Resources", "Data", TEMPLATE_FILE))
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.normpath(path)
    return None


# Results


@dataclass
class ReportFigure:
    """A rendered figure: the PIL RGB ``image``, its ``alt`` text and records (``meta``) for the caption."""

    image: object
    alt: str
    meta: dict = field(default_factory=dict)

    def png(self, colors=MONTAGE_COLORS):
        return encode_png(self.image, colors)

    def data_uri(self, colors=MONTAGE_COLORS):
        return data_uri(self.png(colors))


def encode_png(image, colors=MONTAGE_COLORS):
    """Deterministic PNG bytes: median-cut quantization to ``colors`` colours without dithering, optimized."""
    from PIL import Image

    image = image.convert("RGB")
    if colors:
        image = image.quantize(colors=int(colors), method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def data_uri(png_bytes):
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def window_of(values, threshold=None):
    """Symmetric colour window W of map values (L-23: 99.5th percentile of |value|), widened to 1.1 x the
    threshold when the threshold is at or above it (so suprathreshold voxels are not all the end colour).
    """
    window = figures.symmetric_window([values])
    if threshold is not None and math.isfinite(threshold) and threshold >= window:
        window = float(threshold) * figures.WIDEN_FACTOR
    return float(window)


# Canvas


class Canvas:
    """The display grid and template shared by every figure of a report.

    :param template: template path (or Volume), or None for a black background.
    :param reference: volume(s) that set the grid when there is no template.
    :param convention: "neurological" (default, D-A25) or "radiological".
    :param watermark: text burnt into every figure (:func:`figures.watermark_for`), or None.
    """

    def __init__(self, template=None, reference=None, convention=None, watermark=None):
        self.watermark = figures.WATERMARKS.get(str(watermark).lower(), str(watermark)) if watermark else None
        self.convention = convention or figures.DEFAULT_CONVENTION
        if self.convention not in figures.CONVENTIONS:
            raise ValueError(f"convention must be neurological or radiological, got {self.convention!r}")
        self.template = figures.load_volume(template) if template is not None else None
        self.template_path = os.fspath(template) if isinstance(template, (str, os.PathLike)) else None
        if self.template is not None:
            self.grid = figures.display_grid(self.template)
            gray = figures.template_gray(np.asarray(self.template.data, dtype=np.float32))
            self.gray = figures.Volume(gray, self.template.affine, "template")
        else:
            references = [figures.load_volume(item) for item in ([reference] if not isinstance(reference, (list, tuple)) else reference) if item is not None]
            if not references:
                raise ValueError("A canvas needs a template or a reference image")
            self.grid = figures.display_grid(references)
            self.gray = None
        self.fonts = figures.Fonts(None)
        self._background = {}
        self._projection = None

    # One-plane grids of the display grid

    def plane_grid(self, plane, index):
        """The one-plane grid of the display grid at plane ``index`` (same in-plane voxels)."""
        axis = figures.PLANE_AXIS[plane]
        origin = list(self.grid.origin)
        origin[axis] = self.grid.coordinate(axis, index)
        shape = list(self.grid.shape)
        shape[axis] = 1
        return figures.DisplayGrid(tuple(origin), self.grid.spacing, tuple(shape))

    def on_plane(self, volume, plane, index, order=1):
        """``volume`` resampled (L-23) onto the one-plane grid of ``plane`` at ``index``."""
        return figures.reslice(volume, self.plane_grid(plane, index), order=order)

    def background(self, plane, index):
        if self.gray is None:
            return None
        key = (plane, index)
        if key not in self._background:
            self._background[key] = np.clip(self.on_plane(self.gray, plane, index, order=1), 0.0, 1.0)
        return self._background[key]

    def projection(self, volume, gray=False):
        """Axial maximum-|value| projection of ``volume`` (signed value of maximum |value|; the maximum for the
        template) on the one-plane grid at z = the grid origin. Projected along the volume's own axis that is
        parallel to world z (one 2D resampling), or on the full display grid when the volume is oblique.
        """
        data = np.asarray(volume.data, dtype=np.float32)
        affine = np.asarray(volume.affine, dtype=np.float64)
        columns = affine[:3, :3]
        axis = int(np.argmax(np.abs(columns[2])))
        others = [a for a in range(3) if a != axis]
        aligned = abs(columns[2, axis]) > 0 and np.allclose(columns[2, others], 0.0, atol=1e-6) and np.allclose(columns[:2, axis], 0.0, atol=1e-6)
        target = self.plane_grid("axial", 0)
        if not aligned:
            full = figures.reslice(volume, self.grid, order=1)
            values = full.max(axis=2) if gray else figures.section(full, "axial", None)
            return values[:, :, None]
        moved = np.moveaxis(data, axis, 2)
        projected = moved.max(axis=2) if gray else figures.section(np.where(np.isfinite(moved), moved, 0.0), "axial", None)
        stacked = np.moveaxis(np.repeat(projected[:, :, None], 3, axis=2), 2, axis)
        flat = affine.copy()
        flat[2, 3] = target.origin[2] - columns[2, axis]  # index 1 along the projected axis lies on the target plane
        return figures.reslice(figures.Volume(stacked, flat, volume.name), target, order=1)

    def projection_background(self):
        if self.gray is None:
            return None
        if self._projection is None:
            self._projection = np.clip(self.projection(self.gray, gray=True), 0.0, 1.0)
        return self._projection

    # Slices

    def slices(self, volume=None, coordinates=DEFAULT_SLICES):
        """[(z mm, plane index)] of axial ``coordinates`` inside the display grid and inside the world box of
        ``volume`` (when given), one per plane.
        """
        low, high = -math.inf, math.inf
        if volume is not None:
            corners = np.array([[i, j, k, 1.0] for i in (0, volume.data.shape[0] - 1) for j in (0, volume.data.shape[1] - 1) for k in (0, volume.data.shape[2] - 1)])
            z = (np.asarray(volume.affine, dtype=np.float64) @ corners.T)[2]
            low, high = float(z.min()), float(z.max())
        out, used = [], set()
        for z in coordinates:
            if not (low - 1e-6 <= z <= high + 1e-6) or not self.grid.contains(2, z):
                continue
            index = self.grid.index(2, z)
            if index not in used:
                used.add(index)
                out.append((float(z), index))
        if not out and volume is not None:  # a small field of view: its middle plane
            z = 0.5 * (low + high)
            if self.grid.contains(2, z):
                out.append((z, self.grid.index(2, z)))
        return out

    # Tiles

    def _tile_size(self, plane):
        width_axis, height_axis = {"axial": (0, 1), "coronal": (0, 2), "sagittal": (1, 2)}[plane]
        return self.grid.shape[width_axis], self.grid.shape[height_axis]

    def tile(self, layers, plane, index, top_left="", top_right="", crosshair=None, background=None, font_px=FONT_PX):
        """One tile (PIL RGB) of ``layers`` (:class:`figures.Layer` on the one-plane grid) with its stamps."""
        grid = self.plane_grid(plane, index)
        back = self.background(plane, index) if background is None else background
        scene = figures.Scene(grid, back, list(layers))
        size = self._tile_size(plane)
        image = figures.render_slice(scene, plane, 0, size, self.convention, crosshair)
        self.stamp(image, plane, top_left, top_right, font_px)
        return image

    def stamp(self, image, plane, top_left="", top_right="", font_px=FONT_PX):
        """Burn the side letters (L-23 :func:`figures.side_labels`) and the corner texts (ASCII) into a tile."""
        from PIL import ImageDraw

        draw = ImageDraw.Draw(image)
        font = self.fonts.get(font_px, bold=True)
        pad = 2
        left, right = figures.side_labels(plane, self.convention)
        width, height = image.size
        draw.text((pad, height - pad), self.fonts.text(left), fill=INK, font=font, anchor="lb")
        draw.text((width - pad, height - pad), self.fonts.text(right), fill=INK, font=font, anchor="rb")
        small = self.fonts.get(max(font_px - 1, 6))
        if top_left:
            draw.text((pad, pad), self.fonts.text(top_left), fill=INK, font=small, anchor="lt")
        if top_right:
            draw.text((width - pad, pad), self.fonts.text(top_right), fill=INK, font=small, anchor="rt")

    def finish(self, image, alt, meta):
        """The :class:`ReportFigure` of a sheet, with the canvas watermark burnt in (as L-23 draws it: centred,
        :data:`figures.WATERMARK_COLOR` at :data:`figures.WATERMARK_OPACITY`, bold, sized to the sheet) and recorded in ``meta["watermark"]``.
        """
        meta["watermark"] = self.watermark
        if self.watermark:
            from PIL import Image, ImageDraw

            width, height = image.size
            size = min(width / max(len(self.watermark), 1) * 1.2, height * 0.3)
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
            ImageDraw.Draw(overlay).text((width / 2, height / 2), self.fonts.text(self.watermark), font=self.fonts.get(size, bold=True), fill=figures._rgba(figures.WATERMARK_COLOR, figures.WATERMARK_OPACITY), anchor="mm")
            image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        return ReportFigure(image, alt, meta)

    # Layers

    def layers(self, kind, values, threshold=None, window=None, sign="both", vmax=None, color=None, mode="hard"):
        """The L-23 layer of one map on a plane: ``kind`` diverging (thresholded in ``mode``), sequential (``vmax``:
        integer bins 1..vmax when it is an int, else a 0..vmax ramp), binary or lesion (filled in ``color``).
        """
        if kind == "diverging":
            return figures.Layer(values, "diverging", window or 1.0, threshold, mode if threshold is not None else "unthresholded", sign)
        if kind == "sequential":
            discrete = isinstance(vmax, (int, np.integer))
            return figures.Layer(values, "sequential", vmin=0.0, vmax=float(vmax if vmax else 1.0), discrete=discrete)
        if kind in ("binary", "lesion"):
            return figures.Layer(values, "binary", color=color or figures.LESION_COLOR, opacity=1.0 if kind == "binary" else 0.9)
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")

    # Figures

    def montage(self, map, kind, threshold=None, sign="both", window=None, slices=None, columns=COLUMNS, companion=None, vmax=None, color=None, overlays=(), alt=""):  # noqa: A002
        """Axial montage of one map over the template.

        :param kind: diverging | sequential | binary | lesion.
        :param threshold: hard threshold of a diverging map (|value| >= threshold, or by ``sign``); None = unthresholded.
        :param slices: world z coordinates (default :data:`DEFAULT_SLICES`, clipped to the map's field of view).
        :param companion: add an unthresholded row of the middle slices (default: for thresholded diverging maps).
        :param overlays: extra (map, kind, color) layers drawn on the same tiles (e.g. the negative half of a
          binary sensitivity map).
        :return: :class:`ReportFigure` (meta: kind, window, threshold, slices, vmax).
        """
        volume = figures.load_volume(map)
        values = np.asarray(volume.data, dtype=np.float64)
        order = 0 if kind in ("binary", "lesion") or (kind == "sequential" and isinstance(vmax, (int, np.integer))) else 1
        if kind == "diverging" and window is None:
            window = window_of(values, threshold)
        extra = [(figures.load_volume(item[0]), item[1], item[2] if len(item) > 2 else None) for item in overlays]
        planes = self.slices(volume, DEFAULT_SLICES if slices is None else slices)
        if companion is None:
            companion = kind == "diverging" and threshold is not None
        tiles = []
        for z, index in planes:
            layers = [self.layers(kind, self.on_plane(volume, "axial", index, order), threshold, window, sign, vmax, color)]
            for other, other_kind, other_color in extra:
                layers.append(self.layers(other_kind, self.on_plane(other, "axial", index, 0 if other_kind in ("binary", "lesion") else 1), threshold, window, sign, vmax, other_color))
            tiles.append(self.tile(layers, "axial", index, top_left=f"z={_signed(z)}"))
        rows = [tiles[start:start + columns] for start in range(0, len(tiles), columns)]
        companion_tiles = []
        if companion and planes:
            middle = planes[max(0, (len(planes) - columns) // 2):][:columns]
            for z, index in middle:
                layer = self.layers("diverging", self.on_plane(volume, "axial", index, 1), None, window, sign)
                companion_tiles.append(self.tile([layer], "axial", index, top_left=f"z={_signed(z)}", top_right="unthr."))
        image = _sheet(rows, [companion_tiles] if companion_tiles else [])
        meta = {"kind": kind, "window": window, "threshold": threshold, "sign": sign, "slices": [z for z, _ in planes], "companion": bool(companion_tiles), "vmax": vmax, "convention": self.convention, "display_scale": MONTAGE_DISPLAY_SCALE}
        return self.finish(image, alt or f"Axial montage of {volume.name}", meta)

    def ortho(self, map, point, kind="binary", threshold=None, sign="both", window=None, color=None, vmax=None, alt=""):  # noqa: A002
        """Sagittal, coronal and axial tiles through ``point`` (world mm) with a 1 px crosshair (L-23)."""
        volume = figures.load_volume(map)
        order = 0 if kind in ("binary", "lesion") else 1
        if kind == "diverging" and window is None:
            window = window_of(np.asarray(volume.data, dtype=np.float64), threshold)
        point = tuple(float(value) for value in point)
        tiles = []
        for plane in figures.PLANES:
            axis = figures.PLANE_AXIS[plane]
            index = self.grid.index(axis, point[axis])
            coordinate = self.grid.coordinate(axis, index)
            label = f"{'xyz'[axis]}={_signed(coordinate)}" + ((" (L)" if coordinate < 0 else " (R)") if plane == "sagittal" and coordinate else "")
            layer = self.layers(kind, self.on_plane(volume, plane, index, order), threshold, window, sign, vmax, color)
            tiles.append(self.tile([layer], plane, index, top_left=label, crosshair=point))
        meta = {"kind": kind, "point": list(point), "window": window, "threshold": threshold, "convention": self.convention, "display_scale": MONTAGE_DISPLAY_SCALE}
        return self.finish(_sheet([tiles]), alt or f"Orthogonal views of {volume.name} through ({', '.join(_signed(c) for c in point)}) mm", meta)

    def gallery_strip(self, lesion, t_map, threshold, window, label="", binarize_at=0.5):
        """Three tiles of one lesion (one pixel per display-grid voxel): the lesion fill on the axial slice through its centroid;
        the map on that slice with a 1 px white lesion outline; the axial maximum-|map| projection over the template's
        maximum projection. ``t_map`` may be None (the map tiles are then left out).
        """
        lesion = figures.load_volume(lesion)
        occupancy = np.asarray(lesion.data, dtype=np.float64)
        voxels = np.argwhere(np.nan_to_num(occupancy) >= binarize_at)
        if voxels.size:
            centroid = np.asarray(lesion.affine, dtype=np.float64) @ np.append(voxels.mean(axis=0), 1.0)
            z = float(centroid[2])
        else:
            centroid, z = None, 0.0
        index = self.grid.index(2, z)
        z = self.grid.coordinate(2, index)
        on_plane = self.on_plane(lesion, "axial", index, order=0) >= binarize_at
        fill = figures.Layer(on_plane.astype(np.float32), "binary", color=figures.LESION_COLOR, opacity=0.9)
        tiles = [self.tile([fill], "axial", index, top_left=f"z={_signed(z)}", top_right=str(label))]
        if t_map is not None:
            volume = figures.load_volume(t_map)
            values = self.on_plane(volume, "axial", index, order=1)
            outline = figures.Layer(on_plane.astype(np.float32), "outline", color=_hex(INK))
            tiles.append(self.tile([figures.Layer(values, "diverging", window, threshold, "hard"), outline], "axial", index))
            projected = self.projection(volume)
            layer = figures.Layer(projected, "diverging", window, threshold, "hard")
            tiles.append(self.tile([layer], "axial", 0, top_left="max |T|", background=self.projection_background()))
        meta = {"z": z, "centroid": None if centroid is None else [float(c) for c in centroid[:3]], "empty": not voxels.size, "convention": self.convention, "display_scale": STRIP_DISPLAY_SCALE}
        return self.finish(_sheet([tiles]), f"Lesion {label}: lesion at z={_signed(z)} mm, its network map on that slice and the maximum projection", meta)


def _hex(rgb):
    return "#" + "".join(f"{int(c):02X}" for c in rgb)


def _float_hex(rgb):
    return _hex(round(max(0.0, min(1.0, float(c))) * 255) for c in rgb)


def _signed(value):
    value = round(float(value), 1)
    return f"{value:+g}" if value else "0"


def _sheet(rows, extra_rows=()):
    """Paste rows of tiles onto one black sheet (rows left-aligned, GAP_PX apart; extra rows after a wider gap)."""
    from PIL import Image

    groups = [list(rows), list(extra_rows)]
    width = max((sum(tile.width for tile in row) + GAP_PX * (len(row) - 1) for group in groups for row in group if row), default=1)
    heights = [[max(tile.height for tile in row) for row in group if row] for group in groups]
    height = sum(sum(h) + GAP_PX * max(len(h) - 1, 0) for h in heights if h) + (3 * GAP_PX if heights[1] and heights[0] else 0)
    sheet = Image.new("RGB", (width, max(height, 1)), SHEET)
    y = 0
    for number, group in enumerate(groups):
        if number and group and any(group) and y:
            y += 2 * GAP_PX
        for row in group:
            if not row:
                continue
            x = 0
            for tile in row:
                sheet.paste(tile, (x, y))
                x += tile.width + GAP_PX
            y += max(tile.height for tile in row) + GAP_PX
    return sheet


# Colour bars and plots (SVG strings; colours from colormaps/figures, page colours by CSS class)


def _attr(text):
    return html.escape(str(text), quote=True)


def colorbar_svg(kind, low, high, label, threshold=None, width=300, height=34, vmax=None):
    """Horizontal colour bar: ``kind`` diverging (lnm_diverging over [low, high], the band |value| < threshold
    hatched as hidden), sequential (the SPEC §4.12 stops; integer bins when ``vmax`` is an int) or binary.
    """
    stops = []
    if kind == "diverging":
        lut = figures.diverging_lut()
        n = len(lut)
        for index in range(0, n, max(1, n // 32)):
            stops.append((index / (n - 1), lut[index]))
        stops.append((1.0, lut[-1]))
    elif kind == "sequential":
        if isinstance(vmax, (int, np.integer)) and 0 < vmax <= 64:
            colours = [figures.count_color(count, vmax) for count in range(1, int(vmax) + 1)]
            stops = [(index / max(len(colours) - 1, 1), colour) for index, colour in enumerate(colours)]
        else:
            for t in np.linspace(0.0, 1.0, 9):
                stops.append((float(t), figures.sequential_colors(t)))
    bar_h = 12
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" class="cbar" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{_attr("colour bar: " + label)}">']
    if stops:
        parts.append('<defs><linearGradient id="g" x1="0" x2="1" y1="0" y2="0">')
        parts += [f'<stop offset="{offset:.4f}" stop-color="{_float_hex(colour)}"/>' for offset, colour in stops]
        parts.append("</linearGradient></defs>")
        parts.append(f'<rect x="10" y="2" width="{width - 20}" height="{bar_h}" fill="url(#g)"/>')
    if kind == "diverging" and threshold is not None and high > low and threshold < high:
        x0 = 10 + (width - 20) * (-threshold - low) / (high - low)
        x1 = 10 + (width - 20) * (threshold - low) / (high - low)
        parts.append(f'<rect class="hidden-band" x="{x0:.1f}" y="2" width="{max(x1 - x0, 0):.1f}" height="{bar_h}"/>')
    parts.append(f'<text class="tick" x="10" y="{bar_h + 13}" text-anchor="start">{_attr(_number(low))}</text>')
    parts.append(f'<text class="tick" x="{width - 10}" y="{bar_h + 13}" text-anchor="end">{_attr(_number(high))}</text>')
    if kind == "diverging" and threshold is not None:
        parts.append(f'<text class="tick" x="{width / 2:.0f}" y="{bar_h + 13}" text-anchor="middle">{_attr("hidden below |" + _number(threshold) + "|")}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _number(value):
    value = float(value)
    if value == 0:
        return "0"
    if abs(value) >= 100 or float(value).is_integer():
        return f"{value:.0f}"
    return f"{value:.3g}"


def dot_plot_svg(values, bands=(), labels=None, title="", xmin=None, xmax=None, unit="", width=560, height=96, seed=7):
    """A jittered dot plot (one dot per value, fixed jitter ``seed``) with shaded ``bands`` [(low, high, css class)];
    ``labels`` give each dot a <title> (hover text without scripts).
    """
    finite = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    lo = min(finite + [b[0] for b in bands]) if xmin is None and (finite or bands) else (xmin if xmin is not None else 0.0)
    hi = max(finite + [b[1] for b in bands]) if xmax is None and (finite or bands) else (xmax if xmax is not None else 1.0)
    if hi <= lo:
        hi = lo + 1.0
    left, right, top, axis_y = 36, 14, 8, height - 26

    def x_of(v):
        return left + (width - left - right) * (float(v) - lo) / (hi - lo)

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" class="plot" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{_attr(title)}">']
    for low, high, css in bands:
        a, b = x_of(max(low, lo)), x_of(min(high, hi))
        parts.append(f'<rect class="{_attr(css)}" x="{a:.1f}" y="{top}" width="{max(b - a, 0):.1f}" height="{axis_y - top}"/>')
    parts.append(f'<line class="axis" x1="{left}" y1="{axis_y}" x2="{width - right}" y2="{axis_y}"/>')
    for t in np.linspace(lo, hi, 5):
        parts.append(f'<line class="axis" x1="{x_of(t):.1f}" y1="{axis_y}" x2="{x_of(t):.1f}" y2="{axis_y + 4}"/>')
        parts.append(f'<text class="tick" x="{x_of(t):.1f}" y="{axis_y + 15}" text-anchor="middle">{_attr(_number(t))}</text>')
    if unit:
        parts.append(f'<text class="tick" x="{width - right}" y="{height - 1}" text-anchor="end">{_attr(unit)}</text>')
    jitter = np.random.default_rng(seed).uniform(-1.0, 1.0, len(values)) if len(values) else []
    middle = (top + axis_y) / 2
    spread = (axis_y - top) / 2 - 5
    for number, value in enumerate(values):
        if value is None or not math.isfinite(float(value)):
            continue
        tip = f"<title>{_attr(labels[number])}</title>" if labels else ""
        parts.append(f'<circle class="dot" cx="{x_of(value):.1f}" cy="{middle + spread * jitter[number]:.1f}" r="3">{tip}</circle>')
    parts.append("</svg>")
    return "".join(parts)


def percentile_strip_svg(percentile, k=None, title="", width=120, height=14, low=5.0, high=95.0):
    """A 0-100 strip with the < low and > high bands and a mark at ``percentile`` (QA chips)."""
    label = title or (f"percentile {percentile:.0f}" if percentile is not None else "no percentile")
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" class="pstrip" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{_attr(label)}">']
    scale = (width - 2) / 100.0
    parts.append(f'<rect class="band-low" x="1" y="3" width="{low * scale:.1f}" height="{height - 6}"/>')
    parts.append(f'<rect class="band-amber" x="{1 + low * scale:.1f}" y="3" width="{(high - low) * scale:.1f}" height="{height - 6}"/>')
    parts.append(f'<rect class="band-red" x="{1 + high * scale:.1f}" y="3" width="{(100 - high) * scale:.1f}" height="{height - 6}"/>')
    if percentile is not None and math.isfinite(percentile):
        x = 1 + float(percentile) * scale
        parts.append(f'<line class="mark" x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{height}"/>')
    parts.append("</svg>")
    return "".join(parts)


def colormap_note():
    """Colour sources (for captions): the diverging map's ends and the sequential stops."""
    return {"negative": colormaps.NEGATIVE_COLORS, "positive": colormaps.POSITIVE_COLORS, "sequential": figures.SEQUENTIAL_COLORS, "lesion": figures.LESION_COLOR}


def montage(map, kind, threshold_or_rule=None, template=None, convention=None, slices="auto", watermark=None, **kwargs):  # noqa: A002
    """One montage on its own canvas (see :meth:`Canvas.montage`); ``threshold_or_rule`` is a number or a rule
    dict resolved by :mod:`thresholds` with ``n`` and ``v`` (kwargs) for diverging maps.
    """
    threshold = threshold_or_rule
    n, v = kwargs.pop("n", None), kwargs.pop("v", None)
    if isinstance(threshold_or_rule, dict):
        from . import thresholds

        threshold = thresholds.resolve(threshold_or_rule, int(n), int(v)).value
    canvas = Canvas(template, reference=map, convention=convention, watermark=watermark)
    return canvas.montage(map, kind, threshold=threshold, slices=None if slices == "auto" else slices, **kwargs)


def ortho(map, world_xyz, template=None, convention=None, watermark=None, **kwargs):  # noqa: A002
    return Canvas(template, reference=map, convention=convention, watermark=watermark).ortho(map, world_xyz, **kwargs)


def gallery_strip(lesion_path_or_occupancy, t_map, threshold, window, template=None, convention=None, label="", watermark=None):
    return Canvas(template, reference=lesion_path_or_occupancy, convention=convention, watermark=watermark).gallery_strip(lesion_path_or_occupancy, t_map, threshold, window, label)

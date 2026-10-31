"""Tests of the headless publication figure renderer (figures.py; L-23, decision D-A25).

Synthetic volumes with known answers: a left-hemisphere lesion lands on the L-stamped side in the
neurological convention and on the image right in the radiological one (also when the volume is
stored left-to-right flipped); exact overlap bins; captions with rule, N, df and orientation; one
shared colour range across panels; transparent-threshold alpha; output files that reopen (PNG/TIFF
with Pillow, SVG as XML, PDF header); deterministic PNG bytes; the command line and its run record.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingFiguresTest.py -v
"""

import contextlib
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

from LesionNetworkMappingLib import figures, jobs, nifti, runs

SHAPE = (30, 36, 30)
SPACING = 2.0
SMALL = {"width_mm": 89.0, "dpi": 150}


def ras_affine(shape=SHAPE, spacing=SPACING):
    affine = np.diag([spacing, spacing, spacing, 1.0])
    affine[:3, 3] = [-spacing * (n - 1) / 2.0 for n in shape]
    return affine


def world(shape=SHAPE, affine=None):
    affine = ras_affine(shape) if affine is None else affine
    ijk = np.indices(shape).reshape(3, -1)
    return (affine[:3, :3] @ ijk + affine[:3, 3:4]).T.reshape(*shape, 3)


def ball(center, radius, affine=None):
    return (np.linalg.norm(world(SHAPE, affine) - np.asarray(center, dtype=float), axis=-1) <= radius).astype(np.float32)


def brain(affine=None):
    """An ellipsoidal 'template' (non-zero inside)."""
    w = world(SHAPE, affine)
    inside = (w[..., 0] / 26) ** 2 + (w[..., 1] / 32) ** 2 + (w[..., 2] / 26) ** 2 <= 1
    return np.where(inside, 50.0 + 0.5 * w[..., 1], 0.0).astype(np.float32)


def blob_map(center=(-16, 0, 0), amplitude=10.0, affine=None):
    w = world(SHAPE, affine)
    values = amplitude * np.exp(-np.sum((w - np.asarray(center)) ** 2, axis=-1) / (2 * 6.0**2))
    values -= 0.6 * amplitude * np.exp(-np.sum((w - np.array([14.0, 10.0, 4.0])) ** 2, axis=-1) / (2 * 5.0**2))
    return (values * (brain(affine) > 0)).astype(np.float32)


def colored_pixels(image, bbox):
    """Boolean mask of the saturated (overlay) pixels inside ``bbox`` of an RGB array."""
    x0, y0, x1, y1 = bbox
    tile = np.asarray(image, dtype=np.int16)[y0:y1, x0:x1]
    spread = tile.max(axis=2) - tile.min(axis=2)
    return spread > 60


def stamp_x(figure, tile, text):
    x0, _, x1, _ = tile["bbox"]
    for item in figure.annotations:
        if item.role == "stamp" and item.text == text and x0 - 1 <= item.x <= x1 + 1:
            return item.x
    raise AssertionError(f"No {text} stamp in tile {tile['bbox']}")


@unittest.skipIf(Image is None, "Pillow is not available")
class FiguresTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-figures-")
        self.affine = ras_affine()
        self.template = (brain(), self.affine, "template")
        self.tmap = (blob_map(), self.affine, "meanT")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Orientation

    def _lesion_side(self, figure, tile):
        mask = colored_pixels(figure.base, tile["bbox"])
        self.assertGreater(mask.sum(), 10, "the lesion is not visible")
        columns = np.nonzero(mask)[1] + tile["bbox"][0]
        centroid = columns.mean()
        left, right = stamp_x(figure, tile, "L"), stamp_x(figure, tile, "R")
        return "L" if abs(centroid - left) < abs(centroid - right) else "R", centroid, (left, right)

    def test_left_lesion_on_L_side_neurological_and_right_in_radiological(self):
        for flipped in (False, True):
            affine = self.affine.copy()
            data = ball((-18, 4, 2), 6)
            if flipped:  # stored left-to-right reversed (an LAS file): the affine, not the index, decides
                affine[0, 0] = -SPACING
                affine[0, 3] = SPACING * (SHAPE[0] - 1) / 2.0
                data = data[::-1].copy()
            lesion = (data, affine, "left-lesion")
            for convention in ("neurological", "radiological"):
                with self.subTest(flipped=flipped, convention=convention):
                    pages = figures.gallery([lesion], self.template, convention=convention, **SMALL)
                    figure = pages[0]
                    tile = figure.meta["tiles"][0]
                    side, centroid, (left, right) = self._lesion_side(figure, tile)
                    self.assertEqual(side, "L")
                    middle = (tile["bbox"][0] + tile["bbox"][2]) / 2
                    if convention == "neurological":
                        self.assertLess(centroid, middle)
                        self.assertLess(left, right)
                    else:
                        self.assertGreater(centroid, middle)
                        self.assertGreater(left, right)
                    self.assertIn(f"{convention.capitalize()} convention", figure.caption)

    def test_every_tile_has_stamps(self):
        figure = figures.ortho(self.tmap, None, self.template, rule={"kind": "custom_t", "value": 4.0}, n=100, **SMALL)
        stamps = [item for item in figure.annotations if item.role == "stamp"]
        self.assertEqual(len(stamps), 2 * len(figure.meta["tiles"]))
        planes = {tile["plane"]: tile["stamps"] for tile in figure.meta["tiles"]}
        self.assertEqual(planes["axial"], {"left": "L", "right": "R"})
        self.assertEqual(planes["sagittal"], {"left": "P", "right": "A"})
        mirrored = figures.ortho(self.tmap, None, self.template, convention="radiological", **SMALL)
        planes = {tile["plane"]: tile["stamps"] for tile in mirrored.meta["tiles"]}
        self.assertEqual(planes["coronal"], {"left": "R", "right": "L"})
        # The peak of the map (x = -16) is chosen as the point
        self.assertAlmostEqual(figure.meta["point_mm"][0], -16.0, delta=SPACING)

    def test_reslice_uses_affines(self):
        data = blob_map()
        flipped = self.affine.copy()
        flipped[0, 0] = -SPACING
        flipped[0, 3] = SPACING * (SHAPE[0] - 1) / 2.0
        grid = figures.display_grid(figures.load_volume((data, self.affine)), crop=False, margin_mm=0)
        a = figures.reslice(figures.load_volume((data, self.affine)), grid, order=1)
        b = figures.reslice(figures.load_volume((data[::-1].copy(), flipped)), grid, order=1)
        np.testing.assert_allclose(a, b, atol=1e-5)
        aligned = figures.DisplayGrid(tuple(self.affine[:3, 3]), SPACING, SHAPE)  # the data's own lattice: an identity resampling
        np.testing.assert_allclose(figures.reslice(figures.load_volume((data[::-1].copy(), flipped)), aligned, order=1), data, atol=1e-5)
        np.testing.assert_array_equal(figures.reslice(figures.load_volume((data, self.affine)), aligned, order=0), data)

    # Overlap

    def test_overlap_bins_exact(self):
        lesions = [
            (ball((-10, 0, 0), 8), self.affine, "a"),
            (ball((-4, 0, 0), 8), self.affine, "b"),
            (ball((2, 0, 0), 8), self.affine, "c"),
        ]
        count = sum((lesion[0] >= 0.5).astype(int) for lesion in lesions)
        values, counts = np.unique(count[count > 0], return_counts=True)
        figure = figures.overlap(lesions=lesions, slices="A 0", **SMALL)
        self.assertEqual(figure.meta["max_count"], 3)
        self.assertEqual(figure.meta["n_lesions"], 3)
        self.assertEqual(figure.meta["legend"], "max 3/3 (100%)")
        self.assertEqual(figure.meta["bins"], {int(v): int(c) for v, c in zip(values, counts, strict=False)})
        histogram = {row["lesions"]: row["voxels"] for row in figure.sourcedata if row["kind"] == "histogram"}
        self.assertEqual(histogram, {int(v): int(c) for v, c in zip(values, counts, strict=False)})
        self.assertIn("maximum 3 of 3 lesions (100%)", figure.caption)
        # Rendered pixels are exactly the bin colours (no interpolation between bins) or the background
        tile = figure.meta["tiles"][0]
        x0, y0, x1, y1 = tile["bbox"]
        pixels = {tuple(p) for p in np.asarray(figure.base)[y0:y1, x0:x1].reshape(-1, 3)}
        allowed = {tuple(round(c * 255) for c in figures.count_color(k, 3)) for k in (1, 2, 3)} | {(0, 0, 0)}
        self.assertTrue(pixels <= allowed, pixels - allowed)
        self.assertEqual(pixels - {(0, 0, 0)}, allowed - {(0, 0, 0)})

    def test_overlap_from_count_map(self):
        count = np.zeros(SHAPE, dtype=np.int16)
        count[10:14, 15:20, 14:16] = 2
        count[11:12, 16:17, 15] = 5
        figure = figures.overlap(count=(count, self.affine, "count"), n_lesions=10, slices="A 0 2", **SMALL)
        self.assertEqual(figure.meta["max_count"], 5)
        self.assertEqual(figure.meta["legend"], "max 5/10 (50%)")
        self.assertEqual(figure.meta["bins"], {2: 39, 5: 1})

    # Captions and thresholds

    def test_caption_has_rule_n_df_and_orientation(self):
        figure = figures.montage(self.tmap, self.template, "A -4 0 4", rule={"kind": "fwe_bonferroni", "alpha": 0.05}, n=1000, connectome="GSP1000", **SMALL)
        caption = figure.caption
        for text in ("FWE (Bonferroni)", "\u03b1 = 0.05", "N = 1000", "df = 999", "Neurological convention", "z = -4, 0, +4 mm", "Connectome: GSP1000", "p_unc ≤", "unthresholded companion"):
            self.assertIn(text, caption)
        resolved = figure.meta["thresholds"]["meanT"]
        self.assertEqual(resolved["df"], 999)
        self.assertIn(f"{resolved['value']:.2f}", caption)
        # Thresholded panel plus its unthresholded companion by default
        self.assertEqual([panel["mode"] for panel in figure.meta["panels"]], ["hard", "unthresholded"])
        no_companion = figures.montage(self.tmap, self.template, "A 0", rule={"kind": "custom_t", "value": 5}, n=50, companion=False, **SMALL)
        self.assertEqual([panel["mode"] for panel in no_companion.meta["panels"]], ["hard"])

    def test_caption_without_n(self):
        figure = figures.montage(self.tmap, None, "A 0", threshold=4.0, **SMALL)
        self.assertIn("N unknown", figure.caption)
        with self.assertRaises(ValueError):
            figures.montage(self.tmap, None, "A 0", rule={"kind": "fwe_bonferroni"}, **SMALL)

    def test_shared_range_identical_across_panels(self):
        strong = (blob_map(amplitude=20.0), self.affine, "strong")
        weak = (blob_map(amplitude=5.0), self.affine, "weak")
        conj = (np.minimum(blob_map(amplitude=20.0), blob_map(amplitude=5.0)), self.affine, "conj")
        figure = figures.triptych(strong, weak, conj, self.template, rule={"kind": "custom_t", "value": 3.0}, n=100, **SMALL)
        windows = {panel["window"] for panel in figure.meta["panels"]}
        self.assertEqual(len(figure.meta["panels"]), 6)
        self.assertEqual(len(windows), 1)
        window = windows.pop()
        self.assertEqual(figure.meta["window"], window)
        self.assertEqual(figure.meta["colorbars"][0]["window"], window)
        self.assertGreater(window, 2 * figures.symmetric_window([weak[0]]))  # one range for all, not per panel
        self.assertEqual({row["window"] for row in figure.sourcedata}, {window})
        explicit = figures.replication(strong, weak, self.template, "A 0", window=7.5, **SMALL)
        self.assertEqual({panel["window"] for panel in explicit.meta["panels"]}, {7.5})
        self.assertAlmostEqual(explicit.meta["spatial_r"], figures.spatial_r(strong[0], weak[0])[0], places=10)
        self.assertIn("spatial correlation r = ", explicit.caption)

    def test_grid_covers_every_input_without_template(self):
        # Two lesions far apart, the second on a shifted grid: none may fall outside the display grid
        shifted = self.affine.copy()
        shifted[0, 3] += 1.0
        a = (ball((-20, 0, 0), 5), self.affine, "a")
        b = (ball((20, 20, 16), 5, shifted), shifted, "b")
        pages = figures.gallery([a, b], None, **SMALL)
        areas = {tile["case"]: tile["area_voxels"] for tile in pages[0].meta["tiles"]}
        self.assertGreater(areas["1"], 0)
        self.assertGreater(areas["2"], 0)
        figure = figures.overlap(lesions=[a, b], **SMALL)
        voxels = sum(int((v[0] >= 0.5).sum()) for v in (a, b))
        self.assertEqual(figure.meta["max_count"], 1)
        self.assertAlmostEqual(figure.meta["bins"][1], voxels, delta=0.1 * voxels)  # display grid (nearest)
        far = (blob_map(center=(16, 0, 0)), self.affine, "far")
        near = (blob_map(center=(-16, 0, 0)) * (world()[..., 0] < 0), self.affine, "near")
        grid = figures.triptych(near, far, near, None, "A 0", **SMALL).meta["grid"]
        self.assertGreater(grid["origin"][0] + grid["spacing"] * (grid["shape"][0] - 1), 16)

    def test_same_named_maps_keep_their_own_records(self):
        positive = ((10.0 * ball((-14, 0, 0), 8)), self.affine, "T")
        negative = ((-10.0 * ball((14, 0, 0), 8)), self.affine, "T")
        figure = figures.replication(positive, negative, self.template, "A 0", **SMALL)
        rows = {row["panel"]: row for row in figure.sourcedata if row["panel"]}
        # Linear reslicing blends the ball edges, but each panel keeps its own map's sign
        self.assertTrue(rows["A"]["min"] > 0 and rows["A"]["max"] == 10.0, rows["A"])
        self.assertTrue(rows["B"]["max"] < 0 and rows["B"]["min"] == -10.0, rows["B"])
        self.assertEqual(sorted(figure.meta["thresholds"]), ["discovery", "replication"])
        third = ((5.0 * ball((0, 10, 0), 6)), self.affine, "T")
        figure = figures.triptych(positive, negative, third, self.template, "A 0", threshold=3.0, **SMALL)
        self.assertEqual(len(set(figure.meta["thresholds"])), 3)
        extremes = {row["key"]: (row["min"], row["max"]) for row in figure.sourcedata if row["mode"] == "hard"}
        self.assertEqual(extremes["sensitivity"][1], 10.0)
        self.assertEqual(extremes["specificity"][0], -10.0)
        self.assertEqual(extremes["conjunction"][1], 5.0)

    def test_sparse_threshold_widens_window_and_keeps_tick(self):
        # Fewer than 0.5% of the non-zero voxels pass: the 99.5th percentile window is below the threshold
        values = np.where(brain() > 0, 1.0, 0.0).astype(np.float32)
        values[14, 18, 15] = 9.0
        values[15, 18, 15] = 8.0
        sparse = (values, self.affine, "sparse")
        self.assertLess(figures.symmetric_window([values]), 5.0)
        figure = figures.montage(sparse, self.template, "A 0", threshold=5.0, **SMALL)
        self.assertGreater(figure.meta["window"], 5.0)
        self.assertIn("colour range was widened", figure.caption)
        ticks = [item for item in figure.annotations if item.role == "threshold tick"]
        self.assertEqual(len(ticks), 2)
        # A user window below the threshold keeps the tick, at the bar end
        explicit = figures.montage(sparse, self.template, "A 0", threshold=5.0, window=2.0, **SMALL)
        self.assertEqual(explicit.meta["window"], 2.0)
        labels = [item.text for item in explicit.annotations if item.role == "threshold value"]
        self.assertEqual(len(labels), 2)
        self.assertTrue(all(label[0] in "≤≥" for label in labels), labels)

    def test_glass_projection_follows_sign(self):
        values = np.zeros(SHAPE, dtype=np.float32)
        values[6:10, 16:21, 12:17] = 6.0  # on the same sagittal (left-right) rays as a stronger negative block
        values[19:23, 16:21, 12:17] = -9.0
        projected = figures.section(values, "sagittal", None, "pos")
        self.assertEqual(projected.max(), 6.0)
        self.assertEqual(figures.section(values, "sagittal", None).max(), 0.0)  # signed max |value| hides it
        figure = figures.glass((values, self.affine, "g"), template=self.template, threshold=5.0, sign="pos", **SMALL)
        sagittal = next(row for row in figure.sourcedata if row["plane"] == "sagittal" and row["mode"] == "hard")
        self.assertGreater(sagittal["voxels_suprathreshold"], 0)

    def test_display_modes(self):
        values = np.array([0.0, 1.0, 2.0, -2.0, 4.0, -5.0])
        _, alpha, supra = figures.diverging_rgba(values, 5.0, 4.0, "transparent")
        np.testing.assert_allclose(alpha, [0.0, 1 / 16, 0.25, 0.25, 1.0, 1.0])
        np.testing.assert_array_equal(supra, [False, False, False, False, True, True])
        _, alpha, _ = figures.diverging_rgba(values, 5.0, 4.0, "hard")
        np.testing.assert_array_equal(alpha, [0, 0, 0, 0, 1, 1])
        _, alpha, _ = figures.diverging_rgba(values, 5.0, 4.0, "hard", sign="pos")
        np.testing.assert_array_equal(alpha, [0, 0, 0, 0, 1, 0])
        _, alpha, _ = figures.diverging_rgba(values, 5.0, 4.0, "unthresholded")
        np.testing.assert_array_equal(alpha, [0, 1, 1, 1, 1, 1])
        rgb, _, _ = figures.diverging_rgba(np.array([-5.0, 5.0]), 5.0)
        np.testing.assert_allclose(rgb[0], figures.colormaps.hex_to_rgb("#7FE3F0"), atol=1e-9)
        np.testing.assert_allclose(rgb[1], figures.colormaps.hex_to_rgb("#FDDB54"), atol=1e-9)
        transparent = figures.montage(self.tmap, self.template, "A 0", mode="transparent", rule={"kind": "custom_t", "value": 6.0}, n=100, **SMALL)
        self.assertIn("transparent thresholding", transparent.caption)
        # The 1 px contour of suprathreshold voxels is black
        tile = transparent.meta["tiles"][0]
        x0, y0, x1, y1 = tile["bbox"]
        self.assertTrue((np.asarray(transparent.base)[y0:y1, x0:x1] == 0).all(axis=2).any())

    def test_parse_slices(self):
        self.assertEqual(figures.parse_slices("A -20 -10 0 10 20 30"), ("axial", [-20.0, -10.0, 0.0, 10.0, 20.0, 30.0], None))
        self.assertEqual(figures.parse_slices("C 0"), ("coronal", [0.0], None))
        self.assertEqual(figures.parse_slices("x=-40,40"), ("sagittal", [-40.0, 40.0], None))
        self.assertEqual(figures.parse_slices("auto"), ("axial", None, None))
        self.assertEqual(figures.parse_slices("S auto 4"), ("sagittal", None, 4))
        with self.assertRaises(ValueError):
            figures.parse_slices("A ten")
        figure = figures.montage(self.tmap, self.template, "A auto 3", rule={"kind": "custom_t", "value": 6.0}, n=100, **SMALL)
        coordinates = [item["coordinate_mm"] for item in figure.meta["slices"]]
        self.assertEqual(len(coordinates), 3)
        self.assertEqual(coordinates, sorted(coordinates))
        self.assertTrue(any(abs(c) <= 2 for c in coordinates), coordinates)  # the peak plane z = 0

    # Outputs

    def test_files_open_and_records(self):
        lesions = [(ball((-10, 0, 0), 6), self.affine, "sub-01"), (ball((10, 4, 2), 5), self.affine, "sub-02"), (ball((0, -10, 4), 5), self.affine, "sub-03")]
        written = {
            "montage": figures.write_figure(figures.montage(self.tmap, self.template, "A 0 4", threshold=5.0, n=100, watermark="synthetic", **SMALL), self.tmp, "montage"),
            "glass": figures.write_figure(figures.glass(self.tmap, mask=(brain() > 0, self.affine, "mask"), threshold=5.0, n=100, **SMALL), self.tmp, "glass"),
            "gallery": figures.write_figure(figures.gallery(lesions, self.template, per_page=2, **SMALL), self.tmp, "gallery"),
        }
        for name, result in written.items():
            folder = os.path.join(self.tmp, "figures", name)
            self.assertEqual(result["dir"], folder)
            pngs = [f for f in result["files"] if f.endswith(".png")]
            self.assertEqual(len(pngs), 2 if name == "gallery" else 1)
            for file_name in result["files"]:
                path = os.path.join(folder, file_name)
                self.assertTrue(os.path.isfile(path), path)
                if file_name.endswith((".png", ".tiff")):
                    with Image.open(path) as image:
                        image.verify()
                    with Image.open(path) as image:
                        self.assertEqual(round(image.info["dpi"][0]), SMALL["dpi"])
                elif file_name.endswith(".svg"):
                    root = ET.parse(path).getroot()
                    ns = "{http://www.w3.org/2000/svg}"
                    self.assertEqual(root.tag, ns + "svg")
                    self.assertTrue(root.get("width").endswith("mm"))
                    images = root.findall(ns + "image")
                    self.assertEqual(len(images), 1)
                    self.assertTrue(images[0].get("href").startswith("data:image/png;base64,"))
                    texts = [t.text for t in root.iter(ns + "text")]
                    self.assertIn("L", texts)
                    self.assertIn("R", texts)
                elif file_name.endswith(".pdf"):
                    with open(path, "rb") as f:
                        data = f.read()
                    self.assertTrue(data.startswith(b"%PDF-"))
                    self.assertIn(b"%%EOF", data[-64:])
            with open(os.path.join(folder, "figure.json"), encoding="utf-8") as f:
                text = f.read()
            record = json.loads(text)
            self.assertEqual(record["format"], figures.FORMAT)
            self.assertEqual(record["convention"], "neurological")
            self.assertNotIn(self.tmp, text)  # no local paths in a shared file
            self.assertTrue(all(len(item["sha256"]) == 64 for item in record["inputs"]))
            with open(os.path.join(folder, "caption.txt"), encoding="utf-8") as f:
                self.assertIn("convention", f.read())
            with open(os.path.join(folder, f"{name}_sourcedata.csv"), encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertTrue(rows)
        with open(os.path.join(self.tmp, "figures", "montage", "figure.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["watermark"], "SYNTHETIC")
        with open(os.path.join(self.tmp, "figures", "gallery", "gallery_sourcedata.csv"), encoding="utf-8", newline="") as f:
            self.assertEqual([row["case"] for row in csv.DictReader(f)], ["1", "2", "3"])

    def test_png_bytes_deterministic(self):
        a = figures.montage(self.tmap, self.template, "A -4 0 4", threshold=5.0, n=100, **SMALL).png_bytes()
        b = figures.montage(self.tmap, self.template, "A -4 0 4", threshold=5.0, n=100, **SMALL).png_bytes()
        self.assertEqual(a, b)
        figure = figures.montage(self.tmap, self.template, "A 0", **SMALL)
        rgba = figure.rgba()
        self.assertEqual(rgba.shape, (figure.size[1], figure.size[0], 4))
        self.assertEqual(figure.size[0], round(89.0 / 25.4 * 150))

    def test_inter_font_and_default_font(self):
        figure = figures.montage(self.tmap, None, "A 0", threshold=5.0, n=100, **SMALL)
        self.assertTrue(figure.fonts.default)
        self.assertEqual(figure.fonts.text("|T| ≥ 5 · ±2 \u2212 1"), "|T| >= 5 | +/-2 - 1")
        missing = figures.Fonts(os.path.join(self.tmp, "missing.ttf"))
        self.assertTrue(missing.default)

    def test_watermark_for(self):
        study = os.path.join(self.tmp, "study")
        os.makedirs(study)
        with open(os.path.join(study, "study.json"), "w", encoding="utf-8") as f:
            json.dump({"format": "slicer-studio-study", "tutorial": True}, f)
        self.assertEqual(figures.watermark_for(study), "TUTORIAL DATA")
        self.assertEqual(figures.watermark_for(None, {"name": "Synthetic demo"}), "SYNTHETIC")
        self.assertIsNone(figures.watermark_for({"name": "x"}, {"name": "GSP1000"}))
        # Drawn first, so the slice labels and stamps stay readable on top of it
        figure = figures.montage(self.tmap, self.template, "A 0", watermark="TUTORIAL DATA", **SMALL)
        self.assertEqual(figure.annotations[0].role, "watermark")
        self.assertEqual(sum(item.role == "watermark" for item in figure.annotations), 1)

    def test_command_line_registers_run(self):
        study = os.path.join(self.tmp, "study")
        os.makedirs(study)
        with open(os.path.join(study, "study.json"), "w", encoding="utf-8") as f:
            json.dump({"format": "slicer-studio-study", "version": 1, "name": "t"}, f)
        map_path = os.path.join(study, "meanT.nii.gz")
        tpl_path = os.path.join(self.tmp, "tpl.nii.gz")
        nifti.write_nifti(map_path, blob_map(), self.affine)
        nifti.write_nifti(tpl_path, brain(), self.affine)
        out = os.path.join(study, "derivatives", "lnm")
        argv = ["--preset", "pair", "--map", map_path, "--template", tpl_path, "--rule", '{"kind": "custom_t", "value": 5}', "--n", "200", "--study", study, "--out", out, "--slices", "A 0", "--width-mm", "89", "--dpi", "150", "--formats", "png,svg"]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = figures.main(argv)
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(events[-1]["event"], "done")
        folder = os.path.join(out, "figures", "pair")
        self.assertTrue(os.path.isfile(os.path.join(folder, "pair.png")))
        record = runs.latest(study, "figure")
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "done")
        params = runs.read_params(study, record)
        self.assertEqual(params["maps"], ["meanT.nii.gz"])
        self.assertEqual(params["template"], "tpl.nii.gz")  # outside the study: the file name only
        with open(runs.registry_path(study), encoding="utf-8") as f:
            self.assertNotIn(self.tmp, f.read())


    # Job protocol of the command line: --cancel-file and --resume

    def _cli(self, argv):
        """Run the figure CLI as a job in this process; return (exit code, events)."""
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = jobs.run_job(figures.main, argv, job=figures.JOB, exit=False, signals=False, orphan=False)
        return code, [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]

    def _cli_inputs(self):
        out = os.path.join(self.tmp, "cli")
        os.makedirs(out)
        tpl = os.path.join(self.tmp, "tpl.nii.gz")
        nifti.write_nifti(tpl, brain(), self.affine)
        lesions = []
        for index in range(5):
            path = os.path.join(self.tmp, f"les{index}.nii.gz")
            nifti.write_nifti(path, ball((-20 + 8 * index, 0, 0), 6), self.affine)
            lesions.append(path)
        argv = ["--preset", "gallery", "--template", tpl, "--out", out, "--per-page", "2", "--width-mm", "89", "--dpi", "150", "--formats", "png"]
        for path in lesions:
            argv += ["--lesion", path]
        return out, argv

    def test_command_line_cancel_file_stops_before_any_output(self):
        out, argv = self._cli_inputs()
        stop = os.path.join(self.tmp, "STOP")
        with open(stop, "w", encoding="utf-8"):
            pass
        code, events = self._cli(argv + ["--cancel-file", stop])
        self.assertEqual(code, jobs.EXIT_CANCELLED)
        self.assertEqual(events[-1]["event"], "cancelled")
        self.assertNotIn("done", [event["event"] for event in events])
        self.assertFalse(os.path.exists(os.path.join(out, "figures")))
        self.assertFalse(os.path.exists(runs.registry_path(out)))

    def test_command_line_cancel_between_gallery_pages_leaves_no_partial_figure(self):
        out, argv = self._cli_inputs()
        stop = os.path.join(self.tmp, "STOP")
        pages = []
        original = figures._finish_gallery

        def finish_then_stop(*args, **kwargs):
            pages.append(1)
            if len(pages) == 1:  # the stop request arrives after page 1 is composed
                with open(stop, "w", encoding="utf-8"):
                    pass
            return original(*args, **kwargs)

        figures._finish_gallery = finish_then_stop
        try:
            code, events = self._cli(argv + ["--cancel-file", stop])
        finally:
            figures._finish_gallery = original
        self.assertEqual(code, jobs.EXIT_CANCELLED)
        self.assertEqual(events[-1]["event"], "cancelled")
        self.assertEqual(len(pages), 1, "page 2 was rendered after the stop")
        folder = os.path.join(out, "figures", "gallery")
        self.assertEqual(os.listdir(folder) if os.path.isdir(folder) else [], [])
        self.assertFalse(os.path.exists(runs.registry_path(out)))

    def test_write_figure_cancel_writes_nothing(self):
        figure = figures.montage(self.tmap, self.template, "A 0", threshold=4.0, **SMALL)
        with self.assertRaises(jobs.Cancelled):
            figures.write_figure(figure, self.tmp, "m", ("png", "svg"), cancel=lambda: True)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "figures")))

    def test_command_line_resume_skips_a_completed_figure(self):
        out, argv = self._cli_inputs()
        code, _ = self._cli(argv)
        self.assertEqual(code, 0)
        folder = os.path.join(out, "figures", "gallery")
        page = os.path.join(folder, "gallery_p01.png")
        stamp = os.stat(page).st_mtime_ns
        count = len(runs.load(out))
        self.assertEqual(count, 1)
        code, events = self._cli(argv + ["--resume"])
        self.assertEqual(code, 0)
        self.assertTrue(events[-1].get("resumed"))
        self.assertEqual(events[-1]["event"], "done")
        self.assertEqual(os.stat(page).st_mtime_ns, stamp, "a completed figure was rendered again")
        self.assertEqual(len(runs.load(out)), count, "a completed figure was registered again")
        self.assertEqual([event["event"] for event in events].count("result"), len(events[-1]["outputs"]))
        # Other options (or a deleted file) are not a completed figure: it is rendered and registered again
        code, events = self._cli(argv + ["--resume", "--title", "Other"])
        self.assertEqual(code, 0)
        self.assertFalse(events[-1].get("resumed"))
        self.assertEqual(len(runs.load(out)), count + 1)
        os.remove(page)
        code, events = self._cli(argv + ["--resume", "--title", "Other"])
        self.assertFalse(events[-1].get("resumed"))
        self.assertTrue(os.path.isfile(page))
        # Without --resume the figure is always rendered again
        code, events = self._cli(argv + ["--title", "Other"])
        self.assertFalse(events[-1].get("resumed"))
        with open(os.path.join(folder, "figure.json"), encoding="utf-8") as f:
            self.assertNotIn(self.tmp, f.read())


if __name__ == "__main__":
    unittest.main()

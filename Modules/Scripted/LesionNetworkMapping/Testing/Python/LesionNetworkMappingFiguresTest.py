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
MINUS = chr(0x2212)  # the minus sign figures write in negative values


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

    def test_grid_planes_go_through_voxel_centres_that_are_not_multiples_of_the_spacing(self):
        # 2 mm voxels centred at odd mm, and 1 mm voxels centred at x.5 mm: a grid anchored at multiples of the
        # spacing put every display point between voxel centres, so an isolated voxel of T 7 showed as 0.875
        # (linear) and a lesion saved RAS and LPS landed on different display voxels (nearest)
        for spacing, origin in ((2.0, (-29.0, -35.0, -29.0)), (2.0, (-31.0, -37.0, -27.0)), (1.0, (-14.5, -17.5, -14.5))):
            with self.subTest(spacing=spacing, origin=origin):
                affine = np.diag([spacing, spacing, spacing, 1.0])
                affine[:3, 3] = origin
                template = (brain(ras_affine(spacing=spacing)), affine, "template")
                peak = np.zeros(SHAPE, dtype=np.float32)
                peak[10, 18, 15] = 7.0
                peak[20, 18, 15] = -6.5
                grid = figures.display_grid(figures.load_volume(template))
                for axis in range(3):
                    self.assertAlmostEqual((grid.origin[axis] - origin[axis]) / spacing % 1.0, 0.0, places=6)
                shown = figures.reslice(figures.load_volume((peak, affine, "map")), grid, order=1)
                self.assertAlmostEqual(float(shown.max()), 7.0, places=5)
                self.assertAlmostEqual(float(shown.min()), -6.5, places=5)
                self.assertEqual(int((np.abs(shown) >= 5).sum()), 2)
                figure = figures.ortho((peak, affine, "map"), None, template, rule={"kind": "custom_t", "value": 5.0}, **SMALL)
                self.assertEqual(max(int(row["voxels_suprathreshold"]) for row in figure.sourcedata if "voxels_suprathreshold" in row), 2)
                lesion = (np.linalg.norm(world(SHAPE, affine) - np.array(world(SHAPE, affine)[12, 20, 16]), axis=-1) <= 3.2 * spacing).astype(np.uint8)
                lps = np.diag([-spacing, -spacing, spacing, 1.0])
                lps[:3, 3] = [origin[0] + spacing * (SHAPE[0] - 1), origin[1] + spacing * (SHAPE[1] - 1), origin[2]]
                pair = [(lesion, affine, "ras"), (lesion[::-1, ::-1].copy(), lps, "lps")]
                self.assertEqual(figures.overlap(lesions=pair, template=template, **SMALL).meta["bins"], {2: int(lesion.sum())})
                # Coordinates halfway between planes all take the upper plane: evenly spaced slices stay distinct
                planes = [grid.coordinate(2, grid.index(2, origin[2] + spacing * (k + 0.5))) for k in (4, 5, 6, 7)]
                self.assertEqual(planes, [origin[2] + spacing * k for k in (5, 6, 7, 8)])

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

    def test_overlap_n_lesions_below_maximum_is_refused(self):
        # --n-lesions 1 for a count map with maximum 2 was captioned "maximum 2 of 1 lesions (200%)"; 0 and -3 were accepted too
        count = np.zeros(SHAPE, dtype=np.int16)
        count[10:14, 15:20, 14:16] = 1
        count[11:12, 16:17, 15] = 2
        for value in (1, 0, -3):
            with self.assertRaises(ValueError, msg=str(value)) as caught:
                figures.overlap(count=(count, self.affine, "count"), n_lesions=value, slices="A 0", **SMALL)
            self.assertIn("--n-lesions", str(caught.exception), str(value))
        figure = figures.overlap(count=(count, self.affine, "count"), n_lesions=2, slices="A 0", **SMALL)
        self.assertEqual(figure.meta["legend"], "max 2/2 (100%)")
        out = os.path.join(self.tmp, "overlap_refused")
        tpl = os.path.join(self.tmp, "overlap_tpl.nii.gz")
        cp = os.path.join(self.tmp, "overlap_count.nii.gz")
        nifti.write_nifti(cp, count, self.affine)
        nifti.write_nifti(tpl, self.template[0], self.affine)
        code, events = self._cli(["--preset", "overlap", "--count", cp, "--n-lesions", "1", "--template", tpl, "--slices", "A 0", "--out", out, "--formats", "png"])
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["code"], "input")
        self.assertIn("--n-lesions 1", events[-1]["message"])
        self.assertFalse(os.path.exists(os.path.join(out, "figures")))

    # Captions and thresholds

    def test_caption_has_rule_n_df_and_orientation(self):
        figure = figures.montage(self.tmap, self.template, "A -5 -1 3", rule={"kind": "fwe_bonferroni", "alpha": 0.05}, n=1000, connectome="GSP1000", **SMALL)
        caption = figure.caption
        for text in ("FWE (Bonferroni)", "\u03b1 = 0.05", "N = 1000", "df = 999", "Neurological convention", "z = -5, -1, +3 mm", "Connectome: GSP1000", "p_unc ≤", "unthresholded companion"):
            self.assertIn(text, caption)
        resolved = figure.meta["thresholds"]["meanT"]
        self.assertEqual(resolved["df"], 999)
        self.assertIn(f"{resolved['value']:.2f}", caption)
        # Thresholded panel plus its unthresholded companion by default
        self.assertEqual([panel["mode"] for panel in figure.meta["panels"]], ["hard", "unthresholded"])
        no_companion = figures.montage(self.tmap, self.template, "A 0", rule={"kind": "custom_t", "value": 5}, n=50, companion=False, **SMALL)
        self.assertEqual([panel["mode"] for panel in no_companion.meta["panels"]], ["hard"])

    def test_pair_without_threshold_is_refused_not_captioned_thresholded(self):
        # The pair preset without a rule or threshold drew one unthresholded panel under "Thresholded and unthresholded map"
        for label, kwargs in {"nothing": {}, "mode transparent": {"mode": "transparent"}}.items():
            with self.assertRaises(ValueError, msg=label) as caught:
                figures.pair(self.tmap, self.template, "A 0", **kwargs, **SMALL)
            self.assertIn("The pair preset shows a thresholded map", str(caught.exception), label)
            self.assertIn("--threshold", str(caught.exception), label)
        # The command line refuses before writing anything
        out = os.path.join(self.tmp, "pair_refused")
        tpl = os.path.join(self.tmp, "pair_tpl.nii.gz")
        mp = os.path.join(self.tmp, "pair_map.nii.gz")
        nifti.write_nifti(mp, self.tmap[0], self.affine)
        nifti.write_nifti(tpl, self.template[0], self.affine)
        code, events = self._cli(["--preset", "pair", "--map", mp, "--template", tpl, "--slices", "A 0", "--out", out, "--formats", "png"])
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["code"], "input")
        self.assertIn("The pair preset shows a thresholded map", events[-1]["message"])
        self.assertFalse(os.path.exists(os.path.join(out, "figures")))
        # With a threshold or a rule it stays a pair (thresholded panel plus its companion) with that caption
        for kwargs in ({"threshold": 3.0}, {"rule": {"kind": "custom_t", "value": 3}, "n": 50}):
            figure = figures.pair(self.tmap, self.template, "A 0", **kwargs, **SMALL)
            self.assertEqual([panel["mode"] for panel in figure.meta["panels"]], ["hard", "unthresholded"])
            self.assertIn("Thresholded and unthresholded map of", figure.caption)

    def test_caption_without_n(self):
        figure = figures.montage(self.tmap, None, "A 0", threshold=4.0, **SMALL)
        self.assertIn("N unknown", figure.caption)
        with self.assertRaises(ValueError):
            figures.montage(self.tmap, None, "A 0", rule={"kind": "fwe_bonferroni"}, **SMALL)

    def test_mask_without_map_voxels_is_refused_not_resolved_at_v_1(self):
        # A mask that held none of the map's voxels made V = 1 silently: the FWE (Bonferroni) threshold became
        # the uncorrected one under the FWE label; an explicit V = 0 was taken as "not given"
        rule = {"kind": "fwe_bonferroni", "alpha": 0.05}
        inside = brain() > 0
        full = figures.montage(self.tmap, self.template, "A 0", rule=rule, n=100, mask=(inside, self.affine, "brainmask"), **SMALL)
        resolved = full.meta["thresholds"]["meanT"]
        self.assertEqual(resolved["v"], int(np.count_nonzero(inside)))
        self.assertGreater(resolved["value"], 4.0)
        away = self.affine.copy()
        away[:3, 3] += 1000.0
        nan_map = self.tmap[0].copy()
        nan_map[:5] = np.nan
        on_nan = np.zeros(SHAPE, dtype=bool)
        on_nan[:5] = True
        cases = {
            "empty": (self.tmap, (np.zeros(SHAPE, dtype=bool), self.affine, "emptymask")),
            "outside": (self.tmap, (inside, away, "farmask")),
            "nan only": ((nan_map, self.affine, "meanT"), (on_nan, self.affine, "nanmask")),
        }
        for label, (tmap, mask) in cases.items():
            for kwargs in ({"rule": rule, "n": 100}, {"threshold": 4.0}):
                with self.assertRaises(ValueError, msg=f"{label} {kwargs}") as caught:
                    figures.montage(tmap, self.template, "A 0", mask=mask, **kwargs, **SMALL)
                self.assertIn(mask[2], str(caught.exception))
                self.assertIn("covers no finite voxel", str(caught.exception))
        # An explicit V still works with such a mask; V = 0 is refused as V = -10 is
        explicit = figures.montage(self.tmap, self.template, "A 0", rule=rule, n=100, v=5000, mask=cases["empty"][1], **SMALL)
        self.assertEqual(explicit.meta["thresholds"]["meanT"]["v"], 5000)
        for bad in (0, -10):
            with self.assertRaises(ValueError, msg=str(bad)) as caught:
                figures.montage(self.tmap, self.template, "A 0", rule=rule, n=100, v=bad, **SMALL)
            self.assertIn("must be at least 1", str(caught.exception))
        # The command line refuses before writing anything
        map_path = os.path.join(self.tmp, "maskv-map.nii.gz")
        mask_path = os.path.join(self.tmp, "maskv-empty.nii.gz")
        nifti.write_nifti(map_path, self.tmap[0], self.affine)
        nifti.write_nifti(mask_path, np.zeros(SHAPE, dtype=np.uint8), self.affine)
        out = os.path.join(self.tmp, "o-maskv")
        for extra in (["--mask", mask_path], ["--v", "0"]):
            code, events = self._cli(["--preset", "montage", "--map", map_path, "--rule", json.dumps(rule), "--n", "100", "--out", out, "--formats", "png", *extra])
            self.assertNotEqual(code, 0, extra)
            self.assertEqual(events[-1]["code"], "input", extra)
            self.assertFalse(os.path.exists(os.path.join(out, "figures")), extra)

    def test_rule_without_threshold_is_refused_not_drawn_unthresholded(self):
        # A rule that gives no threshold (N < 2, a negative N, top_pct with no voxel of its sign) made a panel titled
        # "thresholded" that showed the whole map, under a caption saying voxels below the threshold were hidden
        negative = (-np.abs(self.tmap[0]), self.affine, "negT")
        fwe = {"kind": "fwe_bonferroni", "alpha": 0.05}
        top_pos = {"kind": "top_pct", "value": 10, "statistic": "T", "sign": "pos"}
        cases = {
            "fwe n=1": (figures.montage, self.tmap, {"rule": fwe, "n": 1}, "at least 2 subjects"),
            "custom_t n=1": (figures.montage, self.tmap, {"rule": {"kind": "custom_t", "value": 7}, "n": 1}, "at least 2 subjects"),
            "pair n=1": (figures.pair, self.tmap, {"rule": fwe, "n": 1}, "at least 2 subjects"),
            "transparent n=1": (figures.montage, self.tmap, {"rule": fwe, "n": 1, "mode": "transparent"}, "at least 2 subjects"),
            "top_pct pos on a negative map": (figures.montage, negative, {"rule": top_pos, "n": 50}, "no positive values"),
        }
        for label, (preset, tmap, kwargs, reason) in cases.items():
            with self.assertRaises(ValueError, msg=label) as caught:
                preset(tmap, self.template, "A 0", **kwargs, **SMALL)
            self.assertIn("gives no threshold", str(caught.exception), label)
            self.assertIn(reason, str(caught.exception), label)
            self.assertIn(tmap[2], str(caught.exception), label)
        with self.assertRaises(ValueError) as caught:
            figures.triptych(self.tmap, self.tmap, negative, self.template, "A 0", rule=top_pos, n=50, **SMALL)
        self.assertIn("negT", str(caught.exception))
        # Drawn unthresholded on purpose, the rule's sentence stays in the caption and no panel claims a threshold
        plain = figures.montage(self.tmap, self.template, "A 0", rule=fwe, n=1, mode="unthresholded", **SMALL)
        self.assertEqual({panel["mode"] for panel in plain.meta["panels"]}, {"unthresholded"})
        self.assertIn("Maps are shown unthresholded.", plain.caption)
        self.assertNotIn("Thresholded panels", plain.caption)
        # N below 1 is refused (0 used to mean "N unknown", a negative N reached the N < 2 branch)
        for bad in (0, -5):
            with self.assertRaises(ValueError, msg=str(bad)) as caught:
                figures.montage(self.tmap, self.template, "A 0", rule=fwe, n=bad, **SMALL)
            self.assertIn("must be at least 1", str(caught.exception))
        # The command line refuses before writing anything
        map_path = os.path.join(self.tmp, "unresolved-map.nii.gz")
        nifti.write_nifti(map_path, self.tmap[0], self.affine)
        out = os.path.join(self.tmp, "o-unresolved")
        for n in ("1", "-5"):
            code, events = self._cli(["--preset", "montage", "--map", map_path, "--rule", json.dumps(fwe), "--n", n, "--out", out, "--formats", "png"])
            self.assertNotEqual(code, 0, n)
            self.assertEqual(events[-1]["code"], "input", n)
            self.assertFalse(os.path.exists(os.path.join(out, "figures")), n)

    def test_window_must_be_positive_and_finite(self):
        # A negative window inverted the colour bar while the tiles silently used 1.0 (zero fell back to auto)
        for bad in (-2.0, 0.0, float("nan"), float("inf")):
            with self.assertRaises(ValueError, msg=str(bad)) as caught:
                figures.montage(self.tmap, None, "A 0", threshold=4.0, window=bad, **SMALL)
            self.assertIn("window must be positive", str(caught.exception))
        out = os.path.join(self.tmp, "o-window")
        map_path = os.path.join(self.tmp, "window-map.nii.gz")
        nifti.write_nifti(map_path, self.tmap[0], self.affine)
        code, events = self._cli(["--preset", "montage", "--map", map_path, "--window", "-2", "--threshold", "3", "--out", out, "--formats", "png"])
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["code"], "input")
        self.assertIn("window must be positive", events[-1]["message"])
        self.assertFalse(os.path.exists(os.path.join(out, "figures")))
        self.assertEqual(figures.montage(self.tmap, None, "A 0", threshold=4.0, window=2.0, **SMALL).meta["window"], 2.0)

    def test_formats_validated_before_rendering(self):
        # An unknown format used to fail only after the whole figure was rendered; an empty list exited 0 with no image
        out = os.path.join(self.tmp, "o-formats")
        map_path = os.path.join(self.tmp, "formats-map.nii.gz")
        nifti.write_nifti(map_path, self.tmap[0], self.affine)
        rendered = []
        original = figures.montage
        figures.montage = lambda *a, **k: rendered.append(1) or original(*a, **k)
        try:
            for bad, text in (("png,jpg", "Unknown format(s): jpg"), (",", "at least one format"), ("", "at least one format")):
                code, events = self._cli(["--preset", "montage", "--map", map_path, "--threshold", "3", "--out", out, "--formats", bad])
                self.assertEqual(code, jobs.EXIT_USAGE, bad)
                self.assertEqual(events[-1]["code"], "usage")
                self.assertIn(text, events[-1]["message"])
        finally:
            figures.montage = original
        self.assertEqual(rendered, [], "nothing may be rendered for a bad --formats")
        self.assertFalse(os.path.exists(os.path.join(out, "figures")))

    def test_columns_and_per_page_validated(self):
        # --columns -2 wrote a blank figure, montage --columns -1 failed with "max() iterable argument is empty",
        # and --per-page 0 silently became 1
        out = os.path.join(self.tmp, "o-columns")
        map_path = os.path.join(self.tmp, "columns-map.nii.gz")
        nifti.write_nifti(map_path, self.tmap[0], self.affine)
        for preset, option, value in (("gallery", "--columns", "-2"), ("montage", "--columns", "-1"), ("montage", "--columns", "0"),
                                      ("gallery", "--per-page", "0"), ("gallery", "--per-page", "-3")):
            code, events = self._cli(["--preset", preset, "--map", map_path, "--lesion", map_path, option, value, "--out", out, "--formats", "png"])
            self.assertEqual(code, jobs.EXIT_USAGE, (preset, option, value))
            self.assertEqual(events[-1]["code"], "usage")
            self.assertIn(f"{option} must be at least 1", events[-1]["message"])
        self.assertFalse(os.path.exists(os.path.join(out, "figures")))
        for bad in (-1, 0):
            with self.assertRaises(ValueError, msg=str(bad)) as caught:
                figures.montage(self.tmap, None, "A 0", threshold=4.0, columns=bad, **SMALL)
            self.assertIn("columns must be at least 1", str(caught.exception))
        with self.assertRaises(ValueError):
            figures.gallery([(ball((0, 0, 0), 5), self.affine, "a")], None, per_page=0, **SMALL)

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

    def test_gallery_caption_uses_the_singular_for_one_lesion(self):
        # "Lesion gallery (1 lesions)" read wrongly: the count noun follows the number
        lesions = [(ball((-16, 0, 0), 6), self.affine, "a"), (ball((16, 0, 0), 6), self.affine, "b")]
        self.assertIn("Lesion gallery (1 lesion):", figures.gallery(lesions[:1], self.template, **SMALL)[0].caption)
        self.assertIn("Lesion gallery (2 lesions):", figures.gallery(lesions, self.template, **SMALL)[0].caption)
        self.assertIn("Lesion gallery (2 lesions, page 2 of 2)", figures.gallery(lesions, self.template, per_page=1, **SMALL)[1].caption)

    def test_gallery_tiles_have_the_same_size_on_every_page(self):
        # The tile size is set by the column count, not by how many tiles a page holds: a last page with
        # one lesion (or a whole gallery of one) used to get a single page-wide tile
        lesions = [(ball((-10 + 4 * i, 0, 0), 5), self.affine, f"l{i}") for i in range(7)]

        def widths(figure):
            return {tile["bbox"][2] - tile["bbox"][0] for tile in figure.meta["tiles"]}

        pages = figures.gallery(lesions, self.template, per_page=6, **SMALL)
        self.assertEqual([len(page.meta["tiles"]) for page in pages], [6, 1])
        full, short = widths(pages[0]), widths(pages[1])
        self.assertEqual(len(full), 1)
        self.assertEqual(len(short), 1)
        self.assertLessEqual(abs(short.pop() - full.pop()), 1)
        self.assertEqual(pages[0].meta["size_px"][0], pages[1].meta["size_px"][0])
        single = widths(figures.gallery(lesions[:1], self.template, **SMALL)[0]).pop()
        self.assertLessEqual(abs(single - widths(pages[0]).pop()), 1)
        three = figures.gallery(lesions[:2], self.template, columns=3, **SMALL)[0]
        wide = figures.gallery(lesions[:3], self.template, columns=3, **SMALL)[0]
        self.assertLessEqual(abs(widths(three).pop() - widths(wide).pop()), 1)
        self.assertLess(widths(three).pop(), 0.4 * three.meta["size_px"][0])

    def _moved(self, data, dx, name):
        affine = self.affine.copy()
        affine[0, 3] += dx
        return (data, affine, name)

    def test_input_outside_the_template_is_an_error_not_an_empty_figure(self):
        # A map or lesion in another space (here 300 mm away) used to give a blank figure whose caption still
        # reported its threshold fraction, a crosshair "peak" at the grid corner and "maximum 1 of 1 lesions"
        far_map = self._moved(blob_map(), 300.0, "meanT")
        far_lesion = self._moved(ball((-16, 0, 0), 6), 300.0, "les")
        calls = [
            lambda: figures.montage(far_map, self.template, "A 0", threshold=3.0, **SMALL),
            lambda: figures.ortho(far_map, None, self.template, **SMALL),
            lambda: figures.glass(far_map, template=self.template, **SMALL),
            lambda: figures.gallery([far_lesion], self.template, **SMALL),
            lambda: figures.overlap(lesions=[far_lesion], template=self.template, **SMALL),
        ]
        for call in calls:
            with self.assertRaises(ValueError) as caught:
                call()
            self.assertIn("template's field of view", str(caught.exception))
        # the command line reports it as an input error
        map_path = os.path.join(self.tmp, "far.nii.gz")
        tpl_path = os.path.join(self.tmp, "tpl.nii.gz")
        nifti.write_nifti(map_path, far_map[0], far_map[1])
        nifti.write_nifti(tpl_path, brain(), self.affine)
        code, events = self._cli(["--preset", "ortho", "--map", map_path, "--template", tpl_path, "--out", os.path.join(self.tmp, "o"), "--formats", "png"])
        self.assertNotEqual(code, 0)
        self.assertEqual(events[-1]["event"], "error")
        self.assertEqual(events[-1]["code"], "input")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "o", "figures", "ortho")))

    def test_input_partly_outside_the_template_is_noted_in_the_caption(self):
        near_map = (blob_map(), self.affine, "meanT")
        shifted = self._moved(blob_map(), 40.0, "meanT")
        ok = figures.montage(near_map, self.template, "A 0", threshold=3.0, **SMALL)
        self.assertNotIn("outside the template", ok.caption)
        for figure in (figures.montage(shifted, self.template, "A 0", threshold=3.0, **SMALL), figures.glass(shifted, template=self.template, **SMALL)):
            self.assertIn("outside the template's field of view and are not shown", figure.caption)
        lesions = [(ball((-16, 0, 0), 6), self.affine, "a"), self._moved(ball((0, 0, 0), 6), 52.0, "b")]
        for figure in (figures.overlap(lesions=lesions, template=self.template, **SMALL), figures.gallery(lesions, self.template, **SMALL)[0]):
            self.assertIn("1 of 2 lesion lies partly or wholly outside the template's field of view (case 2", figure.caption)
        clean = figures.gallery(lesions[:1], self.template, **SMALL)[0]
        self.assertNotIn("outside the template", clean.caption)

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

    def test_replication_r_does_not_depend_on_storage_order_or_template(self):
        # Two noisy 2 mm maps; the second one is also stored LPS and with its axes swapped (same voxel centres).
        # With a 1 mm template, r used to come from the maps linearly resliced onto the finer display grid (smoothed
        # noise, inflated r and voxel count) whenever the affines differed, even with identical voxel centres.
        rng = np.random.default_rng(5)
        inside = brain() > 0
        signal = blob_map() / 4.0
        first = np.where(inside, signal + rng.normal(0, 1, SHAPE), 0).astype(np.float32)
        second = np.where(inside, signal + rng.normal(0, 1, SHAPE), 0).astype(np.float32)
        expected = float(np.corrcoef(first[inside], second[inside])[0, 1])
        nx, ny, _ = SHAPE
        lps = self.affine @ np.array([[-1.0, 0, 0, nx - 1], [0, -1, 0, ny - 1], [0, 0, 1, 0], [0, 0, 0, 1]])
        swapped = self.affine[:, [2, 1, 0, 3]]
        stored = {"RAS": (second, self.affine), "LPS": (second[::-1, ::-1].copy(), lps), "KJI": (second.transpose(2, 1, 0).copy(), swapped)}
        fine_shape = tuple(2 * n - 1 for n in SHAPE)
        fine_affine = ras_affine(fine_shape, 1.0)
        fine_world = world(fine_shape, fine_affine)
        fine = (np.where((fine_world[..., 0] / 26) ** 2 + (fine_world[..., 1] / 32) ** 2 + (fine_world[..., 2] / 26) ** 2 <= 1, 50.0, 0.0).astype(np.float32), fine_affine, "template 1 mm")
        mask = (inside.astype(np.float32), self.affine, "mask")
        for template in (self.template, fine, None):
            for name, (data, affine) in stored.items():
                with self.subTest(template=None if template is None else template[2], storage=name):
                    figure = figures.replication((first, self.affine, "A"), (data, affine, "B"), template, "A 0", mask=mask, **SMALL)
                    self.assertAlmostEqual(figure.meta["spatial_r"], expected, places=5)
                    self.assertEqual(figure.meta["r_voxels"], int(inside.sum()))
                    self.assertEqual(figure.meta["r_grid"], "native")
                    self.assertIn("on the maps' common grid", figure.caption)
        # Maps on different lattices: r on the coarser map's own grid, whatever the template's spacing
        shifted = self.affine.copy()
        shifted[:3, 3] += 0.5 * SPACING
        values = {}
        for template in (self.template, fine):
            figure = figures.replication((first, self.affine, "A"), (second, shifted, "B"), template, "A 0", mask=mask, **SMALL)
            self.assertEqual(figure.meta["r_grid"], "resampled")
            self.assertLessEqual(figure.meta["r_voxels"], int(inside.sum()))
            values[template[2]] = (figure.meta["spatial_r"], figure.meta["r_voxels"])
        self.assertEqual(values["template"], values["template 1 mm"])

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

    def test_colorbar_tick_labels_never_overprint(self):
        # BB-1: a threshold widened to 1.1 x its value lies at 0.909 of the half bar, closer to the end tick
        # than a label is wide, so "-5.66" and "-5.15" were drawn on top of each other
        for sign, count in (("neg", 1), ("both", 2)):
            figure = figures.montage(self.tmap, self.template, "A 0", threshold=5.146, window=5.66, sign=sign, **SMALL)
            labels = [item for item in figure.annotations if item.role in ("tick value", "threshold value")]
            self.assertEqual(len([item for item in labels if item.role == "threshold value"]), count)
            boxes = []
            for item in labels:
                half = figure.fonts.width(item.text, item.size) / 2
                boxes.append((item.x - half, item.x + half, item.y, item.y + item.size, item.text))
            for index, first in enumerate(boxes):
                self.assertLessEqual(first[3], figure.size[1], first)
                for second in boxes[index + 1 :]:
                    apart = first[1] <= second[0] or second[1] <= first[0] or first[3] <= second[2] or second[3] <= first[2]
                    self.assertTrue(apart, f"colour bar labels overlap: {first} and {second} ({sign})")
        # Ticks that are far apart keep the single row
        plain = figures.montage(self.tmap, self.template, "A 0", threshold=3.0, window=6.0, sign="both", **SMALL)
        rows = {round(item.y) for item in plain.annotations if item.role == "threshold value"}
        ends = {round(item.y) for item in plain.annotations if item.role == "tick value"}
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(ends), 1)

    def test_colorbar_label_and_caption_state_the_displayed_sign(self):
        # --sign neg shows T <= -t: the bar must not read "T >= t", and the caption must say which tail is shown
        rule = {"kind": "custom_t", "value": 4.0}
        figures_by_sign = {sign: figures.montage(self.tmap, self.template, "A 0", rule=rule, n=100, sign=sign, **SMALL) for sign in ("both", "pos", "neg")}
        labels = {sign: figure.meta["colorbars"][0]["label"] for sign, figure in figures_by_sign.items()}
        self.assertIn("threshold |T| ≥ 4", labels["both"])
        self.assertIn("threshold T ≥ 4", labels["pos"])
        self.assertIn(f"threshold T ≤ {MINUS}4", labels["neg"])
        self.assertNotIn("≥", labels["neg"])
        self.assertNotIn("show only", figures_by_sign["both"].caption)
        pos, neg = figures_by_sign["pos"].caption, figures_by_sign["neg"].caption
        self.assertIn("Thresholded panels show only positive values (T ≥ 4,", pos)
        self.assertIn("negative values are hidden", pos)
        self.assertIn(f"Thresholded panels show only negative values (T ≤ {MINUS}4,", neg)
        self.assertIn("positive values are hidden", neg)
        # The shown-tail share is one-tailed: the blob map has both tails above 4, so each is below the |T| share
        values = self.tmap[0][self.tmap[0] != 0]
        for caption, tail in ((pos, values >= 4.0), (neg, values <= -4.0)):
            self.assertTrue(tail.any())
            self.assertIn(f"{figures.thresholds._format_fraction(tail.mean())} of in-mask voxels); ", caption)
        # Every map preset carries the sign sentence; an unthresholded figure has none
        glass = figures.glass(self.tmap, template=self.template, rule=rule, n=100, sign="neg", **SMALL)
        self.assertIn("show only negative values", glass.caption)
        self.assertIn(f"T ≤ {MINUS}4", glass.meta["colorbars"][0]["label"])
        plain = figures.montage(self.tmap, self.template, "A 0", sign="neg", **SMALL)
        self.assertNotIn("show only", plain.caption)

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

    def test_bad_input_messages_name_the_file_or_option(self):
        # Raw Python texts ("Not a gzipped file (b'ga')", "invalid literal for int()") named neither the file nor the option
        out = os.path.join(self.tmp, "o-badinput")
        good = os.path.join(self.tmp, "badinput-good.nii.gz")
        nifti.write_nifti(good, self.tmap[0], self.affine)
        short = os.path.join(self.tmp, "short-ga.nii.gz")
        with open(short, "wb") as f:
            f.write(b"ga")
        text = os.path.join(self.tmp, "plain-text.nii.gz")
        with open(text, "w") as f:
            f.write("garbage\n")
        cases = [
            (["--preset", "triptych", "--map", good, "--map", short, "--map", good], ["short-ga.nii.gz", "Cannot read"]),
            (["--preset", "montage", "--map", text], ["plain-text.nii.gz"]),
            (["--preset", "montage", "--map", good, "--template", short], ["short-ga.nii.gz", "Cannot read"]),
            (["--preset", "montage", "--map", os.path.join(self.tmp, "missing.nii.gz")], ["missing.nii.gz"]),
            (["--preset", "montage", "--map", good, "--slices", "A auto x"], ["slice specification", "A auto x"]),
            (["--preset", "ortho", "--map", good, "--point", "1,2,a"], ["--point needs three numbers"]),
        ]
        for argv, needles in cases:
            code, events = self._cli(argv + ["--out", out, "--formats", "png"])
            self.assertNotEqual(code, 0, argv)
            self.assertEqual(events[-1]["code"], "input", argv)
            for needle in needles:
                self.assertIn(needle, events[-1]["message"], argv)
            for raw in ("Not a gzipped file", "invalid literal", "could not convert"):
                self.assertNotIn(raw, events[-1]["message"], argv)
        with self.assertRaises(ValueError):
            figures.parse_slices("A auto x")

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

    def test_font_that_is_not_a_file_is_an_error_not_pillows_font(self):
        # --font with a typo in the path used to exit 0 and draw with Pillow's font (figure.json: "Pillow default")
        out = os.path.join(self.tmp, "o-font")
        map_path = os.path.join(self.tmp, "font-map.nii.gz")
        nifti.write_nifti(map_path, self.tmap[0], self.affine)
        base = ["--preset", "montage", "--map", map_path, "--threshold", "3", "--slices", "A 0", "--width-mm", "89", "--dpi", "150", "--out", out, "--formats", "png"]
        for bad in (os.path.join(self.tmp, "no-such-Inter-Regular.ttf"), self.tmp):
            code, events = self._cli(base + ["--font", bad])
            self.assertNotEqual(code, 0, bad)
            self.assertEqual(events[-1]["code"], "input", bad)
            self.assertIn(bad, events[-1]["message"])
            self.assertIn("Font file not found", events[-1]["message"])
            self.assertFalse(os.path.exists(os.path.join(out, "figures")), "nothing may be written for a bad --font")
        code, events = self._cli(base)  # no --font: Pillow's font, as documented
        self.assertEqual(code, 0)

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

    def test_map_with_several_volumes_is_refused_not_cut_to_its_first(self):
        # A 4D map used to be drawn from its first volume only (here an empty one, the effect being in volume 2),
        # exit 0 with a normal caption that never said the other volumes were dropped
        effect = blob_map()
        four_d = os.path.join(self.tmp, "map4d.nii.gz")
        nifti.write_nifti(four_d, np.stack([np.zeros_like(effect), effect], axis=-1), self.affine)
        tpl_path = os.path.join(self.tmp, "tpl.nii.gz")
        nifti.write_nifti(tpl_path, brain(), self.affine)
        with self.assertRaises(ValueError) as caught:
            figures.load_volume(four_d)
        self.assertIn("map4d.nii.gz holds 2 volumes", str(caught.exception))
        self.assertNotIn(self.tmp, str(caught.exception))  # the file name only, never a local path
        for preset in ("montage", "ortho", "glass"):
            out = os.path.join(self.tmp, f"o-{preset}")
            code, events = self._cli(["--preset", preset, "--map", four_d, "--template", tpl_path, "--threshold", "3", "--out", out, "--formats", "png"])
            self.assertNotEqual(code, 0, preset)
            self.assertEqual(events[-1]["event"], "error", preset)
            self.assertEqual(events[-1]["code"], "input", preset)
            self.assertIn("holds 2 volumes", events[-1]["message"], preset)
            self.assertFalse(os.path.exists(os.path.join(out, "figures")), preset)
        # a 4D file with a single volume is still read as a 3D image
        single = os.path.join(self.tmp, "map1.nii.gz")
        nifti.write_nifti(single, effect[..., np.newaxis], self.affine)
        volume = figures.load_volume(single)
        self.assertEqual(volume.data.shape, effect.shape)
        np.testing.assert_allclose(volume.data, effect)
        figure = figures.montage(single, tpl_path, "A 0", threshold=3.0, **SMALL)
        self.assertIn("Montage of map1", figure.caption)

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

    def test_render_again_under_the_same_name_removes_the_previous_figures_files(self):
        folder = os.path.join(self.tmp, "figures", "m")
        figure = figures.montage(self.tmap, self.template, "A 0", threshold=4.0, **SMALL)
        figures.write_figure(figure, self.tmp, "m")
        self.assertTrue(os.path.isfile(os.path.join(folder, "m.tiff")))
        with open(os.path.join(folder, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("a file of the user")
        # Fewer formats: the other formats of the previous render are removed, a file of the user stays
        written = figures.write_figure(figure, self.tmp, "m", ("png",))
        self.assertEqual(sorted(os.listdir(folder)), sorted(written["files"] + ["notes.txt"]))
        # A gallery of 3 pages rendered again as 1 page (and back): no pages of the other render are left
        lesions = [(ball((x, 0, 0), 5.0), self.affine, f"lesion{i}") for i, x in enumerate((-12, 0, 12))]
        written = figures.write_figure(figures.gallery(lesions, self.template, per_page=1, **SMALL), self.tmp, "g")
        self.assertIn("g_p03.png", written["files"])
        written = figures.write_figure(figures.gallery(lesions, self.template, per_page=3, **SMALL), self.tmp, "g")
        self.assertEqual(sorted(os.listdir(written["dir"])), sorted(written["files"]))
        written = figures.write_figure(figures.gallery(lesions[:2], self.template, per_page=1, **SMALL), self.tmp, "g", ("png", "svg"))
        self.assertEqual(sorted(os.listdir(written["dir"])), sorted(written["files"]))
        # An edited figure.json cannot point the cleanup at files the figure did not write
        with open(written["figure_json"], encoding="utf-8") as f:
            record = json.load(f)
        record["files"] += ["notes.txt", "../m/m.png"]
        with open(written["figure_json"], "w", encoding="utf-8") as f:
            json.dump(record, f)
        with open(os.path.join(written["dir"], "notes.txt"), "w", encoding="utf-8") as f:
            f.write("a file of the user")
        figures.write_figure(figures.gallery(lesions[:1], self.template, **SMALL), self.tmp, "g", ("png",))
        self.assertTrue(os.path.isfile(os.path.join(written["dir"], "notes.txt")))
        self.assertTrue(os.path.isfile(os.path.join(folder, "m.png")))

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

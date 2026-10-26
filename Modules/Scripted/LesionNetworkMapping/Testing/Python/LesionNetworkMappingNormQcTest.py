"""Tests of the normalization QC numerics (normqc.py; addendum §3.10, A-03 modification (d)).

Synthetic cases with known answers: a head scaled by 0.85 (the raw MNI/native lesion volume ratio
flags it, the ratio normalized for brain size does not) and a collapsed lesion (flagged either way);
Jacobian determinants of linear and sinusoidal displacement fields, on RAS and flipped grids; the
log-Jacobian in the lesion minus the brain, folding, outside-brain and ventricle percentages; outlines;
the suggestion rules with A-03's calibration ranges; the normqc JSON; and the QC mosaic (size,
determinism, and a left lesion drawn on the side stamped L whatever the voxel order of the file).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingNormQcTest.py -v
"""

import json
import math
import os
import shutil
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np
from scipy import ndimage

from LesionNetworkMappingLib import normqc

SPACING = 2.0
SHAPE = (48, 56, 44)
# RAS grid with the origin near the centre of the volume, as in MNI templates
AFFINE = np.array([
    [SPACING, 0, 0, -SPACING * (SHAPE[0] - 1) / 2],
    [0, SPACING, 0, -SPACING * (SHAPE[1] - 1) / 2 - 10],
    [0, 0, SPACING, -SPACING * (SHAPE[2] - 1) / 2],
    [0, 0, 0, 1],
])  # fmt: skip
BRAIN_RADII = np.array([38.0, 46.0, 34.0])
LESION_CENTER = np.array([-20.0, 6.0, 8.0])  # left, anterior, superior
LESION_RADIUS = 9.0


def _world(shape=SHAPE, affine=AFFINE):
    ijk = np.indices(shape, dtype=np.float64).reshape(3, -1).T
    return (ijk @ affine[:3, :3].T + affine[:3, 3]).reshape(*shape, 3)


def _ellipsoid(world, radii, center=(0.0, 0.0, -10.0)):
    return (((world - np.asarray(center)) / np.asarray(radii)) ** 2).sum(axis=-1) <= 1.0


def _sphere(world, center, radius):
    return np.linalg.norm(world - np.asarray(center), axis=-1) <= radius


def _template_and_lesion(world):
    """A smooth template image, its brain mask and a left lesion (all on one grid)."""
    brain = _ellipsoid(world, BRAIN_RADII)
    x, y, z = world[..., 0], world[..., 1], world[..., 2]
    template = brain * (100.0 + 30.0 * np.sin(x / 7.0) * np.cos(y / 9.0) + 20.0 * np.sin(z / 5.0))
    lesion = _sphere(world, LESION_CENTER, LESION_RADIUS).astype(np.float32)
    return template, brain, lesion


def _flip_i(affine, shape):
    """Affine of the same world box stored with the i axis reversed (LAS order)."""
    flip = np.eye(4)
    flip[0, 0] = -1.0
    flip[0, 3] = shape[0] - 1
    return affine @ flip


class NormQcVolumeTest(unittest.TestCase):
    def test_scaled_head_raw_ratio_flags_normalized_passes(self):
        # 1 mm grid: the lesion volumes are then accurate to about 1%
        affine = np.diag([1.0, 1.0, 1.0, 1.0])
        affine[:3, 3] = [-48.0, -66.0, -54.0]
        world = _world((97, 113, 89), affine)
        voxel_ml = 0.001
        scale = 0.85
        template_brain = _ellipsoid(world, BRAIN_RADII)
        native_brain = _ellipsoid(world, BRAIN_RADII * scale, center=np.array([0.0, 0.0, -10.0]) * scale)
        template_lesion = _sphere(world, LESION_CENTER, LESION_RADIUS)
        native_lesion = _sphere(world, LESION_CENTER * scale, LESION_RADIUS * scale)  # the whole head scaled about the origin
        template_brain_ml = normqc.volume_ml(template_brain, affine, mode="binary")
        native_brain_ml = normqc.volume_ml(native_brain, affine, mode="binary")
        mni_lesion_ml = normqc.volume_ml(template_lesion, affine)
        native_lesion_ml = normqc.volume_ml(native_lesion, affine)
        self.assertAlmostEqual(template_brain_ml, np.count_nonzero(template_brain) * voxel_ml)

        # Measured native brain volume
        change = normqc.lesion_volume_change(native_lesion_ml, mni_lesion_ml, native_brain_ml, template_brain_ml)
        self.assertAlmostEqual(change["raw_ratio"], 1 / scale**3, delta=0.03)
        self.assertGreater(change["raw_ratio"], 1.4)  # the raw ratio would flag this head
        self.assertAlmostEqual(change["normalized_ratio"], 1.0, delta=0.02)
        self.assertEqual(change["level"], normqc.LEVEL_OK)
        self.assertAlmostEqual(change["normalized_ratio"], change["raw_ratio"] / change["scale_ratio"])
        raw_level = normqc._range_level(change["raw_ratio"], normqc.THRESHOLDS["normalized_ratio_min"], normqc.THRESHOLDS["normalized_ratio_max"])
        self.assertEqual(raw_level, normqc.LEVEL_AMBER)

        # Native brain volume from the registration's affine (template -> scan points): exact scale
        template_to_scan = np.diag([scale, scale, scale, 1.0])
        self.assertAlmostEqual(normqc.affine_scale(template_to_scan), scale**3)
        estimated = normqc.native_brain_ml_from_affine(template_brain_ml, template_to_scan)
        self.assertAlmostEqual(estimated, template_brain_ml * scale**3)
        change = normqc.lesion_volume_change(native_lesion_ml, mni_lesion_ml, estimated, template_brain_ml)
        self.assertAlmostEqual(change["scale_ratio"], 1 / scale**3)
        self.assertAlmostEqual(change["normalized_ratio"], 1.0, delta=0.02)
        self.assertEqual(change["level"], normqc.LEVEL_OK)

        # A collapsed lesion in the same scaled head is flagged after normalization
        collapsed = normqc.lesion_volume_change(native_lesion_ml, 0.2 * mni_lesion_ml, estimated, template_brain_ml)
        self.assertAlmostEqual(collapsed["normalized_ratio"], 0.2 * change["normalized_ratio"])
        self.assertEqual(collapsed["level"], normqc.LEVEL_AMBER)
        # The thresholds are the A-03 rules (0.70-1.40)
        self.assertEqual(normqc.lesion_volume_change(1.0, 0.69, 1.0, 1.0)["level"], normqc.LEVEL_AMBER)
        self.assertEqual(normqc.lesion_volume_change(1.0, 0.71, 1.0, 1.0)["level"], normqc.LEVEL_OK)
        self.assertEqual(normqc.lesion_volume_change(1.0, 1.41, 1.0, 1.0)["level"], normqc.LEVEL_AMBER)
        self.assertEqual(normqc.lesion_volume_change(0.0, 1.0, 1.0, 1.0)["level"], normqc.LEVEL_NA)

    def test_compute_metrics_uses_the_affine_scale(self):
        world = _world()
        template, brain, lesion = _template_and_lesion(world)
        native_ml = normqc.volume_ml(lesion, AFFINE) * 0.85**3
        metrics = normqc.compute_metrics(template, template, brain, lesion, AFFINE, native_lesion_ml=native_ml, template_to_scan_affine=np.diag([0.85, 0.85, 0.85, 1.0]))
        self.assertAlmostEqual(metrics["raw_ratio"], 1 / 0.85**3)
        self.assertAlmostEqual(metrics["normalized_ratio"], 1.0)
        self.assertAlmostEqual(metrics["ncc_xl"], 1.0)
        self.assertEqual(metrics["side"], "L")
        np.testing.assert_allclose(metrics["centroid_mni"], LESION_CENTER, atol=0.6)
        self.assertEqual(metrics["outside_brain_pct"], 0.0)
        self.assertEqual(normqc.evaluate(metrics)["suggestion"], normqc.SUGGEST_OK)

    def test_volume_modes(self):
        image = np.array([[[0.2, 0.5, 1.0, 0.0]]])
        self.assertAlmostEqual(normqc.volume_ml(image, voxel_volume_mm3=1000.0), 1.7)
        self.assertAlmostEqual(normqc.volume_ml(image, voxel_volume_mm3=1000.0, mode="binary"), 2.0)
        self.assertAlmostEqual(normqc.volume_ml(image, np.diag([-2.0, 2.0, 2.0, 1.0]), mode="binary"), 0.016)


class NormQcJacobianTest(unittest.TestCase):
    def test_linear_field(self):
        # u = A x: J = det(I + A) everywhere, also at the edges (one-sided differences are exact)
        a = np.array([[0.10, 0.05, 0.0], [-0.02, -0.20, 0.03], [0.04, 0.0, 0.15]])
        shape = (12, 14, 10)
        world = _world(shape)
        field = world @ a.T
        expected = np.linalg.det(np.eye(3) + a)
        jac = normqc.jacobian_from_displacement(field, spacing=(SPACING, SPACING, SPACING))
        np.testing.assert_allclose(jac, expected, rtol=1e-10)
        jac = normqc.jacobian_from_displacement(field, affine=AFFINE, chunk=3)
        np.testing.assert_allclose(jac, expected, rtol=1e-10)
        # Same world field stored with a flipped i axis: the affine accounts for the flip
        flipped = field[::-1]
        jac = normqc.jacobian_from_displacement(flipped, affine=_flip_i(AFFINE, shape), chunk=5)
        np.testing.assert_allclose(jac, expected, rtol=1e-10)
        # Anisotropic spacing
        anisotropic = np.diag([1.0, 2.0, 3.0, 1.0])
        field = _world(shape, anisotropic) @ a.T
        np.testing.assert_allclose(normqc.jacobian_from_displacement(field, spacing=(1, 2, 3)), expected, rtol=1e-10)

    def test_sinusoidal_field_and_folding(self):
        shape = (64, 6, 6)
        world = _world(shape, np.diag([1.0, 1.0, 1.0, 1.0]))
        x = world[..., 0]
        wavelength = 32.0
        for amplitude in (2.0, 8.0):
            field = np.zeros((*shape, 3))
            field[..., 0] = amplitude * np.sin(2 * np.pi * x / wavelength)
            jac = normqc.jacobian_from_displacement(field, spacing=(1, 1, 1), chunk=7)
            analytic = 1 + amplitude * 2 * np.pi / wavelength * np.cos(2 * np.pi * x / wavelength)
            # Central differences: relative error of the derivative ~ (2 pi h / L)^2 / 6
            np.testing.assert_allclose(jac[1:-1], analytic[1:-1], atol=amplitude * 2 * np.pi / wavelength * 0.01)
            brain = np.ones(shape, dtype=bool)
            folded = normqc.folding_pct(jac, brain)
            if amplitude == 2.0:
                self.assertEqual(folded, 0.0)  # min J = 1 - 0.39
            else:
                # J <= 0 where cos(2 pi x / L) <= -L / (2 pi a): a known fraction of the period
                expected = 100.0 * np.count_nonzero(jac <= 0) / jac.size
                self.assertAlmostEqual(folded, expected)
                self.assertGreater(folded, 20.0)

    def test_log_jacobian_lesion_minus_brain(self):
        world = _world()
        _, brain, lesion = _template_and_lesion(world)
        jac = np.where(brain, 1.1, 1.0)
        jac[lesion >= 0.5] = 0.5
        expected = math.log(0.5) - np.log(jac[brain]).mean()
        self.assertAlmostEqual(normqc.log_jacobian_lesion_minus_brain(jac, lesion, brain), expected)
        self.assertLess(expected, -normqc.THRESHOLDS["abs_logJ_lesion_minus_brain_max"])
        # Folded voxels are left out of the means and counted by folding_pct
        jac[tuple(np.argwhere(brain & (lesion < 0.5))[0])] = -0.1
        self.assertTrue(math.isfinite(normqc.log_jacobian_lesion_minus_brain(jac, lesion, brain)))
        self.assertAlmostEqual(normqc.folding_pct(jac, brain), 100.0 / np.count_nonzero(brain))
        # A uniform deformation gives 0 (the lesion changed like the brain)
        self.assertAlmostEqual(normqc.log_jacobian_lesion_minus_brain(np.full(SHAPE, 0.7), lesion, brain), 0.0)
        self.assertTrue(math.isnan(normqc.log_jacobian_lesion_minus_brain(np.ones(SHAPE), np.zeros(SHAPE), brain)))


class NormQcMasksTest(unittest.TestCase):
    def test_outline(self):
        mask = np.zeros((9, 9, 9), dtype=bool)
        mask[2:7, 2:7, 2:7] = True
        edge = normqc.outline(mask)
        self.assertEqual(edge.dtype, bool)
        self.assertEqual(int(edge.sum()), 125 - 27)
        self.assertFalse(edge[4, 4, 4])
        self.assertTrue(edge[2, 4, 4])
        self.assertFalse((edge & ~mask).any())
        square = np.zeros((6, 6))
        square[1:5, 1:5] = 1.0
        self.assertEqual(int(normqc.outline(square).sum()), 12)
        # Voxels on the volume border are outlined (outside counts as background)
        self.assertEqual(int(normqc.outline(np.ones((3, 3, 3))).sum()), 26)
        self.assertFalse(normqc.outline(np.zeros((4, 4, 4))).any())
        # Fractions >= 0.5 count as inside
        self.assertEqual(int(normqc.outline(np.array([0.2, 0.6, 0.9, 0.4])).sum()), 2)

    def test_dice_outside_and_ventricles(self):
        a = np.zeros((10, 10, 10), dtype=bool)
        b = np.zeros_like(a)
        a[:5] = True
        b[:4] = True
        self.assertAlmostEqual(normqc.brain_mask_dice(a, b), 2 * 400 / 900)
        self.assertEqual(normqc.brain_mask_dice(a, a), 1.0)
        self.assertTrue(math.isnan(normqc.brain_mask_dice(np.zeros(5), np.zeros(5))))
        with self.assertRaises(ValueError):
            normqc.brain_mask_dice(a, b[:5])

        lesion = np.zeros((10, 10, 10))
        lesion[3:6, 0, 0] = [1.0, 0.5, 0.5]
        self.assertAlmostEqual(normqc.pct_outside_brain(lesion, a), 100.0 * 0.5 / 2.0)
        self.assertAlmostEqual(normqc.pct_outside_brain(lesion, b), 100.0 * 1.0 / 2.0)
        self.assertAlmostEqual(normqc.pct_outside_brain(lesion, np.ones_like(a)), 0.0)
        self.assertTrue(math.isnan(normqc.pct_outside_brain(np.zeros_like(lesion), a)))

        labels = np.zeros((10, 10, 10), dtype=np.int16)
        labels[4, 0, 0] = 4
        labels[5, 0, 0] = 43
        self.assertAlmostEqual(normqc.pct_in_ventricles(lesion, labels), 50.0)
        self.assertAlmostEqual(normqc.pct_in_ventricles(lesion, labels, labels=[4]), 25.0)
        prior = labels.astype(np.float32) / 43.0  # probabilities: only the 43 voxel is >= 0.5
        self.assertAlmostEqual(normqc.pct_in_ventricles(lesion, prior), 25.0)
        self.assertAlmostEqual(normqc.pct_in_ventricles(lesion, labels, lesion_space="MNI152NLin2009cAsym", ventricle_space="MNI152NLin2009aSym"), 50.0)
        with self.assertRaises(ValueError):
            normqc.pct_in_ventricles(lesion, labels, lesion_space="MNI152NLin6Asym", ventricle_space="MNI152NLin2009cAsym")

    def test_similarity_excludes_the_dilated_lesion(self):
        world = _world()
        template, brain, lesion = _template_and_lesion(world)
        rng = np.random.default_rng(0)
        warped = template.copy()
        near = ndimage.binary_dilation(lesion > 0, iterations=2)  # the excluded region (6-connected, 2 voxels)
        warped[near] = rng.uniform(0, 300, int(near.sum()))
        self.assertLess(normqc.ncc(template, warped, brain), 0.99)
        self.assertAlmostEqual(normqc.ncc_xl(template, warped, brain, lesion), 1.0)
        self.assertAlmostEqual(normqc.nmi_xl(template, warped, brain, lesion), 2.0)
        self.assertAlmostEqual(normqc.nmi(template, template, brain), 2.0)
        independent = rng.uniform(0, 1, SHAPE)
        self.assertLess(normqc.nmi(template, independent, brain), 1.1)
        self.assertAlmostEqual(normqc.ncc(template, 3 * template + 5, brain), 1.0)
        self.assertTrue(math.isnan(normqc.ncc(template, np.ones(SHAPE), brain)))
        region = normqc.similarity_region(brain, lesion)
        self.assertFalse((region & near).any())
        np.testing.assert_array_equal(region, brain & normqc.similarity_region(np.ones(SHAPE), lesion))


class NormQcRulesTest(unittest.TestCase):
    GOOD = {"ncc_xl": 0.70, "normalized_ratio": 1.0, "logJ_lesion_minus_brain": -0.10, "folding_pct": 0.02, "outside_brain_pct": 0.05, "brain_dice": 0.95, "ventricle_pct": 0.0}

    def test_calibrated_ranges(self):
        result = normqc.evaluate(self.GOOD)
        self.assertEqual(result["suggestion"], normqc.SUGGEST_OK)
        self.assertEqual(set(result["levels"].values()), {normqc.LEVEL_OK})
        for key, ranges in normqc.CALIBRATION.items():
            metric = "logJ_lesion_minus_brain" if key.startswith("abs_") else key
            good = [v for name, span in ranges.items() if name == "good" for v in span]
            bad = [v for name, span in ranges.items() if name != "good" for v in span]
            for value in good:
                self.assertEqual(normqc.evaluate({**self.GOOD, metric: value})["levels"][metric], normqc.LEVEL_OK, (metric, value))
            for value in bad:
                self.assertEqual(normqc.evaluate({**self.GOOD, metric: value})["levels"][metric], normqc.LEVEL_AMBER, (metric, value))

    def test_ncc_alone_misses_a_collapse(self):
        # A-03: the small-lesion collapse had NCC 0.774 (good) but the volume and Jacobian checks flag it
        collapse = {**self.GOOD, "ncc_xl": 0.774, "normalized_ratio": 0.21, "logJ_lesion_minus_brain": -1.4}
        result = normqc.evaluate(collapse)
        self.assertEqual(result["levels"]["ncc_xl"], normqc.LEVEL_OK)
        self.assertEqual(result["suggestion"], normqc.SUGGEST_REVIEW)
        self.assertEqual(len(result["reasons"]), 2)
        self.assertIn("0.21", result["reasons"][0])

    def test_stage_and_method_reasons(self):
        affine_only = {key: value for key, value in self.GOOD.items() if key not in ("logJ_lesion_minus_brain", "folding_pct")}
        self.assertEqual(normqc.evaluate({**affine_only, "ncc_xl": 0.45})["suggestion"], normqc.SUGGEST_OK)
        self.assertEqual(normqc.evaluate({**self.GOOD, "ncc_xl": 0.45})["suggestion"], normqc.SUGGEST_REVIEW)
        self.assertEqual(normqc.evaluate({**affine_only, "ncc_xl": 0.45}, {"stage": "bspline"})["suggestion"], normqc.SUGGEST_REVIEW)
        for method in ({"dilate_mm": -1}, {"masking": False}, {"cross_modality": True}, {"scan_only": True}, {"attempts": [{"name": "A1"}, {"name": "A2"}]}, {"attempts": 3}):
            self.assertEqual(normqc.evaluate(self.GOOD, method)["suggestion"], normqc.SUGGEST_REVIEW, method)
        self.assertEqual(normqc.evaluate(self.GOOD, {"cross_modality": True, "two_step": True, "attempts": 1, "dilate_mm": 3})["suggestion"], normqc.SUGGEST_OK)
        self.assertEqual(normqc.evaluate({**self.GOOD, "folding_pct": float("nan")})["levels"]["folding_pct"], normqc.LEVEL_NA)

    def test_normqc_json(self):
        metrics = {**self.GOOD, "ncc_xl": np.float32(0.3), "folding_pct": float("nan"), "centroid_mni": np.array([-20.0, 6.0, 8.0]), "side": "L", "n": np.int64(3)}
        result = normqc.evaluate(metrics)
        doc = normqc.normqc_json(metrics, result, {"tool": "BRAINSFit", "dilate_mm": 3}, versions={"numpy": np.__version__}, subject="sub-01", template="MNI152NLin2009cAsym")
        text = json.dumps(doc, allow_nan=False)
        loaded = json.loads(text)
        self.assertEqual(loaded["format"], normqc.NORMQC_FORMAT)
        self.assertEqual(loaded["suggestion"], normqc.SUGGEST_REVIEW)
        self.assertEqual(loaded["levels"]["ncc_xl"], normqc.LEVEL_AMBER)
        self.assertIsNone(loaded["metrics"]["folding_pct"])
        self.assertEqual(loaded["metrics"]["centroid_mni"], [-20.0, 6.0, 8.0])
        self.assertEqual(loaded["metrics"]["n"], 3)
        self.assertAlmostEqual(loaded["thresholds"]["abs_logJ_lesion_minus_brain_max"], math.log(1.4), places=4)
        self.assertIn("not a verdict", loaded["note"])
        # Levels only: the suggestion follows the levels
        doc = normqc.normqc_json(self.GOOD, {"ncc_xl": normqc.LEVEL_OK}, None, versions={})
        self.assertEqual(doc["suggestion"], normqc.SUGGEST_OK)
        self.assertIn("LesionNetworkMappingLib", normqc.normqc_json({}, {}, None)["versions"])


class NormQcMosaicTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="lnm_normqc_")
        world = _world()
        self.template, self.brain, self.lesion = _template_and_lesion(world)
        self.warped = np.roll(self.template, 1, axis=1)

    def tearDown(self):
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def _red_pixels(self, image, box):
        x0, y0, x1, y1 = box
        rgb = np.asarray(image.convert("RGB"))[y0:y1, x0:x1].astype(int)
        ys, xs = np.nonzero((rgb[..., 0] == 255) & (rgb[..., 1] == 0) & (rgb[..., 2] == 0))
        return xs + x0, ys + y0

    def test_mosaic_size_and_determinism(self):
        from PIL import Image

        for checkerboard in (True, False):
            path = os.path.join(self.tempdir, f"m{int(checkerboard)}.png")
            layout = normqc.qc_mosaic_png((self.template, AFFINE), (self.warped, AFFINE), self.brain, self.lesion, path, checkerboard=checkerboard, title="tpl | sub-01 | research use only")
            self.assertTrue(os.path.isfile(path))
            # Cells: the largest in-plane extent in mm (1 pixel per mm by default)
            extent = [n * SPACING for n in SHAPE]
            cell_w = max(extent[0], extent[0], extent[1])
            cell_h = max(extent[1], extent[2], extent[2])
            expected = (3 * cell_w + 2 * normqc.CELL_GAP, normqc.TITLE_HEIGHT + 3 * cell_h + 2 * normqc.CELL_GAP)
            with Image.open(path) as image:
                self.assertEqual(image.size, expected)
                self.assertEqual(image.mode, "RGB")
            self.assertEqual(tuple(layout["size"]), expected)
            self.assertEqual(len(layout["cells"]), 9)
            self.assertEqual([cell["plane"] for cell in layout["cells"]], [plane for plane in normqc.PLANES for _ in range(3)])
            self.assertLess(os.path.getsize(path), 300 * 1024)
            again = os.path.join(self.tempdir, f"again{int(checkerboard)}.png")
            normqc.qc_mosaic_png((self.template, AFFINE), (self.warped, AFFINE), self.brain, self.lesion, again, checkerboard=checkerboard, title="tpl | sub-01 | research use only")
            with open(path, "rb") as f1, open(again, "rb") as f2:
                self.assertEqual(f1.read(), f2.read())
        # Slices go through the lesion centroid; plain mosaics step by offset_mm around it
        np.testing.assert_allclose(layout["centroid_mm"], LESION_CENTER, atol=SPACING)
        self.assertEqual(layout["slices"]["axial"][1] - layout["slices"]["axial"][0], 4)
        # Half the scale gives half the cells
        small = normqc.qc_mosaic_png(self.template, self.warped, self.brain, self.lesion, os.path.join(self.tempdir, "s.png"), affine=AFFINE, pixels_per_mm=0.5)
        self.assertEqual(small["size"], (3 * 56 + 2 * normqc.CELL_GAP, 3 * 56 + 2 * normqc.CELL_GAP))

    def test_left_lesion_on_the_L_side(self):
        from PIL import Image

        paths = {}
        for name, affine, flip in (("ras", AFFINE, False), ("las", _flip_i(AFFINE, SHAPE), True)):
            # Same world content stored in RAS or LAS voxel order
            def stored(volume, flip=flip):
                return volume[::-1] if flip else volume

            path = os.path.join(self.tempdir, f"{name}.png")
            paths[name] = path
            layout = normqc.qc_mosaic_png(stored(self.template), stored(self.warped), stored(self.brain), stored(self.lesion), path, affine=affine)
            with Image.open(path) as image:
                for cell in layout["cells"]:
                    if cell["col"] != 0:
                        continue
                    xs, ys = self._red_pixels(image, cell["box"])
                    self.assertGreater(len(xs), 10, cell["plane"])
                    x0, y0, x1, y1 = cell["box"]
                    middle_x = (x0 + x1) / 2
                    stamps = cell["stamps"]
                    if cell["plane"] in ("axial", "coronal"):
                        # The lesion (x = -20 mm) is drawn on the side stamped L
                        self.assertLess(stamps["L"][0], middle_x)
                        self.assertGreater(stamps["R"][0], middle_x)
                        self.assertLess(xs.mean(), middle_x, (name, cell["plane"]))
                        self.assertLess(abs(xs.mean() - stamps["L"][0]), abs(xs.mean() - stamps["R"][0]))
                    else:
                        # Sagittal: the lesion is anterior (y = +6 mm, brain centre y = 0) and drawn on the A side
                        self.assertLess(stamps["A"][0], middle_x)
                        self.assertLess(xs.mean(), middle_x, (name, cell["plane"]))
                    if cell["plane"] in ("coronal", "sagittal"):
                        # Superior lesion (z = +8 mm, brain centre z = -10 mm): above the middle, on the S side
                        self.assertLess(ys.mean(), (y0 + y1) / 2)
                        self.assertLess(stamps["S"][1], stamps["I"][1])
        # The voxel order of the file does not change the picture
        with open(paths["ras"], "rb") as f1, open(paths["las"], "rb") as f2:
            self.assertEqual(f1.read(), f2.read())

    def test_mirrored_lesion_moves_to_the_R_side(self):
        from PIL import Image

        mirrored = _sphere(_world(), LESION_CENTER * [-1, 1, 1], LESION_RADIUS).astype(np.float32)
        path = os.path.join(self.tempdir, "right.png")
        layout = normqc.qc_mosaic_png(self.template, self.warped, self.brain, mirrored, path, affine=AFFINE)
        axial = layout["cells"][0]
        with Image.open(path) as image:
            xs, _ = self._red_pixels(image, axial["box"])
        self.assertGreater(xs.mean(), (axial["box"][0] + axial["box"][2]) / 2)

    def test_errors(self):
        path = os.path.join(self.tempdir, "x.png")
        with self.assertRaises(ValueError):
            normqc.qc_mosaic_png(None, self.warped, self.brain, self.lesion, path, affine=AFFINE)
        with self.assertRaises(ValueError):
            normqc.qc_mosaic_png(self.template, self.warped, self.brain, self.lesion, path)
        with self.assertRaises(ValueError):
            normqc.qc_mosaic_png((self.template, AFFINE), (self.warped, AFFINE * 2), self.brain, self.lesion, path)
        # Without the template, the plain mosaic works and an empty lesion falls back to the brain centre
        layout = normqc.qc_mosaic_png(None, self.warped, self.brain, np.zeros(SHAPE), path, checkerboard=False, affine=AFFINE)
        self.assertEqual(len(layout["cells"]), 9)


if __name__ == "__main__":
    unittest.main()

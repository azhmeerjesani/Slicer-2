"""Tests of the figure-fit numerics (figurefit.py; addendum §3.10, lesion-input research §1.5-1.7).

Synthetic landmarks with known answers: exact recovery of a similarity and of an affine (RMS 0), the
rule that an affine needs at least 4 pairs, reflections refused by the similarity, leave-one-out RMS
above RMS with noise, the residual levels, the cut plane (orthonormal, equal to Slicer's standard
slices without tilt, pitch and roll applied, ±20° limit, SetSliceToRASByNTP copy), the explicit L/R
convention (a radiological figure point lands on the other side), the figure IJK-to-RAS matrix, and
the versioned fit JSON (round trip, consistency checks, schema errors).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingFigureFitTest.py -v
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

from LesionNetworkMappingLib import figurefit

# Figure pixels of a few landmarks (u = column, v = row), spread like anatomy on a 300 x 360 crop
FIG_PX = np.array([[150.0, 20.0], [152.0, 340.0], [110.0, 120.0], [190.0, 118.0], [151.0, 250.0], [40.0, 170.0], [262.0, 172.0]])


def similarity_matrix(scale, angle_deg, translation):
    angle = math.radians(angle_deg)
    matrix = np.eye(3)
    matrix[:2, :2] = scale * np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
    matrix[:2, 2] = translation
    return matrix


AFFINE = np.array([[0.52, 0.07, -71.0], [-0.04, 0.47, -88.0], [0.0, 0.0, 1.0]])
SHA = "ab" * 32


class FitTest(unittest.TestCase):
    def test_similarity_exact(self):
        truth = similarity_matrix(0.48, 7.5, [-70.0, -90.0])
        dst = figurefit.apply_2d(truth, FIG_PX)
        fit = figurefit.umeyama_similarity(FIG_PX, dst)
        np.testing.assert_allclose(fit, truth, atol=1e-9)
        self.assertLess(figurefit.rms(fit, FIG_PX, dst), 1e-9)
        self.assertLess(figurefit.loo_rms("similarity", FIG_PX, dst), 1e-9)
        # two pairs determine a similarity
        np.testing.assert_allclose(figurefit.umeyama_similarity(FIG_PX[:2], dst[:2]), truth, atol=1e-9)

    def test_affine_exact(self):
        dst = figurefit.apply_2d(AFFINE, FIG_PX)
        fit = figurefit.affine_2d(FIG_PX, dst)
        np.testing.assert_allclose(fit, AFFINE, atol=1e-9)
        self.assertLess(figurefit.rms(fit, FIG_PX, dst), 1e-9)
        self.assertLess(figurefit.loo_rms("affine", FIG_PX, dst), 1e-9)
        np.testing.assert_allclose(figurefit.affine_2d(FIG_PX[:4], dst[:4]), AFFINE, atol=1e-9)

    def test_affine_needs_four_pairs(self):
        dst = figurefit.apply_2d(AFFINE, FIG_PX)
        with self.assertRaisesRegex(ValueError, "at least 4"):
            figurefit.affine_2d(FIG_PX[:3], dst[:3])
        with self.assertRaisesRegex(ValueError, "at least 4"):
            figurefit.fit_2d("affine", FIG_PX[:3], dst[:3])
        self.assertEqual(figurefit.MIN_AFFINE_PAIRS, 4)
        # LOO needs a fold of at least 3 pairs, so 4 pairs in all
        self.assertIsNone(figurefit.loo_rms("affine", FIG_PX[:3], dst[:3]))
        self.assertIsNotNone(figurefit.loo_rms("affine", FIG_PX[:4], dst[:4]))
        with self.assertRaisesRegex(ValueError, "collinear"):
            line = np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]])
            figurefit.affine_2d(line, line)

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            figurefit.umeyama_similarity(FIG_PX[:1], FIG_PX[:1])
        with self.assertRaises(ValueError):
            figurefit.umeyama_similarity(FIG_PX, FIG_PX[:3])
        with self.assertRaisesRegex(ValueError, "coincide"):
            figurefit.umeyama_similarity([[1.0, 1.0]] * 3, FIG_PX[:3])
        bad = FIG_PX.copy()
        bad[0, 0] = np.nan
        with self.assertRaisesRegex(ValueError, "finite"):
            figurefit.affine_2d(bad, FIG_PX)
        with self.assertRaises(ValueError):
            figurefit.fit_2d("rigid", FIG_PX, FIG_PX)
        with self.assertRaises(ValueError):
            figurefit.loo_rms("rigid", FIG_PX, FIG_PX)

    def test_similarity_refuses_reflection(self):
        truth = similarity_matrix(0.5, 0.0, [0.0, 0.0]) @ np.diag([-1.0, 1.0, 1.0])
        dst = figurefit.apply_2d(truth, FIG_PX)
        fit = figurefit.umeyama_similarity(FIG_PX, dst)
        self.assertGreater(np.linalg.det(fit[:2, :2]), 0)
        self.assertGreater(figurefit.rms(fit, FIG_PX, dst), 10.0)
        self.assertTrue(figurefit.mirror_suspected(FIG_PX, dst))
        self.assertFalse(figurefit.mirror_suspected(FIG_PX, figurefit.apply_2d(similarity_matrix(0.5, 3.0, [1.0, 2.0]), FIG_PX)))

    def test_affine_refuses_reflection(self):
        # an affine could absorb an L/R mirror exactly; it must refuse instead (the convention is explicit)
        dst = figurefit.apply_2d(AFFINE @ np.diag([-1.0, 1.0, 1.0]), FIG_PX)
        with self.assertRaisesRegex(ValueError, "reflection"):
            figurefit.affine_2d(FIG_PX, dst)
        with self.assertRaisesRegex(ValueError, "reflection"):
            figurefit.fit_2d("affine", FIG_PX, dst)
        # reflected folds give no LOO RMS
        self.assertIsNone(figurefit.loo_rms("affine", FIG_PX, dst))

    def test_noise_loo_above_rms(self):
        rng = np.random.default_rng(22)
        for kind, truth in (("similarity", similarity_matrix(0.5, -4.0, [-75.0, -85.0])), ("affine", AFFINE)):
            for _trial in range(5):
                dst = figurefit.apply_2d(truth, FIG_PX) + rng.normal(scale=1.5, size=FIG_PX.shape)
                fit = figurefit.fit_2d(kind, FIG_PX, dst)
                value = figurefit.rms(fit, FIG_PX, dst)
                loo = figurefit.loo_rms(kind, FIG_PX, dst)
                self.assertGreater(value, 0.1)
                self.assertGreater(loo, value, kind)
        # the affine (more parameters) never has a larger in-sample RMS than the similarity
        dst = figurefit.apply_2d(similarity_matrix(0.5, 2.0, [0.0, 0.0]), FIG_PX) + rng.normal(scale=1.0, size=FIG_PX.shape)
        self.assertLessEqual(
            figurefit.rms(figurefit.affine_2d(FIG_PX, dst), FIG_PX, dst),
            figurefit.rms(figurefit.umeyama_similarity(FIG_PX, dst), FIG_PX, dst) + 1e-12,
        )

    def test_residual_level(self):
        self.assertEqual(figurefit.residual_level(1.94).level, "green")
        # the level follows the displayed value: 1.96 reads "2.0 mm" and is amber
        self.assertEqual(figurefit.residual_level(1.96).level, "amber")
        self.assertEqual(figurefit.residual_level(1.6).text, "RMS 1.6 mm — the residual measures landmark consistency, not lesion accuracy.")
        self.assertEqual(figurefit.residual_level(2.0).level, "amber")
        self.assertEqual(figurefit.residual_level(4.0).level, "amber")
        self.assertEqual(figurefit.residual_level(4.06).level, "red")
        self.assertEqual(figurefit.residual_level(None).level, "none")
        for value in (None, 1.0, 3.0, 9.0):
            self.assertIn("measures landmark consistency, not lesion accuracy", figurefit.residual_level(value).text)
        text, level = figurefit.residual_chip(1.6, 2.3, 5)
        self.assertEqual(text, "RMS 1.6 mm · LOO 2.3 mm (5 pairs)")
        self.assertEqual(level.level, "amber")  # follows the larger LOO RMS
        self.assertEqual(figurefit.residual_chip(0.5, None, 2)[0], "RMS 0.5 mm · LOO n/a (2 pairs)")

    def test_initial_pixel_size(self):
        self.assertAlmostEqual(figurefit.initial_pixel_size(280), 0.5)
        for bad in (0, -3, float("nan")):
            with self.assertRaises(ValueError):
                figurefit.initial_pixel_size(bad)

    def test_suggested_landmarks(self):
        axial = figurefit.suggested_landmarks("axial")
        for name in ("midline anterior", "midline posterior", "left frontal-horn tip", "right frontal-horn tip", "4th ventricle", "left sylvian fissure", "right sylvian fissure"):
            self.assertIn(name, axial)
        for plane in figurefit.PLANES:
            names = figurefit.suggested_landmarks(plane)
            self.assertGreaterEqual(len(names), figurefit.MIN_AFFINE_PAIRS)
            self.assertEqual(len(names), len(set(names)))
        with self.assertRaises(ValueError):
            figurefit.suggested_landmarks("oblique")


class CutPlaneTest(unittest.TestCase):
    # Slicer's standard SliceToRAS (patient right on screen left), vtkMRMLSliceNode::Get*SliceToRASMatrix
    STANDARD = {
        "axial": np.array([[-1.0, 0, 0], [0, 1, 0], [0, 0, 1]]),
        "coronal": np.array([[-1.0, 0, 0], [0, 0, 1], [0, 1, 0]]),
        "sagittal": np.array([[0, 0, -1.0], [-1, 0, 0], [0, 1, 0]]),
    }

    def assertOrthonormal(self, cut):
        frame = cut.slice_to_ras()[:3, :3]
        np.testing.assert_allclose(frame.T @ frame, np.eye(3), atol=1e-12)
        self.assertAlmostEqual(np.linalg.det(frame), 1.0)
        self.assertAlmostEqual(float(np.dot(cut.normal, cut.transverse)), 0.0)

    def test_standard_planes(self):
        for plane, standard in self.STANDARD.items():
            cut = figurefit.cut_plane(plane, 12.5)
            self.assertOrthonormal(cut)
            matrix = cut.slice_to_ras()
            np.testing.assert_allclose(matrix[:3, 0], standard[:, 0], atol=1e-12, err_msg=plane)
            np.testing.assert_allclose(matrix[:3, 1], standard[:, 1], atol=1e-12, err_msg=plane)
            # the normal may point the other way (NTP frames are right-handed); the plane is the same
            self.assertAlmostEqual(abs(float(np.dot(matrix[:3, 2], standard[:, 2]))), 1.0)
            axis = {"axial": 2, "coronal": 1, "sagittal": 0}[plane]
            self.assertEqual(cut.origin[axis], 12.5)
            self.assertEqual(np.count_nonzero(cut.origin), 1)

    def test_pitch_and_roll(self):
        for plane, standard in self.STANDARD.items():
            for pitch, roll in ((15.0, 0.0), (0.0, -12.0), (-20.0, 20.0), (7.0, 3.0)):
                cut = figurefit.cut_plane(plane, -4.0, pitch, roll)
                self.assertOrthonormal(cut)
                y_axis = cut.slice_to_ras()[:3, 1]
                x_axis = cut.slice_to_ras()[:3, 0]
                # the top edge tilts towards +Z of the standard slice by the pitch (roll keeps y)
                self.assertAlmostEqual(float(np.dot(y_axis, standard[:, 2])), math.sin(math.radians(pitch)), msg=plane)
                self.assertAlmostEqual(float(np.dot(y_axis, standard[:, 1])), math.cos(math.radians(pitch)), msg=plane)
                # the right edge tilts towards +Z by the roll and stays in the standard x-z plane after pitch
                self.assertAlmostEqual(float(np.dot(x_axis, standard[:, 0])), math.cos(math.radians(roll)), msg=plane)
                angle = math.degrees(math.acos(min(1.0, abs(float(np.dot(cut.normal, standard[:, 2]))))))
                expected = math.degrees(math.acos(math.cos(math.radians(pitch)) * math.cos(math.radians(roll))))
                self.assertAlmostEqual(angle, expected, places=6)
                # the turning point stays on the plane
                self.assertAlmostEqual(float(cut.to_plane([cut.origin])[1][0]), 0.0)

    def test_pitch_superior(self):
        # axial, pitch +10: the anterior part of the plane rises
        cut = figurefit.cut_plane("axial", 0.0, 10.0, 0.0)
        anterior = cut.origin + 50.0 * cut.y_axis
        self.assertAlmostEqual(anterior[2], 50.0 * math.sin(math.radians(10.0)))
        self.assertGreater(anterior[1], 0)

    def test_tilt_limits(self):
        figurefit.cut_plane("axial", 0.0, 20.0, -20.0)
        for pitch, roll in ((20.5, 0.0), (0.0, -21.0)):
            with self.assertRaisesRegex(ValueError, "20"):
                figurefit.cut_plane("axial", 0.0, pitch, roll)
        with self.assertRaises(ValueError):
            figurefit.cut_plane("axial", float("inf"))
        with self.assertRaises(ValueError):
            figurefit.cut_plane("transverse")

    def test_ntp_copy(self):
        # SetSliceToRASByNTP orthogonalises T against N and normalises
        normal = np.array([0.0, 0.2, 2.0])
        transverse = np.array([-1.0, 0.3, 0.1])
        for orientation in (0, 1, 2):
            matrix = figurefit.ntp_slice_to_ras(normal, transverse, [1.0, 2.0, 3.0], orientation)
            frame = matrix[:3, :3]
            np.testing.assert_allclose(frame.T @ frame, np.eye(3), atol=1e-12)
            np.testing.assert_allclose(matrix[:3, 3], [1.0, 2.0, 3.0])
        matrix = figurefit.ntp_slice_to_ras(normal, transverse, [0, 0, 0], 0)
        np.testing.assert_allclose(matrix[:3, 2], normal / np.linalg.norm(normal))
        with self.assertRaises(ValueError):
            figurefit.ntp_slice_to_ras(normal, transverse, [0, 0, 0], 3)

    def test_plane_coordinates_round_trip(self):
        cut = figurefit.cut_plane("coronal", -18.0, 6.0, -9.0)
        points = np.array([[10.0, -4.0], [-33.0, 25.0], [0.0, 0.0]])
        ras = cut.to_ras(points)
        back, distance = cut.to_plane(ras)
        np.testing.assert_allclose(back, points, atol=1e-12)
        np.testing.assert_allclose(distance, 0.0, atol=1e-12)
        _plane, distance = cut.to_plane(ras + 3.0 * cut.normal)
        np.testing.assert_allclose(distance, 3.0, atol=1e-12)


class ConventionTest(unittest.TestCase):
    def test_convention_matrix(self):
        np.testing.assert_array_equal(figurefit.convention_matrix("radiological"), np.diag([-1.0, 1.0, 1.0]))
        np.testing.assert_array_equal(figurefit.convention_matrix("neurological"), np.eye(3))
        self.assertEqual(figurefit.DEFAULT_CONVENTION, "radiological")
        with self.assertRaises(ValueError):
            figurefit.convention_matrix("left")

    def test_radiological_mirror_flips_x(self):
        cut = figurefit.cut_plane("axial", 10.0)
        spacing = figurefit.initial_pixel_size(280)
        size = (301, 361)
        centre = np.array([150.0, 180.0, 0.0, 1.0])
        right_of_image = np.array([250.0, 180.0, 0.0, 1.0])
        neuro = figurefit.figure_ijk_to_ras(None, cut, spacing, "neurological", size_px=size)
        radio = figurefit.figure_ijk_to_ras(None, cut, spacing, "radiological", size_px=size)
        np.testing.assert_allclose((neuro @ centre)[:3], [0.0, 0.0, 10.0], atol=1e-9)
        np.testing.assert_allclose((radio @ centre)[:3], [0.0, 0.0, 10.0], atol=1e-9)
        # image right is the patient's right (+R) in a neurological figure and left (-R) in a radiological one
        neuro_point = (neuro @ right_of_image)[:3]
        radio_point = (radio @ right_of_image)[:3]
        self.assertAlmostEqual(neuro_point[0], 50.0)
        self.assertAlmostEqual(radio_point[0], -50.0)
        np.testing.assert_allclose(neuro_point[1:], radio_point[1:])
        # image down is posterior on an axial plane, inferior on a coronal plane
        below = np.array([150.0, 280.0, 0.0, 1.0])
        self.assertAlmostEqual((neuro @ below)[1], -50.0)
        coronal = figurefit.figure_ijk_to_ras(None, figurefit.cut_plane("coronal", 0.0), spacing, "radiological", size_px=size)
        self.assertAlmostEqual((coronal @ below)[2], -50.0)
        self.assertAlmostEqual((coronal @ right_of_image)[0], -50.0)

    def test_fit_with_convention(self):
        # A radiological figure: patient left (template x < 0) drawn on the image right
        cut = figurefit.cut_plane("axial", 20.0, 8.0, 0.0)
        truth = similarity_matrix(0.5, 3.0, [-75.0, -90.0])
        plane_points = figurefit.apply_2d(truth, figurefit.apply_2d(figurefit.convention_matrix("radiological"), FIG_PX))
        tpl_ras = cut.to_ras(plane_points)
        fit = figurefit.fit_figure(FIG_PX, tpl_ras, cut, "radiological", "similarity")
        np.testing.assert_allclose(fit.matrix, truth, atol=1e-9)
        self.assertLess(fit.rms_mm, 1e-9)
        self.assertFalse(fit.mirror_suspected)
        self.assertEqual(fit.n_pairs, len(FIG_PX))
        np.testing.assert_allclose(fit.out_of_plane_mm, 0.0, atol=1e-9)
        ijk_to_ras = figurefit.figure_ijk_to_ras(fit, cut, convention="radiological")
        homogeneous = np.column_stack([FIG_PX, np.zeros(len(FIG_PX)), np.ones(len(FIG_PX))])
        np.testing.assert_allclose((homogeneous @ ijk_to_ras.T)[:, :3], tpl_ras, atol=1e-9)
        # the k column is the plane normal scaled to the pixel size
        np.testing.assert_allclose(ijk_to_ras[:3, 2], cut.normal * 0.5, atol=1e-12)
        # the same landmarks fitted with the wrong convention: no proper similarity fits, mirror suspected
        wrong = figurefit.fit_figure(FIG_PX, tpl_ras, cut, "neurological", "similarity")
        self.assertGreater(wrong.rms_mm, 10.0)
        self.assertTrue(wrong.mirror_suspected)
        # an affine cannot hide the wrong convention behind a reflection
        with self.assertRaisesRegex(ValueError, "reflection"):
            figurefit.fit_figure(FIG_PX, tpl_ras, cut, "neurological", "affine")
        # affine fit through the same path
        affine = figurefit.fit_figure(FIG_PX, tpl_ras, cut, "radiological", "affine")
        self.assertLess(affine.rms_mm, 1e-9)
        with self.assertRaises(ValueError):
            figurefit.fit_figure(FIG_PX[:3], tpl_ras[:3], cut, "radiological", "affine")
        with self.assertRaises(ValueError):
            figurefit.fit_figure(FIG_PX, tpl_ras[:3], cut, "radiological", "similarity")

    def test_parent_transform(self):
        cut = figurefit.cut_plane("sagittal", -30.0)
        target = figurefit.figure_ijk_to_ras(None, cut, 0.6, "radiological", size_px=(200, 220))
        # a PNG loaded by Slicer has IJKToRAS diag(-1, -1, 1) (LPS storage)
        volume = np.diag([-1.0, -1.0, 1.0, 1.0])
        parent = figurefit.parent_transform(target, volume)
        np.testing.assert_allclose(parent @ volume, target, atol=1e-12)
        with self.assertRaises(ValueError):
            figurefit.figure_ijk_to_ras(None, cut, 0.6)
        with self.assertRaises(ValueError):
            figurefit.figure_ijk_to_ras(np.zeros((3, 3)), cut)


class FitJsonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="figurefit-")
        self.cut = figurefit.cut_plane("axial", 22.0, -6.6, 2.0)
        rng = np.random.default_rng(5)
        plane_points = figurefit.apply_2d(AFFINE, figurefit.apply_2d(figurefit.convention_matrix("radiological"), FIG_PX))
        self.tpl_ras = self.cut.to_ras(plane_points + rng.normal(scale=0.8, size=plane_points.shape))
        self.fit = figurefit.fit_figure(FIG_PX, self.tpl_ras, self.cut, "radiological", "affine")
        names = figurefit.suggested_landmarks("axial")
        self.landmarks = [{"name": names[i], "fig_px": FIG_PX[i], "tpl_ras": self.tpl_ras[i]} for i in range(len(FIG_PX))]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def record(self, **overrides):
        arguments = {
            "image_sha256": SHA,
            "crop_px": [12, 30, 301, 361],
            "template": {"id": "MNI152NLin6Asym", "res": 1, "sha256": "cd" * 32},
            "tracer": "rater A",
            "z_mm": 22.0,
            "pitch_deg": -6.6,
            "roll_deg": 2.0,
            "created": "2026-10-22T09:30:00+00:00",
        }
        arguments.update(overrides)
        return figurefit.make_record(self.fit, "axial", "radiological", self.landmarks, self.cut, **arguments)

    def test_round_trip(self):
        record = self.record()
        self.assertEqual(record["format"], "slicer-studio-figure-fit")
        self.assertEqual(record["version"], 1)
        for key in ("image_sha256", "crop_px", "plane", "convention", "landmarks", "model", "matrix", "rms_mm", "loo_rms_mm", "cut_plane", "template", "tracer", "created"):
            self.assertIn(key, record)
        for key in ("origin_ras", "normal", "z_mm", "pitch_deg", "roll_deg"):
            self.assertIn(key, record["cut_plane"])
        self.assertGreater(record["loo_rms_mm"], record["rms_mm"])
        path = os.path.join(self.tmp, "fig-1_fit.json")
        figurefit.write_fit_json(path, record)
        self.assertEqual(os.listdir(self.tmp), ["fig-1_fit.json"])
        loaded = figurefit.read_fit_json(path)
        self.assertEqual(loaded, json.loads(json.dumps(record)))
        np.testing.assert_allclose(loaded["matrix"], figurefit.figure_ijk_to_ras(self.fit, self.cut, convention="radiological"))
        cut = figurefit.record_cut_plane(loaded)
        np.testing.assert_allclose(cut.normal, self.cut.normal)
        # no local paths in the shared file
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(self.tmp, text)
        # unknown keys are kept; the default creation time is ISO 8601
        loaded["note"] = "kept"
        figurefit.write_fit_json(path, loaded)
        self.assertEqual(figurefit.read_fit_json(path)["note"], "kept")
        self.assertIn("T", self.record(created=None)["created"])

    def assertInvalid(self, record, pattern):
        with self.assertRaises(figurefit.FigureFitError) as context:
            figurefit.validate_record(record)
        self.assertRegex(str(context.exception), pattern)
        self.assertIsInstance(context.exception, ValueError)

    def test_schema_errors(self):
        good = self.record()

        def changed(path, value):
            record = json.loads(json.dumps(good))
            target = record
            for key in path[:-1]:
                target = target[key]
            if value is KeyError:
                del target[path[-1]]
            else:
                target[path[-1]] = value
            return record

        self.assertInvalid([], "JSON object")
        self.assertInvalid(changed(["format"], "other"), "format")
        self.assertInvalid(changed(["version"], 2), "newer")
        self.assertInvalid(changed(["version"], "1"), "version")
        self.assertInvalid(changed(["image_sha256"], "xyz"), "image_sha256")
        self.assertInvalid(changed(["crop_px"], [0, 0, 0, 10]), "crop_px")
        self.assertInvalid(changed(["plane"], "oblique"), "plane")
        self.assertInvalid(changed(["convention"], KeyError), "convention")
        self.assertInvalid(changed(["landmarks", 0, "tpl_ras"], [1, 2]), "landmark 1")
        self.assertInvalid(changed(["landmarks"], good["landmarks"][:3]), "at least 4")
        self.assertInvalid(changed(["model"], "rigid"), "model")
        self.assertInvalid(changed(["matrix"], [[1, 0, 0]]), "4x4")
        self.assertInvalid(changed(["matrix", 3], [0, 0, 1, 1]), "last row")
        self.assertInvalid(changed(["matrix", 0, 3], good["matrix"][0][3] + 1.0), "matrix does not match")
        self.assertInvalid(changed(["rms_mm"], -1), "rms_mm")
        self.assertInvalid(changed(["rms_mm"], good["rms_mm"] + 0.5), "rms_mm .* does not match")
        self.assertInvalid(changed(["loo_rms_mm"], None), "loo_rms_mm .* does not match")
        self.assertInvalid(changed(["cut_plane", "pitch_deg"], 25.0), "pitch_deg")
        self.assertInvalid(changed(["cut_plane", "z_mm"], 30.0), "does not match plane")
        self.assertInvalid(changed(["cut_plane", "normal"], [0, 0]), "normal")
        self.assertInvalid(changed(["template", "res"], 0), "template")
        self.assertInvalid(changed(["template", "sha256"], "abc"), "template.sha256")
        self.assertInvalid(changed(["tracer"], None), "tracer")
        self.assertInvalid(changed(["created"], "yesterday"), "created")
        self.assertInvalid(changed(["created"], "2026-10-22"), "created")
        self.assertInvalid(changed(["created"], "2026-10-22T09:30:00"), "UTC offset")
        # a reflected fit_2d (matrix rebuilt to match) is refused: the convention is recorded, not fitted
        mirrored = changed(["fit_2d"], (np.asarray(good["fit_2d"]) @ np.diag([-1.0, 1.0, 1.0])).tolist())
        self.assertInvalid(mirrored, "reflection")
        self.assertInvalid(changed(["fit_2d", 2], [0, 0, 2]), "fit_2d must have the last row")
        # a record labelled similarity must carry a scaled rotation
        sheared = changed(["model"], "similarity")
        self.assertInvalid(sheared, "scaled rotation")
        # a huge JSON integer is a schema error, not an OverflowError
        self.assertInvalid(changed(["rms_mm"], 10**400), "rms_mm")
        # files that are not UTF-8 or not JSON raise FigureFitError
        path = os.path.join(self.tmp, "garbled.json")
        for content in (b"\xff\xfe{", b"{", b"[" * 100000):
            with open(path, "wb") as f:
                f.write(content)
            with self.assertRaises(figurefit.FigureFitError):
                figurefit.read_fit_json(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(good).replace('"rms_mm": ', '"rms_mm": 1' + "0" * 400 + ', "x": '))
        with self.assertRaises(figurefit.FigureFitError):
            figurefit.read_fit_json(path)
        # NaN is not valid JSON for a fit record
        with self.assertRaises(ValueError):
            figurefit.write_fit_json(os.path.join(self.tmp, "nan.json"), changed(["rms_mm"], float("nan")))
        # every problem is listed
        record = changed(["plane"], "oblique")
        record["tracer"] = 5
        with self.assertRaises(figurefit.FigureFitError) as context:
            figurefit.validate_record(record)
        self.assertEqual(len(context.exception.errors), 2)
        # an invalid record is never written
        bad_path = os.path.join(self.tmp, "bad.json")
        with self.assertRaises(figurefit.FigureFitError):
            figurefit.write_fit_json(bad_path, record)
        self.assertFalse(os.path.exists(bad_path))
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaisesRegex(figurefit.FigureFitError, "not valid JSON"):
            figurefit.read_fit_json(bad_path)


if __name__ == "__main__":
    unittest.main()

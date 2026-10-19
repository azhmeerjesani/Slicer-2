"""Unit tests of the space/grid registry (LesionNetworkMappingLib.geometry) and the template-family
transforms (LesionNetworkMappingLib.transforms).

Run without Slicer's GUI (no network; SimpleITK is bundled with Slicer's Python):

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingSpaceTest.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

import numpy as np

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from LesionNetworkMappingLib import geometry, nifti, transforms

try:
    import SimpleITK as sitk
except ImportError:  # pragma: no cover (bundled with Slicer)
    sitk = None

# detect_grid's grids before the registry (results must not change)
LEGACY_KNOWN_GRIDS = (
    ("MNI152NLin6Asym", 1, (182, 218, 182), 1.0, (-91.0, -126.0, -72.0)),
    ("MNI152NLin6Asym", 2, (91, 109, 91), 2.0, (-90.0, -126.0, -72.0)),
    ("MNI152NLin2009cAsym", 1, (193, 229, 193), 1.0, (-96.0, -132.0, -78.0)),
    ("MNI152NLin2009cAsym", 2, (97, 115, 97), 2.0, (-96.5, -132.5, -78.5)),
)
EXPECTED_CANDIDATES = {
    "NLin6-1mm": (("MNI152NLin6Asym",), "fsl6", 1, False),
    "NLin6-2mm": (("MNI152NLin6Asym",), "fsl6", 2, False),
    "TF2009-1mm": (("MNI152NLin2009cAsym", "MNI152NLin2009aSym", "MNI152NLin2009cSym"), "2009", 1, True),
    "TF2009-2mm": (("MNI152NLin2009cAsym", "MNI152NLin2009aSym", "MNI152NLin2009cSym"), "2009", 2, True),
    "McGill2009a-1mm": (("MNI152NLin2009aSym",), "2009", 1, False),
    "SPM-1mm": (("MNI152Lin", "MNIColin27"), "spm", 1, True),
}


def _ras(spec):
    """RAS+ storage of a registry grid."""
    affine = np.diag([spec.spacing, spec.spacing, spec.spacing, 1.0])
    affine[:3, 3] = spec.lowest_center
    return affine


def _las(spec):
    """Radiological (x flipped) storage of a registry grid."""
    affine = _ras(spec)
    affine[0, 0] = -spec.spacing
    affine[0, 3] = spec.lowest_center[0] + spec.spacing * (spec.shape[0] - 1)
    return affine


class GridRegistryTest(unittest.TestCase):
    def test_registry_contents(self):
        self.assertEqual(sorted(geometry.GRID_BY_ID), sorted(EXPECTED_CANDIDATES))
        self.assertEqual(geometry.KNOWN_GRIDS, LEGACY_KNOWN_GRIDS)
        nlin6 = geometry.GRID_BY_ID["NLin6-1mm"]
        np.testing.assert_array_equal(nlin6.affine, [[-1, 0, 0, 90], [0, 1, 0, -126], [0, 0, 1, -72], [0, 0, 0, 1]])  # FSL / TemplateFlow res-01 storage (LAS)
        np.testing.assert_array_equal(geometry.GRID_BY_ID["TF2009-1mm"].affine, [[1, 0, 0, -96], [0, 1, 0, -132], [0, 0, 1, -78], [0, 0, 0, 1]])
        self.assertEqual(geometry.GRID_BY_ID["TF2009-1mm"].space, "MNI152NLin2009cAsym")
        for spec in geometry.GRID_REGISTRY:
            with self.subTest(grid=spec.grid_id):
                self.assertEqual(spec.space, spec.candidates[0])
                self.assertEqual(spec.ambiguous, len(spec.candidates) > 1)
                self.assertIn(spec.family, geometry.SPACE_FAMILIES)
                self.assertEqual(spec.grid.shape, spec.shape)

    def test_candidates_for_every_grid_and_storage(self):
        permutation = np.array([[0, 1.0, 0, 0], [0, 0, 1, 0], [1, 0, 0, 0], [0, 0, 0, 1]])  # axes stored (k, i, j)
        for spec in geometry.GRID_REGISTRY:
            candidates, family, resolution, ambiguous = EXPECTED_CANDIDATES[spec.grid_id]
            expected = geometry.GridMatch(spec.grid_id, candidates, family, resolution, ambiguous)
            shape = spec.shape
            storages = {
                "ras": (shape, _ras(spec)),
                "radiological": (shape, _las(spec)),
                "registry": (shape, spec.affine),
                "permuted": ((shape[2], shape[0], shape[1]), _las(spec) @ permutation),
                "shifted 0.4 mm": (shape, _ras(spec) + np.array([[0, 0, 0, 0.4], [0, 0, 0, -0.4], [0, 0, 0, 0], [0, 0, 0, 0]])),
            }
            for storage, (grid_shape, affine) in storages.items():
                with self.subTest(grid=spec.grid_id, storage=storage):
                    match = geometry.detect_grid_candidates(grid_shape, affine)
                    self.assertEqual(match, expected)
                    self.assertEqual(match.space, candidates[0])
            with self.subTest(grid=spec.grid_id, storage="shifted 1.5 mm"):
                self.assertIsNone(geometry.detect_grid_candidates(shape, _ras(spec) + np.array([[0, 0, 0, 1.5], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]])))
            with self.subTest(grid=spec.grid_id, storage="one slice less"):
                self.assertIsNone(geometry.detect_grid_candidates((shape[0], shape[1], shape[2] - 1), _ras(spec)))
        self.assertIsNone(geometry.detect_grid_candidates((91, 109, 91), np.diag([1.5, 1.5, 1.5, 1.0])))
        self.assertIsNone(geometry.detect_grid_candidates((4, 4), np.eye(4)))

    def test_detect_grid_unchanged(self):
        for space, resolution, shape, spacing, origin in LEGACY_KNOWN_GRIDS:
            spec = geometry.GridSpec("x", shape, spacing, origin, (space,), "x")
            for affine in (_ras(spec), _las(spec)):
                self.assertEqual(geometry.detect_grid(shape, affine), (space, resolution))
                self.assertEqual(geometry.detect_space(shape, affine), space)
        # Grids added to the registry stay "native" for detect_grid/detect_space(shape, affine)
        for grid_id in ("McGill2009a-1mm", "SPM-1mm"):
            spec = geometry.GRID_BY_ID[grid_id]
            self.assertEqual(geometry.detect_grid(spec.shape, spec.affine), ("native", None))
            self.assertEqual(geometry.detect_space(spec.shape, spec.affine), "native")

    def test_detect_space_of_a_path_or_geometry(self):
        tmp = tempfile.mkdtemp(prefix="lnm-space-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        mcgill = geometry.GRID_BY_ID["McGill2009a-1mm"]
        path = os.path.join(tmp, "atlas.nii.gz")
        nifti.write_nifti(path, np.zeros(mcgill.shape, dtype=np.uint8), mcgill.affine)
        self.assertEqual(geometry.detect_space(path), ("MNI152NLin2009aSym", 1))
        tf2009 = geometry.GRID_BY_ID["TF2009-2mm"]
        self.assertEqual(geometry.detect_space(tf2009.grid), ("MNI152NLin2009cAsym", 2))
        self.assertEqual(geometry.detect_space((tf2009.shape, tf2009.affine)), ("MNI152NLin2009cAsym", 2))
        self.assertEqual(geometry.detect_space({"shape": (91, 109, 91), "affine": _ras(geometry.GRID_BY_ID["NLin6-2mm"])}), ("MNI152NLin6Asym", 2))
        native = os.path.join(tmp, "native.nii")
        nifti.write_nifti(native, np.zeros((10, 12, 14), dtype=np.uint8), np.diag([0.9, 0.9, 3.0, 1.0]))
        self.assertIsNone(geometry.detect_space(native))
        with self.assertRaises(TypeError):
            geometry.detect_space(42)

    def test_space_families(self):
        cases = {
            "MNI152NLin6Asym": "fsl6",
            "tpl-MNI152NLin6Asym": "fsl6",
            "MNI152NLin2009cAsym": "2009",
            "space-MNI152NLin2009aSym": "2009",
            "mni152nlin2009csym": "2009",
            "MNI152NLin2009bAsym": "2009b",
            "MNI152Lin": "spm",
            "MNIColin27": "colin",
            "MNIPediatricAsym": "pediatric",
            "native": "native",
            "T1w": "native",
            "fsaverage": "unknown",
            "": "unknown",
            None: "unknown",
        }
        for space, family in cases.items():
            with self.subTest(space=space):
                self.assertEqual(geometry.space_family(space), family)
        for spec in geometry.GRID_REGISTRY:
            self.assertEqual(geometry.space_family(spec.space), spec.family if spec.grid_id != "SPM-1mm" else "spm")
        self.assertTrue(geometry.same_family("MNI152NLin2009cAsym", "MNI152NLin2009aSym"))
        self.assertFalse(geometry.same_family("MNI152NLin2009cAsym", "MNI152NLin6Asym"))
        self.assertFalse(geometry.same_family("MNI152NLin2009bAsym", "MNI152NLin2009cAsym"))
        self.assertFalse(geometry.same_family("native", "native"))  # subject spaces are never one family
        self.assertFalse(geometry.same_family("fsaverage", "fsaverage"))

    def test_numpy_only_paths_do_not_import_simpleitk(self):
        code = textwrap.dedent(
            """
            import sys
            import numpy as np
            from LesionNetworkMappingLib import geometry, nifti, study, transforms
            spec = geometry.GRID_BY_ID["TF2009-1mm"]
            assert geometry.detect_grid_candidates(spec.shape, spec.affine).ambiguous
            assert geometry.detect_grid(spec.shape, spec.affine) == ("MNI152NLin2009cAsym", 1)
            assert transforms.parse_transform_name("tpl-A_from-B_mode-image_xfm.h5") == ("B", "A")
            transforms.transform_direction("tpl-A_from-B_mode-image_xfm.h5", "points", "A", "B")
            print("SimpleITK" in sys.modules)
            """,
        )
        env = dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=MODULE_DIR, timeout=120, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")


class TransformNameTest(unittest.TestCase):
    def test_parse(self):
        parse = transforms.parse_transform_name
        self.assertEqual(parse("tpl-MNI152NLin2009cAsym_from-MNI152NLin6Asym_mode-image_xfm.h5"), ("MNI152NLin6Asym", "MNI152NLin2009cAsym"))
        self.assertEqual(parse("/x/sub-01_from-T1w_to-MNI152NLin6Asym_mode-image_xfm.h5"), ("T1w", "MNI152NLin6Asym"))
        self.assertEqual(parse("sub-01_from-T1w_to-MNI152NLin6Asym_desc-affine_mode-image_xfm_Inverse.h5"), ("MNI152NLin6Asym", "T1w"))
        self.assertEqual(parse("sub-01_from-T1w_to-X_xfm.txt"), ("T1w", "X"))
        self.assertIsNone(parse("xfm.h5"))
        self.assertIsNone(parse("sub-01_to-X_xfm.h5"))
        with self.assertRaises(ValueError):
            parse("tpl-A_from-B_mode-points_xfm.h5")
        with self.assertRaises(ValueError):
            parse("tpl-A_from-B_to-C_xfm.h5")
        direction = parse("tpl-B_from-A_mode-image_xfm.h5")
        self.assertEqual((direction.image_from, direction.image_to, direction.points_from, direction.points_to), ("A", "B", "B", "A"))

    def test_direction_enforced(self):
        forward = "tpl-B_from-A_mode-image_xfm.h5"
        self.assertEqual(transforms.transform_direction(forward, "image", "A", "B"), ("A", "B"))
        self.assertEqual(transforms.transform_direction(forward, "image", "a", "tpl-b"), ("A", "B"))  # case and prefixes ignored
        self.assertEqual(transforms.transform_direction(forward, "points", "B", "A"), ("A", "B"))
        with self.assertRaisesRegex(transforms.DirectionError, "tpl-B_from-A_mode-image_xfm.h5"):
            transforms.transform_direction(forward, "image", "B", "A")
        with self.assertRaisesRegex(transforms.DirectionError, "use tpl-A_from-B_mode-image_xfm.h5"):
            transforms.transform_direction(forward, "points", "A", "B")
        with self.assertRaises(transforms.DirectionError):
            transforms.transform_direction(forward, "points", target_space="B")
        with self.assertRaises(transforms.DirectionError):
            transforms.transform_direction("xfm.h5", "image")
        self.assertEqual(transforms.transform_direction("xfm.h5", "image", "A", "B"), ("A", "B"))
        self.assertEqual(transforms.transform_direction("xfm.h5", "points", "A", "B"), ("B", "A"))
        with self.assertRaises(ValueError):
            transforms.transform_direction(forward, "mesh")


def _write_grid_image(path, data, affine, **kwargs):
    nifti.write_nifti(path, data, affine, **kwargs)
    return path


@unittest.skipIf(sitk is None, "SimpleITK is not available")
class TransformsTest(unittest.TestCase):
    """Synthetic composite transform (affine + displacement field) between spaces A and B."""

    A_AFFINE = np.array([[2.0, 0, 0, -60], [0, 2, 0, -70], [0, 0, 2, -50], [0, 0, 0, 1]])  # RAS storage
    B_AFFINE = np.array([[-2.0, 0, 0, 59], [0, 2, 0, -71], [0, 0, 2, -49], [0, 0, 0, 1]])  # radiological storage, shifted by 1 mm
    SHAPE = (60, 70, 60)

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-transforms-test-")
        # B -> A point map (resamples A images onto B): F(x) = affine(x + d(x)), ITK applying the last added first
        spacing, size, origin = 8.0, 27, -104.0
        axes = origin + spacing * np.arange(size)
        z, y, x = np.meshgrid(axes, axes, axes, indexing="ij")
        displacement = np.stack([2.0 * np.sin(y / 25.0), 1.5 * np.cos(z / 30.0), 1.8 * np.sin(x / 20.0)], axis=-1)
        field = sitk.GetImageFromArray(displacement, isVector=True)
        field.SetSpacing((spacing,) * 3)
        field.SetOrigin((origin,) * 3)
        field = sitk.Cast(field, sitk.sitkVectorFloat64)
        inverse_field = sitk.InvertDisplacementField(field, maximumNumberOfIterations=50, maxErrorToleranceThreshold=0.01, meanErrorToleranceThreshold=0.0001, enforceBoundaryCondition=True)
        affine = sitk.AffineTransform(3)
        affine.SetMatrix((1.02, 0.05, 0.0, -0.04, 0.99, 0.03, 0.0, -0.02, 1.01))
        affine.SetTranslation((3.0, -2.0, 4.0))
        forward = sitk.CompositeTransform(3)
        forward.AddTransform(affine)
        forward.AddTransform(sitk.DisplacementFieldTransform(sitk.Image(field)))
        backward = sitk.CompositeTransform(3)
        backward.AddTransform(sitk.DisplacementFieldTransform(sitk.Image(inverse_field)))
        backward.AddTransform(affine.GetInverse())
        cls.forward = os.path.join(cls.tmp, "tpl-B_from-A_mode-image_xfm.h5")  # A images onto B; B points -> A points
        cls.backward = os.path.join(cls.tmp, "tpl-A_from-B_mode-image_xfm.h5")  # B images onto A; A points -> B points
        sitk.WriteTransform(forward, cls.forward)
        sitk.WriteTransform(backward, cls.backward)
        cls.ref_a = _write_grid_image(os.path.join(cls.tmp, "ref_a.nii.gz"), np.zeros(cls.SHAPE, dtype=np.uint8), cls.A_AFFINE)
        cls.ref_b = _write_grid_image(os.path.join(cls.tmp, "ref_b.nii.gz"), np.zeros(cls.SHAPE, dtype=np.int16), cls.B_AFFINE, sform_code=2, qform_code=1)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def _delta(self, name, ijk, affine=None, shape=None):
        data = np.zeros(shape or self.SHAPE, dtype=np.float32)
        data[tuple(ijk)] = 1.0
        return _write_grid_image(self.path(name), data, self.A_AFFINE if affine is None else affine), geometry.voxel_to_world(np.array([ijk]), self.A_AFFINE if affine is None else affine)[0]

    @staticmethod
    def _centroid(path):
        data, affine, _ = nifti.read_nifti(path)
        ijk = np.argwhere(data > 0)
        weights = data[data > 0]
        return geometry.voxel_to_world((ijk * weights[:, np.newaxis]).sum(axis=0, keepdims=True) / weights.sum(), affine)[0]

    def test_delta_warp_agrees_with_opposite_point_file(self):
        for ijk in ((30, 35, 30), (20, 45, 38), (41, 28, 22)):
            with self.subTest(ijk=ijk):
                source, center_a = self._delta("delta.nii.gz", ijk)
                out = self.path("delta_in_b.nii.gz")
                provenance = transforms.warp_image(source, self.forward, self.ref_b, out, interpolation="linear")
                self.assertEqual(provenance["direction"], "image")
                self.assertEqual((provenance["source_space"], provenance["target_space"]), ("A", "B"))
                self.assertEqual(provenance["transform_sha256"], transforms.file_sha256(self.forward))
                expected_b = transforms.transform_points(center_a, self.backward, "A", "B")
                distance = np.linalg.norm(self._centroid(out) - expected_b)
                self.assertLess(distance, 2.0)  # 1 voxel of the B grid
                # The same file applied to points goes the other way: B -> A
                np.testing.assert_allclose(transforms.transform_points(expected_b, self.forward, "B", "A"), center_a, atol=0.5)

    def test_round_trip_within_half_a_millimetre(self):
        rng = np.random.default_rng(7)
        landmarks = rng.uniform(-45.0, 45.0, size=(40, 3))
        in_b = transforms.transform_points(landmarks, self.backward, "A", "B")
        self.assertGreater(np.linalg.norm(in_b - landmarks, axis=1).min(), 1.0)  # the transform does move points
        back = transforms.transform_points(in_b, self.forward, "B", "A")
        self.assertLess(np.linalg.norm(back - landmarks, axis=1).max(), 0.5)
        mapped, distances = transforms.round_trip(landmarks, self.backward, self.forward)
        np.testing.assert_allclose(mapped, in_b)
        self.assertLess(distances.max(), 0.5)
        with self.assertRaises(transforms.DirectionError):
            transforms.round_trip(landmarks, self.backward, self.backward)

    def test_output_header_equals_reference(self):
        source, _ = self._delta("delta_header.nii.gz", (30, 35, 30))
        for reference, name in ((self.ref_b, "on_b.nii.gz"), (self.ref_a, "on_a.nii")):
            for interpolation, binarize_at in (("linear", None), ("nearest", None), ("linear", 0.05)):
                with self.subTest(reference=os.path.basename(reference), interpolation=interpolation, binarize_at=binarize_at):
                    out = self.path(name)
                    xfm = self.forward if reference == self.ref_b else self.path("tpl-A_from-A_mode-image_xfm.h5")
                    if xfm != self.forward:
                        sitk.WriteTransform(sitk.Euler3DTransform(), xfm)
                    transforms.warp_image(source, xfm, reference, out, interpolation=interpolation, binarize_at=binarize_at)
                    out_header, ref_header = nifti.read_raw_header(out), nifti.read_raw_header(reference)
                    for field in nifti.GEOMETRY_FIELDS:
                        self.assertEqual(out_header[field].tobytes(), ref_header[field].tobytes(), field)
                    self.assertEqual(tuple(out_header["dim"][1:4]), tuple(ref_header["dim"][1:4]))
                    data, _, _ = nifti.read_nifti(out)
                    if binarize_at is not None:
                        self.assertEqual(data.dtype, np.uint8)
                        self.assertTrue(set(np.unique(data)) <= {0, 1})
                        self.assertGreater(data.sum(), 0)
                    elif interpolation == "linear":
                        self.assertEqual(data.dtype, np.float32)
        # Identity warp on the source grid gives the source back exactly
        np.testing.assert_array_equal(nifti.read_nifti(self.path("on_a.nii"))[0], (nifti.read_nifti(source)[0] >= 0.05).astype(np.uint8))

    def test_nrrd_source_matches_nifti_source(self):
        source, _ = self._delta("delta_nrrd.nii.gz", (25, 30, 33))
        nrrd = self.path("delta.nrrd")
        sitk.WriteImage(sitk.ReadImage(source), nrrd)
        transforms.warp_image(source, self.forward, self.ref_b, self.path("from_nii.nii.gz"))
        transforms.warp_image(nrrd, self.forward, self.ref_b, self.path("from_nrrd.nii.gz"))
        np.testing.assert_array_equal(nifti.read_nifti(self.path("from_nii.nii.gz"))[0], nifti.read_nifti(self.path("from_nrrd.nii.gz"))[0])

    def test_wrong_direction_raises(self):
        source, center = self._delta("delta_wrong.nii.gz", (30, 35, 30))
        out = self.path("wrong.nii.gz")
        with self.assertRaisesRegex(transforms.DirectionError, "tpl-A_from-B"):
            transforms.transform_points(center, self.forward, "A", "B")
        with self.assertRaisesRegex(transforms.DirectionError, "tpl-B_from-A"):
            transforms.warp_image(source, self.backward, self.ref_b, out, source_space="A", target_space="B")
        unnamed = self.path("xfm.h5")
        shutil.copyfile(self.forward, unnamed)
        with self.assertRaises(transforms.DirectionError):
            transforms.warp_image(source, unnamed, self.ref_b, out)
        self.assertFalse(os.path.exists(out))
        transforms.warp_image(source, unnamed, self.ref_b, out, source_space="A", target_space="B")  # declared direction
        with self.assertRaises(ValueError):
            transforms.warp_image(source, self.forward, self.ref_b, out, interpolation="cubic")

    def test_sform_and_qform_that_disagree_are_refused(self):
        """SimpleITK places such a file through its qform while the header affine is the sform: never warp from or onto it."""
        source, _ = self._delta("delta_disagree.nii.gz", (30, 35, 30))
        reference = _write_grid_image(self.path("ref_disagree.nii"), np.zeros(self.SHAPE, dtype=np.uint8), self.B_AFFINE)
        record = nifti.read_raw_header(reference)
        record["srow_x"] = self.B_AFFINE[0] + np.array([0.0, 0.0, 0.0, 3.0])  # sform 3 mm away from the qform
        with open(reference, "r+b") as f:
            f.write(record.tobytes())
        out = self.path("disagree.nii.gz")
        with self.assertRaisesRegex(ValueError, "sform and qform disagree"):
            transforms.warp_image(source, self.forward, reference, out)
        with self.assertRaisesRegex(ValueError, "sform and qform disagree"):
            transforms.warp_image(reference, self.backward, self.ref_a, out)  # the same file as a source (B space)
        self.assertFalse(os.path.exists(out))

    def test_template_family_of_the_grids_is_checked(self):
        nlin6, tf2009 = geometry.GRID_BY_ID["NLin6-2mm"], geometry.GRID_BY_ID["TF2009-2mm"]
        on_nlin6 = _write_grid_image(self.path("lesion_nlin6.nii.gz"), np.zeros(nlin6.shape, dtype=np.uint8), nlin6.affine)
        on_2009 = _write_grid_image(self.path("lesion_2009.nii.gz"), np.zeros(tf2009.shape, dtype=np.uint8), tf2009.affine)
        xfm = self.path("tpl-MNI152NLin6Asym_from-MNI152NLin2009cAsym_mode-image_xfm.h5")  # 2009c images onto the NLin6 grid
        sitk.WriteTransform(sitk.TranslationTransform(3, (1.0, 2.0, -1.0)), xfm)
        out = self.path("lesion_in_nlin6.nii.gz")
        provenance = transforms.warp_image(on_2009, xfm, on_nlin6, out, interpolation="nearest")
        self.assertEqual(geometry.detect_space(out), ("MNI152NLin6Asym", 2))
        self.assertEqual(provenance["target_space"], "MNI152NLin6Asym")
        with self.assertRaisesRegex(transforms.DirectionError, "NLin6-2mm grid"):
            transforms.warp_image(on_nlin6, xfm, on_nlin6, out)  # an NLin6 image through a from-2009c file
        with self.assertRaisesRegex(transforms.DirectionError, "TF2009-2mm grid"):
            transforms.warp_image(on_2009, xfm, on_2009, out)  # onto a 2009c grid with a tpl-NLin6 file

    def test_brainsfit_two_step_composite(self):
        """A flattened two-step composite (rigid, then BRAINSFit's affine + BSpline), named from-<mod>_to-<tpl>."""
        rigid = sitk.Euler3DTransform((0.0, 0.0, 0.0), 0.05, -0.03, 0.02, (2.0, -1.0, 3.0))
        affine = sitk.AffineTransform(3)
        affine.SetMatrix((1.03, 0.02, 0.0, 0.0, 0.97, 0.01, 0.0, 0.0, 1.0))
        domain = sitk.Image([10, 10, 10], sitk.sitkFloat32)
        domain.SetOrigin((-100.0, -100.0, -100.0))
        domain.SetSpacing((20.0, 20.0, 20.0))
        bspline = sitk.BSplineTransformInitializer(domain, [4, 4, 4])
        parameters = np.array(bspline.GetParameters())
        parameters[:] = np.random.default_rng(3).normal(0.0, 1.5, parameters.size)
        bspline.SetParameters(tuple(parameters))
        composite = sitk.CompositeTransform(3)
        for transform in (rigid, affine, bspline):
            composite.AddTransform(transform)
        xfm = self.path("sub-01_from-T1w_to-SpaceB_mode-image_xfm.h5")
        sitk.WriteTransform(composite, xfm)
        self.assertEqual(transforms.transform_kinds(transforms.read_transform(xfm)), ["Euler3DTransform", "AffineTransform", "BSplineTransform"])
        source, center = self._delta("native_delta.nii.gz", (30, 35, 30))
        out = self.path("native_in_b.nii.gz")
        transforms.warp_image(source, xfm, self.ref_b, out)
        # The image transform maps target points to source points: the warped delta's center maps back to the source delta
        centroid = self._centroid(out)
        np.testing.assert_allclose(transforms.transform_points(centroid, xfm, "SpaceB", "T1w"), center, atol=2.0)
        with self.assertRaises(transforms.DirectionError):
            transforms.transform_points(center, xfm, "T1w", "SpaceB")  # only the affine has an inverse file


@unittest.skipIf(sitk is None, "SimpleITK is not available")
class TransformsCommandLineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-transforms-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.forward = os.path.join(self.tmp, "tpl-B_from-A_mode-image_xfm.h5")
        self.backward = os.path.join(self.tmp, "tpl-A_from-B_mode-image_xfm.h5")
        translation = sitk.TranslationTransform(3, (4.0, -2.0, 6.0))  # LPS
        sitk.WriteTransform(translation, self.forward)
        sitk.WriteTransform(translation.GetInverse(), self.backward)

    def run_cli(self, *args):
        env = dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))
        result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.transforms", *args], capture_output=True, text=True, env=env, cwd=self.tmp, timeout=300, check=False)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return result.returncode, events

    def test_warp_points_and_check(self):
        source = os.path.join(self.tmp, "lesion.nii.gz")
        data = np.zeros((20, 20, 20), dtype=np.uint8)
        data[10, 10, 10] = 1
        nifti.write_nifti(source, data, np.diag([2.0, 2.0, 2.0, 1.0]))
        out = os.path.join(self.tmp, "lesion_b.nii.gz")
        code, events = self.run_cli("warp", "--input", source, "--transform", self.forward, "--reference", source, "--output", out, "--interpolation", "nearest", "--binarize-at", "0.5")
        self.assertEqual(code, 0, events)
        self.assertEqual([event["event"] for event in events], ["start", "stage", "result", "done"])
        self.assertEqual(events[-1]["provenance"]["direction"], "image")
        moved, _, _ = nifti.read_nifti(out)
        # B voxel (i, j, k) samples A at B + (-4, 2, 6) RAS mm: the A voxel at index 10 lands at index (12, 9, 7)
        self.assertEqual(tuple(np.argwhere(moved)[0]), (12, 9, 7))

        code, events = self.run_cli("points", "--transform", self.backward, "--point", "20,20,20", "--source-space", "A", "--target-space", "B")
        self.assertEqual(code, 0, events)
        np.testing.assert_allclose(events[1]["points"], [[24.0, 18.0, 14.0]])  # A point (20, 20, 20) is at B voxel center (12, 9, 7) * 2 mm
        table = os.path.join(self.tmp, "points.tsv")
        with open(table, "w", encoding="utf-8") as f:
            f.write("name\tx\ty\tz\nL1\t20\t20\t20\nL2\t0\t0\t0\n")
        out_table = os.path.join(self.tmp, "points_b.tsv")
        code, events = self.run_cli("points", "--transform", self.backward, "--input", table, "--output", out_table)
        self.assertEqual(code, 0, events)
        np.testing.assert_allclose(transforms.read_points_table(out_table), [[24.0, 18.0, 14.0], [4.0, -2.0, -6.0]])

        code, events = self.run_cli("points", "--transform", self.forward, "--point", "20,20,20", "--source-space", "A", "--target-space", "B")
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["code"], "direction")
        self.assertIn("tpl-A_from-B", events[-1]["message"])

        code, events = self.run_cli("check", "--transform", self.backward, "--inverse", self.forward, "--sha256")
        self.assertEqual(code, 0, events)
        result = events[1]
        self.assertEqual(result["named_direction"]["points"], {"from": "A", "to": "B"})
        self.assertLess(result["round_trip"]["max_mm"], 1e-6)
        self.assertEqual(result["round_trip"]["n"], 27)
        code, events = self.run_cli("check", "--transform", self.backward, "--inverse", self.backward)
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["code"], "direction")

        code, events = self.run_cli("warp", "--input", source)
        self.assertEqual(code, 2)
        self.assertEqual(events[-1]["code"], "usage")


if __name__ == "__main__":
    unittest.main()

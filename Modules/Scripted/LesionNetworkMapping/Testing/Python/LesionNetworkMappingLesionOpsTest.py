"""Tests of the per-lesion intake numerics (lesionops.py; addendum §3.10, lesion-input research §1, §4).

Synthetic cases with known answers: dimensionality of single-slice, multi-slice, multi-plane, oblique
and 3D tracings on RAS, radiological and permuted grids; the exact total thickness of fractional
extrusion and the recorded binary semantics; the world mirror (exact on grids symmetric about x = 0,
fractional on the 2009c 2 mm grid); the side check; CSF shares on synthetic priors and the refusal
across template families; mask hygiene; the duplicate index (Dice equal to brute force, save/load);
and the qc_flags codes. The 1000-lesion index benchmark is a separate script (L-13 verification).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingLesionOpsTest.py -v
"""

import os
import shutil
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import geometry, lesionops

SHAPE = (40, 44, 36)


def ras_affine(spacing=(1.0, 1.0, 1.0), shape=SHAPE):
    """RAS grid whose voxel centres are symmetric about x = 0 (and centred in y and z)."""
    affine = np.diag([*spacing, 1.0])
    affine[:3, 3] = [-spacing[a] * (shape[a] - 1) / 2.0 for a in range(3)]
    return affine


def las_affine(spacing=(1.0, 1.0, 1.0), shape=SHAPE):
    """Radiological (LAS) storage of the same box: voxel index i runs from the patient's right to left (x decreasing)."""
    affine = ras_affine(spacing, shape)
    affine[0, 0] = -spacing[0]
    affine[0, 3] = spacing[0] * (shape[0] - 1) / 2.0
    return affine


def permuted_affine(shape=SHAPE):
    """Voxel axis 0 runs along world z, axis 1 along x, axis 2 along y."""
    affine = np.zeros((4, 4))
    affine[2, 0] = affine[0, 1] = affine[1, 2] = 1.0
    affine[3, 3] = 1.0
    affine[:3, 3] = [-(shape[1] - 1) / 2.0, -(shape[2] - 1) / 2.0, -(shape[0] - 1) / 2.0]
    return affine


def world_grid(shape, affine):
    ijk = np.indices(shape, dtype=np.float64).reshape(3, -1).T
    return geometry.voxel_to_world(ijk, affine).reshape(*shape, 3)


def ball(shape, affine, centre, radius):
    world = world_grid(shape, affine)
    return (np.linalg.norm(world - np.asarray(centre), axis=-1) <= radius).astype(np.uint8)


class DimensionalityTest(unittest.TestCase):
    def test_single_axial_slice(self):
        mask = np.zeros(SHAPE, dtype=np.uint8)
        mask[10:20, 12:22, 18] = 1
        info = lesionops.detect_dimensionality(mask, ras_affine())
        self.assertEqual(info["dimensionality"], "2D")
        self.assertEqual(info["planes"], ["axial"])
        self.assertEqual(info["drawn_axis"], "axial")
        self.assertEqual(info["n_slices"], {"axial": 1, "coronal": 10, "sagittal": 10})

    def test_axes_mapped_through_the_affine(self):
        # One slice along voxel axis 0, which is world z (axial) on the permuted grid
        mask = np.zeros(SHAPE, dtype=np.uint8)
        mask[5, 10:20, 8:14] = 1
        info = lesionops.detect_dimensionality(mask, permuted_affine())
        self.assertEqual(info["dimensionality"], "2D")
        self.assertEqual(info["planes"], ["axial"])
        self.assertEqual(info["n_slices"], {"axial": 1, "coronal": 6, "sagittal": 10})
        # One slice along voxel axis 0 of a radiological grid is sagittal
        mask = np.zeros(SHAPE, dtype=np.uint8)
        mask[7, 10:20, 8:14] = 1
        self.assertEqual(lesionops.detect_dimensionality(mask, las_affine())["planes"], ["sagittal"])

    def test_multi2d_levels_and_planes(self):
        mask = np.zeros(SHAPE, dtype=np.uint8)
        mask[5:15, 5:15, 10] = 1
        mask[20:30, 20:30, 25] = 1  # another axial level
        info = lesionops.detect_dimensionality(mask, ras_affine())
        self.assertEqual(info["dimensionality"], "multi2D")
        self.assertEqual(info["planes"], ["axial"])
        self.assertEqual(info["n_components"], 2)
        self.assertEqual(info["n_slices"]["axial"], 2)
        mask[30:38, 5, 2:9] = 1  # a coronal drawing
        info = lesionops.detect_dimensionality(mask, ras_affine())
        self.assertEqual(info["dimensionality"], "multi2D")
        self.assertEqual(info["planes"], ["axial", "coronal"])

    def test_3d_empty_and_single_voxel(self):
        affine = ras_affine()
        self.assertEqual(lesionops.detect_dimensionality(ball(SHAPE, affine, (0, 0, 0), 5), affine)["dimensionality"], "3D")
        # Two adjacent axial slices are one 3D component
        mask = np.zeros(SHAPE, dtype=np.uint8)
        mask[5:15, 5:15, 10:12] = 1
        self.assertEqual(lesionops.detect_dimensionality(mask, affine)["dimensionality"], "3D")
        empty = lesionops.detect_dimensionality(np.zeros(SHAPE), affine)
        self.assertIsNone(empty["dimensionality"])
        self.assertEqual(empty["n_slices"], {"axial": 0, "coronal": 0, "sagittal": 0})
        single = np.zeros(SHAPE)
        single[3, 4, 5] = 1
        self.assertEqual(lesionops.detect_dimensionality(single, affine)["dimensionality"], "2D")

    def test_oblique_plane_normal(self):
        affine = ras_affine()
        normal = np.array([0.0, np.sin(0.4), np.cos(0.4)])
        mask = oblique_disc(affine, normal, 8.0)
        self.assertEqual(lesionops.detect_dimensionality(mask, affine)["dimensionality"], "3D")
        info = lesionops.detect_dimensionality(mask, affine, plane_normal=normal)
        self.assertEqual(info["dimensionality"], "2D")
        self.assertEqual(info["planes"], ["oblique"])
        self.assertEqual(info["n_slices_normal"], 1)
        # Two parallel discs 6 mm apart along the normal
        shifted = oblique_disc(affine, normal, 8.0, offset=6.0)
        info = lesionops.detect_dimensionality(np.maximum(mask, shifted), affine, plane_normal=normal)
        self.assertEqual(info["dimensionality"], "multi2D")


def oblique_disc(affine, normal, radius, offset=0.0, shape=SHAPE):
    """A one-layer disc rasterized on the plane n.x = offset (voxels the plane passes through)."""
    world = world_grid(shape, affine).reshape(-1, 3)
    distance = world @ normal - offset
    step = np.abs(affine[:3, :3].T @ normal).sum()
    foot = world - distance[:, np.newaxis] * normal
    inside = (np.abs(distance) < step / 2.0) & (np.linalg.norm(foot - offset * normal, axis=1) <= radius)
    return inside.reshape(shape).astype(np.uint8)


class ExtrusionTest(unittest.TestCase):
    def check_exact(self, spacing, axis_plane, total_mm, affine_fn=ras_affine):
        affine = affine_fn(spacing)
        mask = np.zeros(SHAPE, dtype=np.uint8)
        world_axis = lesionops.PLANE_WORLD_AXIS[axis_plane]
        index = [slice(8, 20), slice(10, 24), slice(9, 21)]
        voxel_axis = int(np.argmax(np.abs(affine[world_axis, :3])))
        index[voxel_axis] = SHAPE[voxel_axis] // 2
        mask[tuple(index)] = 1
        out, record = lesionops.extrude(mask, affine, total_mm)
        voxel_spacing = np.sqrt((affine[:3, :3] ** 2).sum(axis=0))
        area_mm2 = mask.sum() * np.prod(voxel_spacing) / voxel_spacing[voxel_axis]
        volume_mm3 = out.sum() * np.prod(voxel_spacing)
        self.assertAlmostEqual(volume_mm3, area_mm2 * total_mm, places=9, msg=f"{spacing} {axis_plane} {total_mm}")
        self.assertEqual(record["semantics"], "total, centred on drawn plane")
        self.assertEqual(record["mode"], "fractional")
        self.assertTrue(record["probabilistic"])
        self.assertEqual(record["output_desc"], "lesionProb")
        self.assertEqual(record["effective_mm"], total_mm)
        self.assertFalse(record["clipped"])
        # Centred: the slice weights are symmetric about the drawn slice and never above 1
        profile = out.sum(axis=tuple(a for a in range(3) if a != voxel_axis))
        centre = SHAPE[voxel_axis] // 2
        np.testing.assert_allclose(profile[centre - 5:centre + 6], profile[centre - 5:centre + 6][::-1])
        self.assertLessEqual(out.max(), 1.0)
        return out, record

    def test_fractional_thickness_times_area_is_exact(self):
        for spacing in ((1.0, 1.0, 1.0), (2.0, 2.0, 2.0), (0.9, 1.2, 1.5)):
            for plane in ("axial", "coronal", "sagittal"):
                for total in (2.0, 3.0, 0.5, 5.0, 4.2):
                    self.check_exact(spacing, plane, total)
        # Radiological and permuted storage
        self.check_exact((1.0, 1.0, 1.0), "sagittal", 2.0, las_affine)
        self.check_exact((1.0, 1.0, 1.0), "axial", 3.0, lambda spacing: permuted_affine())

    def test_fractional_weights(self):
        out, record = self.check_exact((1.0, 1.0, 1.0), "axial", 2.0)
        centre = SHAPE[2] // 2
        self.assertEqual(sorted(np.unique(out[12, 15, centre - 2:centre + 3]).tolist()), [0.0, 0.5, 1.0])
        self.assertEqual(out[12, 15, centre - 1], 0.5)
        self.assertEqual(out[12, 15, centre + 1], 0.5)
        self.assertEqual(record["voxels_per_side"], 1)
        self.assertIn("exact total thickness 2 mm", record["description"])

    def test_binary_semantics_recorded(self):
        mask = np.zeros(SHAPE, dtype=np.uint8)
        mask[8:20, 10:24, 18] = 1
        out, record = lesionops.extrude(mask, ras_affine(), 2.0, mode="binary_pm")
        self.assertEqual(record["mode"], "binary_pm")
        self.assertEqual(record["semantics"], "total, centred on drawn plane")
        self.assertEqual(record["voxels_per_side"], 1)
        self.assertEqual(record["effective_mm"], 3.0)
        self.assertEqual(record["description"], "binary ±1 mm (3 voxels at 1 mm)")
        self.assertFalse(record["probabilistic"])
        self.assertEqual(out.sum(), 3 * mask.sum())
        self.assertEqual(set(np.unique(out).tolist()), {0.0, 1.0})
        np.testing.assert_array_equal(out[:, :, 17:20], np.repeat(mask[:, :, 18:19], 3, axis=2))
        # 2 mm at 2 mm keeps the drawn slice only (±1 mm lies inside it)
        out, record = lesionops.extrude(mask, ras_affine((2.0, 2.0, 2.0)), 2.0, mode="binary_pm")
        self.assertEqual(record["voxels_per_side"], 0)
        self.assertEqual(out.sum(), mask.sum())

    def test_drawn_values_kept_and_multi2d(self):
        mask = np.zeros(SHAPE)
        mask[5:15, 5:15, 10] = 0.5
        mask[20:30, 25, 5:15] = 1.0  # coronal drawing
        out, record = lesionops.extrude(mask, ras_affine(), 2.0)
        self.assertEqual(record["dimensionality"], "multi2D")
        self.assertEqual(record["planes"], ["axial", "coronal"])  # equal sizes: the first voxel decides
        self.assertAlmostEqual(out.sum(), 2.0 * mask.sum())
        self.assertEqual(out[7, 7, 11], 0.25)
        self.assertEqual(out[22, 26, 7], 0.5)

    def test_refusals_and_clipping(self):
        affine = ras_affine()
        with self.assertRaises(ValueError):
            lesionops.extrude(ball(SHAPE, affine, (0, 0, 0), 4), affine, 2.0)
        with self.assertRaises(ValueError):
            lesionops.extrude(np.zeros(SHAPE), affine, 2.0)
        mask = np.zeros(SHAPE)
        mask[5:10, 5:10, 0] = 1
        with self.assertRaises(ValueError):
            lesionops.extrude(mask, affine, 2.0, mode="sideways")
        with self.assertRaises(ValueError):
            lesionops.extrude(mask, affine, 0.0)
        out, record = lesionops.extrude(mask, affine, 2.0)
        self.assertTrue(record["clipped"])
        self.assertAlmostEqual(out.sum(), 1.5 * mask.sum())

    def test_oblique_normal(self):
        affine = ras_affine()
        normal = np.array([0.0, np.sin(0.35), np.cos(0.35)])
        radius = 9.0
        mask = oblique_disc(affine, normal, radius)
        out, record = lesionops.extrude(mask, affine, 4.0, normal=normal)
        self.assertTrue(record["oblique"])
        self.assertIn("approximate", record["description"])
        # Approximate: each drawn voxel's footprint on the plane is its whole cell, so the rim adds a few %
        self.assertAlmostEqual(out.sum() / (np.pi * radius**2 * 4.0), 1.0, delta=0.05)
        binary, record = lesionops.extrude(mask, affine, 4.0, normal=normal, mode="binary_pm")
        self.assertEqual(record["voxels_per_side"], 2)
        self.assertAlmostEqual(binary.sum() / (np.pi * radius**2 * 5.0), 1.0, delta=0.05)
        # A normal along a voxel axis is the exact path
        flat = np.zeros(SHAPE)
        flat[5:15, 5:15, 10] = 1
        out, record = lesionops.extrude(flat, affine, 2.0, normal=[0, 0, -1])
        self.assertFalse(record["oblique"])
        self.assertAlmostEqual(out.sum(), 2.0 * flat.sum())


class MirrorTest(unittest.TestCase):
    def test_exact_on_symmetric_grid(self):
        for affine in (ras_affine(), las_affine(), ras_affine((2.0, 1.5, 1.0))):
            rng = np.random.default_rng(3)
            mask = (rng.random(SHAPE) > 0.995).astype(np.uint8)
            mask[5:12, 10:20, 8:16] = 1
            fractional, binary, note = lesionops.mirror_world(mask, affine)
            np.testing.assert_array_equal(binary, mask[::-1])  # x is voxel axis 0 on these grids
            np.testing.assert_array_equal(fractional, mask[::-1].astype(float))
            self.assertIn("approximate", note)
            self.assertNotIn("off the voxel centres", note)
        # The NLin6 2 mm grid (FSL MNI152) is symmetric about x = 0 as well
        grid = geometry.GRID_BY_ID["NLin6-2mm"].grid
        mask = np.zeros(grid.shape, dtype=np.uint8)
        mask[20:26, 50:55, 40:44] = 1
        fractional, binary, _note = lesionops.mirror_world(mask, grid.affine)
        np.testing.assert_array_equal(binary, mask[::-1])

    def test_fractional_on_2009c_2mm_grid(self):
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        mask = np.zeros(grid.shape, dtype=np.uint8)
        mask[30:35, 50:55, 40:45] = 1
        fractional, binary, note = lesionops.mirror_world(mask, grid.affine)
        self.assertAlmostEqual(fractional.sum(), mask.sum())  # volume kept
        self.assertGreater(binary.sum(), mask.sum())  # the half voxels >= 0.5 both count
        self.assertEqual(sorted(np.unique(fractional).tolist()), [0.0, 0.5, 1.0])
        self.assertIn("approximate", note)
        self.assertIn("1 mm (0.5 voxel) off the voxel centres", note)
        # The mirrored lesion's centroid is the mirror of the original's
        weights = np.argwhere(mask)
        x = geometry.voxel_to_world(weights, grid.affine)[:, 0].mean()
        found = np.argwhere(fractional)
        mirrored_x = (geometry.voxel_to_world(found, grid.affine)[:, 0] * fractional[tuple(found.T)]).sum() / fractional.sum()
        self.assertAlmostEqual(mirrored_x, -x)
        # Mirroring twice restores the lesion only approximately (the half-voxel shift does not cancel)
        twice, _binary, _note = lesionops.mirror_world(fractional, grid.affine)
        self.assertAlmostEqual(twice.sum(), mask.sum())


class SideCheckTest(unittest.TestCase):
    def test_sides(self):
        for affine in (ras_affine(), las_affine(), permuted_affine()):
            left = ball(SHAPE, affine, (-10, 0, 0), 4)
            result = lesionops.side_check(left, affine, "left")
            self.assertEqual(result["traced_side"], "L")
            self.assertEqual(result["reported_side"], "L")
            self.assertFalse(result["mismatch"])
            self.assertEqual(result["level"], "ok")
            self.assertAlmostEqual(result["left_frac"], 1.0)
            result = lesionops.side_check(left, affine, "R")
            self.assertTrue(result["mismatch"])
            self.assertEqual(result["level"], "amber")
            self.assertEqual(result["message"], "Lesion is in the LEFT hemisphere but the source says RIGHT. Radiological figure?")

    def test_majority_and_midline(self):
        affine = ras_affine((2.0, 2.0, 2.0))
        # x centres are -39, ..., -1, 1, ..., 39: a lesion from x = -1 to 9 (6 voxels) is 1/6 left
        mask = np.zeros(SHAPE)
        mask[19:25, 20:22, 18] = 1
        result = lesionops.side_check(mask, affine, None)
        self.assertAlmostEqual(result["left_frac"], 1 / 6)
        self.assertEqual(result["traced_side"], "R")
        self.assertIsNone(result["message"])
        # 60/40 with the centroid 2 mm off the midline: no side reaches 80%
        mask = np.zeros(SHAPE)
        mask[17:22, 20, 18] = 1  # x = -5..3: 3 left, 2 right voxels
        result = lesionops.side_check(mask, affine, "L")
        self.assertAlmostEqual(result["left_frac"], 0.6)
        self.assertEqual(result["traced_side"], "midline")
        self.assertFalse(result["mismatch"])
        self.assertEqual(result["level"], "info")
        mask = np.zeros(SHAPE)
        mask[10:24, 20, 18] = 1  # x = -19..7: centroid -6 mm, 10/14 left
        result = lesionops.side_check(mask, affine, "bilateral")
        self.assertEqual(result["traced_side"], "bilateral")
        self.assertEqual(result["level"], "ok")
        # A voxel centred on x = 0 counts half on each side
        affine = ras_affine((1.0, 1.0, 1.0), (41, 44, 36))
        mask = np.zeros((41, 44, 36))
        mask[20, 5, 5] = 1
        result = lesionops.side_check(mask, affine, "midline")
        self.assertAlmostEqual(result["left_frac"], 0.5)
        self.assertEqual(result["traced_side"], "midline")
        self.assertIsNone(lesionops.side_check(np.zeros(SHAPE), affine, "L")["traced_side"])

    def test_symmetric_lesions_far_from_midline_are_bilateral(self):
        # Two symmetric parts (bilateral strokes): centroid on the midline, but nothing near it
        affine = ras_affine((2.0, 2.0, 2.0))
        both = np.maximum(ball(SHAPE, affine, (-25, 0, 0), 6), ball(SHAPE, affine, (25, 0, 0), 6))
        result = lesionops.side_check(both, affine, "bilateral")
        self.assertAlmostEqual(result["left_frac"], 0.5)
        self.assertAlmostEqual(result["centroid_x_mm"], 0.0)
        self.assertEqual(result["traced_side"], "bilateral")
        self.assertEqual(result["level"], "ok")
        self.assertIsNone(result["message"])
        # A lesion straddling the midline is still "midline"
        straddle = ball(SHAPE, affine, (0, 0, 0), 6)
        self.assertEqual(lesionops.side_check(straddle, affine, None)["traced_side"], "midline")

    def test_normalize_side_spellings(self):
        for text, expected in (
            ("Left", "L"), ("lh", "L"), ("LH", "L"), ("left hemisphere", "L"), ("L", "L"),
            ("right", "R"), ("rh", "R"), ("Right hemisphere", "R"),
            ("bilat", "bilateral"), ("Bilateral", "bilateral"), ("both", "bilateral"), ("L+R", "bilateral"),
            ("midline", "midline"), ("", None), ("unknown", None), (None, None),
        ):
            self.assertEqual(lesionops.normalize_side(text), expected, text)


class CsfShareTest(unittest.TestCase):
    def setUp(self):
        self.grid = geometry.GRID_BY_ID["NLin6-2mm"].grid
        self.lesion = np.zeros(self.grid.shape, dtype=np.uint8)
        self.lesion[40:50, 50:55, 40:44] = 1  # 200 voxels
        self.csf = np.zeros(self.grid.shape)
        self.csf[40:45, 50:55, 40:44] = 0.9  # half of the lesion
        self.csf[45:46, 50:55, 40:44] = 0.5  # not above 0.5

    def test_share_on_synthetic_prior(self):
        result = lesionops.csf_share(self.lesion, self.grid.affine, csf=(self.csf, self.grid.affine, "MNI152NLin6Asym"))
        self.assertAlmostEqual(result.pct, 50.0)
        self.assertIsNone(result.reason)
        self.assertTrue(result.flagged)
        labels = np.zeros(self.grid.shape, dtype=np.int16)
        labels[40:42, 50:55, 40:44] = 4
        labels[42:44, 50:55, 40:44] = 7
        result = lesionops.csf_share(self.lesion, self.grid.affine, ventricle_labels=(labels, self.grid.affine, "fsl6", [4]))
        self.assertAlmostEqual(result.pct, 20.0)
        self.assertFalse(result.flagged)
        result = lesionops.csf_share(self.lesion, self.grid.affine, ventricle_labels=(labels, self.grid.affine, "fsl6"))
        self.assertAlmostEqual(result.pct, 40.0)
        # A 1 mm prior of the same family: the lesion is resampled onto it (exact voxel overlaps). The
        # lesion spans x = -9..11 mm; the prior holds the voxels with centres x <= 0 (x < 0.5 mm)
        fine = geometry.GRID_BY_ID["NLin6-1mm"].grid
        prior = (world_grid(fine.shape, fine.affine)[..., 0] <= 0).astype(np.float32)
        result = lesionops.csf_share(self.lesion, self.grid.affine, csf=(prior, fine.affine, "MNI152NLin6Asym"))
        self.assertAlmostEqual(result.pct, 100.0 * 9.5 / 20.0, places=6)

    def test_family_mismatch_refused(self):
        result = lesionops.csf_share(self.lesion, self.grid.affine, csf=(self.csf, self.grid.affine, "MNI152NLin2009cAsym"))
        self.assertIsNone(result.pct)
        self.assertIn("not aligned", result.reason)
        self.assertFalse(result.flagged)
        # An unknown lesion grid without a space label is refused too
        result = lesionops.csf_share(np.ones((5, 5, 5)), np.eye(4), csf=(np.ones((5, 5, 5)), np.eye(4), "fsl6"))
        self.assertIsNone(result.pct)
        result = lesionops.csf_share(np.ones((5, 5, 5)), np.eye(4), csf=(np.ones((5, 5, 5)), np.eye(4), "fsl6"), lesion_space="MNI152NLin6Asym")
        self.assertAlmostEqual(result.pct, 100.0)
        with self.assertRaises(ValueError):
            lesionops.csf_share(self.lesion, self.grid.affine)


class HygieneTest(unittest.TestCase):
    def test_checks(self):
        affine = ras_affine()
        mask = np.zeros(SHAPE)
        mask[10:14, 10:14, 10:14] = 1
        clean = lesionops.hygiene(mask, affine, template_grid=geometry.Grid(SHAPE, affine), connectome_grid=geometry.Grid(SHAPE, affine))
        for code in ("nonbinary", "empty", "subvoxel", "outside", "xform", "grid", "multipart"):
            self.assertFalse(clean[code], code)
        self.assertEqual(clean["messages"], [])
        self.assertEqual(clean["n_parts"], 1)
        self.assertAlmostEqual(clean["volume_ml"], 0.064)

        warped = mask * 0.7
        warped[0, 0, 0] = np.nan
        result = lesionops.hygiene(warped, affine)
        self.assertTrue(result["nonbinary"])
        self.assertEqual(result["n_nonbinary"], 65)
        self.assertTrue(lesionops.hygiene(np.zeros(SHAPE), affine)["empty"])

        two = mask.copy()
        two[30:32, 30:32, 30:32] = 1
        self.assertTrue(lesionops.hygiene(two, affine)["multipart"])
        self.assertFalse(lesionops.hygiene(two, affine, multifocal=True)["multipart"])
        diagonal = np.zeros(SHAPE)
        diagonal[5, 5, 5] = diagonal[6, 6, 6] = 1  # 26-connected
        self.assertEqual(lesionops.hygiene(diagonal, affine)["n_parts"], 1)

        # One 1 mm voxel on a 2 mm connectome grid fills 1/8 voxel
        tiny = np.zeros(SHAPE)
        tiny[20, 20, 20] = 1
        result = lesionops.hygiene(tiny, affine, connectome_grid=geometry.Grid((20, 22, 18), ras_affine((2.0, 2.0, 2.0), (20, 22, 18))))
        self.assertAlmostEqual(result["connectome_sum"], 0.125)
        self.assertTrue(result["subvoxel"])

        brain = np.zeros(SHAPE)
        brain[:12] = 1
        result = lesionops.hygiene(mask, affine, brain_mask=brain)
        self.assertAlmostEqual(result["outside_pct"], 50.0)
        self.assertTrue(result["outside"])
        result = lesionops.hygiene(mask, affine, brain_mask=(np.ones((20, 22, 18)), ras_affine((2.0, 2.0, 2.0), (20, 22, 18))))
        self.assertAlmostEqual(result["outside_pct"], 0.0)

        result = lesionops.hygiene(mask, affine, template_grid="NLin6-1mm")
        self.assertTrue(result["grid"])
        self.assertFalse(result["grid_matches"])
        self.assertTrue(lesionops.hygiene(mask, las_affine())["left_handed"])

    def test_template_grid_in_another_voxel_order(self):
        # FSL's NLin6 2 mm grid is stored LAS; TemplateFlow's res-02 file holds the same voxels as RAS
        las = geometry.GRID_BY_ID["NLin6-2mm"].grid
        ras = las.affine.copy()
        ras[0, 0] = -las.affine[0, 0]
        ras[0, 3] = las.affine[0, 3] + las.affine[0, 0] * (las.shape[0] - 1)
        self.assertIsNotNone(geometry.signed_permutation(ras, las.affine))
        mask = np.zeros(las.shape, dtype=np.uint8)
        mask[30:34, 50:54, 40:44] = 1
        result = lesionops.hygiene(mask, ras, template_grid="NLin6-2mm")
        self.assertFalse(result["grid"])
        self.assertTrue(result["grid_matches"])
        self.assertEqual([m["code"] for m in result["messages"]], ["grid_order"])
        self.assertEqual(result["messages"][0]["level"], "info")
        self.assertNotIn("grid", lesionops.qc_flags({"hygiene": result}))
        self.assertEqual(lesionops.qc_flags({"hygiene": lesionops.hygiene(mask, las.affine, template_grid="NLin6-2mm")}), [])
        # Shifted by one voxel or cropped: a real difference, with both sides described differently
        cropped = lesionops.hygiene(mask[:90], ras, template_grid="NLin6-2mm")
        self.assertTrue(cropped["grid"])
        shifted = ras.copy()
        shifted[0, 3] += 2.0
        result = lesionops.hygiene(mask, shifted, template_grid="NLin6-2mm")
        self.assertTrue(result["grid"])
        text = result["messages"][-1]["text"]
        self.assertEqual(result["messages"][-1]["code"], "grid")
        lesion_part, template_part = text.split(") is not the study template grid (")
        self.assertNotEqual(lesion_part.split("(", 1)[1], template_part.rstrip(")"))

    def test_xform_disagreement(self):
        affine = ras_affine()
        header = {"sform_code": 4, "qform_code": 4, "shape": SHAPE, "sform": affine, "qform": affine.copy()}
        self.assertFalse(lesionops.hygiene(np.ones(SHAPE), affine, header=header)["xform"])
        header["qform"] = affine.copy()
        header["qform"][0, 3] += 0.5
        result = lesionops.hygiene(np.ones(SHAPE), affine, header=header)
        self.assertAlmostEqual(result["xform_mm"], 0.5)
        self.assertTrue(result["xform"])


class LesionIndexTest(unittest.TestCase):
    def setUp(self):
        self.grid = geometry.GRID_BY_ID["NLin6-2mm"].grid
        self.temp = tempfile.mkdtemp(prefix="lnm-lesionops-")

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def random_lesions(self, count, seed=5):
        rng = np.random.default_rng(seed)
        lesions = {}
        for k in range(count):
            centre = rng.uniform([-40, -60, -20], [40, 40, 50])
            if k % 5 == 4:  # near-duplicates of an earlier lesion
                centre = previous + rng.normal(0, 1.5, 3)
            radius = rng.uniform(4, 12)
            lesions[f"sub-{k:03d}"] = ball(self.grid.shape, self.grid.affine, centre, radius)
            previous = centre
        return lesions

    def test_dice_equals_brute_force(self):
        lesions = self.random_lesions(40)
        index = lesionops.LesionIndex()
        for sid, mask in lesions.items():
            index.update(sid, mask, self.grid.affine)
        dense = {sid: mask.astype(bool) for sid, mask in lesions.items()}
        n_found = 0
        for threshold in (0.6, 0.3):
            for sid in lesions:
                expected = []
                for other in lesions:
                    if other == sid:
                        continue
                    dice = 2.0 * np.count_nonzero(dense[sid] & dense[other]) / (dense[sid].sum() + dense[other].sum())
                    if dice >= threshold:
                        expected.append((other, dice))
                found = index.candidates(sid, threshold)
                self.assertEqual([s for s, _ in found], [s for s, _ in sorted(expected, key=lambda item: (-item[1], item[0]))])
                for (_, dice), (_, brute) in zip(found, sorted(expected, key=lambda item: (-item[1], item[0])), strict=True):
                    self.assertAlmostEqual(dice, brute, places=12)
                n_found += len(found)
        self.assertGreater(n_found, 0)
        pairs = index.all_pairs(0.6)
        self.assertEqual(len(pairs), sum(len(index.candidates(sid)) for sid in lesions) // 2)

    def test_1mm_input_and_fractional_occupancy(self):
        fine = geometry.GRID_BY_ID["NLin6-1mm"].grid
        lesion = ball(fine.shape, fine.affine, (20, -10, 15), 7)
        index = lesionops.LesionIndex()
        index.update("fine", lesion, fine.affine)
        expected = np.flatnonzero(geometry.resample_to_grid(lesion, fine.affine, self.grid, mode="fraction") >= 0.5)
        np.testing.assert_array_equal(index.indices("fine"), expected)
        self.assertEqual(index.indices("fine").dtype, np.int32)
        # A lesion below half a 2 mm voxel keeps its fullest voxel
        tiny = np.zeros(fine.shape, dtype=np.uint8)
        tiny[100, 100, 100] = 1
        index.update("tiny", tiny, fine.affine)
        self.assertEqual(index.indices("tiny").size, 1)
        index.update("empty", np.zeros(fine.shape, dtype=np.uint8), fine.affine)
        self.assertEqual(index.candidates("empty"), [])
        with self.assertRaises(ValueError):
            index.update("other", lesion, fine.affine, grid2mm="TF2009-2mm")

    def test_save_load_and_keys(self):
        lesions = self.random_lesions(12)
        index = lesionops.LesionIndex()
        lesion_file = os.path.join(self.temp, "sub-000_lesion.nii.gz")
        with open(lesion_file, "wb") as f:
            f.write(b"x" * 10)
        for sid, mask in lesions.items():
            index.update(sid, mask, self.grid.affine, key=lesionops.file_key(lesion_file) if sid == "sub-000" else None)
        path = lesionops.index_path(self.temp)
        self.assertEqual(path, os.path.join(self.temp, ".studio", "cache", "lesion_index.npz"))
        index.save(path)
        loaded = lesionops.LesionIndex.load(path)
        self.assertEqual(loaded.sids(), index.sids())
        for sid in lesions:
            np.testing.assert_array_equal(loaded.indices(sid), index.indices(sid))
            self.assertEqual(loaded.candidates(sid), index.candidates(sid))
        self.assertTrue(loaded.is_current("sub-000", lesion_file))
        self.assertFalse(loaded.is_current("sub-001", lesion_file))  # no key stored
        with open(lesion_file, "ab") as f:
            f.write(b"y")
        self.assertFalse(loaded.is_current("sub-000", lesion_file))
        # Another grid or a missing file gives an empty index (it is a cache)
        self.assertEqual(len(lesionops.LesionIndex.load(path, "TF2009-2mm")), 0)
        self.assertEqual(len(lesionops.LesionIndex.load(os.path.join(self.temp, "missing.npz"))), 0)
        loaded.remove("sub-003")
        self.assertNotIn("sub-003", loaded)
        empty = lesionops.LesionIndex()
        empty.save(path)
        self.assertEqual(len(lesionops.LesionIndex.load(path)), 0)
        self.assertEqual([name for name in os.listdir(os.path.dirname(path)) if name.endswith(".tmp")], [])

    def test_corrupt_cache_gives_empty_index(self):
        index = lesionops.LesionIndex()
        for sid, mask in self.random_lesions(6).items():
            index.update(sid, mask, self.grid.affine)
        path = os.path.join(self.temp, "lesion_index.npz")
        index.save(path)
        with open(path, "rb") as f:
            content = f.read()
        for corrupt in (b"", b"PK\x03\x04garbage", content[: len(content) // 2], b"not an index"):
            with open(path, "wb") as f:
                f.write(corrupt)
            loaded = lesionops.LesionIndex.load(path)
            self.assertEqual(len(loaded), 0)

    def test_sid_normalised_to_text(self):
        index = lesionops.LesionIndex()
        mask = ball(self.grid.shape, self.grid.affine, (20, 0, 10), 8)
        index.update(1, mask, self.grid.affine)
        index.update("2", mask, self.grid.affine)
        self.assertIn(1, index)
        self.assertEqual(index.sids(), ["1", "2"])
        self.assertEqual(index.candidates(1), [("2", 1.0)])
        self.assertEqual(index.indices(1).size, index.indices("1").size)
        index.remove(1)
        self.assertNotIn("1", index)


class QcFlagsTest(unittest.TestCase):
    def test_codes(self):
        affine = ras_affine()
        left = ball(SHAPE, affine, (-10, 0, 0), 4) * 0.8
        left[0, 0, 0] = 1  # a second part
        results = {
            "hygiene": lesionops.hygiene(left, affine),
            "side": lesionops.side_check(left, affine, "R"),
            "csf": lesionops.CsfShare(35.0),
            "duplicates": [("sub-002", 0.8)],
        }
        self.assertEqual(lesionops.qc_flags(results), ["side_mismatch", "csf", "nonbinary", "multipart", "duplicate"])
        self.assertEqual(lesionops.qc_flags({}), [])
        self.assertEqual(lesionops.qc_flags({"csf": lesionops.CsfShare(None, "other family")}), [])
        self.assertEqual(lesionops.qc_flags({"hygiene": lesionops.hygiene(np.zeros(SHAPE), affine)}), ["empty"])
        self.assertEqual(list(lesionops.QC_FLAGS), ["side_mismatch", "csf", "nonbinary", "empty", "subvoxel", "outside", "xform", "grid", "multipart", "duplicate"])


if __name__ == "__main__":
    unittest.main()

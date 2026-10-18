"""Unit tests of the pure numpy/scipy lesion network mapping library (LesionNetworkMappingLib).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingLibTest.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np
from scipy import io as sio
from scipy import stats as scipy_stats

from LesionNetworkMappingLib import connectome, engine, geometry, nifti, stats

# float16 stores the unit-norm time series with unit roundoff u = 2**-11. By Cauchy-Schwarz the error
# of r = x . s is at most ~u from the voxel row plus ~2u from the seed, i.e. |dr| <= 3u ~ 1.5e-3.
FLOAT16_R_ATOL = 1.5e-3
# float32 storage and float32 GEMM over <= 100 time points: errors are ~1e-7.
FLOAT32_R_ATOL = 1e-5

RAS_AFFINE = np.array([[2.0, 0, 0, -90], [0, 2, 0, -126], [0, 0, 2, -72], [0, 0, 0, 1]])
RADIOLOGICAL_AFFINE = np.array([[-2.0, 0, 0, 90], [0, 2, 0, -126], [0, 0, 2, -72], [0, 0, 0, 1]])


def _oblique_affine():
    angle = np.deg2rad(20.0)
    rotation = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
    affine = np.eye(4)
    affine[:3, :3] = rotation @ np.diag([1.5, 2.0, 2.5])
    affine[:3, 3] = [-10.25, 20.5, 3.0]
    return affine.astype(np.float32).astype(np.float64)


def _pearson_rows(seed, data):
    """Pearson r between a seed time course (T,) and each row of data (V, T); NaN for constant rows."""
    s = seed - seed.mean()
    x = data - data.mean(axis=1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        return (x @ s) / (np.linalg.norm(x, axis=1) * np.linalg.norm(s))


class _TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


class NiftiTest(_TempDirTestCase):
    def test_round_trip_orientations_dtypes_and_compression(self):
        rng = np.random.default_rng(0)
        shape = (7, 6, 5)
        for label, affine in (("ras", RAS_AFFINE), ("radiological", RADIOLOGICAL_AFFINE), ("oblique", _oblique_affine())):
            for ext in (".nii", ".nii.gz"):
                for dtype in (np.uint8, np.int8, np.int16, np.uint16, np.int32, np.float32, np.float64):
                    with self.subTest(orientation=label, ext=ext, dtype=np.dtype(dtype).name):
                        if np.issubdtype(dtype, np.integer):
                            info = np.iinfo(dtype)
                            data = rng.integers(info.min, info.max, size=shape, endpoint=True).astype(dtype)
                        else:
                            data = rng.standard_normal(shape).astype(dtype)
                        path = self.path(f"{label}_{np.dtype(dtype).name}{ext}")
                        nifti.write_nifti(path, data, affine)
                        read, read_affine, header = nifti.read_nifti(path)
                        self.assertEqual(read.dtype, np.dtype(dtype))
                        np.testing.assert_array_equal(read, data)
                        np.testing.assert_array_equal(read_affine, affine)
                        self.assertEqual((header["sform_code"], header["qform_code"]), (4, 4))
                        self.assertEqual(header["affine_source"], "sform")
                        np.testing.assert_allclose(header["qform"], affine, atol=1e-5)
                        self.assertEqual(header["qfac"], -1.0 if np.linalg.det(affine[:3, :3]) < 0 else 1.0)

    def test_qform_only_files(self):
        for affine in (RAS_AFFINE, RADIOLOGICAL_AFFINE, _oblique_affine()):
            path = self.path("qform.nii.gz")
            nifti.write_nifti(path, np.zeros((3, 4, 5), np.uint8), affine, sform_code=0, qform_code=1)
            _, read_affine, header = nifti.read_nifti(path)
            self.assertEqual(header["affine_source"], "qform")
            np.testing.assert_allclose(read_affine, affine, atol=1e-5)

    def test_scaling_slope_and_intercept(self):
        raw = np.arange(24, dtype=np.int16).reshape(2, 3, 4)
        path = self.path("scaled.nii")
        nifti.write_nifti(path, raw, RAS_AFFINE, scl_slope=0.5, scl_inter=10.0)
        scaled, _, header = nifti.read_nifti(path)
        self.assertEqual(scaled.dtype, np.float32)
        np.testing.assert_array_equal(scaled, raw * 0.5 + 10.0)
        self.assertEqual((header["scl_slope"], header["scl_inter"]), (0.5, 10.0))
        unscaled, _, _ = nifti.read_nifti(path, apply_scaling=False)
        np.testing.assert_array_equal(unscaled, raw)

    def test_big_endian_4d_and_float16(self):
        data = np.random.default_rng(1).standard_normal((4, 3, 2, 5)).astype(np.float16)
        path = self.path("big.nii.gz")
        nifti.write_nifti(path, data, RADIOLOGICAL_AFFINE, byteorder=">", time_step=2.5)
        read, affine, header = nifti.read_nifti(path)
        self.assertEqual(header["byteorder"], ">")
        self.assertEqual(read.shape, (4, 3, 2, 5))
        self.assertEqual(read.dtype, np.float32)  # float16 has no NIfTI-1 code: stored as float32
        np.testing.assert_array_equal(read, data.astype(np.float32))
        np.testing.assert_array_equal(affine, RADIOLOGICAL_AFFINE)
        self.assertEqual(header["pixdim"][4], 2.5)
        self.assertEqual(nifti.read_nifti_header(path)["shape"], (4, 3, 2, 5))

    def test_reads_nonstandard_float16(self):
        data = np.arange(8, dtype=np.float16).reshape(2, 2, 2) / 4
        path = self.path("half.nii")
        nifti.write_nifti(path, data.astype(np.float32), RAS_AFFINE)
        with open(path, "r+b") as f:  # rewrite as datatype FLOAT32 with bitpix 16 and half-float payload
            f.seek(72)
            f.write(np.int16(16).tobytes())
            f.seek(352)
            f.write(data.tobytes(order="F"))
            f.truncate()
        read, _, _ = nifti.read_nifti(path)
        self.assertEqual(read.dtype, np.float16)
        np.testing.assert_array_equal(read, data)

    def test_rejects_unsupported_files(self):
        with self.assertRaises(ValueError):
            nifti.read_nifti_header(self.path("image.nrrd"))
        path = self.path("garbage.nii")
        with open(path, "wb") as f:
            f.write(b"\0" * 400)
        with self.assertRaises(ValueError):
            nifti.read_nifti(path)

    def test_consistent_with_simpleitk(self):
        try:
            import SimpleITK as sitk
        except ImportError:
            self.skipTest("SimpleITK is not available")
        data = np.random.default_rng(2).integers(0, 100, size=(6, 5, 4)).astype(np.int16)
        lps = np.diag([-1.0, -1.0, 1.0, 1.0])
        for affine in (RAS_AFFINE, RADIOLOGICAL_AFFINE, _oblique_affine()):
            # Our writer -> ITK reader
            path = self.path("ours.nii.gz")
            nifti.write_nifti(path, data, affine)
            image = sitk.ReadImage(path)
            np.testing.assert_array_equal(sitk.GetArrayFromImage(image), data.transpose(2, 1, 0))
            itk_affine = np.eye(4)
            itk_affine[:3, :3] = np.array(image.GetDirection()).reshape(3, 3) * np.array(image.GetSpacing())
            itk_affine[:3, 3] = image.GetOrigin()
            np.testing.assert_allclose(lps @ itk_affine, affine, atol=1e-4)
            # ITK writer -> our reader
            path = self.path("itk.nii.gz")
            sitk.WriteImage(image, path)
            read, read_affine, _ = nifti.read_nifti(path)
            np.testing.assert_array_equal(read, data)
            np.testing.assert_allclose(read_affine, affine, atol=1e-4)


class GeometryTest(unittest.TestCase):
    @staticmethod
    def _grid_1mm(radiological=False):
        shape = (61, 61, 61)
        affine = np.diag([1.0, 1.0, 1.0, 1.0])
        affine[:3, 3] = [-30.0, -30.0, -30.0]
        if radiological:
            affine[0, 0], affine[0, 3] = -1.0, 30.0
        return geometry.Grid(shape, affine)

    @staticmethod
    def _grid_2mm(radiological=False, shift=0.0):
        shape = (31, 31, 31)
        affine = np.diag([2.0, 2.0, 2.0, 1.0])
        affine[:3, 3] = [-30.0 + shift, -30.0, -30.0 + shift]
        if radiological:
            affine[0, 0], affine[0, 3] = -2.0, 30.0 + shift
        return geometry.Grid(shape, affine)

    @staticmethod
    def _sphere(grid, center, radius):
        ijk = np.indices(grid.shape).reshape(3, -1).T
        world = grid.voxel_to_world(ijk)
        return (np.linalg.norm(world - np.asarray(center), axis=1) <= radius).reshape(grid.shape).astype(np.float64)

    @staticmethod
    def _box_overlap_reference(source, src_affine, dst):
        """Exact occupancy for diagonal affines: intersect the world-space boxes of every non-zero
        source voxel with the nearby destination voxels (slow, small inputs only).
        """
        out = np.zeros(dst.shape)
        src_half = np.abs(np.diag(src_affine)[:3]) / 2
        dst_half = np.abs(np.diag(dst.affine)[:3]) / 2
        neighbours = np.indices((7, 7, 7)).reshape(3, -1).T - 3
        for ijk in np.argwhere(source != 0):
            center = geometry.voxel_to_world([ijk], src_affine)[0]
            q = np.floor(dst.world_to_voxel([center])[0] + 0.5).astype(int) + neighbours
            q = q[np.all((q >= 0) & (q < dst.shape), axis=1)]
            dst_centers = dst.voxel_to_world(q)
            overlap = np.clip(np.minimum(center + src_half, dst_centers + dst_half) - np.maximum(center - src_half, dst_centers - dst_half), 0.0, None)
            np.add.at(out, tuple(q.T), source[tuple(ijk)] * overlap.prod(axis=1) / dst.voxel_volume)
        return out

    @staticmethod
    def _centroid(volume, grid):
        ijk = np.indices(grid.shape).reshape(3, -1).T
        weights = volume.reshape(-1)
        return (grid.voxel_to_world(ijk) * weights[:, np.newaxis]).sum(axis=0) / weights.sum()

    def test_world_voxel_round_trip(self):
        grid = geometry.Grid((10, 11, 12), _oblique_affine())
        ijk = np.array([[0, 0, 0], [3.5, 2, 7], [9, 10, 11]])
        np.testing.assert_allclose(grid.world_to_voxel(grid.voxel_to_world(ijk)), ijk, atol=1e-12)
        np.testing.assert_allclose(grid.spacing, [1.5, 2.0, 2.5], atol=1e-6)
        np.testing.assert_allclose(geometry.affine_from_spm_mat(RAS_AFFINE) @ [1, 1, 1, 1], RAS_AFFINE @ [2, 2, 2, 1])

    def test_resampling_preserves_world_location(self):
        center = (7.3, -9.1, 4.4)
        for src_radiological in (False, True):
            src = self._grid_1mm(src_radiological)
            lesion = self._sphere(src, center, 5.0)
            results = {}
            for dst_radiological in (False, True):
                dst = self._grid_2mm(dst_radiological)
                with self.subTest(src_radiological=src_radiological, dst_radiological=dst_radiological):
                    fraction = geometry.resample_to_grid(lesion, src.affine, dst, mode="fraction")
                    np.testing.assert_allclose(self._centroid(fraction, dst), self._centroid(lesion, src), atol=0.05)
                    np.testing.assert_allclose(self._centroid(fraction, dst), center, atol=0.3)
                    for mode in ("nearest", "linear"):
                        resampled = geometry.resample_to_grid(lesion, src.affine, dst, mode=mode)
                        np.testing.assert_allclose(self._centroid(resampled, dst), center, atol=0.6)
                    results[dst_radiological] = fraction
            # Mirror-image destination grids cover the same voxel centers: identical maps after flipping i
            np.testing.assert_allclose(results[True][::-1], results[False], atol=1e-12)
        # Upsampling 2 mm -> 1 mm keeps the location too
        coarse = self._grid_2mm(True)
        lesion = self._sphere(coarse, center, 6.0)
        fine = self._grid_1mm()
        for mode in ("fraction", "linear"):
            upsampled = geometry.resample_to_grid(lesion, coarse.affine, fine, mode=mode)
            np.testing.assert_allclose(self._centroid(upsampled, fine), self._centroid(lesion, coarse), atol=0.1)

    def test_fractional_occupancy_conserves_volume(self):
        src = self._grid_1mm()
        lesion = self._sphere(src, (3.0, 1.0, -2.0), 6.5)
        lesion_volume = lesion.sum() * src.voxel_volume
        for label, dst in (("aligned", self._grid_2mm()), ("shifted", self._grid_2mm(shift=0.3)), ("radiological", self._grid_2mm(True, 0.7))):
            with self.subTest(grid=label):
                fraction = geometry.resample_to_grid(lesion, src.affine, dst, mode="fraction")
                self.assertGreaterEqual(fraction.min(), 0.0)
                self.assertLessEqual(fraction.max(), 1.0)
                self.assertAlmostEqual(fraction.sum() * dst.voxel_volume / lesion_volume, 1.0, places=9)
        oblique = geometry.Grid((25, 25, 25), _oblique_affine())
        oblique.affine[:3, 3] = -oblique.affine[:3, :3] @ [12, 12, 12]  # center the grid on the origin
        fraction = geometry.resample_to_grid(lesion, src.affine, oblique, mode="fraction")
        self.assertAlmostEqual(fraction.sum() * oblique.voxel_volume / lesion_volume, 1.0, delta=0.01)
        self.assertLessEqual(fraction.max(), 1.0)
        # Oblique destination voxels inside the source field of view are exactly 1 for a full source
        full = geometry.resample_to_grid(np.ones(src.shape), src.affine, oblique, mode="fraction")
        np.testing.assert_array_equal(full[8:17, 8:17, 8:17], 1.0)
        # Aligned interior voxels fully covered by the lesion get exactly 1
        aligned = geometry.resample_to_grid(lesion, src.affine, self._grid_2mm(), mode="fraction")
        self.assertEqual(aligned.max(), 1.0)

    def test_fraction_with_non_integer_voxel_size_ratios(self):
        # e.g. 1.5 mm (CAT12/SPM) or 0.9375 mm lesion masks onto the 2 mm connectome grid
        rng = np.random.default_rng(7)
        dst = self._grid_2mm(radiological=True, shift=0.3)
        for spacing, flip in ((1.5, False), (1.5, True), (0.9375, False), (3.0, True), ((1.2, 0.8, 2.5), False)):
            spacing = np.broadcast_to(np.asarray(spacing, dtype=np.float64), (3,))
            affine = np.diag([*spacing, 1.0])
            affine[:3, 3] = [-40.3, -40.1, -39.7]
            if flip:
                affine[0, 0], affine[0, 3] = -spacing[0], 40.3
            src = geometry.Grid(np.ceil(80 / spacing).astype(int), affine)
            with self.subTest(spacing=spacing.tolist(), flip=flip):
                # A source covering the whole destination grid gives exactly 1 everywhere
                full = geometry.resample_to_grid(np.ones(src.shape), affine, dst, mode="fraction")
                np.testing.assert_allclose(full, 1.0, rtol=0, atol=1e-12)
                # The volume of a sphere is conserved exactly
                sphere = self._sphere(src, (3.0, 1.0, -2.0), 9.5)
                fraction = geometry.resample_to_grid(sphere, affine, dst, mode="fraction")
                self.assertAlmostEqual(fraction.sum() * dst.voxel_volume / (sphere.sum() * src.voxel_volume), 1.0, places=9)
                self.assertLessEqual(fraction.max(), 1.0 + 1e-12)
                # Sparse values: the exact voxel-box intersections
                sparse = np.zeros(src.shape)
                box = tuple(slice(n // 2 - 6, n // 2 + 6) for n in src.shape)
                sparse[box] = (rng.random(sparse[box].shape) < 0.05) * rng.uniform(0.5, 1.0, sparse[box].shape)
                np.testing.assert_allclose(geometry.resample_to_grid(sparse, affine, dst, mode="fraction"), self._box_overlap_reference(sparse, affine, dst), rtol=0, atol=1e-12)
        # The storage order of the source axes does not matter (axis permutation in the affine)
        lesion = self._sphere(src, (3.0, 1.0, -2.0), 7.0)
        permuted_affine = affine.copy()
        permuted_affine[:3, :3] = affine[:3, (2, 0, 1)]
        permuted = geometry.resample_to_grid(np.transpose(lesion, (2, 0, 1)), permuted_affine, dst, mode="fraction")
        np.testing.assert_allclose(permuted, geometry.resample_to_grid(lesion, affine, dst, mode="fraction"), rtol=0, atol=1e-12)

    def test_tiny_lesion_survives_downsampling(self):
        src = self._grid_1mm(radiological=True)
        for voxel in ((30, 30, 30), (31, 30, 29), (0, 60, 17)):
            lesion = np.zeros(src.shape)
            lesion[voxel] = 1.0
            world = src.voxel_to_world(np.array([voxel]))[0]
            for dst_radiological in (False, True):
                dst = self._grid_2mm(dst_radiological)
                with self.subTest(voxel=voxel, dst_radiological=dst_radiological):
                    fraction = geometry.resample_to_grid(lesion, src.affine, dst, mode="fraction")
                    self.assertAlmostEqual(fraction.sum(), 1.0 / 8.0, places=12)
                    self.assertGreater(fraction.max(), engine.MIN_WEIGHT)
                    nearest_voxel = tuple(np.floor(dst.world_to_voxel([world])[0] + 0.5).astype(int))
                    self.assertGreater(fraction[nearest_voxel], 0.0)
                    weights = engine.lesion_weights(fraction, np.arange(dst.n_voxels))
                    self.assertGreater(np.count_nonzero(weights), 0)

    def test_masked_vector_round_trip(self):
        rng = np.random.default_rng(3)
        mask = rng.random((5, 6, 7)) > 0.5
        indices = geometry.mask_indices(mask)
        np.testing.assert_array_equal(indices, np.flatnonzero(mask))
        volume = rng.standard_normal((5, 6, 7))
        vector = geometry.to_masked_vector(volume, indices)
        np.testing.assert_array_equal(vector, volume[mask])
        back = geometry.from_masked_vector(vector, indices, mask.shape)
        np.testing.assert_array_equal(back[mask], volume[mask])
        self.assertTrue(np.all(back[~mask] == 0))
        stack = geometry.from_masked_vector(np.stack([vector, 2 * vector]), indices, mask.shape)
        self.assertEqual(stack.shape, (2, 5, 6, 7))
        np.testing.assert_array_equal(geometry.to_masked_vector(stack, indices)[1], 2 * vector)


def _write_bundle(path, raw_subjects, mask, affine=RAS_AFFINE, dtype="float16"):
    writer = connectome.ConnectomeWriter(path, mask, affine, name="Test", dtype=dtype)
    for index, runs in enumerate(raw_subjects):
        writer.add_subject(f"s{index:02d}", runs)
    return connectome.ConnectomeBundle.open(writer.finalize())


def _random_subjects(rng, n_voxels, n_subjects, runs, shared=0.8):
    """Raw time series with a common latent signal (so correlations are far from 0) and baselines."""
    subjects = []
    for _ in range(n_subjects):
        subject = []
        for length in runs:
            latent = rng.standard_normal(length)
            loadings = rng.uniform(-shared, shared, size=(n_voxels, 1))
            subject.append(50.0 + loadings * latent + rng.standard_normal((n_voxels, length)))
        subjects.append(subject)
    return subjects


class EngineTest(_TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.rng = np.random.default_rng(4)
        self.mask = np.zeros((8, 9, 10), dtype=bool)
        self.mask[1:7, 1:8, 2:9] = True
        self.n_voxels = int(self.mask.sum())
        weights = np.zeros((3, self.n_voxels), dtype=np.float32)
        weights[0, self.rng.choice(self.n_voxels, 12, replace=False)] = 1.0
        weights[1, self.rng.choice(self.n_voxels, 30, replace=False)] = self.rng.uniform(0.05, 1.0, 30)
        weights[2, self.rng.choice(self.n_voxels, 1)] = 0.125
        self.weights = weights

    def _reference_r(self, raw_runs, weights):
        """Mean over runs of the Pearson r between the weighted seed and every voxel (float64)."""
        per_run = [_pearson_rows(weights @ run, run) for run in raw_runs]
        return np.mean(per_run, axis=0)

    def test_single_run_matches_corrcoef(self):
        raw = _random_subjects(self.rng, self.n_voxels, 1, (40,))
        raw[0][0][5] = 7.0  # a zero-variance voxel
        for dtype, atol in (("float32", FLOAT32_R_ATOL), ("float16", FLOAT16_R_ATOL)):
            with self.subTest(dtype=dtype):
                bundle = _write_bundle(self.path(f"single_{dtype}.lnmc"), raw, self.mask, dtype=dtype)
                subject = bundle.load_subject("s00")
                r = engine.subject_correlation(subject.data, subject.norms, subject.runs, self.weights)
                self.assertEqual(r.shape, (self.n_voxels, 3))
                for lesion in range(3):
                    with np.errstate(invalid="ignore", divide="ignore"):  # the zero-variance voxel gives NaN
                        reference = np.corrcoef(np.vstack([self.weights[lesion] @ raw[0][0], raw[0][0]]))[0, 1:]
                    finite = np.isfinite(reference)
                    np.testing.assert_allclose(r[finite, lesion], np.clip(reference[finite], -engine.RMAX, engine.RMAX), atol=atol)
                    self.assertEqual(r[5, lesion], 0.0)
                    if dtype == "float16":  # typical error is much smaller than the worst-case bound
                        self.assertLess(np.abs(r[finite, lesion] - reference[finite]).mean(), 2e-4)

    def test_multi_run_average_matches_per_run_corrcoef(self):
        raw = _random_subjects(self.rng, self.n_voxels, 1, (25, 35, 18))
        bundle = _write_bundle(self.path("runs.lnmc"), raw, self.mask, dtype="float32")
        subject = bundle.load_subject("s00")
        self.assertEqual(subject.runs, (25, 35, 18))
        r = engine.subject_correlation(subject.data, subject.norms, subject.runs, self.weights)
        for lesion in range(3):
            reference = self._reference_r(raw[0], self.weights[lesion])
            np.testing.assert_allclose(r[:, lesion], np.clip(reference, -engine.RMAX, engine.RMAX), atol=FLOAT32_R_ATOL)

    def test_welford_t_matches_ttest_1samp(self):
        raw = _random_subjects(self.rng, self.n_voxels, 7, (20, 22))
        bundle = _write_bundle(self.path("welford.lnmc"), raw, self.mask)
        maps = engine.compute_network_maps(bundle, self.weights)
        self.assertEqual(maps.n, 7)
        self.assertFalse(maps.cancelled)
        per_subject_r = []
        for subject_id in bundle.subject_ids:
            subject = bundle.load_subject(subject_id)
            per_subject_r.append(engine.subject_correlation(subject.data, subject.norms, subject.runs, self.weights))
        per_subject_r = np.array(per_subject_r)  # (n, V, L)
        per_subject_z = np.arctanh(per_subject_r)
        reference_t = scipy_stats.ttest_1samp(per_subject_z.astype(np.float64), 0.0, axis=0).statistic
        np.testing.assert_allclose(maps.t, reference_t.T, rtol=1e-6, atol=1e-6)
        np.testing.assert_allclose(maps.avg_fz, per_subject_z.mean(axis=0).T, rtol=1e-6, atol=1e-7)
        np.testing.assert_allclose(maps.avg_r, per_subject_r.mean(axis=0).T, rtol=1e-6, atol=1e-7)
        first = maps[0]
        self.assertEqual(set(first), {"AvgR", "AvgFz", "T", "N"})
        self.assertEqual(first["N"], 7)
        volume = maps.volume(1, "T", bundle)
        self.assertEqual(volume.shape, bundle.shape)
        np.testing.assert_array_equal(volume[bundle.mask], maps.t[1])

    def test_batched_equals_sequential(self):
        raw = _random_subjects(self.rng, self.n_voxels, 4, (30,))
        bundle = _write_bundle(self.path("batch.lnmc"), raw, self.mask)
        batched = engine.compute_network_maps(bundle, self.weights, chunk=97)
        for lesion in range(3):
            single = engine.compute_network_maps(bundle, self.weights[lesion], chunk=1000)
            np.testing.assert_allclose(batched.avg_r[lesion], single.avg_r[0], atol=1e-6)
            # arctanh amplifies GEMM rounding differences near |r| = 1 (a seed voxel with itself)
            well_conditioned = np.abs(single.avg_r[0]) < 0.99
            self.assertGreater(well_conditioned.mean(), 0.9)
            np.testing.assert_allclose(batched.avg_fz[lesion][well_conditioned], single.avg_fz[0][well_conditioned], atol=1e-6)
            np.testing.assert_allclose(batched.t[lesion][well_conditioned], single.t[0][well_conditioned], rtol=1e-5, atol=1e-5)
        empty = engine.compute_network_maps(bundle, np.zeros((1, self.n_voxels), np.float32))
        self.assertFalse(np.any(empty.t) or np.any(empty.avg_r))
        with self.assertRaises(ValueError):
            engine.compute_network_maps(bundle, np.ones((1, self.n_voxels + 1), np.float32))

    def test_subject_subsets_are_deterministic(self):
        raw = _random_subjects(self.rng, self.n_voxels, 8, (12,))
        bundle = _write_bundle(self.path("subset.lnmc"), raw, self.mask)
        ids = bundle.subject_ids
        self.assertEqual(bundle.select_subjects(3), ids[:3])
        self.assertEqual(bundle.select_subjects(100), ids)
        first = bundle.select_subjects(4, random=True, seed=11)
        self.assertEqual(first, bundle.select_subjects(4, random=True, seed=11))
        self.assertEqual(len(set(first)), 4)
        self.assertEqual(first, sorted(first, key=ids.index))
        self.assertTrue(any(bundle.select_subjects(4, random=True, seed=s) != first for s in range(12, 20)))
        by_count = engine.compute_network_maps(bundle, self.weights, subjects=4, random_subset=True, seed=11)
        by_list = engine.compute_network_maps(bundle, self.weights, subjects=first, prefetch=False)
        self.assertEqual(by_count.subject_ids, first)
        np.testing.assert_array_equal(by_count.t, by_list.t)
        self.assertEqual(engine.compute_network_maps(bundle, self.weights, subjects=3).subject_ids, ids[:3])

    def test_cancellation_and_snapshots(self):
        raw = _random_subjects(self.rng, self.n_voxels, 6, (12,))
        bundle = _write_bundle(self.path("cancel.lnmc"), raw, self.mask)
        cancel = threading.Event()
        calls = []

        def progress(done, total, elapsed):
            calls.append((done, total))
            self.assertGreaterEqual(elapsed, 0.0)
            if done == 2:
                cancel.set()

        maps = engine.compute_network_maps(bundle, self.weights, progress=progress, cancel=cancel)
        self.assertTrue(maps.cancelled)
        self.assertEqual(maps.n, 2)
        self.assertEqual(calls, [(1, 6), (2, 6)])
        reference = engine.compute_network_maps(bundle, self.weights, subjects=2)
        np.testing.assert_array_equal(maps.t, reference.t)
        self.assertEqual(engine.compute_network_maps(bundle, self.weights, cancel=lambda: True).n, 0)
        # Closing the bundle during a run (e.g. from another thread) never yields "complete" partial maps
        closed = engine.compute_network_maps(bundle, self.weights, progress=lambda done, total, elapsed: done == 3 and bundle.close())
        self.assertTrue(closed.cancelled)
        self.assertEqual(closed.n, 3)

        snapshots = []
        full = engine.compute_network_maps(bundle, self.weights, snapshot_every=2, snapshot=snapshots.append)
        self.assertEqual([s.n for s in snapshots], [2, 4])
        np.testing.assert_array_equal(snapshots[0].t, reference.t)
        self.assertEqual(full.n, 6)

    def test_planted_network(self):
        meta = connectome.make_synthetic(self.path("synthetic.lnmc"), seed=1)
        bundle = connectome.ConnectomeBundle.open(meta["path"])
        blobs = meta["blobs"]
        weights = blobs["A"]["mask"].astype(np.float32)[np.newaxis]
        maps = engine.compute_network_maps(bundle, weights)
        elsewhere = ~(blobs["A"]["mask"] | blobs["A2"]["mask"] | blobs["B"]["mask"])
        self.assertGreater(maps.t[0][blobs["A2"]["mask"]].mean(), 10.0)
        self.assertLess(maps.t[0][blobs["B"]["mask"]].mean(), -10.0)
        self.assertLess(abs(maps.t[0][elsewhere].mean()), 0.5)
        self.assertGreater(maps.avg_r[0][blobs["A2"]["mask"]].mean(), 0.3)

    def test_lesion_preparation_and_qc(self):
        meta = connectome.make_synthetic(self.path("qc.lnmc"), n_subjects=2)
        bundle = connectome.ConnectomeBundle.open(meta["path"])
        # A 1 mm radiological lesion: 3x3x3 mm cube at the center of blob A, plus voxels outside the mask
        lesion_affine = np.diag([-1.0, 1.0, 1.0, 1.0])
        lesion_affine[:3, 3] = [30.0, -30.0, -30.0]
        lesion_grid = geometry.Grid((61, 61, 61), lesion_affine)
        center = np.round(lesion_grid.world_to_voxel([meta["blobs"]["A"]["center_world"]])[0]).astype(int)
        lesion = np.zeros(lesion_grid.shape, dtype=np.uint8)
        lesion[center[0] - 1:center[0] + 2, center[1] - 1:center[1] + 2, center[2] - 1:center[2] + 2] = 1
        lesion[0:2, 0:2, 0:2] = 1  # corner of the field of view: outside the ellipsoid mask
        weights, qc = engine.prepare_lesion((lesion, lesion_affine), bundle)
        self.assertGreater(qc["voxels_in_mask"], 0)
        self.assertAlmostEqual(qc["volume_mm3"], 35.0, places=6)
        self.assertGreater(qc["weight_outside_fraction"], 0.0)
        self.assertLess(qc["weight_outside_fraction"], 0.5)
        self.assertGreater(weights[meta["blobs"]["A"]["mask"]].sum(), 0.9 * 27 / 8)
        self.assertTrue(np.all(weights[~meta["blobs"]["A"]["mask"]] == 0))
        # A 1.5 mm lesion inside the mask: nothing is reported outside and the volume is kept
        affine = np.diag([1.5, 1.5, 1.5, 1.0])
        affine[:3, 3] = -15.2
        grid = geometry.Grid((21, 21, 21), affine)
        sphere = np.linalg.norm(grid.voxel_to_world(np.indices(grid.shape).reshape(3, -1).T), axis=1) <= 5.0
        weights, qc = engine.prepare_lesion((sphere.reshape(grid.shape).astype(np.uint8), affine), bundle)
        self.assertEqual(qc["weight_outside_fraction"], 0.0)
        self.assertAlmostEqual(qc["volume_mm3"], sphere.sum() * 1.5**3, places=6)
        self.assertAlmostEqual(weights.sum() * bundle.grid.voxel_volume / qc["volume_mm3"], 1.0, delta=1e-3)


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(5)

    def test_thresholds(self):
        self.assertAlmostEqual(stats.bonferroni_t_threshold(228483, 999), 5.22, delta=0.01)
        self.assertAlmostEqual(stats.bonferroni_t_threshold(292019, 999), 5.27, delta=0.01)
        self.assertAlmostEqual(stats.t_to_p(7.0, 999) / 4.7e-12, 1.0, delta=0.05)
        self.assertAlmostEqual(float(stats.t_to_p(4.25, 97)), 4.9e-5, delta=0.1e-5)
        t = np.array([-6.0, -1.0, 0.0, 2.5, 9.0])
        np.testing.assert_allclose(stats.p_to_t(stats.t_to_p(t, 50), 50), np.abs(t), rtol=1e-6)
        np.testing.assert_array_equal(stats.threshold_map(t, 2.5), [-6.0, 0.0, 0.0, 2.5, 9.0])
        np.testing.assert_array_equal(stats.threshold_map(t, 2.5, "pos"), [0.0, 0.0, 0.0, 2.5, 9.0])
        np.testing.assert_array_equal(stats.threshold_map(t, 2.5, "neg"), [-6.0, 0.0, 0.0, 0.0, 0.0])
        np.testing.assert_array_equal(stats.binarize(t, 2.5), [True, False, False, True, True])
        np.testing.assert_array_equal(stats.binarize(t), [True, True, False, True, True])

    def test_overlap_sensitivity_conjunction_against_brute_force(self):
        maps = self.rng.random((9, 50)) > 0.4
        count, percent = stats.overlap(maps)
        for v in range(50):
            self.assertEqual(count[v], sum(int(maps[i, v]) for i in range(9)))
            self.assertAlmostEqual(percent[v], 100.0 * count[v] / 9)
        np.testing.assert_array_equal(stats.sensitivity_map(percent, 66.0), percent >= 66.0)
        a, b, c = maps[:3]
        np.testing.assert_array_equal(stats.conjunction(a, b, c), [a[v] and b[v] and c[v] for v in range(50)])

    def test_liebermeister_against_brute_force(self):
        n1, n2, n_voxels = 7, 9, 300
        cases = self.rng.random((n1, n_voxels)) < self.rng.uniform(0, 1, n_voxels)
        controls = self.rng.random((n2, n_voxels)) < self.rng.uniform(0, 0.6, n_voxels)
        p = stats.liebermeister(cases, controls, min_fraction=0.1)
        for v in range(n_voxels):
            a, c = int(cases[:, v].sum()), int(controls[:, v].sum())
            if (a + c) < 0.1 * (n1 + n2):
                self.assertEqual(p[v], 1.0)
                continue
            expected = scipy_stats.fisher_exact([[a + 1, n1 - a], [c, n2 - c + 1]], alternative="greater").pvalue
            self.assertAlmostEqual(p[v] / expected, 1.0, places=9)

    def test_welch_t_matches_scipy(self):
        cases = self.rng.standard_normal((8, 40)) + 0.5
        controls = 2.0 * self.rng.standard_normal((13, 40))
        t, df = stats.welch_t(cases, controls)
        reference = scipy_stats.ttest_ind(cases, controls, equal_var=False, axis=0)
        np.testing.assert_allclose(t, reference.statistic, rtol=1e-10)
        np.testing.assert_allclose(df, reference.df, rtol=1e-10)

    def test_symptom_correlation_matches_corrcoef(self):
        maps = self.rng.standard_normal((15, 30))
        scores = maps[:, 3] * 2 + self.rng.standard_normal(15)
        r, p = stats.symptom_correlation(maps, scores)
        for v in range(30):
            self.assertAlmostEqual(r[v], np.corrcoef(maps[:, v], scores)[0, 1], places=12)
            self.assertAlmostEqual(p[v], scipy_stats.pearsonr(maps[:, v], scores).pvalue, places=10)
        rho, p_rho = stats.symptom_correlation(maps, scores, method="spearman")
        reference = scipy_stats.spearmanr(maps[:, 3], scores)
        self.assertAlmostEqual(rho[3], reference.statistic, places=12)
        self.assertAlmostEqual(p_rho[3], reference.pvalue, places=8)

    def test_permutation_fwe_matches_explicit_permutations(self):
        cases = self.rng.standard_normal((6, 25))
        controls = self.rng.standard_normal((7, 25))
        cases[:, :3] += 3.0
        p = stats.permutation_maxstat_fwe(cases, controls, n_perm=200, seed=9, batch=17)
        data = np.vstack([cases, controls])
        rng = np.random.default_rng(9)
        max_null = []
        for _ in range(200):
            order = rng.permutation(13)
            group = np.zeros(13, dtype=bool)
            group[order[:6]] = True
            max_null.append(np.abs(stats.welch_t(data[group], data[~group])[0]).max())
        observed = np.abs(stats.welch_t(cases, controls)[0])
        expected = (1 + (np.array(max_null)[np.newaxis, :] >= observed[:, np.newaxis] - 1e-9).sum(axis=1)) / 201.0
        np.testing.assert_allclose(p, expected)
        self.assertTrue(np.all(p[:3] < 0.05))
        self.assertTrue(np.all(p[3:] > 0.05))
        positive = stats.permutation_maxstat_fwe(cases, controls, n_perm=50, seed=1, stat="meandiff", tail="pos")
        self.assertTrue(np.all((positive > 0) & (positive <= 1)))

    def test_spatial_correlation(self):
        a = self.rng.standard_normal(100)
        b = a + self.rng.standard_normal(100)
        mask = self.rng.random(100) > 0.3
        self.assertAlmostEqual(stats.spatial_correlation(a, b, mask), np.corrcoef(a[mask], b[mask])[0, 1], places=12)
        a[0] = np.nan
        self.assertAlmostEqual(stats.spatial_correlation(a, b), np.corrcoef(a[1:], b[1:])[0, 1], places=12)
        self.assertTrue(np.isnan(stats.spatial_correlation(np.ones(5), np.arange(5))))

    def test_peak_table(self):
        grid = geometry.Grid((30, 30, 30), RADIOLOGICAL_AFFINE)
        ijk = np.indices(grid.shape).reshape(3, -1).T
        volume = np.zeros(grid.n_voxels)
        peaks = {(5, 5, 5): 10.0, (20, 22, 8): -8.0, (6, 6, 5): 9.0, (25, 4, 20): 6.0}
        for center, height in peaks.items():
            volume += height * np.exp(-((ijk - center) ** 2).sum(axis=1) / 2.0)
        volume = volume.reshape(grid.shape)
        table = stats.peak_table(volume, grid, top_k=3, min_distance_mm=8.0)
        self.assertEqual([p["ijk"] for p in table], [(5, 5, 5), (20, 22, 8), (25, 4, 20)])
        np.testing.assert_allclose(table[0]["world"], grid.voxel_to_world([(5, 5, 5)])[0])
        self.assertAlmostEqual(table[1]["value"], volume[20, 22, 8])
        positive = stats.peak_table(volume, grid, top_k=5, min_distance_mm=2.0, sign="pos")
        self.assertEqual(positive[1]["ijk"], (6, 6, 5))
        self.assertTrue(all(p["value"] > 0 for p in positive))
        mask = np.zeros(grid.shape, dtype=bool)
        mask[15:, :, :] = True
        indices = geometry.mask_indices(mask)
        masked = stats.peak_table(volume[mask], grid, top_k=1, mask_indices=indices)
        self.assertEqual(masked[0]["ijk"], (20, 22, 8))


class BundleTest(_TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.rng = np.random.default_rng(6)
        self.mask = np.zeros((6, 7, 8), dtype=bool)
        self.mask[1:5, 2:6, 1:7] = True
        self.n_voxels = int(self.mask.sum())

    def _alive_prefetch_threads(self, timeout=3.0):
        deadline = time.time() + timeout
        while True:
            alive = [t for t in threading.enumerate() if t.name == "LNM-prefetch" and t.is_alive()]
            if not alive or time.time() > deadline:
                return alive
            time.sleep(0.02)

    def test_writer_reader_round_trip(self):
        raw = _random_subjects(self.rng, self.n_voxels, 3, (15, 20))
        raw[1][0][4] = 3.0  # zero variance in the first run only
        bundle = _write_bundle(self.path("rt.lnmc"), raw, self.mask, affine=RADIOLOGICAL_AFFINE)
        self.assertEqual(bundle.metadata["format"], "lnmc")
        self.assertEqual(bundle.metadata["version"], 1)
        self.assertEqual(bundle.metadata["dtype"], "float16")
        self.assertEqual(bundle.n_subjects, 3)
        self.assertEqual(bundle.subject_ids, ["s00", "s01", "s02"])
        self.assertEqual(bundle.shape, self.mask.shape)
        self.assertEqual(bundle.n_voxels, self.n_voxels)
        np.testing.assert_array_equal(bundle.affine, RADIOLOGICAL_AFFINE)
        np.testing.assert_array_equal(bundle.mask, self.mask)
        self.assertTrue(bundle.grid.matches(geometry.Grid(self.mask.shape, RADIOLOGICAL_AFFINE)))
        self.assertIsNone(bundle.degree)
        self.assertEqual(bundle.data_size_bytes, 3 * self.n_voxels * 35 * 2)
        for index, subject in enumerate(bundle.iter_subjects()):
            self.assertEqual(subject.id, f"s{index:02d}")
            self.assertEqual(subject.data.shape, (self.n_voxels, 35))
            self.assertEqual(subject.data.dtype, np.float16)
            self.assertEqual(subject.norms.shape, (2, self.n_voxels))
            start = 0
            for run, length in enumerate(subject.runs):
                original = raw[index][run] - raw[index][run].mean(axis=1, keepdims=True)
                segment = subject.data[:, start:start + length].astype(np.float64)
                norms = np.linalg.norm(original, axis=1)
                if index == 1 and run == 0:
                    self.assertEqual(subject.norms[run, 4], 0.0)
                    self.assertFalse(np.any(segment[4]))
                    norms[4] = 0.0
                np.testing.assert_allclose(subject.norms[run], norms, rtol=1e-6)
                valid = norms > 0
                np.testing.assert_allclose(np.linalg.norm(segment[valid], axis=1), 1.0, atol=2e-3)
                np.testing.assert_allclose(segment[valid] * norms[valid, np.newaxis], original[valid], atol=2e-3 * norms.max())
                start += length
        # Prefetching with recycled buffers yields the same data as direct loading
        for subject in bundle.iter_subjects(prefetch=True, reuse_buffers=True):
            np.testing.assert_array_equal(subject.data, bundle.load_subject(subject.id).data)
        for subject in bundle.iter_subjects(prefetch=False, reuse_buffers=True):
            np.testing.assert_array_equal(subject.data, bundle.load_subject(subject.id).data)
        with self.assertRaises(KeyError):
            bundle.load_subject("missing")
        with self.assertRaises(FileExistsError):
            connectome.ConnectomeWriter(self.path("rt.lnmc"), self.mask, RAS_AFFINE)

    def test_writer_validation_and_resume(self):
        path = self.path("resume.lnmc")
        writer = connectome.ConnectomeWriter(path, self.mask, RAS_AFFINE)
        with self.assertRaises(ValueError):
            writer.add_subject("bad/id", [np.zeros((self.n_voxels, 5))])
        with self.assertRaises(ValueError):
            writer.add_subject("s1", [np.zeros((self.n_voxels + 1, 5))])
        writer.add_subject("s1", _random_subjects(self.rng, self.n_voxels, 1, (6,))[0])
        with self.assertRaises(ValueError):
            writer.add_subject("s1", _random_subjects(self.rng, self.n_voxels, 1, (6,))[0])
        # Interrupted before finalize: resume keeps the subject that was written
        resumed = connectome.ConnectomeWriter(path, self.mask, RAS_AFFINE, resume=True)
        self.assertTrue(resumed.has_subject("s1"))
        resumed.add_subject("s2", _random_subjects(self.rng, self.n_voxels, 1, (6, 7))[0])
        bundle = connectome.ConnectomeBundle.open(resumed.finalize())
        self.assertEqual(bundle.subject_ids, ["s1", "s2"])
        self.assertEqual(bundle.runs("s2"), (6, 7))
        self.assertFalse(os.path.exists(os.path.join(path, connectome.PARTIAL_MANIFEST_NAME)))
        self.assertEqual(connectome.sanitize_subject_id("sub 01/ses:1"), "sub_01_ses_1")
        # Overwriting first removes the previous manifest, subject files and derived maps
        connectome.compute_degree(bundle, n_seeds=5)
        old_files = [*bundle.subject_files("s1"), *bundle.subject_files("s2")]
        overwriting = connectome.ConnectomeWriter(path, self.mask, RAS_AFFINE, overwrite=True)
        for stale in (os.path.join(path, connectome.MANIFEST_NAME), os.path.join(path, "derived", "degree.npy"), *old_files):
            self.assertFalse(os.path.exists(stale), stale)
        overwriting.add_subject("s3", _random_subjects(self.rng, self.n_voxels, 1, (6,))[0])
        self.assertEqual(connectome.ConnectomeBundle.open(overwriting.finalize()).subject_ids, ["s3"])
        # Resuming onto a bundle with another mask (same voxel count) or affine is refused
        for mask, affine in ((np.roll(self.mask, 1, axis=0), RAS_AFFINE), (self.mask, RADIOLOGICAL_AFFINE)):
            with self.assertRaisesRegex(ValueError, "Cannot resume"):
                connectome.ConnectomeWriter(path, mask, affine, resume=True)
        self.assertEqual(connectome.ConnectomeBundle.open(path).subject_ids, ["s3"])

    def test_prefetch_thread_stops_on_early_close(self):
        bundle = _write_bundle(self.path("prefetch.lnmc"), _random_subjects(self.rng, self.n_voxels, 6, (10,)), self.mask)
        for reuse_buffers in (False, True):
            for subject in bundle.iter_subjects(reuse_buffers=reuse_buffers):
                self.assertEqual(subject.id, "s00")
                break
            self.assertEqual(self._alive_prefetch_threads(), [])
        # Abandoned generator closed by the bundle while suspended
        iterator = bundle.iter_subjects(reuse_buffers=True)
        next(iterator)
        bundle.close()
        self.assertEqual(self._alive_prefetch_threads(), [])
        self.assertEqual(list(iterator), [])
        # Read errors in the background thread reach the consumer
        os.remove(bundle.subject_files("s02")[0])
        seen = []
        with self.assertRaises(FileNotFoundError):
            for subject in bundle.iter_subjects():
                seen.append(subject.id)
        self.assertEqual(seen, ["s00", "s01"])
        self.assertEqual(self._alive_prefetch_threads(), [])

    def test_from_nifti_timeseries(self):
        mask_path = self.path("mask.nii.gz")
        nifti.write_nifti(mask_path, self.mask.astype(np.uint8), RADIOLOGICAL_AFFINE)
        series = {}
        for name, length in (("sub-01_run-1", 9), ("sub-01_run-2", 11), ("sub-02_run-1", 10)):
            data = 100.0 + self.rng.standard_normal((*self.mask.shape, length)).astype(np.float32)
            path = self.path(f"{name}_bold.nii.gz")
            nifti.write_nifti(path, data, RADIOLOGICAL_AFFINE, time_step=2.0)
            series[name] = data[self.mask]
        progress = []
        out = connectome.from_nifti_timeseries(self.path("sub-*_bold.nii.gz"), mask_path, self.path("nifti.lnmc"), runs_grouping=r"(?P<subject>sub-\d+)_run", citation="test", progress=lambda *args: progress.append(args[:2]))
        bundle = connectome.ConnectomeBundle.open(out)
        self.assertEqual(bundle.name, "nifti")
        self.assertEqual(bundle.citation, "test")
        self.assertEqual(bundle.subject_ids, ["sub-01", "sub-02"])
        self.assertEqual(bundle.runs("sub-01"), (9, 11))
        self.assertEqual(progress, [(1, 2), (2, 2)])
        np.testing.assert_array_equal(bundle.affine, RADIOLOGICAL_AFFINE)
        subject = bundle.load_subject("sub-01")
        expected, _ = connectome.normalize_run(series["sub-01_run-2"])
        np.testing.assert_allclose(subject.data[:, 9:].astype(np.float64), expected, atol=1e-3)
        # Without grouping every file is a subject
        files = [self.path(f"{name}_bold.nii.gz") for name in sorted(series)]
        single = connectome.ConnectomeBundle.open(connectome.from_nifti_timeseries(files, mask_path, self.path("single.lnmc")))
        self.assertEqual(single.subject_ids, ["sub-01_run-1_bold", "sub-01_run-2_bold", "sub-02_run-1_bold"])
        # Names that only become equal once sanitized are not merged silently
        clash = self.path("sub-01 run-1_bold.nii.gz")
        shutil.copy(files[0], clash)
        with self.assertRaisesRegex(ValueError, "both become"):
            connectome.from_nifti_timeseries([files[0], clash], mask_path, self.path("clash.lnmc"))
        os.remove(clash)
        # Files on another grid are rejected before anything is converted
        nifti.write_nifti(self.path("sub-03_run-1_bold.nii.gz"), np.zeros((*self.mask.shape, 5), np.float32), RAS_AFFINE)
        with self.assertRaisesRegex(ValueError, "not on the mask grid"):
            connectome.from_nifti_timeseries(self.path("sub-*_bold.nii.gz"), mask_path, self.path("bad.lnmc"))

    def test_from_leaddbs(self):
        shape = (6, 7, 8)
        spm_mat = np.array([[-2.0, 0, 0, 92], [0, 2, 0, -128], [0, 0, 2, -74], [0, 0, 0, 1]])
        mask_c = self.mask.reshape(-1)
        outidx = np.sort(np.ravel_multi_index(np.unravel_index(np.flatnonzero(mask_c), shape), shape, order="F")) + 1
        n_voxels = len(outidx)
        dataset = self.path("GSPtest")
        os.makedirs(os.path.join(dataset, "vol"))
        sub_ids = np.empty((2, 1), dtype=object)
        sub_ids[0, 0] = np.array(["subA", "subA_run1.mat", "subA_run2"], dtype=object)
        sub_ids[1, 0] = np.array(["subB", "subB_run1.mat"], dtype=object)
        vol = {"outidx": outidx.astype(np.float64)[:, np.newaxis], "space": {"dim": np.array([shape], np.float64), "mat": spm_mat, "fname": "space.nii"}, "subIDs": sub_ids}
        sio.savemat(os.path.join(dataset, "dataset_volsurf.mat"), {"vol": vol})
        with open(os.path.join(dataset, "dataset_info.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "GSPtest", "type": "fMRI_timecourses", "formatversion": 1.1}, f)
        gmtc = {}
        for run_file, length in (("subA_run1.mat", 12), ("subA_run2.mat", 9), ("subB_run1.mat", 10)):
            gmtc[run_file] = (50 + self.rng.standard_normal((length, n_voxels))).astype(np.float32)  # T x V, outidx order
            sio.savemat(os.path.join(dataset, "vol", run_file), {"gmtc": gmtc[run_file]})

        bundle = connectome.ConnectomeBundle.open(connectome.from_leaddbs(dataset, self.path("lead.lnmc")))
        self.assertEqual(bundle.subject_ids, ["subA", "subB"])
        self.assertEqual(bundle.runs("subA"), (12, 9))
        np.testing.assert_array_equal(bundle.affine, geometry.affine_from_spm_mat(spm_mat))
        np.testing.assert_array_equal(bundle.mask, self.mask)
        # Voxel k of the bundle (C order) must carry the gmtc column of the same voxel (Fortran order)
        ijk = np.stack(np.unravel_index(bundle.mask_indices, shape), axis=1)
        column = np.searchsorted(outidx - 1, np.ravel_multi_index(tuple(ijk.T), shape, order="F"))
        subject = bundle.load_subject("subA")
        expected, norms = connectome.normalize_run(gmtc["subA_run2.mat"].T[column])
        np.testing.assert_allclose(subject.data[:, 12:].astype(np.float64), expected, atol=1e-3)
        np.testing.assert_allclose(subject.norms[1], norms, rtol=1e-6)
        # A single-subject dataset (loadmat squeezes the outer cell) converts too
        vol["subIDs"] = np.empty((1, 1), dtype=object)
        vol["subIDs"][0, 0] = np.array(["subB", "subB_run1.mat"], dtype=object)
        sio.savemat(os.path.join(dataset, "dataset_volsurf.mat"), {"vol": vol})
        one = connectome.ConnectomeBundle.open(connectome.from_leaddbs(dataset, self.path("one.lnmc")))
        self.assertEqual(one.subject_ids, ["subB"])
        # MATLAB v7.3 (HDF5) files are rejected with a clear message
        with open(os.path.join(dataset, "vol", "subB_run1.mat"), "wb") as f:
            f.write(b"MATLAB 7.3 MAT-file, Platform: GLNXA64, Created on: Mon Jan  1 00:00:00 2024 HDF5 schema 1.00 .".ljust(512, b" ") + b"\x89HDF\r\n\x1a\n")
        with self.assertRaisesRegex(connectome.UnsupportedFormatError, "h5py"):
            connectome.from_leaddbs(dataset, self.path("v73.lnmc"))
        # Subject ids that become equal once sanitized are rejected before anything is converted
        vol["subIDs"] = np.empty((2, 1), dtype=object)
        vol["subIDs"][0, 0] = np.array(["sub A", "subA_run1.mat"], dtype=object)
        vol["subIDs"][1, 0] = np.array(["sub/A", "subA_run2.mat"], dtype=object)
        sio.savemat(os.path.join(dataset, "dataset_volsurf.mat"), {"vol": vol})
        with self.assertRaisesRegex(ValueError, "duplicate subject id"):
            connectome.from_leaddbs(dataset, self.path("duplicates.lnmc"))

    def test_compute_degree(self):
        raw = _random_subjects(self.rng, self.n_voxels, 3, (14, 16))
        bundle = _write_bundle(self.path("degree.lnmc"), raw, self.mask, dtype="float32")
        degree = connectome.compute_degree(bundle, n_seeds=20, seed=3)
        np.testing.assert_array_equal(bundle.degree, degree)
        seeds = np.sort(np.random.default_rng(3).choice(self.n_voxels, 20, replace=False))
        reference = np.zeros(self.n_voxels)
        for runs in raw:
            r = np.mean([np.array([_pearson_rows(run[s], run) for s in seeds]) for run in runs], axis=0)  # (seeds, V)
            fz = np.abs(np.arctanh(np.clip(r, -engine.RMAX, engine.RMAX)))
            fz[np.arange(len(seeds)), seeds] = 0.0
            counts = np.full(self.n_voxels, len(seeds), dtype=np.float64)
            counts[seeds] -= 1
            reference += fz.sum(axis=0) / counts
        np.testing.assert_allclose(degree, reference / len(raw), atol=1e-5)
        mean_r = connectome.compute_degree(bundle, n_seeds=20, seed=3, measure="r", store=False)
        self.assertTrue(np.all(np.abs(mean_r) <= 1.0))


class CommandLineTest(_TempDirTestCase):
    def _run(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = MODULE_DIR + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.engine", *args], check=False, capture_output=True, text=True, env=env, cwd=self.tmp, timeout=300)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return result, events

    def test_engine_cli(self):
        meta = connectome.make_synthetic(self.path("synthetic.lnmc"), n_subjects=5, name="Synthetic Test")
        bundle = connectome.ConnectomeBundle.open(meta["path"])
        # Lesion drawn on a 1 mm radiological grid around the center of blob A
        lesion_affine = np.diag([-1.0, 1.0, 1.0, 1.0])
        lesion_affine[:3, 3] = [25.0, -25.0, -25.0]
        grid = geometry.Grid((51, 51, 51), lesion_affine)
        world = grid.voxel_to_world(np.indices(grid.shape).reshape(3, -1).T)
        lesion = (np.linalg.norm(world - meta["blobs"]["A"]["center_world"], axis=1) <= 3.0).reshape(grid.shape)
        lesion_path = self.path("patient01_lesion.nii.gz")
        nifti.write_nifti(lesion_path, lesion.astype(np.uint8) * 255, lesion_affine)

        out_dir = self.path("out")
        result, events = self._run("--bundle", meta["path"], "--lesion", lesion_path, "--out", out_dir, "--threads", "1", "--snapshot-every", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds[0], "start")
        self.assertEqual(kinds[-1], "done")
        self.assertEqual([e["done"] for e in events if e["event"] == "progress"], [1, 2, 3, 4, 5])
        self.assertEqual([e["n"] for e in events if e["event"] == "snapshot"], [2, 4])
        self.assertEqual(events[0]["lesions"], ["patient01_lesion"])
        self.assertGreater(events[0]["qc"][0]["voxels_in_mask"], 0)
        done = events[-1]
        self.assertEqual(done["n"], 5)
        expected = [self.path("out", f"patient01_lesion_conn-SyntheticTest_desc-{desc}_funcmap.nii.gz") for desc in ("AvgR", "AvgRFz", "T")]
        self.assertEqual(done["outputs"], expected)
        for path in expected:
            data, affine, header = nifti.read_nifti(path)
            np.testing.assert_array_equal(affine, bundle.affine)
            self.assertEqual(data.shape, bundle.shape)
            self.assertEqual(data.dtype, np.float32)
            self.assertFalse(np.any(data[~bundle.mask]))
        t_map, _, header = nifti.read_nifti(expected[2])
        self.assertEqual((header["intent_code"], header["intent_params"][0]), (3, 4.0))
        a2 = geometry.from_masked_vector(meta["blobs"]["A2"]["mask"], bundle.mask_indices, bundle.shape)
        b = geometry.from_masked_vector(meta["blobs"]["B"]["mask"], bundle.mask_indices, bundle.shape)
        self.assertGreater(t_map[a2].mean(), 5.0)
        self.assertLess(t_map[b].mean(), -5.0)
        # Same result as the in-process engine
        weights, _ = engine.prepare_lesion(lesion_path, bundle)
        maps = engine.compute_network_maps(bundle, weights)
        np.testing.assert_allclose(t_map[bundle.mask], maps.t[0], rtol=1e-5, atol=1e-5)

        # A random subset and an error
        result, events = self._run("--bundle", meta["path"], "--lesion", lesion_path, "--out", out_dir, "--subjects", "3", "--random", "--seed", "4")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events[-1]["n"], 3)
        result, events = self._run("--bundle", self.path("missing.lnmc"), "--lesion", lesion_path, "--out", out_dir)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(events[-1]["event"], "error")
        self.assertIn("missing.lnmc", events[-1]["message"])
        # Invalid arguments are reported as a JSON error event too (argparse exit code 2)
        result, events = self._run("--bundle", meta["path"], "--lesion", lesion_path, "--out", out_dir, "--threads", "abc")
        self.assertEqual(result.returncode, 2)
        self.assertEqual([event["event"] for event in events], ["error"])
        self.assertIn("--threads", events[0]["message"])

    def test_benchmark_runs(self):
        result = engine.benchmark(V=5000, T=40, L=3, n_subjects=2)
        self.assertGreater(result["per_subject_best_s"], 0.0)
        self.assertEqual((result["V"], result["T"], result["L"]), (5000, 40, 3))


if __name__ == "__main__":
    unittest.main()

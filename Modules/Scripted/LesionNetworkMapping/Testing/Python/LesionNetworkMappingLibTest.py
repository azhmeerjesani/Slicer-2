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

from LesionNetworkMappingLib import connectome, engine, export, geometry, nifti, qa, stats, thresholds

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


class WriteLikeTest(_TempDirTestCase):
    """nifti.write_like: Slicer arrays written on the exact voxel grid and header of a reference image."""

    def _reference(self, name, affine, shape=(7, 6, 5), **kwargs):
        path = self.path(name)
        nifti.write_nifti(path, np.zeros(shape, np.int16), affine, **kwargs)
        return path

    def _assert_same_geometry_bytes(self, reference_path, path):
        reference, written = nifti.read_raw_header(reference_path), nifti.read_raw_header(path)
        for field in nifti.GEOMETRY_FIELDS:
            self.assertEqual(reference[field].tobytes(), written[field].tobytes(), field)
        self.assertEqual(reference.dtype.byteorder, written.dtype.byteorder)

    def test_round_trips_on_ras_and_radiological_references(self):
        rng = np.random.default_rng(10)
        shape = (7, 6, 5)
        lesion = (rng.random(shape) > 0.7).astype(np.uint8)  # [i, j, k] in reference file order
        for label, affine, extra in (
            ("ras", RAS_AFFINE, {}),
            ("radiological", RADIOLOGICAL_AFFINE, {}),
            ("radiological_big_endian_codes", RADIOLOGICAL_AFFINE, {"byteorder": ">", "sform_code": 2, "qform_code": 1, "xyzt_units": 2}),
        ):
            reference_path = self._reference(f"{label}_ref.nii.gz", affine, **extra)
            ref_affine = nifti.read_nifti_header(reference_path)["affine"]
            ni, nj, _ = shape
            cases = {
                # The array loaded from the reference itself (Slicer keeps the file voxel order)
                "identity": (lesion, ref_affine),
                # The same voxels on a grid whose i axis runs the other way
                "flipped_i": (lesion[::-1], ref_affine @ np.array([[-1.0, 0, 0, ni - 1], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])),
                # Axes stored in another order (k, i, j) with j flipped
                "permuted": (np.transpose(lesion[:, ::-1, :], (2, 0, 1)), ref_affine @ np.array([[0, 1.0, 0, 0], [0, 0, -1, nj - 1], [1, 0, 0, 0], [0, 0, 0, 1]])),
            }
            # A cropped extent (e.g. a segment's own geometry) offset by whole voxels
            cropped = np.zeros(shape, np.uint8)
            cropped[2:6, 1:4, 1:5] = lesion[2:6, 1:4, 1:5]
            cases["cropped"] = (lesion[2:6, 1:4, 1:5], ref_affine @ np.array([[1.0, 0, 0, 2], [0, 1, 0, 1], [0, 0, 1, 1], [0, 0, 0, 1]]))
            for case, (array_ijk, ijk_to_ras) in cases.items():
                with self.subTest(reference=label, case=case):
                    path = self.path(f"{label}_{case}.nii.gz")
                    self.assertEqual(nifti.write_like(path, np.transpose(array_ijk, (2, 1, 0)), ijk_to_ras, reference_path), path)
                    data, read_affine, header = nifti.read_nifti(path)
                    self.assertEqual(data.dtype, np.uint8)
                    np.testing.assert_array_equal(data, cropped if case == "cropped" else lesion)
                    np.testing.assert_array_equal(read_affine, ref_affine)
                    self._assert_same_geometry_bytes(reference_path, path)
                    self.assertEqual(header["qfac"], nifti.read_nifti_header(reference_path)["qfac"])

    def test_rejects_other_grids_and_lost_voxels(self):
        reference_path = self._reference("ref.nii", RADIOLOGICAL_AFFINE)
        lesion = np.zeros((5, 6, 7), np.uint8)  # [k, j, i]
        lesion[2, 3, 4] = 1
        half_voxel = RADIOLOGICAL_AFFINE.copy()
        half_voxel[:3, 3] += [1.0, 0.0, 0.0]
        finer = RADIOLOGICAL_AFFINE.copy()
        finer[:3, :3] /= 2.0
        for label, affine in (("half voxel shift", half_voxel), ("other spacing", finer), ("oblique", _oblique_affine())):
            with self.subTest(label), self.assertRaisesRegex(ValueError, "voxel grid"):
                nifti.write_like(self.path("bad.nii.gz"), lesion, affine, reference_path)
        shifted = RADIOLOGICAL_AFFINE @ np.array([[1.0, 0, 0, 3], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        with self.assertRaisesRegex(ValueError, "outside"):
            nifti.write_like(self.path("outside.nii.gz"), lesion, shifted, reference_path)
        with self.assertRaisesRegex(ValueError, "fit"):
            nifti.write_like(self.path("range.nii.gz"), lesion * 300.0, RADIOLOGICAL_AFFINE, reference_path)
        # Zeros falling outside the reference are fine
        nifti.write_like(self.path("zeros.nii.gz"), np.zeros_like(lesion), shifted, reference_path)
        self.assertFalse(os.path.exists(self.path("bad.nii.gz")))

    def test_consistent_with_simpleitk(self):
        try:
            import SimpleITK as sitk
        except ImportError:
            self.skipTest("SimpleITK is not available")
        lesion = np.zeros((7, 6, 5), np.uint8)
        lesion[1:3, 2:5, 3] = 1
        for affine in (RAS_AFFINE, RADIOLOGICAL_AFFINE):
            reference_path = self._reference("itk_ref.nii.gz", affine)
            path = self.path("itk_lesion.nii.gz")
            nifti.write_like(path, lesion.transpose(2, 1, 0), affine, reference_path)
            reference, image = sitk.ReadImage(reference_path), sitk.ReadImage(path)
            self.assertEqual(reference.GetOrigin(), image.GetOrigin())
            self.assertEqual(reference.GetDirection(), image.GetDirection())
            self.assertEqual(reference.GetSpacing(), image.GetSpacing())
            np.testing.assert_array_equal(sitk.GetArrayFromImage(image), lesion.transpose(2, 1, 0))


class SpaceDetectionTest(unittest.TestCase):
    def test_known_template_grids(self):
        nlin6 = np.array([[-2.0, 0, 0, 90], [0, 2, 0, -126], [0, 0, 2, -72], [0, 0, 0, 1]])  # FSL radiological storage
        nlin6_ras = np.array([[2.0, 0, 0, -90], [0, 2, 0, -126], [0, 0, 2, -72], [0, 0, 0, 1]])
        nlin6_1mm = np.array([[-1.0, 0, 0, 90], [0, 1, 0, -126], [0, 0, 1, -72], [0, 0, 0, 1]])
        c2009_2mm = np.array([[2.0, 0, 0, -96.5], [0, 2, 0, -132.5], [0, 0, 2, -78.5], [0, 0, 0, 1]])
        c2009_1mm = np.array([[1.0, 0, 0, -96], [0, 1, 0, -132], [0, 0, 1, -78], [0, 0, 0, 1]])
        cases = [
            ((91, 109, 91), nlin6, ("MNI152NLin6Asym", 2)),
            ((91, 109, 91), nlin6_ras, ("MNI152NLin6Asym", 2)),
            ((182, 218, 182), nlin6_1mm, ("MNI152NLin6Asym", 1)),
            ((97, 115, 97), c2009_2mm, ("MNI152NLin2009cAsym", 2)),
            ((193, 229, 193), c2009_1mm, ("MNI152NLin2009cAsym", 1)),
            ((97, 115, 97), c2009_2mm @ np.diag([1.0, 1.0, 1.0, 1.0]) + np.array([[0, 0, 0, 0.9], [0, 0, 0, -0.5], [0, 0, 0, 0], [0, 0, 0, 0]]), ("MNI152NLin2009cAsym", 2)),
            ((97, 115, 97), c2009_2mm + np.array([[0, 0, 0, 1.5], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]), ("native", None)),
            ((97, 115, 96), c2009_2mm, ("native", None)),
            ((91, 109, 91), np.diag([1.5, 1.5, 1.5, 1.0]), ("native", None)),
            ((91, 109, 91), _oblique_affine(), ("native", None)),
        ]
        # Axes stored in another order (k, i, j): same voxel centers, same space
        permutation = np.array([[0, 1.0, 0, 0], [0, 0, 1, 0], [1, 0, 0, 0], [0, 0, 0, 1]])
        cases.append(((91, 91, 109), nlin6 @ permutation, ("MNI152NLin6Asym", 2)))
        for shape, affine, expected in cases:
            with self.subTest(shape=shape, affine=affine.tolist()):
                self.assertEqual(geometry.detect_grid(shape, affine), expected)
                self.assertEqual(geometry.detect_space(shape, affine), expected[0])

    def test_signed_permutation(self):
        flip = RADIOLOGICAL_AFFINE @ np.array([[-1.0, 0, 0, 30], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        mapping = geometry.signed_permutation(flip, RADIOLOGICAL_AFFINE)
        self.assertEqual(mapping, geometry.SignedPermutation((0, 1, 2), (-1, 1, 1), (30, 0, 0)))
        permuted = RAS_AFFINE @ np.array([[0, 0, 1.0, 0], [1, 0, 0, -2], [0, 1, 0, 5], [0, 0, 0, 1]])
        self.assertEqual(geometry.signed_permutation(permuted, RAS_AFFINE), geometry.SignedPermutation((1, 2, 0), (1, 1, 1), (0, -2, 5)))
        self.assertIsNone(geometry.signed_permutation(_oblique_affine(), RAS_AFFINE))
        self.assertIsNone(geometry.signed_permutation(RAS_AFFINE @ np.diag([0.5, 1, 1, 1]), RAS_AFFINE))
        shifted = RAS_AFFINE.copy()
        shifted[0, 3] += 0.01  # 0.005 voxel: beyond the 1e-3 tolerance
        self.assertIsNone(geometry.signed_permutation(shifted, RAS_AFFINE))
        shifted[0, 3] = RAS_AFFINE[0, 3] + 0.001  # float32 rounding noise
        self.assertIsNotNone(geometry.signed_permutation(shifted, RAS_AFFINE))


class ThresholdsTest(unittest.TestCase):
    def test_fwe_bonferroni(self):
        for n_voxels, expected in ((228_483, 5.22), (292_019, 5.27)):
            resolved = thresholds.resolve(None, 1000, n_voxels)
            self.assertEqual((resolved.statistic, resolved.df, resolved.banner, resolved.allowed), ("T", 999, None, True))
            self.assertAlmostEqual(resolved.value, expected, delta=0.01)
            self.assertAlmostEqual(resolved.p_unc, 0.05 / n_voxels)
            self.assertEqual(resolved.p_fwe, 0.05)
            self.assertAlmostEqual(stats.t_to_p(resolved.value, 999), 0.05 / n_voxels, delta=1e-12)
        self.assertEqual(thresholds.describe(thresholds.resolve({"kind": "fwe_bonferroni"}, 1000, 292_019)), "⇒ |T| ≥ 5.27 at N=1000 (df=999), V=292,019 · p_unc ≤ 1.7e-7")
        # Re-resolved from N: a smaller N gives a higher threshold
        self.assertGreater(thresholds.resolve(None, 100, 292_019).value, 5.27)
        self.assertAlmostEqual(thresholds.resolve({"kind": "fwe_bonferroni", "alpha": 0.01}, 1000, 292_019).p_fwe, 0.01)

    def test_t_convention_needs_n_close_to_1000(self):
        rule = {"kind": "t_convention", "value": 7, "reference_n": 1000}
        resolved = thresholds.resolve(rule, 312, 292_019, note="partial")
        self.assertFalse(resolved.allowed)
        self.assertEqual(resolved.banner, "This threshold was defined for N≈1000; this map has N=312 (partial). Use FWE or effect size.")
        self.assertEqual(thresholds.resolve(rule, 312, 292_019).banner, "This threshold was defined for N≈1000; this map has N=312. Use FWE or effect size.")
        self.assertEqual(resolved.value, 7.0)
        self.assertAlmostEqual(resolved.p_unc, float(stats.t_to_p(7.0, 311)))
        # "Keep T=7" is an explicit, recorded choice
        kept = thresholds.resolve({**rule, "override": True}, 312, 292_019)
        self.assertTrue(kept.allowed)
        self.assertIsNotNone(kept.banner)
        self.assertTrue(kept.rule["override"])
        # Within 10% of the reference N: no banner; above it: a banner but allowed
        for n, banner, allowed in ((1000, False, True), (900, False, True), (1100, False, True), (899, True, False), (1101, True, True)):
            with self.subTest(n=n):
                resolved = thresholds.resolve(rule, n, 292_019)
                self.assertEqual((resolved.banner is not None, resolved.allowed), (banner, allowed))
        self.assertAlmostEqual(thresholds.resolve(rule, 1000, 292_019).p_unc, 4.7e-12, delta=0.1e-12)

    def test_effect_size_and_custom_rules(self):
        fz = thresholds.resolve({"kind": "fz_abs", "value": 0.05}, 312, 292_019)
        self.assertEqual((fz.statistic, fz.value, fz.p_unc, fz.p_fwe, fz.banner, fz.allowed), ("Fz", 0.05, None, None, None, True))
        self.assertEqual(thresholds.describe(fz), "⇒ |Fz| ≥ 0.05 at N=312, V=292,019 · effect size (no p-value)")
        custom = thresholds.resolve({"kind": "custom_t", "value": 4.25, "reference_n": 98}, 98, 1000)
        self.assertEqual((custom.value, custom.banner, custom.allowed), (4.25, None, True))
        self.assertAlmostEqual(custom.p_unc, 4.9e-5, delta=0.1e-5)
        self.assertAlmostEqual(custom.p_fwe, custom.p_unc * 1000)
        self.assertIn("N≈98; this map has N=200", thresholds.resolve({"kind": "custom_t", "value": 4.25, "reference_n": 98}, 200, 1000).banner)
        self.assertIsNone(thresholds.resolve({"kind": "custom_t", "value": 4.25}, 200, 1000).banner)
        too_few = thresholds.resolve(None, 1, 1000)
        self.assertFalse(too_few.allowed)
        self.assertIsNone(too_few.to_dict()["value"])

    def test_json_round_trip_and_validation(self):
        for rule in (None, {"kind": "t_convention"}, {"kind": "fz_abs", "value": 0.1}, {"kind": "custom_t", "value": 6, "reference_n": 500, "override": 1}):
            normalized = thresholds.normalize_rule(rule)
            self.assertEqual(thresholds.rule_from_json(thresholds.rule_to_json(rule)), normalized)
            resolved = thresholds.resolve(normalized, 640, 50_000)
            self.assertEqual(thresholds.Resolved.from_json(resolved.to_json()), resolved)
            self.assertEqual(json.loads(json.dumps(resolved.to_dict()))["rule"], normalized)
        self.assertEqual(thresholds.rule_from_json(""), thresholds.DEFAULT_RULE)
        self.assertEqual(thresholds.normalize_rule({"kind": "t_convention"}), {"kind": "t_convention", "value": 7.0, "reference_n": 1000})
        for bad in ({"kind": "fdr"}, {"kind": "fwe_bonferroni", "alpha": 2}, {"kind": "custom_t", "value": -1}, {"kind": "t_convention", "reference_n": 1}, [1, 2], "{"):
            with self.subTest(rule=bad), self.assertRaises(ValueError):
                thresholds.normalize_rule(bad)
        with self.assertRaises(ValueError):
            thresholds.resolve(None, 100, 0)


class QATest(_TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.meta = connectome.make_synthetic(self.path("qa.lnmc"), n_subjects=4, seed=2)
        self.bundle = connectome.ConnectomeBundle.open(self.meta["path"])
        self.rng = np.random.default_rng(12)

    def _blob_weights(self, name="A"):
        weights = self.meta["blobs"][name]["mask"].astype(np.float32)
        weights[weights > 0] *= self.rng.uniform(0.2, 1.0, int(weights.sum())).astype(np.float32)  # fractional occupancy
        return weights

    def test_degree_similarity(self):
        degree = connectome.compute_degree(self.bundle, n_seeds=300, seed=1)
        r, rho = qa.degree_similarity(degree, degree)
        self.assertAlmostEqual(r, 1.0, places=6)
        self.assertAlmostEqual(rho, 1.0, places=6)
        r, rho = qa.degree_similarity(-2.0 * degree, degree)
        self.assertAlmostEqual(r, -1.0, places=6)
        self.assertAlmostEqual(rho, -1.0, places=6)
        noisy = degree + self.rng.standard_normal(degree.size).astype(np.float32) * degree.std()
        noisy[:5] = np.nan
        valid = np.isfinite(noisy)
        self.assertAlmostEqual(qa.degree_similarity(noisy, degree)[0], np.corrcoef(noisy[valid], degree[valid])[0, 1], places=6)
        self.assertAlmostEqual(qa.degree_similarity(noisy, degree)[1], scipy_stats.spearmanr(noisy[valid], degree[valid]).statistic, places=6)
        self.assertTrue(all(np.isnan(qa.degree_similarity(np.ones(10), np.arange(10.0)))))
        # The planted network (A, A2, B) has the highest degree: a lesion in A gives a map whose
        # strongest values (A2, B) lie on hubs, so |T| tracks degree and T is positively correlated
        maps = engine.compute_network_maps(self.bundle, self.meta["blobs"]["A"]["mask"].astype(np.float32))
        r_signed, rho_signed = qa.degree_similarity(maps.t[0], degree)
        r_abs, _ = qa.degree_similarity(np.abs(maps.t[0]), degree)
        self.assertGreater(r_signed, 0.0)
        self.assertGreater(rho_signed, 0.0)
        self.assertGreater(r_abs, 0.5)
        self.assertGreater(r_abs, r_signed)
        values = qa.lesion_qa(maps.t[0], degree)
        self.assertEqual((values["degree_r"], values["degree_rho"]), (r_signed, rho_signed))
        self.assertIsNone(values["null"])

    def test_chip_levels(self):
        self.assertEqual(qa.chip_level(0.29), ("neutral", "low"))
        self.assertEqual(qa.chip_level(0.3), ("amber", "moderate"))
        self.assertEqual(qa.chip_level(-0.45), ("amber", "moderate"))
        self.assertEqual(qa.chip_level(0.6)[0], "red")
        self.assertIn("connectome hubs", qa.chip_level(0.9)[1])
        self.assertEqual(qa.chip_level(float("nan")), ("neutral", "n/a"))
        self.assertEqual((qa.null_level(0.74), qa.null_level(0.75), qa.null_level(None)), ("neutral", "amber", "neutral"))

    def test_relocated_lesions_are_rigid_shifts_inside_the_mask(self):
        bundle = self.bundle
        weights = self._blob_weights("B")
        total = weights.sum(dtype=np.float64)
        # Brain mask: the half of the connectome mask with i >= 8 (the lesion must move into it)
        ijk = np.stack(np.unravel_index(bundle.mask_indices, bundle.shape), axis=1)
        brain = ijk[:, 0] >= 8
        nulls, shifts = qa.relocated_lesions(weights, bundle, brain_mask=brain, k=25, seed=3, return_shifts=True)
        self.assertEqual(nulls.shape, (25, bundle.n_voxels))
        self.assertEqual(nulls.dtype, np.float32)
        self.assertEqual(shifts.shape, (25, 3))
        self.assertEqual(len({tuple(s) for s in shifts}), 25)
        self.assertFalse(np.any(np.all(shifts == 0, axis=1)))
        lesion_volume = engine.unmask(weights, bundle)
        for null, shift in zip(nulls, shifts, strict=True):
            self.assertGreaterEqual(null[brain].sum(dtype=np.float64), 0.95 * total - 1e-6)
            # Exactly the lesion's weights moved by the shift (parts leaving the connectome mask dropped)
            expected = np.zeros(bundle.shape, np.float32)
            src = np.argwhere(lesion_volume > 0)
            dst = src + shift
            keep = np.all((dst >= 0) & (dst < bundle.shape), axis=1)
            expected[tuple(dst[keep].T)] = lesion_volume[tuple(src[keep].T)]
            np.testing.assert_array_equal(engine.unmask(null, bundle), np.where(bundle.mask, expected, 0))
        # Deterministic for a seed, different for another seed; the bundle or (grid shape, mask indices) work alike
        again = qa.relocated_lesions(weights, bundle.grid, mask_indices=bundle.mask_indices, brain_mask=brain, k=25, seed=3)
        np.testing.assert_array_equal(again, nulls)
        self.assertFalse(np.array_equal(qa.relocated_lesions(weights, bundle, brain_mask=brain, k=25, seed=4), nulls))
        # A lesion that cannot fit inside the brain mask gives fewer (here no) relocations
        everywhere = np.ones(bundle.n_voxels, np.float32)
        self.assertEqual(qa.relocated_lesions(everywhere, bundle, k=5, max_attempts=50).shape, (0, bundle.n_voxels))
        self.assertEqual(qa.relocated_lesions(np.zeros(bundle.n_voxels, np.float32), bundle, k=5).shape, (0, bundle.n_voxels))
        with self.assertRaises(ValueError):
            qa.relocated_lesions(weights[:-1], bundle)

    def test_null_summary(self):
        n_voxels, k = 500, 30
        common = self.rng.standard_normal(n_voxels)
        nulls = common + 0.7 * self.rng.standard_normal((k, n_voxels))
        observed = common + 0.3 * self.rng.standard_normal(n_voxels)
        summary = qa.null_summary(observed, nulls)
        r = np.array([np.corrcoef(observed, null)[0, 1] for null in nulls])
        self.assertEqual(summary["k"], k)
        self.assertAlmostEqual(summary["r_median"], np.median(r), places=10)
        self.assertAlmostEqual(summary["r_p05"], np.percentile(r, 5), places=10)
        self.assertAlmostEqual(summary["r_p95"], np.percentile(r, 95), places=10)
        self.assertAlmostEqual(summary["r_mean_map"], np.corrcoef(observed, nulls.mean(axis=0))[0, 1], places=10)
        loo = np.array([np.corrcoef(nulls[j], np.delete(nulls, j, axis=0).mean(axis=0))[0, 1] for j in range(k)])
        self.assertAlmostEqual(summary["percentile"], 100.0 * np.mean(loo <= summary["r_mean_map"]), places=10)
        self.assertEqual(summary["percentile"], 100.0)  # less noisy than any relocated map
        self.assertGreaterEqual(summary["r_mean_map"], qa.NULL_AMBER)
        self.assertIn("(median of 30; 5\u201395%:", qa.describe_null(summary))
        self.assertIn("100th percentile", qa.describe_null(summary))
        distinct = qa.null_summary(self.rng.standard_normal(n_voxels), nulls)
        self.assertLess(abs(distinct["r_mean_map"]), 0.3)
        self.assertLess(distinct["percentile"], 50.0)
        empty = qa.null_summary(observed, np.zeros((0, n_voxels)))
        self.assertEqual(empty["k"], 0)
        self.assertIsNone(empty["r_median"])
        # The null maps may cover a voxel subset
        voxels = np.arange(0, n_voxels, 3)
        values = qa.lesion_qa(observed, None, nulls[:, voxels], voxels)
        self.assertAlmostEqual(values["null"]["r_mean_map"], np.corrcoef(observed[voxels], nulls[:, voxels].mean(axis=0))[0, 1], places=10)
        self.assertEqual(values["null_level"], "amber")

    def test_null_columns_in_the_same_pass(self):
        weights = self._blob_weights("A")
        nulls = qa.relocated_lesions(weights, self.bundle, k=6, seed=1)
        plain = engine.compute_network_maps(self.bundle, weights)
        voxels = engine.null_voxel_sample(self.bundle.n_voxels, 1000, seed=2)
        self.assertEqual(len(voxels), 1000)
        self.assertIsNone(engine.null_voxel_sample(self.bundle.n_voxels, 0))
        with_nulls = engine.compute_network_maps(self.bundle, weights, null_weights=nulls, null_voxels=voxels)
        np.testing.assert_array_equal(with_nulls.t, plain.t)  # the lesion's own maps do not change
        np.testing.assert_array_equal(with_nulls.null_voxels, voxels)
        reference = engine.compute_network_maps(self.bundle, nulls)
        np.testing.assert_allclose(with_nulls.null_t, reference.t[:, voxels], rtol=1e-4, atol=1e-4)  # float32 accumulation
        everywhere = engine.compute_network_maps(self.bundle, weights, null_weights=nulls)
        self.assertIsNone(everywhere.null_voxels)
        np.testing.assert_allclose(everywhere.null_t, reference.t, rtol=1e-4, atol=1e-4)
        # Sparse relocated lesions (as the CLI uses them) give the same null maps
        sparse = qa.relocated_lesions(weights, self.bundle, k=6, seed=1, sparse=True)
        self.assertEqual(len(sparse), 6)
        for row, (positions, values) in zip(nulls, sparse, strict=True):
            np.testing.assert_array_equal(np.flatnonzero(row), positions)
            np.testing.assert_array_equal(row[positions], values)
        from_sparse = engine.compute_network_maps(self.bundle, weights, null_weights=sparse, null_voxels=voxels)
        np.testing.assert_allclose(from_sparse.null_t, with_nulls.null_t, rtol=1e-5, atol=1e-5)
        np.testing.assert_array_equal(from_sparse.t, plain.t)
        with self.assertRaises(ValueError):
            engine.compute_network_maps(self.bundle, weights, null_weights=nulls, null_voxels=voxels[::-1])


def _sphere_lesion(path, center_world, radius=3.0):
    """A sphere drawn on a 1 mm radiological grid (as Slicer users often have), saved as NIfTI."""
    lesion_affine = np.diag([-1.0, 1.0, 1.0, 1.0])
    lesion_affine[:3, 3] = [25.0, -25.0, -25.0]
    grid = geometry.Grid((51, 51, 51), lesion_affine)
    world = grid.voxel_to_world(np.indices(grid.shape).reshape(3, -1).T)
    lesion = (np.linalg.norm(world - np.asarray(center_world), axis=1) <= radius).reshape(grid.shape)
    nifti.write_nifti(path, lesion.astype(np.uint8), lesion_affine)
    return path


class ExportTest(_TempDirTestCase):
    SIDECAR_KEYS = {"format", "version", "Description", "software", "connectome", "lesion", "N", "df", "subject_ids_sha256", "subset", "threshold", "qa", "maps", "timestamp", "disclaimer"}

    def test_names(self):
        self.assertEqual(export.DISCLAIMER, "Research use only — not for clinical decisions. Lesion network mapping specificity is debated (van den Heuvel et al., 2026, Nat Neurosci, doi:10.1038/s41593-025-02196-7).")
        self.assertEqual(
            os.path.basename(export.map_paths("out", "sub-01", "GSP 1000 (MF)", preview_n=100)["T"]),
            "sub-01_conn-GSP1000MF_desc-TPreviewN100_funcmap.nii.gz",
        )
        self.assertEqual(os.path.basename(export.map_paths("out", "sub-01", "GSP1000")["AvgFz"]), "sub-01_conn-GSP1000_desc-AvgRFz_funcmap.nii.gz")
        self.assertEqual(os.path.basename(export.sidecar_path("out", "sub-01", "GSP1000")), "sub-01_conn-GSP1000_lnm.json")
        self.assertEqual(os.path.basename(export.sidecar_path("out", "sub-01", "GSP1000", 100)), "sub-01_conn-GSP1000_desc-PreviewN100_lnm.json")
        self.assertEqual(export.derivatives_dir("study", "GSP1000", 312), os.path.join("study", "derivatives", "lnm", "conn-GSP1000_n-312"))
        self.assertEqual(os.path.basename(export.group_map_path("out", "GSP1000", "sens90")), "group_conn-GSP1000_desc-sens90_map.nii.gz")
        with self.assertRaises(ValueError):
            export.group_map_path("out", "GSP1000", "sens-90")
        self.assertEqual(engine._lesion_name("/x/sub-07_space-MNI152NLin2009cAsym_desc-lesion_mask.nii.gz"), "sub-07")
        self.assertEqual(engine._lesion_name("sub-07_ses-2_label-lesion_roi.nii"), "sub-07_ses-2")
        self.assertEqual(engine._lesion_name("patient01_lesion.nii.gz"), "patient01_lesion")
        peaks = stats.peak_table(np.array([0.0, 5.0, -7.0]), geometry.Grid((3, 1, 1), RAS_AFFINE), top_k=2, min_distance_mm=0.5)
        export.write_peaks_tsv(self.path("peaks.tsv"), peaks, ["Precuneus"])
        with open(self.path("peaks.tsv"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "x\ty\tz\tvalue\tlabel\n-86.0\t-126.0\t-72.0\t-7\tPrecuneus\n-88.0\t-126.0\t-72.0\t5\tn/a\n")

    def test_write_run_from_memory(self):
        meta = connectome.make_synthetic(self.path("export.lnmc"), n_subjects=3, name="Synthetic Export")
        bundle = connectome.ConnectomeBundle.open(meta["path"])
        weights = np.stack([meta["blobs"]["A"]["mask"], meta["blobs"]["B"]["mask"]]).astype(np.float32)
        maps = engine.compute_network_maps(bundle, weights, subjects=2)
        qa_values = {name: qa.lesion_qa(maps.t[i], None) for i, name in enumerate(("sub-01", "sub-02"))}
        out = self.path("derivatives")
        files = export.write_run(out, maps, ["sub-01", "sub-02"], bundle, rule={"kind": "t_convention"}, qa=qa_values, run_meta=None, preview_n=2, software={"slicer": "5.13.0"})
        names = sorted(os.listdir(out))
        self.assertIn("sub-01_conn-SyntheticExport_desc-TPreviewN2_funcmap.nii.gz", names)
        self.assertIn("sub-02_conn-SyntheticExport_desc-AvgRPreviewN2_funcmap.nii.gz", names)
        self.assertIn("sub-02_conn-SyntheticExport_desc-PreviewN2_lnm.json", names)
        self.assertEqual(len(files["maps"]), 6)
        for name in ("provenance.json", "methods.md", "reproduce.sh"):
            self.assertIn(name, names)
        with open(files["sidecars"][0], encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(set(sidecar), self.SIDECAR_KEYS)
        self.assertEqual((sidecar["N"], sidecar["df"]), (2, 1))
        self.assertEqual(sidecar["subset"]["preview"], True)
        self.assertEqual(sidecar["threshold"]["rule"]["kind"], "t_convention")
        self.assertFalse(sidecar["threshold"]["resolved"]["allowed"])
        self.assertIn("(preview)", sidecar["threshold"]["resolved"]["banner"])
        self.assertEqual(sidecar["software"]["slicer"], "5.13.0")
        self.assertEqual(sidecar["subject_ids_sha256"], export.subjects_sha256(bundle.subject_ids[:2]))
        self.assertEqual(sidecar["connectome"]["manifest_sha256"], export.file_sha256(os.path.join(meta["path"], "connectome.json")))
        self.assertEqual(sidecar["disclaimer"], export.DISCLAIMER)
        t_map, affine, header = nifti.read_nifti(os.path.join(out, sidecar["maps"]["T"]))
        np.testing.assert_array_equal(affine, bundle.affine)
        self.assertEqual((header["intent_code"], header["intent_params"][0]), (3, 1.0))
        np.testing.assert_array_equal(t_map[bundle.mask], maps.t[0])

    def test_export_cli_run_and_reproduce(self):
        meta = connectome.make_synthetic(self.path("run.lnmc"), n_subjects=4, name="Synthetic")
        lesion_path = _sphere_lesion(self.path("sub-01_space-MNI152NLin2009cAsym_desc-lesion_mask.nii.gz"), meta["blobs"]["A"]["center_world"])
        run_dir = self.path("run")
        env = dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))
        args = ["--bundle", os.path.relpath(meta["path"], self.tmp), "--lesion", lesion_path, "--out", run_dir, "--subjects", "3", "--random", "--seed", "5"]
        args += ["--desc-suffix", "PreviewN3", "--null-relocations", "4", "--run-json", os.path.join(run_dir, "run.json"), "--threads", "1"]
        result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.engine", *args], check=False, capture_output=True, text=True, env=env, cwd=self.tmp, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = self.path("derivatives", "lnm", "conn-Synthetic_n-3")
        files = export.export_run(run_dir, out, rule={"kind": "fwe_bonferroni", "alpha": 0.05})
        sidecar_name = "sub-01_conn-Synthetic_desc-PreviewN3_lnm.json"
        self.assertEqual([os.path.basename(p) for p in files["sidecars"]], [sidecar_name])
        for source, target in zip(sorted(glob_maps(run_dir)), sorted(files["maps"]), strict=True):
            self.assertEqual(os.path.basename(source), os.path.basename(target))
            with open(source, "rb") as a, open(target, "rb") as b:
                self.assertEqual(a.read(), b.read())
        with open(os.path.join(out, sidecar_name), encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(set(sidecar), self.SIDECAR_KEYS)
        self.assertEqual(sidecar["N"], 3)
        self.assertEqual(sidecar["subset"], {"preview": True, "partial": False, "n_requested": 3, "random": True, "seed": 5})
        self.assertEqual(sidecar["lesion"]["sha256"], export.file_sha256(lesion_path))
        self.assertGreater(sidecar["lesion"]["voxels_on_connectome_grid"], 0)
        self.assertEqual(set(sidecar["qa"]), {"degree_r", "degree_rho", "degree_level", "null", "null_level"})
        self.assertEqual(sidecar["qa"]["null"]["k"], 4)
        self.assertEqual(sidecar["threshold"]["derived"], thresholds.describe(thresholds.resolve(None, 3, 3752)))
        with open(os.path.join(out, "provenance.json"), encoding="utf-8") as f:
            provenance = json.load(f)
        self.assertEqual(provenance["disclaimer"], export.DISCLAIMER)
        self.assertEqual(provenance["run"]["status"], "done")
        self.assertEqual(provenance["null"]["k"], 4)
        self.assertEqual(provenance["lesions"][0]["sidecar"], sidecar_name)
        with open(os.path.join(out, "methods.md"), encoding="utf-8") as f:
            methods = f.read()
        for text in (export.DISCLAIMER, "doi:10.1093/brain/awv228", "doi:10.1056/NEJMra1706158", "doi:10.1038/s41593-025-02196-7", "N = 3 subjects (preview", "relocated to 4 random positions"):
            self.assertIn(text, methods)
        # reproduce.sh recomputes the same maps
        script = os.path.join(out, "reproduce.sh")
        self.assertTrue(os.stat(script).st_mode & 0o111)
        reproduced = self.path("reproduced")
        result = subprocess.run(["sh", script], check=False, capture_output=True, text=True, env=dict(env, PYTHONSLICER=sys.executable, OUT=reproduced), cwd=self.path(), timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path in glob_maps(run_dir):
            original = nifti.read_nifti(path)[0]
            np.testing.assert_array_equal(nifti.read_nifti(os.path.join(reproduced, os.path.basename(path)))[0], original)
        with self.assertRaises(FileNotFoundError):
            export.export_run(self.path("missing"), out)

        # A stopped run (terminated after a snapshot) exports its latest snapshot as a partial run
        run_json = os.path.join(run_dir, "run.json")
        with open(run_json, encoding="utf-8") as f:
            run = json.load(f)
        run.update({"status": "running", "snapshot_n": 2, "end": None})
        for key in ("n", "cancelled", "qa_json"):
            run.pop(key)
        export.write_json_atomic(run_json, run)
        with self.assertRaisesRegex(ValueError, "did not finish"):
            export.export_run(run_dir, self.path("stopped"))
        connectome.compute_degree(connectome.ConnectomeBundle.open(meta["path"]), n_seeds=50)
        files = export.export_run(run_dir, self.path("stopped"), rule={"kind": "t_convention"}, partial=True)
        with open(files["sidecars"][0], encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual((sidecar["N"], sidecar["df"], sidecar["subset"]["partial"], sidecar["subject_ids_sha256"]), (2, 1, True, None))
        self.assertEqual(sidecar["threshold"]["resolved"]["banner"], "This threshold was defined for N\u22481000; this map has N=2 (partial). Use FWE or effect size.")
        self.assertIsNotNone(sidecar["qa"]["degree_r"])  # computed from the T map file
        with open(files["reproduce"], encoding="utf-8") as f:
            self.assertIn("stopped after 2 subjects", f.read())


def glob_maps(folder):
    return [os.path.join(folder, name) for name in os.listdir(folder) if name.endswith("_funcmap.nii.gz")]


class QACommandLineTest(_TempDirTestCase):
    def _run(self, module, *args):
        env = dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))
        start = time.perf_counter()
        result = subprocess.run([sys.executable, "-m", f"LesionNetworkMappingLib.{module}", *args], check=False, capture_output=True, text=True, env=env, cwd=self.tmp, timeout=300)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return result, events, time.perf_counter() - start

    def test_degree_job_and_null_relocations(self):
        meta = connectome.make_synthetic(self.path("synthetic.lnmc"), n_subjects=5, name="Synthetic Test")
        bundle = connectome.ConnectomeBundle.open(meta["path"])
        lesion_path = _sphere_lesion(self.path("patient01_lesion.nii.gz"), meta["blobs"]["A"]["center_world"])

        result, events, _ = self._run("qa", "degree", "--bundle", meta["path"], "--seeds", "200", "--subjects", "3", "--seed", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([e["event"] for e in events], ["start", "progress", "progress", "progress", "done"])
        np.testing.assert_array_equal(bundle.degree, connectome.compute_degree(bundle, n_seeds=200, subjects=3, seed=1, store=False))
        with open(os.path.join(meta["path"], "derived", "degree.json"), encoding="utf-8") as f:
            degree_json = json.load(f)
        self.assertEqual({key: degree_json[key] for key in ("K", "subjects", "seed", "measure")}, {"K": 200, "subjects": 3, "seed": 1, "measure": "abs_fz"})
        self.assertEqual(qa.read_degree_metadata(meta["path"])["K"], 200)
        result, events, _ = self._run("qa", "degree", "--bundle", self.path("missing.lnmc"))
        self.assertEqual((result.returncode, events[-1]["event"]), (1, "error"))
        result, events, _ = self._run("qa", "degree")
        self.assertEqual((result.returncode, events[-1]["event"]), (2, "error"))

        out_dir = self.path("out")
        run_json = self.path("run.json")
        args = ["--bundle", meta["path"], "--lesion", lesion_path, "--out", out_dir, "--threads", "1", "--null-relocations", "10", "--run-json", run_json, "--snapshot-every", "2"]
        result, events, seconds = self._run("engine", *args, "--threshold-rule", '{"kind": "t_convention"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(seconds, 10.0)
        kinds = [event["event"] for event in events]
        self.assertEqual((kinds[0], kinds[-1]), ("start", "done"))
        self.assertEqual(events[0]["null_relocations"], [10])
        done = events[-1]
        self.assertEqual(set(done["qa"]), {"patient01_lesion"})
        values = done["qa"]["patient01_lesion"]
        self.assertGreater(values["degree_r"], 0.0)
        self.assertEqual(set(values["null"]), {"r_mean_map", "r_median", "r_p05", "r_p95", "percentile", "k"})
        self.assertEqual(values["null"]["k"], 10)
        with open(done["qa_json"], encoding="utf-8") as f:
            qa_json = json.load(f)
        self.assertEqual(qa_json["lesions"], done["qa"])
        self.assertEqual((qa_json["n"], qa_json["null"]["k"], qa_json["degree"]["K"]), (5, 10, 200))
        with open(run_json, encoding="utf-8") as f:
            run = json.load(f)
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["subject_ids_sha256"], export.subjects_sha256(bundle.subject_ids))
        self.assertEqual(run["bundle"]["manifest_sha256"], export.file_sha256(os.path.join(meta["path"], "connectome.json")))
        self.assertEqual(run["threshold_rule"], thresholds.normalize_rule({"kind": "t_convention"}))
        self.assertEqual(set(run["versions"]) >= {"python", "numpy", "scipy"}, True)
        self.assertEqual(run["argv"][-2:], ["--threshold-rule", '{"kind": "t_convention"}'])
        self.assertTrue(run["start"] <= run["end"])
        # The lesion's maps are those of a run without nulls; null maps are not written by default
        t_map = nifti.read_nifti(done["outputs"][2])[0]
        weights, _ = engine.prepare_lesion(lesion_path, bundle)
        np.testing.assert_allclose(t_map[bundle.mask], engine.compute_network_maps(bundle, weights).t[0], rtol=1e-5, atol=1e-5)
        self.assertEqual(sorted(os.listdir(out_dir)), sorted([*map(os.path.basename, done["outputs"]), "qa.json"]))

        # Preview naming, a brain mask on another grid and the 4D null maps
        brain_affine = np.diag([1.0, 1.0, 1.0, 1.0])
        brain_affine[:3, 3] = -30.0
        brain = np.zeros((61, 61, 61), np.uint8)
        brain[:, 25:, :] = 1  # y >= -5 mm
        nifti.write_nifti(self.path("brain.nii.gz"), brain, brain_affine)
        preview_dir = self.path("preview")
        args = ["--bundle", meta["path"], "--lesion", lesion_path, "--out", preview_dir, "--subjects", "3", "--desc-suffix", "PreviewN3", "--null-relocations", "3", "--brain-mask", self.path("brain.nii.gz"), "--write-null"]
        result, events, _ = self._run("engine", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        done = events[-1]
        self.assertEqual([os.path.basename(p) for p in done["outputs"]], [f"patient01_lesion_conn-SyntheticTest_desc-{d}PreviewN3_funcmap.nii.gz" for d in ("AvgR", "AvgRFz", "T")])
        self.assertEqual([os.path.basename(p) for p in done["null_outputs"]], ["patient01_lesion_conn-SyntheticTest_desc-RelocatedTPreviewN3_funcmap.nii.gz"])
        null_maps, affine, header = nifti.read_nifti(done["null_outputs"][0])
        self.assertEqual(null_maps.shape, (*bundle.shape, 3))
        np.testing.assert_array_equal(affine, bundle.affine)
        self.assertEqual(header["intent_code"], 3)
        self.assertEqual(done["qa"]["patient01_lesion"]["null"]["k"], 3)
        # Invalid suffixes are rejected like other invalid arguments
        result, events, _ = self._run("engine", "--bundle", meta["path"], "--lesion", lesion_path, "--out", out_dir, "--desc-suffix", "Preview_N3")
        self.assertEqual((result.returncode, [e["event"] for e in events]), (2, ["error"]))
        result, events, _ = self._run("engine", "--bundle", meta["path"], "--lesion", lesion_path, "--out", out_dir, "--threshold-rule", '{"kind": "fdr"}')
        self.assertEqual((result.returncode, [e["event"] for e in events]), (2, ["error"]))
        self.assertIn("--threshold-rule", events[0]["message"])


if __name__ == "__main__":
    unittest.main()

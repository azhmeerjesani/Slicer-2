"""Tests of SmartFillAlgorithm.py (the Smart fill computation), without the application.

Run with PythonSlicer (numpy and VTK only), for example:
  PythonSlicer -m unittest SmartFillAlgorithmTest.py
The module is loaded from its file, so that the slicer-dependent SegmentEditorEffects package is not imported.
Phantoms are analytic partial-volume balls with seeded noise; the thresholds were validated with the reference
implementation of the algorithm.
"""

import importlib.util
import os
import time
import unittest

import numpy as np
import vtk
from vtk.util import numpy_support

MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "EditorEffects", "Python",
                           "SegmentEditorEffects", "SmartFillAlgorithm.py")
BUNDLED_MASK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "Scripted", "SampleData",
                                 "Resources", "Data", "tpl-MNI152NLin2009cAsym_res-02_desc-brain_mask.nii.gz")


def loadAlgorithm():
    spec = importlib.util.spec_from_file_location("SmartFillAlgorithm", os.path.normpath(MODULE_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sfa = loadAlgorithm()


def ball(shape, center, radius):
    kk, jj, ii = np.ogrid[0:shape[0], 0:shape[1], 0:shape[2]]
    distance = np.sqrt((kk - center[0]) ** 2 + (jj - center[1]) ** 2 + (ii - center[2]) ** 2)
    return np.clip(radius - distance + 0.5, 0, 1).astype(np.float32)


def place(shape, offset, region):
    output = np.zeros(shape, bool)
    output[offset[0]:offset[0] + region.shape[0], offset[1]:offset[1] + region.shape[1], offset[2]:offset[2] + region.shape[2]] = region
    return output


def dice(a, b):
    return 2.0 * np.count_nonzero(a & b) / max(1, np.count_nonzero(a) + np.count_nonzero(b))


def fullFilter(mask, radiusKji, openValue=None, closeValue=None):
    """Reference: the morphological filter run on the whole array at once (no streaming)."""
    image = sfa.wrapArray(mask.astype(np.uint8))
    if openValue is None:
        imageFilter = vtk.vtkImageDilateErode3D()
        imageFilter.SetDilateValue(1)
        imageFilter.SetErodeValue(0)
    else:
        imageFilter = vtk.vtkImageOpenClose3D()
        imageFilter.SetOpenValue(openValue)
        imageFilter.SetCloseValue(closeValue)
    imageFilter.SetInputData(image)
    imageFilter.SetKernelSize(2 * radiusKji[2] + 1, 2 * radiusKji[1] + 1, 2 * radiusKji[0] + 1)
    imageFilter.Update()
    return numpy_support.vtk_to_numpy(imageFilter.GetOutput().GetPointData().GetScalars()).reshape(mask.shape) != 0


class SmartFillAlgorithmTest(unittest.TestCase):
    shape = (96, 96, 96)

    def setUp(self):
        self.rng = np.random.default_rng(0)

    def noise(self, sigma, shape=None):
        return self.rng.normal(0, sigma, shape or self.shape).astype(np.float32)

    def test_a_bright_ball(self):
        fraction = ball(self.shape, (48, 48, 48), 10)
        image = 100 * (1 - fraction) + 160 * fraction + self.noise(4)
        offset, region, info = sfa.smartFill(image, (48, 48, 48), (1, 1, 1), 200.0)
        score = dice(place(self.shape, offset, region), fraction > 0.5)
        print(f"(a) bright ball Dice {score:.3f}")
        self.assertGreaterEqual(score, 0.95)
        self.assertFalse(info["limitReached"])

    def test_b_leak_guard_cuts_bridges(self):
        for width in (1, 2):
            fractionLesion = ball(self.shape, (48, 48, 38), 9)
            fractionVentricle = ball(self.shape, (48, 48, 66), 9)
            image = np.full(self.shape, 100.0, np.float32)
            image = image * (1 - fractionLesion) + 45 * fractionLesion
            image = image * (1 - fractionVentricle) + 30 * fractionVentricle
            image[48:48 + width, 48:48 + width, 46:60] = 38  # thin bridge between the lesion and the "ventricle"
            image += self.noise(3)
            scores = {}
            for leakGuardMm in (1.5, 0.0):
                offset, region, _info = sfa.smartFill(image, (48, 48, 38), (1, 1, 1), 200.0, leakGuardMm=leakGuardMm)
                scores[leakGuardMm] = dice(place(self.shape, offset, region), fractionLesion > 0.5)
            print(f"(b) bridge {width} voxel(s): Dice with leak guard {scores[1.5]:.3f}, without {scores[0.0]:.3f}")
            self.assertGreaterEqual(scores[1.5], 0.95)
            self.assertLessEqual(scores[0.0], 0.70)

    def test_c_lesion_larger_than_max_radius(self):
        fraction = ball(self.shape, (48, 48, 48), 30)
        image = 100 * (1 - fraction) + 160 * fraction + self.noise(4)
        _offset, _region, info = sfa.smartFill(image, (48, 48, 48), (1, 1, 1), 200.0, maxRadiusMm=20)
        self.assertTrue(info["limitReached"] or info.get("noEdge"))

    def test_d_ct_wide_window(self):
        fraction = ball(self.shape, (48, 48, 48), 8)
        image = 35 * (1 - fraction) + 65 * fraction
        image[:, :, :10] = 1000  # bone
        image[:, :, -6:] = -1000  # air
        image = image + self.noise(4)
        offset, region, info = sfa.smartFill(image, (48, 48, 48), (1, 1, 1), 3000.0)
        score = dice(place(self.shape, offset, region), fraction > 0.5)
        print(f"(d) CT-like phantom with a 3000 HU window: Dice {score:.3f}, scale {info['grower'].scale:.1f}")
        self.assertGreaterEqual(score, 0.93)

    def test_e_fill_holes(self):
        fraction = ball(self.shape, (48, 48, 48), 10)
        core = ball(self.shape, (48, 48, 48), 4)
        image = 100 * (1 - fraction) + 160 * fraction
        image = image * (1 - core) + 90 * core
        image += self.noise(3)
        filled = {}
        for fillAndSmooth in (True, False):
            offset, region, _info = sfa.smartFill(image, (48, 48, 55), (1, 1, 1), 200.0, fillAndSmooth=fillAndSmooth)
            filled[fillAndSmooth] = bool(place(self.shape, offset, region)[48, 48, 48])
        self.assertTrue(filled[True])
        self.assertFalse(filled[False])

    def test_f_2d_mode(self):
        fraction = ball(self.shape, (48, 48, 48), 10)
        image = 100 * (1 - fraction) + 160 * fraction + self.noise(4)
        for axis in (0, 1, 2):
            offset, region, _info = sfa.smartFill(image, (48, 48, 48), (1, 1, 1), 200.0, mode="2d", sliceAxis=axis)
            result = place(self.shape, offset, region)
            self.assertEqual(sorted(set(np.argwhere(result)[:, axis].tolist())), [48])
            truthSlice = np.take(fraction > 0.5, 48, axis=axis)
            score = dice(np.take(result, 48, axis=axis), truthSlice)
            self.assertGreaterEqual(score, 0.95)

    def test_g_allowed_mask(self):
        fraction = ball(self.shape, (48, 48, 48), 10)
        image = 100 * (1 - fraction) + 160 * fraction + self.noise(4)
        allowed = np.ones(self.shape, bool)
        allowed[:, :, 52:] = False
        offset, region, info = sfa.smartFill(image, (48, 48, 48), (1, 1, 1), 200.0, allowed=allowed)
        result = place(self.shape, offset, region)
        self.assertGreater(np.count_nonzero(result), 0)
        self.assertEqual(np.count_nonzero(result & ~allowed), 0)
        offset, region, info = sfa.smartFill(image, (48, 48, 60), (1, 1, 1), 200.0, allowed=allowed)
        self.assertTrue(info.get("outsideAllowed"))
        self.assertEqual(np.count_nonzero(region), 0)

    def test_h_intensity_range(self):
        fraction = ball(self.shape, (48, 48, 48), 10)
        image = 100 * (1 - fraction) + 160 * fraction + self.noise(4)
        intensityRange = (150.0, 1000.0)
        offset, region, _info = sfa.smartFill(image, (48, 48, 48), (1, 1, 1), 200.0, intensityRange=intensityRange)
        result = place(self.shape, offset, region)
        self.assertGreater(np.count_nonzero(result), 0)
        # region growing respects the range; only the final closing / hole filling may add voxels inside the lesion
        grower = sfa.RegionGrower(image, (48, 48, 48), (1, 1, 1), 200.0, sliceAxis=None, intensityRange=intensityRange)
        _tolerance, raw, _info = grower.autoTolerance()
        values = grower.sub[raw]
        self.assertTrue(np.all((values >= intensityRange[0]) & (values <= intensityRange[1])))
        outside = result & ((image < intensityRange[0]) | (image > intensityRange[1]))
        self.assertTrue(np.all((fraction > 0.5)[outside]), "voxels outside the range only inside the lesion (hole filling)")
        grower = sfa.RegionGrower(image, (48, 48, 48), (1, 1, 1), 200.0, intensityRange=(500.0, 1000.0))
        self.assertTrue(grower.outsideAllowed)

    def test_i_compose_parts(self):
        offset, labels = sfa.composeParts([(sfa.ADD, (10, 10, 10), np.ones((4, 4, 4), bool)),
                                           (sfa.REMOVE, (12, 12, 12), np.ones((4, 4, 4), bool))])
        self.assertEqual(list(map(int, offset)), [10, 10, 10])
        self.assertEqual(labels.shape, (6, 6, 6))
        self.assertEqual(int(np.count_nonzero(labels == sfa.ADD)), 56)
        self.assertEqual(int(np.count_nonzero(labels == sfa.REMOVE)), 64)
        self.assertEqual(sfa.composeParts([]), (None, None))

    def test_j_template_grid_and_mask_reader(self):
        path = os.path.normpath(BUNDLED_MASK_PATH)
        if not os.path.exists(path):
            self.skipTest(f"bundled mask not found: {path}")
        mask, affine = sfa.readNifti1(path)
        self.assertEqual(mask.shape, (97, 115, 97))
        self.assertEqual(int(np.count_nonzero(mask > 0)), 235840)
        reader = vtk.vtkNIFTIImageReader()
        reader.SetFileName(path)
        reader.Update()
        dims = reader.GetOutput().GetDimensions()
        vtkArray = numpy_support.vtk_to_numpy(reader.GetOutput().GetPointData().GetScalars()).reshape(dims[::-1])
        # the file stores uint8 with scl_slope 1/255: readNifti1 applies the scaling, vtkNIFTIImageReader reports it
        self.assertTrue(np.array_equal(vtkArray != 0, mask != 0))
        self.assertTrue(np.allclose(vtkArray * reader.GetRescaleSlope() + reader.GetRescaleIntercept(), mask))

        dimsIjk = (97, 115, 97)
        self.assertEqual(sfa.templateGridOf(dimsIjk, affine), ("MNI152NLin2009cAsym", 2))
        # the same grid stored LPS (i and j flipped) is recognized too
        lps = affine.copy()
        lps[:3, 0] *= -1
        lps[:3, 1] *= -1
        lps[0, 3] = affine[0, 3] + affine[0, 0] * (dimsIjk[0] - 1)
        lps[1, 3] = affine[1, 3] + affine[1, 1] * (dimsIjk[1] - 1)
        self.assertEqual(sfa.templateGridOf(dimsIjk, lps), ("MNI152NLin2009cAsym", 2))
        # a shift beyond the 1 mm tolerance is rejected
        for shiftMm in (1.5, 2.0):
            shifted = affine.copy()
            shifted[0, 3] += shiftMm
            self.assertIsNone(sfa.templateGridOf(dimsIjk, shifted))
        self.assertIsNone(sfa.templateGridOf((96, 115, 97), affine))
        # a vtkMatrix4x4 works too
        matrix = vtk.vtkMatrix4x4()
        for r in range(4):
            for c in range(4):
                matrix.SetElement(r, c, affine[r, c])
        self.assertEqual(sfa.templateGridOf(dimsIjk, matrix), ("MNI152NLin2009cAsym", 2))

        mapped = sfa.maskToReference(mask, affine, dimsIjk, affine)
        self.assertTrue(np.array_equal(mapped, mask != 0))
        # the LPS-stored grid gets the mask flipped along i and j
        mappedLps = sfa.maskToReference(mask, affine, dimsIjk, lps)
        self.assertTrue(np.array_equal(mappedLps, (mask != 0)[:, ::-1, ::-1]))
        shifted = affine.copy()
        shifted[0, 3] += 1.0
        self.assertIsNone(sfa.maskToReference(mask, affine, dimsIjk, shifted))

    def test_k_timing_256(self):
        shape = (256, 256, 256)
        fraction = ball(shape, (128, 128, 128), 15)
        image = 100 * (1 - fraction) + 160 * fraction + self.noise(4, shape)
        sfa.smartFill(image, (128, 128, 128), (1, 1, 1), 200.0)  # first call initializes VTK and numpy code paths
        times = []
        for repeat in range(5):
            start = time.perf_counter()
            sfa.smartFill(image, (128 + repeat, 128, 128), (1, 1, 1), 200.0)
            times.append(1000 * (time.perf_counter() - start))
        median = float(np.median(times))
        print(f"(k) smartFill on 256^3, r=15 mm: {[round(t) for t in times]} ms, median {median:.0f} ms (target 150 ms)")
        self.assertLessEqual(median, 300.0)

    def test_l_generator_parity_and_steps(self):
        fraction = ball(self.shape, (48, 48, 48), 10)
        image = 100 * (1 - fraction) + 160 * fraction + self.noise(4)
        expectedOffset, expectedRegion, expectedInfo = sfa.smartFill(image, (48, 48, 49), (1, 1, 1), 200.0, maxRadiusMm=40.0)
        generator = sfa.RegionGrower.iterCreate(image, (48, 48, 49), (1, 1, 1), 200.0, maxRadiusMm=40.0)
        grower = sfa.runToCompletion(generator)
        tolerance, region, _info = sfa.runToCompletion(grower.iterAutoTolerance())
        region = sfa.runToCompletion(sfa.iterFinalize(grower, region, 1.5, True))
        self.assertTrue(np.array_equal(grower.offsetKji, expectedOffset))
        self.assertTrue(np.array_equal(region, expectedRegion))
        self.assertAlmostEqual(tolerance, expectedInfo["tolerance"])

        shape = (256, 256, 256)
        for radius in (30, 45):
            mask = ball(shape, (128, 128, 128), radius) > 0.5
            slices = sfa.boundingBoxSlices(mask, pad=4)
            sub = np.ascontiguousarray(mask[slices])
            for radiusKji in ((2, 2, 2), (3, 1, 2)):
                self.assertTrue(np.array_equal(sfa.morph(sub, radiusKji, 1, 0), fullFilter(sub, radiusKji, 1, 0)))
                self.assertTrue(np.array_equal(sfa.morph(sub, radiusKji, 0, 1), fullFilter(sub, radiusKji, 0, 1)))
                self.assertTrue(np.array_equal(sfa.dilate(sub, radiusKji), fullFilter(sub, radiusKji)))

        # row-decomposed kernels (and the per-offset fallback) on random masks, including 2D kernels and several slabs
        noiseMask = np.random.default_rng(1).random((40, 37, 35)) > 0.7
        for radiusKji in ((1, 1, 1), (2, 2, 0), (0, 2, 2), (2, 0, 2), (1, 2, 3), (3, 3, 3), (0, 0, 1)):
            key = tuple(radiusKji)
            for useRows in (True, False):
                rows = sfa.kernelRows(key)
                if not useRows:
                    sfa._kernelRowsCache[key] = None
                try:
                    self.assertTrue(np.array_equal(sfa.morph(noiseMask, radiusKji, 1, 0), fullFilter(noiseMask, radiusKji, 1, 0)))
                    self.assertTrue(np.array_equal(sfa.morph(noiseMask, radiusKji, 0, 1), fullFilter(noiseMask, radiusKji, 0, 1)))
                    self.assertTrue(np.array_equal(sfa.dilate(noiseMask, radiusKji), fullFilter(noiseMask, radiusKji)))
                finally:
                    sfa._kernelRowsCache[key] = rows

        # largest generator step for an escalated r=45 lesion on 256^3
        fraction = ball(shape, (128, 128, 128), 45)
        image = 100 * (1 - fraction) + 160 * fraction + self.noise(4, shape)
        sfa.smartFill(np.zeros((24, 24, 24), np.float32) + self.noise(1, (24, 24, 24)), (12, 12, 12), (1, 1, 1), 10.0)
        generator = sfa.iterSmartFill(image, (128, 128, 143), (1, 1, 1), 200.0)
        steps = []
        while True:
            start = time.perf_counter()
            try:
                next(generator)
            except StopIteration as stop:
                result = stop.value
                steps.append(1000 * (time.perf_counter() - start))
                break
            steps.append(1000 * (time.perf_counter() - start))
        print(f"(l) escalated r=45 on 256^3: {len(steps)} steps, total {sum(steps):.0f} ms, largest step {max(steps):.1f} ms"
              f" (escalated: {result[2].get('escalated')})")
        self.assertTrue(result[2].get("escalated"))
        self.assertLessEqual(max(steps), 60.0)
        score = dice(place(shape, result[0], result[1]), fraction > 0.5)
        self.assertGreaterEqual(score, 0.95)

        # noise realizations where a below-noise tolerance percolates through the lesion to the first-pass radius
        # limit (only noise-level edges before it): the radius must still be extended
        for seed in (5, 10):
            image = 100 * (1 - fraction) + 160 * fraction + np.random.default_rng(seed).normal(0, 4, shape).astype(np.float32)
            offset, region, info = sfa.smartFill(image, (128, 128, 143), (1, 1, 1), 200.0)
            self.assertTrue(info.get("escalated"), f"seed {seed}")
            self.assertGreaterEqual(dice(place(shape, offset, region), fraction > 0.5), 0.95, f"seed {seed}")

    def test_m_stepped_percentiles(self):
        """The intensity scale of large boxes uses stepped percentiles: identical to numpy.percentile."""
        for kind in range(4):
            count = 1_500_000 + 250_000 * kind
            if kind == 0:
                values = self.rng.normal(100, 4, count).astype(np.float32)
            elif kind == 1:
                values = self.rng.integers(-1000, 3000, count).astype(np.float32)
            elif kind == 2:
                values = self.rng.normal(100, 4, count).astype(np.float32)
                values[:count // 2] = 0  # zero background
            else:
                values = np.round(self.rng.normal(0, 1, count), 1).astype(np.float32)  # many ties
            steps = []
            generator = sfa.iterPercentiles(values.reshape(-1, 50, 50) if count % 2500 == 0 else values, (5.0, 95.0))
            while True:
                start = time.perf_counter()
                try:
                    next(generator)
                except StopIteration as stop:
                    result = stop.value
                    break
                finally:
                    steps.append(1000 * (time.perf_counter() - start))
            np.testing.assert_allclose(result, np.percentile(values, [5, 95]), rtol=1e-12, atol=0, err_msg=f"case {kind}")
            print(f"(m) case {kind}: {count} values, steps {[round(t, 1) for t in steps]} ms")


if __name__ == "__main__":
    unittest.main()

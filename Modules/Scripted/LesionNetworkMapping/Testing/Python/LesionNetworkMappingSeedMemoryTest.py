"""Map's seed check of a large lesion stays within memory (bug bash BB-1, map-seed-check-large-lesion-oom-kill).

A 572 MB lesion (830^3 voxels) that the drop guard accepted got Slicer Studio killed for lack of memory as soon as Map
opened: ``LesionNetworkMappingLogic.checkSeed`` (on the GUI thread) exported the segment to a 16-bit labelmap, then
``seedArray`` made a bool and a uint8 copy of it and ``checkSeed`` a clipped copy for the volume. Now ``seedArray`` reads
the segment's own labelmap into one uint8 mask (exporting only when the segment is not on the reference grid), the check
itself borrows the segment's voxels without any copy (touching 572 MB of new memory alone froze the window for 2 s), the
volume of a binary mask is its voxel count, and a seed that needs more memory than is left is refused (kind "toolarge")
before anything is read.

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingSeedMemoryTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import tracemalloc
import unittest
from unittest import mock

try:
    import slicer
    import vtk
    import vtk.util.numpy_support
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

SHAPE = (200, 200, 200)  # [k, j, i]: 8 MB of uint8 voxels


class LesionNetworkMappingSeedMemoryTest(unittest.TestCase):
    def setUp(self):
        slicer.mrmlScene.Clear(0)

    def tearDown(self):
        slicer.mrmlScene.Clear(0)

    def _lesion(self, label=1, full=False):
        """A lesion of 160 x 140 x 120 voxels; ``full``: plus the two corner voxels of the grid, so that the segment's own
        labelmap covers the whole reference grid (as a lesion file opened in Slicer Studio does).
        """
        import numpy as np

        labelmap = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "big_lesion")
        labelmap.SetSpacing(1.0, 1.0, 1.0)
        array = np.zeros(SHAPE, np.uint8)
        array[20:180, 30:170, 40:160] = label  # 160 x 140 x 120 voxels
        if full:
            array[0, 0, 0] = array[-1, -1, -1] = label
        slicer.util.updateVolumeFromArray(labelmap, array)
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "big_lesion")
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(labelmap)
        self.assertTrue(slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(labelmap, segmentation))
        slicer.mrmlScene.RemoveNode(labelmap)
        return segmentation, segmentation.GetSegmentation().GetNthSegmentID(0)

    def test_seed_array_is_binary_uint8(self):
        import numpy as np

        from LesionNetworkMapping import LesionNetworkMappingLogic

        segmentation, segmentId = self._lesion(label=7)
        data, affine = LesionNetworkMappingLogic.seedArray(segmentation, segmentId)
        self.assertEqual(data.dtype, np.uint8)
        self.assertEqual(data.shape, SHAPE[::-1])
        self.assertEqual(set(np.unique(data).tolist()), {0, 1})
        self.assertEqual(int(np.count_nonzero(data)), 160 * 140 * 120)
        self.assertEqual(int(data[50, 40, 30]), 1)  # [i, j, k]
        self.assertEqual(int(data[30, 40, 50]), 0)
        self.assertEqual(affine.shape, (4, 4))

    def test_direct_read_matches_export(self):
        """The segment read from its own labelmap equals the exported labelmap (two segments, oblique grid, offset extent)."""
        import numpy as np

        from LesionNetworkMapping import LesionNetworkMappingLogic

        labelmap = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "two_lesions")
        labelmap.SetSpacing(1.5, 2.0, 2.5)
        labelmap.SetOrigin(-30.0, 12.0, 7.5)
        directions = vtk.vtkMatrix4x4()
        transform = vtk.vtkTransform()
        transform.RotateZ(20)
        transform.RotateX(-10)
        transform.GetMatrix(directions)
        labelmap.SetIJKToRASDirectionMatrix(directions)
        array = np.zeros((30, 40, 50), np.uint8)
        array[5:12, 10:20, 15:30] = 1
        array[15:25, 22:35, 3:9] = 2
        slicer.util.updateVolumeFromArray(labelmap, array)
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "two_lesions")
        # A reference geometry whose extent does not start at 0 (a cropped grid)
        geometry = slicer.vtkOrientedImageData()
        geometry.SetExtent(-4, 52, 2, 41, -3, 30)
        matrix = vtk.vtkMatrix4x4()
        labelmap.GetIJKToRASMatrix(matrix)
        geometry.SetImageToWorldMatrix(matrix)
        segmentation.GetSegmentation().SetConversionParameter(slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName(),
                                                              slicer.vtkSegmentationConverter.SerializeImageGeometry(geometry))
        self.assertTrue(slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(labelmap, segmentation))
        slicer.mrmlScene.RemoveNode(labelmap)
        segmentation.GetSegmentation().AddEmptySegment("", "empty")
        self.assertEqual(segmentation.GetSegmentation().GetNumberOfSegments(), 3)
        for index in range(3):
            segmentId = segmentation.GetSegmentation().GetNthSegmentID(index)
            direct = LesionNetworkMappingLogic._seedArrayFromLabelmap(segmentation, segmentId)
            self.assertIsNotNone(direct)
            with mock.patch.object(LesionNetworkMappingLogic, "_seedArrayFromLabelmap", return_value=None):
                exported = LesionNetworkMappingLogic.seedArray(segmentation, segmentId)
            self.assertEqual(direct[0].shape, exported[0].shape, segmentId)
            self.assertEqual(direct[0].dtype, exported[0].dtype)
            np.testing.assert_array_equal(direct[0], exported[0], err_msg=segmentId)
            np.testing.assert_allclose(direct[1], exported[1], atol=1e-6, err_msg=segmentId)
        self.assertEqual(int(np.count_nonzero(LesionNetworkMappingLogic.seedArray(segmentation, segmentation.GetSegmentation().GetNthSegmentID(1))[0])), 10 * 13 * 6)

    def test_check_makes_no_full_size_copies(self):
        """Before the fix the check allocated three full-size numpy copies (bool, uint8, clipped) of the lesion."""
        from LesionNetworkMapping import LesionNetworkMappingLogic
        from StudioLib import importer

        segmentation, segmentId = self._lesion(full=True)
        voxels = SHAPE[0] * SHAPE[1] * SHAPE[2]
        logic = LesionNetworkMappingLogic()
        with mock.patch.object(importer, "_availableMemoryBytes", return_value=None):
            logic.checkSeed(segmentation, segmentId)  # imports and caches of a first check are not the lesion's copies
            tracemalloc.start()
            try:
                check = logic.checkSeed(segmentation, segmentId)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertTrue(check["ok"], check)
        self.assertAlmostEqual(check["volumeMl"], (160 * 140 * 120 + 2) / 1000.0, places=3)
        # No copy at all: the check reads the segment's own voxels (before: a 16-bit export, then bool, uint8 and clipped
        # copies; one uint8 mask would still be new memory to touch on the GUI thread)
        self.assertLess(peak, voxels // 4, f"checkSeed allocated {peak:,} bytes of numpy copies for a {voxels:,}-voxel lesion")

    def test_borrowed_seed_is_a_read_only_view_only_when_it_is_the_mask(self):
        """``borrow`` gives the segment's own voxels (read-only) for a 0/1 uint8 labelmap on the reference grid, else a copy."""
        import numpy as np

        from LesionNetworkMapping import LesionNetworkMappingLogic

        segmentation, segmentId = self._lesion(full=True)
        copied, affine = LesionNetworkMappingLogic.seedArray(segmentation, segmentId)
        self.assertTrue(copied.flags.writeable)  # by default the caller's own copy, as before
        borrowed, borrowedAffine = LesionNetworkMappingLogic.seedArray(segmentation, segmentId, borrow=True)
        self.assertFalse(borrowed.flags.writeable)
        self.assertEqual(borrowed.dtype, np.uint8)
        np.testing.assert_array_equal(borrowed, copied)
        np.testing.assert_allclose(borrowedAffine, affine)
        image = segmentation.GetSegmentation().GetSegment(segmentId).GetRepresentation(slicer.vtkSegmentationConverter.GetBinaryLabelmapRepresentationName())
        own = vtk.util.numpy_support.vtk_to_numpy(image.GetPointData().GetScalars())
        self.assertTrue(np.shares_memory(borrowed, own))
        self.assertFalse(np.shares_memory(copied, own))
        # Another label value than 1 is not a 0/1 mask: binarized into a copy, the segment's voxels stay as they are
        segmentation, segmentId = self._lesion(label=7, full=True)
        image = segmentation.GetSegmentation().GetSegment(segmentId).GetRepresentation(slicer.vtkSegmentationConverter.GetBinaryLabelmapRepresentationName())
        own = vtk.util.numpy_support.vtk_to_numpy(image.GetPointData().GetScalars())
        own[own != 0] = 7
        segmentation.GetSegmentation().GetSegment(segmentId).SetLabelValue(7)
        data, _affine = LesionNetworkMappingLogic.seedArray(segmentation, segmentId, borrow=True)
        self.assertTrue(data.flags.writeable)
        self.assertFalse(np.shares_memory(data, own))
        self.assertEqual(set(np.unique(data).tolist()), {0, 1})
        self.assertEqual(int(own.max()), 7)
        self.assertEqual(LesionNetworkMappingLogic().checkSeed(segmentation, segmentId)["volumeMl"], (160 * 140 * 120 + 2) / 1000.0)
        # A labelmap smaller than the reference grid is padded into a copy
        segmentation, segmentId = self._lesion()
        data, _affine = LesionNetworkMappingLogic.seedArray(segmentation, segmentId, borrow=True)
        self.assertTrue(data.flags.writeable)
        self.assertEqual(int(np.count_nonzero(data)), 160 * 140 * 120)

    def test_seed_larger_than_free_memory_is_refused(self):
        from LesionNetworkMapping import LesionNetworkMappingLogic
        from StudioLib import importer

        segmentation, segmentId = self._lesion()
        logic = LesionNetworkMappingLogic()
        with mock.patch.object(importer, "_availableMemoryBytes", return_value=10 * 1024 * 1024), \
                mock.patch.object(LesionNetworkMappingLogic, "analysisSeedArray", side_effect=AssertionError("exported")):
            check = logic.checkSeed(segmentation, segmentId)
        self.assertFalse(check["ok"])
        self.assertEqual(check["kind"], "toolarge")
        self.assertIn("too large to map", check["reason"])
        # With enough memory the same lesion is checked as usual
        with mock.patch.object(importer, "_availableMemoryBytes", return_value=64 * 1024 * 1024 * 1024):
            self.assertTrue(logic.checkSeed(segmentation, segmentId)["ok"])

    def test_connectome_step_is_refused_when_memory_is_short(self):
        """Resampling onto a connectome holds the lesion as float64: it is asked for only once the lesion is on its grid."""
        import types

        import numpy as np

        from LesionNetworkMapping import LesionNetworkMappingLogic
        from StudioLib import importer

        segmentation, segmentId = self._lesion()
        voxels = SHAPE[0] * SHAPE[1] * SHAPE[2]
        bundle = types.SimpleNamespace(shape=SHAPE, affine=np.eye(4), metadata={})
        logic = LesionNetworkMappingLogic()
        with mock.patch.object(importer, "_availableMemoryBytes", return_value=voxels * 5):
            self.assertTrue(logic.checkSeed(segmentation, segmentId)["ok"])  # without a connectome it fits
            check = logic.checkSeed(segmentation, segmentId, bundle)
        self.assertFalse(check["ok"])
        self.assertEqual(check["kind"], "toolarge")
        self.assertAlmostEqual(check["volumeMl"], 160 * 140 * 120 / 1000.0, places=3)


if __name__ == "__main__":
    unittest.main()

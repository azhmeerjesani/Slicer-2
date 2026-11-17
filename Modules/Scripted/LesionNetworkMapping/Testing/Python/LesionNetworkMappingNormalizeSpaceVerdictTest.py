"""The Normalize to MNI card and Map's Seed check give one verdict on a lesion's space (bug bash BB-1).

A lesion file on the MNI152NLin2009cAsym 2 mm grid whose origin is a fraction of a millimetre off the built-in grid
(TemplateFlow's files use -96, -132, -78) was mapped as MNI by the Seed check (``LesionNetworkMappingLogic.seedSpace``,
1 mm origin tolerance) while ``normalize_controller.isNativeSegmentation`` (``StudioLib.space.spaceOf``, 0.01 mm) called
it native, so the Normalize card offered to warp an MNI lesion a second time. Both now ask ``seedSpace``.

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingNormalizeSpaceVerdictTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

#: The built-in MNI152NLin2009cAsym 2 mm grid: lowest voxel centre -96.5, -132.5, -78.5 (RAS)
SHAPE = (97, 115, 97)
ORIGIN = (-96.5, -132.5, -78.5)


class LesionNetworkMappingNormalizeSpaceVerdictTest(unittest.TestCase):
    def setUp(self):
        slicer.mrmlScene.Clear(0)

    def tearDown(self):
        slicer.mrmlScene.Clear(0)

    def _lesion(self, offsetMm):
        """A segmentation with one small segment on the 2009c 2 mm grid shifted by ``offsetMm`` along R, A and S."""
        import numpy as np

        labelmap = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", f"lesion_off{offsetMm}")
        labelmap.SetSpacing(2.0, 2.0, 2.0)
        labelmap.SetOrigin(*(value + offsetMm for value in ORIGIN))
        array = np.zeros(SHAPE[::-1], np.uint8)
        array[40:44, 60:64, 45:49] = 1
        slicer.util.updateVolumeFromArray(labelmap, array)
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", f"lesion_off{offsetMm}")
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(labelmap)  # as a lesion file opened alone
        self.assertTrue(slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(labelmap, segmentation))
        slicer.mrmlScene.RemoveNode(labelmap)
        return segmentation, segmentation.GetSegmentation().GetNthSegmentID(0)

    def _verdicts(self, offsetMm):
        from LesionNetworkMapping import LesionNetworkMappingLogic
        from LesionNetworkMappingUI import normalize_controller as nc

        segmentation, segmentId = self._lesion(offsetMm)
        seedSpace = LesionNetworkMappingLogic().checkSeed(segmentation, segmentId)["space"]
        return seedSpace, nc.isNativeSegmentation(segmentation), nc.canNormalize(segmentation, segmentId)

    def test_exact_grid_is_mni_for_both(self):
        seedSpace, native, _answer = self._verdicts(0.0)
        self.assertEqual(tuple(seedSpace), ("MNI152NLin2009cAsym", 2))
        self.assertFalse(native)

    def test_slightly_off_grid_is_mni_for_both(self):
        for offset in (0.5, 0.9):
            with self.subTest(offset=offset):
                seedSpace, native, answer = self._verdicts(offset)
                self.assertEqual(tuple(seedSpace), ("MNI152NLin2009cAsym", 2))
                self.assertFalse(native, "the Seed check maps this lesion as MNI: the Normalize card must not call it native")
                self.assertFalse(answer[0])

    def test_far_off_grid_is_native_for_both(self):
        seedSpace, native, _answer = self._verdicts(1.5)
        self.assertIsNone(seedSpace)
        self.assertTrue(native)

    def test_native_attribute_wins(self):
        from LesionNetworkMappingUI import normalize_controller as nc

        segmentation, _segmentId = self._lesion(0.0)
        segmentation.SetAttribute("Studio.Space", "native")
        self.assertTrue(nc.isNativeSegmentation(segmentation))


if __name__ == "__main__":
    unittest.main()

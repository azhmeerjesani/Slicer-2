"""A lesion mapped outside a study keeps its BIDS subject in the run and export names (SPEC 7.11: ``<sub>_conn-<label>_...``).

``_safeName`` drops underscores, so a node named ``sub-01_space-MNI152NLin2009cAsym_desc-lesion_mask`` gave
``sub-01space-MNI152NLin2009cAsymdesc-lesionmask-Lesion`` and BIDS tools read a wrong subject. It needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingLesionNameTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class LesionNetworkMappingLesionNameTest(unittest.TestCase):
    def setUp(self):
        slicer.mrmlScene.Clear(0)

    def tearDown(self):
        slicer.mrmlScene.Clear(0)

    def _seg(self, nodeName, segmentName="Lesion"):
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", nodeName)
        node.GetSegmentation().AddEmptySegment(segmentName, segmentName)
        return node

    def _name(self, node, segmentName):
        from LesionNetworkMapping import _lesionFallbackLabel, _safeName

        return _safeName(_lesionFallbackLabel(node, segmentName))

    def test_bids_named_lesion_keeps_its_subject(self):
        node = self._seg("sub-01_space-MNI152NLin2009cAsym_desc-lesion_mask")
        self.assertEqual(self._name(node, "Lesion"), "sub-01")

    def test_subject_of_a_loaded_set_is_not_doubled(self):
        node = self._seg("sub-01 lesion", "sub-01 lesion")
        self.assertEqual(self._name(node, "sub-01 lesion"), "sub-01")

    def test_subject_is_read_from_the_file_name(self):
        node = self._seg("Lesion image")
        storage = node.CreateDefaultStorageNode()
        slicer.mrmlScene.AddNode(storage)
        node.SetAndObserveStorageNodeID(storage.GetID())
        storage.SetFileName("/data/sub-07_space-MNI_desc-lesion_mask.seg.nrrd")
        self.assertEqual(self._name(node, "Lesion"), "sub-07")

    def test_name_without_a_subject_is_unchanged(self):
        self.assertEqual(self._name(self._seg("lesion"), "Lesion"), "lesion-Lesion")
        self.assertEqual(self._name(self._seg("Stroke A", "Stroke A"), "Stroke A"), "StrokeA")


if __name__ == "__main__":
    unittest.main()

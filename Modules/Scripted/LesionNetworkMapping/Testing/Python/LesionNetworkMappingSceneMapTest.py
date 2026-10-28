"""A map restored from the scene (a reopened project) shows its peak table when Map is entered.

The Result card of a scene volume without a run of this session (``_adoptSceneResult``) used to show the map and
its rule but no peak table until the sign or the statistic was clicked. It needs the Slicer application (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingSceneMapTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

import numpy as np

PEAKS = ((4, 4, 4, 9.0), (4, 4, 14, 8.0), (14, 14, 4, 7.0), (14, 4, 14, -6.5), (14, 14, 14, 6.0))


def _pump(ms):
    import time

    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


class LesionNetworkMappingSceneMapTest(unittest.TestCase):
    def setUp(self):
        slicer.mrmlScene.Clear(0)
        slicer.util.selectModule("LesionNetworkMapping")
        self.widget = slicer.modules.LesionNetworkMappingWidget

    def tearDown(self):
        slicer.mrmlScene.Clear(0)

    def _sceneMap(self):
        data = np.zeros((19, 19, 19), np.float32)
        for i, j, k, value in PEAKS:
            data[k, j, i] = value
        affine = np.diag([4.0, 4.0, 4.0, 1.0])
        volume = slicer.util.addVolumeFromArray(data, ijkToRAS=affine, name="Lesion T", nodeClassName="vtkMRMLScalarVolumeNode")
        volume.SetAttribute("LNM.Result", "1")
        volume.SetAttribute("LNM.Statistic", "T")
        volume.SetAttribute("LNM.N", "120")
        return volume

    def test_peak_table_shown_for_a_map_restored_from_the_scene(self):
        widget = self.widget
        widget.exit()
        volume = self._sceneMap()
        widget.loadedMap = None
        widget.runOutputs = {}
        widget._peaks = []
        widget.initializeParameterNode()
        widget._parameterNode.resultVolume = volume
        widget.enter()
        _pump(300)
        self.assertIsNotNone(widget.loadedMap)
        self.assertTrue(widget.loadedMap.get("fromScene"))
        self.assertEqual(len(widget._peaks), 5, widget._peaks)
        self.assertFalse(widget.peaksTable.isHidden())
        self.assertEqual(widget.peaksTable.rowCount, 5)

    def test_project_nifti_export_reads_the_threshold_of_a_reopened_map_before_map_is_shown(self):
        """A reopened project exports the map with its thresholded mask without Map having been entered (BB-1)."""
        from StudioLib import niftiexport

        widget = self.widget
        widget.exit()
        volume = self._sceneMap()
        widget.initializeParameterNode()
        widget._parameterNode.resultVolume = volume
        # What a reopened project looks like: the scene holds the map and the parameter node, the widget has restored nothing
        widget.setParameterNode(None)
        widget.loadedMap = None
        widget.runOutputs = {}
        widget._peaks = []
        widget.resolved = None
        self.assertFalse(widget._hasResult())
        options = niftiexport.mapOptionsFromMapModule()
        self.assertEqual(list(options), [volume.GetID()], options)
        self.assertGreater(options[volume.GetID()]["threshold"], 0.0)
        self.assertTrue(options[volume.GetID()]["rule"])
        # Entering Map afterwards still shows the peak table of the map restored quietly
        widget.enter()
        _pump(300)
        self.assertEqual(len(widget._peaks), 5, widget._peaks)
        self.assertEqual(widget.peaksTable.rowCount, 5)

    def test_nifti_export_options_without_a_map_in_the_scene_change_nothing(self):
        from StudioLib import niftiexport

        self.widget.exit()
        self.widget.setParameterNode(None)
        self.assertEqual(niftiexport.mapOptionsFromMapModule(), {})
        self.assertIsNone(self.widget._parameterNode)


if __name__ == "__main__":
    unittest.main()

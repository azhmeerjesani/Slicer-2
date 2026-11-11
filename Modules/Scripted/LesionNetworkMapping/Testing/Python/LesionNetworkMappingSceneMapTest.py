"""A map restored from the scene (a reopened project) shows its peak table when Map is entered.

The Result card of a scene volume without a run of this session (``_adoptSceneResult``) used to show the map and
its rule but no peak table until the sign or the statistic was clicked. It needs the Slicer application (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingSceneMapTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import types
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

    def _sceneMap(self, statistic="T", scale=1.0):
        data = np.zeros((19, 19, 19), np.float32)
        for i, j, k, value in PEAKS:
            data[k, j, i] = value * scale
        affine = np.diag([4.0, 4.0, 4.0, 1.0])
        volume = slicer.util.addVolumeFromArray(data, ijkToRAS=affine, name=f"Lesion {statistic}", nodeClassName="vtkMRMLScalarVolumeNode")
        volume.SetAttribute("LNM.Result", "1")
        volume.SetAttribute("LNM.Statistic", statistic)
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

    def test_peak_table_rounds_half_mm_coordinates_like_the_clusters_card(self):
        """On a grid with voxel centres at .5 mm (the 2009c 2 mm grid) the Result card's peak table rounds half away from
        zero and never prints "-0", as the Clusters card does (``:.0f`` gave "-0, -58, 40" for (-0.5, -58.5, 39.5)).
        """
        from LesionNetworkMappingUI.card_clusters import formatCoordinate

        widget = self.widget
        widget.exit()
        data = np.zeros((19, 19, 19), np.float32)
        for i, j, k, value in PEAKS:
            data[k, j, i] = value
        # Voxel centres at .5 mm: voxel (i, j, k) is at (2i - 8.5, 2j - 12.5, 2k - 6.5), so (4, 4, 4) is (-0.5, -4.5, 1.5)
        affine = np.array([[2.0, 0, 0, -8.5], [0, 2.0, 0, -12.5], [0, 0, 2.0, -6.5], [0, 0, 0, 1.0]])
        volume = slicer.util.addVolumeFromArray(data, ijkToRAS=affine, name="Lesion T", nodeClassName="vtkMRMLScalarVolumeNode")
        volume.SetAttribute("LNM.Result", "1")
        volume.SetAttribute("LNM.Statistic", "T")
        volume.SetAttribute("LNM.N", "120")
        widget.loadedMap = None
        widget.runOutputs = {}
        widget._peaks = []
        widget.initializeParameterNode()
        widget._parameterNode.resultVolume = volume
        widget.enter()
        _pump(300)
        self.assertEqual(len(widget._peaks), 5, widget._peaks)
        halves = 0
        for row, peak in enumerate(widget._peaks):
            halves += sum(1 for v in peak["world"] if abs(abs(v) % 1.0 - 0.5) < 1e-6)
            expected = ", ".join(formatCoordinate(v) for v in peak["world"])
            shown = widget.peaksTable.item(row, 1).text()
            self.assertEqual(shown, expected, peak["world"])
            self.assertNotIn("-0,", shown + ",", shown)
        self.assertGreaterEqual(halves, 10, "the test grid must put the peaks at half millimetres")

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

    def test_reopened_stopped_partial_map_keeps_its_partial_label_in_result_step_and_chip(self):
        """A map reopened from the project with LNM.Partial=1 still reads "(partial)" in the Result step and the chip (BB-1)."""
        widget = self.widget
        widget.exit()
        volume = self._sceneMap()
        volume.SetAttribute("LNM.Partial", "1")
        volume.SetAttribute("LNM.N", "50")
        widget.loadedMap = None
        widget.runOutputs = {}
        widget._peaks = []
        widget.initializeParameterNode()
        widget._parameterNode.resultVolume = volume
        widget.enter()
        _pump(300)
        self.assertTrue(widget.loadedMap.get("partial"))
        widget._updateSteps()
        widget._updateStatusChip()
        summary = str(widget._cardWidgets["result"][1].property("studioStepSummary"))
        self.assertIn("N = 50 (partial)", summary)
        if widget._statusChipText is not None:  # the chip exists only under the Studio shell
            self.assertEqual(widget._statusChipText, "Stopped · N=50 (partial)")

    def test_legend_and_export_rule_state_the_threshold_of_the_derived_line(self):
        """An Fz map: the legend and the NIfTI sidecar Rule name the threshold as the derived line does (BB-1), never
        rounded to one decimal ("|Fz| ≥ 0.05", not "≥ 0.1"; a top-10% cut of 0.0x, not "≥ 0.0").
        """
        from LesionNetworkMappingLib import thresholds
        from StudioLib import niftiexport

        widget = self.widget
        widget.exit()
        volume = self._sceneMap("Fz", scale=0.01)
        widget.loadedMap = None
        widget.runOutputs = {}
        widget._peaks = []
        widget.initializeParameterNode()
        widget._parameterNode.resultVolume = volume
        widget._parameterNode.sign = "both"
        widget._parameterNode.statistic = "Fz"  # as the parameter node of a reopened project showing its Fz map
        widget.enter()
        _pump(300)
        self.assertEqual(widget.currentStatistic(), "Fz")
        for rule, expected in (({"kind": "fz_abs", "value": 0.05}, "|Fz| ≥ 0.05"), ({"kind": "fz_abs", "value": 0.12}, "|Fz| ≥ 0.12")):
            widget.setRule(rule)
            _pump(100)
            state = widget.resultLegendState()
            self.assertEqual(state["threshold"], rule["value"])
            self.assertTrue(state["thresholdText"].endswith(expected), state["thresholdText"])
            self.assertIn(f"≥ {thresholds.format_value(widget.resolved)} ", str(widget.derivedLabel.text))
            options = niftiexport.mapOptionsFromMapModule()[volume.GetID()]
            self.assertEqual(options["threshold"], rule["value"])
            self.assertTrue(options["rule"].endswith(expected), options)
        widget.setRule({"kind": "top_pct", "value": 10.0, "statistic": "Fz", "sign": "both"})
        _pump(100)
        state = widget.resultLegendState()
        cut = thresholds.format_value(widget.resolved)
        self.assertGreater(state["threshold"], 0.0)
        self.assertLess(state["threshold"], 0.1)
        self.assertNotEqual(float(cut), 0.0)
        self.assertTrue(state["thresholdText"].endswith(f"|Fz| ≥ {cut}"), state["thresholdText"])
        self.assertIn(f"(≥ {cut})", str(widget.derivedLabel.text))

    def test_legend_does_not_claim_an_unthresholded_map_while_the_rule_hides_it(self):
        """A rule on the shown statistic that is not applied (the |T| >= 7 convention at N=120, before Keep) shows nothing
        (the Result card says so): the legend says "Rule not applied", not "Unthresholded". A rule on another statistic really shows the
        whole map, which stays "Unthresholded". The NIfTI export still sees no threshold.
        """
        import types

        widget = self.widget
        widget.exit()
        volume = self._sceneMap("T")
        widget.loadedMap = None
        widget.runOutputs = {}
        widget._peaks = []
        widget.initializeParameterNode()
        widget._parameterNode.resultVolume = volume
        widget._parameterNode.statistic = "T"
        widget.enter()
        _pump(300)
        self.assertEqual(widget.currentStatistic(), "T")
        rule = {"kind": "t_convention", "value": 7.0, "reference_n": 1000}
        widget.currentRule = lambda: rule  # the rule as chosen; the resolution below is what it gives at N=120
        try:
            widget.resolved = types.SimpleNamespace(value=7.0, allowed=False, rule=rule, statistic="T")
            state = widget.resultLegendState()
            self.assertIsNone(state["threshold"])
            self.assertNotEqual(state["thresholdText"], "Unthresholded")
            self.assertIn("Rule not applied", state["thresholdText"])
            # Kept with Keep: the rule is applied
            rule = {"kind": "t_convention", "value": 7.0, "reference_n": 1000, "override": True}
            widget.resolved.rule = rule
            state = widget.resultLegendState()
            self.assertEqual(state["threshold"], 7.0)
            # A rule on another statistic: the whole map is shown
            rule = {"kind": "fz_abs", "value": 0.05}
            widget.resolved = types.SimpleNamespace(value=0.05, allowed=False, rule=rule, statistic="Fz")
            state = widget.resultLegendState()
            self.assertIsNone(state["threshold"])
            self.assertEqual(state["thresholdText"], "Unthresholded")
        finally:
            del widget.currentRule

    def test_nifti_export_options_without_a_map_in_the_scene_change_nothing(self):
        from StudioLib import niftiexport

        self.widget.exit()
        self.widget.setParameterNode(None)
        self.assertEqual(niftiexport.mapOptionsFromMapModule(), {})
        self.assertIsNone(self.widget._parameterNode)


    def test_peak_regions_are_looked_up_again_when_atlases_arrive(self):
        """Get atlases finishing names the peaks without a new Compute (BB-1: the Region column stayed "—")."""
        from LesionNetworkMappingLib import atlases
        from LesionNetworkMappingUI.card_clusters import sharedAtlases

        widget = self.widget
        widget.exit()
        volume = self._sceneMap()
        widget.loadedMap = None
        widget.runOutputs = {}
        widget._peaks = []
        widget.initializeParameterNode()
        widget._parameterNode.resultVolume = volume
        downloaded = []
        original = atlases.peak_regions

        def fake(points, space=None, cache=None, **kwargs):
            # None while no atlas is on the computer, the names once it is
            return [f"Region {index}" for index in range(len(points))] if downloaded else None

        atlases.peak_regions = fake
        try:
            widget.enter()
            _pump(500)
            self.assertEqual(len(widget._peaks), 5)
            column = [widget.peaksTable.item(row, 0).text() for row in range(5)]
            self.assertEqual(column, ["—"] * 5)
            self.assertIn("No atlas downloaded", widget.peaksTable.item(0, 0).toolTip())
            manager = sharedAtlases(widget.ctx)
            self.assertIn(widget._onAtlasesChanged, manager.listeners)
            # A download is running: nothing is looked up yet
            manager.fetching = {"thread": types.SimpleNamespace(is_alive=lambda: True)}
            downloaded.append(True)
            manager.notify()
            _pump(300)
            self.assertEqual(widget.peaksTable.item(0, 0).text(), "—")
            # The download finished (the atlases changed, the manager is not busy any more)
            manager.fetching = None
            manager.notify()
            _pump(500)
            column = [widget.peaksTable.item(row, 0).text() for row in range(5)]
            self.assertEqual(column, [f"Region {index}" for index in range(5)])
            self.assertNotIn("No atlas downloaded", widget.peaksTable.item(0, 0).toolTip())
            # Atlases downloaded elsewhere (the Lesion card of Draw) while Map was left: found again when Map is entered
            downloaded.clear()
            widget._showPeakRegions(widget._peaks, None)
            self.assertEqual(widget.peaksTable.item(0, 0).text(), "—")
            downloaded.append(True)
            widget.enter()
            _pump(500)
            self.assertEqual(widget.peaksTable.item(0, 0).text(), "Region 0")
        finally:
            atlases.peak_regions = original


if __name__ == "__main__":
    unittest.main()

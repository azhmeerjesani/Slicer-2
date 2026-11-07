"""Tests that the pages without a Result card leave the drawn lesion's map out of the views (BB-1).

After a drawn lesion was mapped, the Coordinates, Stimulation and Cohort pages (which have no Result card) kept the lesion's
network map in the views with its legend, its layer chips ("Network T · 80%", "Lesion outline") and the lesion's outline,
also after the coordinate seeds were mapped, and the Map ⋯ menu offered "Export map as NIfTI…" for it: it read as the map of
that page's seeds (bug map-single:seedset-and-cohort-pages-keep-lesion-map-and-legend). Those pages now take the run's map
out of the views (the legend reads no map) and give the lesion its own look; the run is kept, and the Lesion page shows it
again.

They need the Slicer application and run the engine on a tiny synthetic connectome (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingSeedModeMapUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
import tempfile
import time
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


def _waitFor(condition, timeoutS):
    deadline = time.monotonic() + timeoutS
    while not condition():
        if time.monotonic() > deadline:
            return False
        slicer.app.processEvents()
        time.sleep(0.005)
    return True


class LesionNetworkMappingSeedModeMapUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm seed mode map ")
        slicer.mrmlScene.Clear(0)
        slicer.util.selectModule("LesionNetworkMapping")
        _waitFor(lambda: False, 0.05)
        cls.widget = slicer.modules.LesionNetworkMappingWidget

    @classmethod
    def tearDownClass(cls):
        widget = cls.widget
        if widget._parameterNode is not None and widget.seedMode() != "lesion":
            widget.seedModeGroup.button(widget.seedModes.index("lesion")).click()
        slicer.mrmlScene.Clear(0)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _sphereLesion(self, meta, radius=4.0):
        """A segmentation with a "Lesion" sphere segment in planted blob A, on the connectome grid."""
        import numpy as np

        affine = np.asarray(meta["affine"])
        shape = tuple(meta["shape"])
        world = np.indices(shape).reshape(3, -1).T @ affine[:3, :3].T + affine[:3, 3]
        sphere = (np.linalg.norm(world - np.asarray(meta["blobs"]["A"]["center_world"]), axis=1) <= radius).reshape(shape)
        volume = slicer.util.addVolumeFromArray(np.transpose(sphere.astype(np.uint8), (2, 1, 0)), ijkToRAS=affine, name="seedgrid", nodeClassName="vtkMRMLLabelMapVolumeNode")
        segNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesion segmentation")
        segNode.CreateDefaultDisplayNodes()
        slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(volume, segNode)
        segmentId = segNode.GetSegmentation().GetNthSegmentID(0)
        segNode.GetSegmentation().GetSegment(segmentId).SetName("Lesion")
        segNode.SetReferenceImageGeometryParameterFromVolumeNode(volume)
        return segNode, segmentId

    def _foregrounds(self):
        return [composite.GetForegroundVolumeID() for composite in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode")]

    def _click(self, mode):
        widget = self.widget
        widget.seedModeGroup.button(widget.seedModes.index(mode)).click()
        _waitFor(lambda: False, 0.05)
        self.assertEqual(widget.seedMode(), mode)

    def test_seed_set_and_cohort_pages_hide_the_lesion_map(self):
        from LesionNetworkMappingLib import connectome, thresholds

        widget = self.widget
        widget.initializeParameterNode()
        meta = connectome.make_synthetic(os.path.join(self.tmp, "seedmode.lnmc"), shape=(20, 24, 20), n_subjects=20, runs=(20,), seed=0, name="Seed mode test")
        segNode, segmentId = self._sphereLesion(meta)
        widget.refreshConnectomes(select=os.path.abspath(meta["path"]))
        widget.ctx.setSeed(segNode, segmentId)
        widget.updateGUIFromParameterNode()
        widget.updateSeed()
        widget.acceptTermsCheckBox.setChecked(True)
        widget.subjectsGroup.button(0).click()
        widget.setRule(thresholds.DEFAULT_RULE)
        self.assertEqual(widget.seedMode(), "lesion")
        self.assertTrue(widget.computeButton.enabled, widget.computeReasonLabel.text)
        job = widget.compute()
        self.assertTrue(_waitFor(job.isFinished, 300), "the engine did not finish")
        self.assertEqual(job.state, "done", f"{job.failureReason} {list(job.logTail)}")
        self.assertTrue(_waitFor(lambda: widget.resolved is not None and widget._hasResult() and widget.ctx.currentRun is not None, 30))
        _waitFor(lambda: False, 0.3)  # the deferred refresh of the final map
        lesionMap, run = widget._parameterNode.resultVolume, widget.ctx.currentRun
        displayNode = segNode.GetDisplayNode()
        self.assertIn(lesionMap.GetID(), self._foregrounds())
        self.assertIsNotNone(widget.legendState())
        self.assertTrue(widget.legendState()["outline"])
        self.assertFalse(displayNode.GetVisibility2DFill(), "the mapped lesion is not shown as an outline")

        for mode in ("coordinates", "stimulation", "cohort"):
            if mode not in widget.seedModes:
                continue
            self._click(mode)
            self.assertNotIn("result", widget._visibleCardIds())
            self.assertNotIn(lesionMap.GetID(), self._foregrounds(), f"the {mode} page shows the drawn lesion's map")
            self.assertIsNone(widget.legendState(), f"the {mode} page has a legend (and ⋯ Export map as NIfTI…) for the lesion's map")
            self.assertTrue(displayNode.GetVisibility2DFill(), f"the {mode} page shows the drawn lesion as the map's outline")
            # The run is kept: navigation never discards it
            self.assertIsNotNone(lesionMap.GetScene())
            self.assertIs(widget._parameterNode.resultVolume, lesionMap)
            self.assertIs(widget.ctx.currentRun, run)
            self.assertTrue(widget._hasResult())
            # Entering Map again on this page (mappage.enter) does not bring it back either
            widget.showMapLayers()
            self.assertNotIn(lesionMap.GetID(), self._foregrounds(), f"entering Map on the {mode} page shows the drawn lesion's map")

        self._click("lesion")
        self.assertIn("result", widget._visibleCardIds())
        self.assertIn(lesionMap.GetID(), self._foregrounds(), "the Lesion page does not show its map again")
        state = widget.legendState()
        self.assertIsNotNone(state)
        self.assertTrue(state["outline"])
        self.assertFalse(displayNode.GetVisibility2DFill(), "the Lesion page does not show the lesion as an outline again")
        self.assertIs(widget.ctx.currentRun, run)

    def test_parked_run_hint_names_the_segmentation_of_another_segmentation(self):
        """BB-1 map-single:parked-run-hint-names-segment-of-other-segmentation: with a segment of the same name chosen in
        another segmentation, the hint names the segmentation to choose, not the segment already selected.
        """
        widget = self.widget
        scene = slicer.mrmlScene
        mapped = scene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions")
        mappedSegment = mapped.GetSegmentation().AddEmptySegment("lesion-a", "Lesion")
        other = scene.AddNewNodeByClass("vtkMRMLSegmentationNode", "sub-02_lesion")
        otherSegment = other.GetSegmentation().AddEmptySegment("lesion-b", "Lesion")
        parked = {"runSeed": (mapped.GetID(), mappedSegment), "state": ("Lesions-Lesion",), "runBundle": None}
        original = widget._seedKey
        self.addCleanup(setattr, widget, "_seedKey", original)
        widget._seedKey = lambda: (other.GetID(), otherSegment)
        text = widget._parkedSeedHint(parked, "Lesions-Lesion")
        self.assertIn("segmentation \u201cLesions\u201d", text)
        self.assertIn("choose that segmentation", text)
        # The same segmentation chosen (another segment): the segment is what to choose
        widget._seedKey = lambda: (mapped.GetID(), "other-segment")
        text = widget._parkedSeedHint(parked, "Lesions-Lesion")
        self.assertIn("choose that segment in Seed", text)
        self.assertNotIn("segmentation", text)


if __name__ == "__main__":
    unittest.main()

"""Tests that closing the scene while Map runs stops the run and leaves Map ready (BB-1).

Closing the scene (Ctrl+N or Ctrl+W, "Close scene (discard modifications)") during a Map run left the engine running: each
later event and its end raised a TypeError (the maps of the run folder were looked up after the scene close had forgotten
the folder), the Compute step stayed on "Mapping…" with Map network disabled for the rest of the session (bug
long-run-interactions:map-close-scene-midrun-engine-events-traceback-compute-stuck). Closing the scene now stops the run, as
Normalize stops its job, and the run's events no longer reach the next scene.

It needs the Slicer application and runs the engine on a small synthetic connectome (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCloseSceneMidRunUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import logging
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


class _ErrorRecords(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class LesionNetworkMappingCloseSceneMidRunUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm close scene mid run ")
        slicer.mrmlScene.Clear(0)
        slicer.util.selectModule("LesionNetworkMapping")
        _waitFor(lambda: False, 0.05)
        cls.widget = slicer.modules.LesionNetworkMappingWidget

    @classmethod
    def tearDownClass(cls):
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

    def test_closing_the_scene_stops_the_run_and_leaves_map_ready(self):
        from LesionNetworkMappingLib import connectome, thresholds

        widget = self.widget
        widget.initializeParameterNode()
        meta = connectome.make_synthetic(os.path.join(self.tmp, "closescene.lnmc"), shape=(20, 24, 20), n_subjects=40, runs=(40,), seed=0, name="Close scene test")
        segNode, segmentId = self._sphereLesion(meta)
        widget.refreshConnectomes(select=os.path.abspath(meta["path"]))
        widget.ctx.setSeed(segNode, segmentId)
        widget.updateGUIFromParameterNode()
        widget.updateSeed()
        widget.acceptTermsCheckBox.setChecked(True)
        widget.subjectsGroup.button(0).click()
        widget.setRule(thresholds.DEFAULT_RULE)
        self.assertTrue(widget.computeButton.enabled, widget.computeReasonLabel.text)

        errors = _ErrorRecords()
        logging.getLogger().addHandler(errors)
        try:
            job = widget.compute()
            self.assertIsNotNone(job)
            self.assertFalse(job.isFinished(), "the run ended before the scene was closed: the test needs a slower run")
            self.assertEqual(str(widget.computeReasonLabel.text), "Mapping…")
            runDir = widget.runDir

            slicer.mrmlScene.Clear(0)  # what Ctrl+N / Ctrl+W, "Close scene (discard modifications)", does

            self.assertTrue(_waitFor(job.isFinished, 120), "the run did not end")
            _waitFor(lambda: False, 0.5)  # the job's last events and its end are handled
        finally:
            logging.getLogger().removeHandler(errors)

        failed = [message for message in errors.messages if "Mapping" in message and "failed" in message]
        self.assertEqual(failed, [], "an event of the closed scene's run reached Map")
        # Map is ready for the next scene: no "Mapping…", no stale progress row, no run of the closed scene
        self.assertNotEqual(str(widget.computeReasonLabel.text), "Mapping…")
        self.assertFalse(widget.computeState()[1] == "Mapping…")
        self.assertTrue(widget.progress.widget.isHidden())
        self.assertEqual(str(widget.resultTitleLabel.text), "No map yet: Map network computes it.")
        self.assertIsNone(widget.ctx.currentRun)
        self.assertIsNone(widget._parameterNode.resultVolume if widget._parameterNode is not None else None)
        self.assertTrue(os.path.isdir(runDir), "the run folder stays in the cache")
        self.assertIsNone(widget.job)
        self.assertIsNone(widget.runDir)
        self.assertEqual(widget.runOutputs, {})
        self.assertEqual(job.state, "cancelled", "closing the scene stops the run")


if __name__ == "__main__":
    unittest.main()

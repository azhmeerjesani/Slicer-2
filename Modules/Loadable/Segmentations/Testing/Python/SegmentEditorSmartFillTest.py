"""Tests of the Smart fill Segment Editor effect (interaction, preview, Apply, undo, observers, settings).

Works with and without the main window: without views, the API path previewAtIjk() is used instead of clicks.
Run with: Slicer --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'SegmentEditorSmartFillTest')"
"""

import os
import sys
import time
import unittest

import numpy as np
import qt
import vtk

import slicer

BUILT_IN_EFFECT_ORDER = ["Threshold", "Paint", "Draw", "Erase", "Level tracing", "Grow from seeds", "Fill between slices",
                         "Margin", "Hollow", "Smoothing", "Scissors", "Islands", "Logical operators", "Mask volume"]


def ballFraction(shape, center, radius):
    kk, jj, ii = np.ogrid[0:shape[0], 0:shape[1], 0:shape[2]]
    distance = np.sqrt((kk - center[0]) ** 2 + (jj - center[1]) ** 2 + (ii - center[2]) ** 2)
    return np.clip(radius - distance + 0.5, 0, 1).astype(np.float32)


def dice(a, b):
    return 2.0 * np.count_nonzero(a & b) / max(1, np.count_nonzero(a) + np.count_nonzero(b))


class SegmentEditorSmartFillTest(unittest.TestCase):
    """Smart fill effect tests on synthetic volumes (no network)."""

    def setUp(self):
        slicer.mrmlScene.Clear(0)
        self.editor = None

    def tearDown(self):
        if self.editor is not None:
            self.editor.setActiveEffectByName("")
            if self.hasViews():
                # Ctrl+click (remove) is passed on to the view, where Ctrl+left-drag blends the layers. The blend action
                # keeps a raw pointer to the segmentation display node (vtkMRMLSliceIntersectionWidget
                # StartActionSegmentationDisplayNode), which dangles once the scene is cleared and crashes the next
                # Ctrl+click. Toggling the label opacity twice ("g") resets that pointer before the scene is cleared.
                interactor = self.interactor()
                for _ in range(2):
                    interactor.SetKeySym("g")
                    interactor.InvokeEvent(vtk.vtkCommand.KeyPressEvent)
            self.editor.setMRMLScene(None)
            self.editor = None
        slicer.mrmlScene.Clear(0)

    #
    # Helpers
    #

    def createScene(self, shape=(128, 128, 128), lesions=(((64, 64, 64), 12, 160.0),), background=100.0, noise=4.0):
        rng = np.random.default_rng(1)
        image = np.full(shape, background, np.float32)
        self.truth = np.zeros(shape, bool)
        for center, radius, value in lesions:
            fraction = ballFraction(shape, center, radius)
            image = image * (1 - fraction) + value * fraction
            self.truth |= fraction > 0.5
        image += rng.normal(0, noise, shape).astype(np.float32)
        self.volumeNode = slicer.util.addVolumeFromArray(image.astype(np.int16), name="SmartFillTestVolume")
        self.volumeNode.CreateDefaultDisplayNodes()
        displayNode = self.volumeNode.GetDisplayNode()
        displayNode.SetAutoWindowLevel(False)
        displayNode.SetWindowLevel(200, 120)
        self.segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
        self.segmentationNode.CreateDefaultDisplayNodes()
        self.segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(self.volumeNode)
        self.segmentId = self.segmentationNode.GetSegmentation().AddEmptySegment("Lesion")

        self.editor = slicer.qMRMLSegmentEditorWidget()
        self.editor.setMRMLScene(slicer.mrmlScene)
        self.editorNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentEditorNode")
        self.editor.setMRMLSegmentEditorNode(self.editorNode)
        self.editor.setSegmentationNode(self.segmentationNode)
        self.editor.setSourceVolumeNode(self.volumeNode)
        self.editor.setCurrentSegmentID(self.segmentId)
        self.editor.maximumNumberOfUndoStates = 10
        if self.hasViews():
            slicer.util.setSliceViewerLayers(background=self.volumeNode, fit=True)
        self.editor.setActiveEffectByName("Smart fill")
        self.effect = self.editor.activeEffect().self()
        self.assertEqual(self.editor.activeEffect().name, "Smart fill")
        slicer.app.processEvents()  # prefetch (lazy numpy import) runs from a zero-delay timer

    @staticmethod
    def hasViews():
        return slicer.app.layoutManager() is not None

    def segmentArray(self):
        return slicer.util.arrayFromSegmentBinaryLabelmap(self.segmentationNode, self.segmentId, self.volumeNode) > 0

    def previewArray(self):
        """Labels of the pending preview on the volume grid (0, 1 add, 2 remove)."""
        output = np.zeros(self.truth.shape, np.uint8)
        extent, labels = self.effect.previewExtent, self.effect.previewLabels
        if extent is None:
            return output
        output[extent[4]:extent[5] + 1, extent[2]:extent[3] + 1, extent[0]:extent[1] + 1] = labels
        return output

    def waitForComputation(self, timeoutSeconds=30.0):
        deadline = time.time() + timeoutSeconds
        while self.effect.isComputing() and time.time() < deadline:
            slicer.app.processEvents()
        self.assertFalse(self.effect.isComputing())

    def redWidget(self):
        return slicer.app.layoutManager().sliceWidget("Red")

    def centerViewsOn(self, kji):
        ijkToRas = vtk.vtkMatrix4x4()
        self.volumeNode.GetIJKToRASMatrix(ijkToRas)
        ras = ijkToRas.MultiplyPoint([kji[2], kji[1], kji[0], 1])[:3]
        slicer.vtkMRMLSliceNode.JumpAllSlices(slicer.mrmlScene, *ras, slicer.vtkMRMLSliceNode.CenteredJumpSlice)
        slicer.app.processEvents()
        return ras

    def xyOf(self, sliceWidget, kji):
        ijkToRas = vtk.vtkMatrix4x4()
        self.volumeNode.GetIJKToRASMatrix(ijkToRas)
        ras = ijkToRas.MultiplyPoint([kji[2], kji[1], kji[0], 1])
        rasToXy = vtk.vtkMatrix4x4()
        vtk.vtkMatrix4x4.Invert(sliceWidget.mrmlSliceNode().GetXYToRAS(), rasToXy)
        xy = rasToXy.MultiplyPoint(ras)
        return (round(xy[0]), round(xy[1]))

    def click(self, kji, modifiers=()):
        """Click at a voxel: in the Red view when views exist, else through previewAtIjk."""
        if self.hasViews():
            red = self.redWidget()
            xy = self.xyOf(red, kji)
            slicer.util.clickAndDrag(red, start=xy, end=xy, steps=1, modifiers=list(modifiers))
            self.waitForComputation()
        else:
            operation = "extend" if "Shift" in modifiers else "remove" if "Control" in modifiers else "new"
            self.effect.previewAtIjk((kji[2], kji[1], kji[0]), operation)

    def interactor(self):
        return self.redWidget().sliceView().interactorStyle().GetInteractor()

    def pressKey(self, keySym):
        """Key press in the Red view (or the equivalent API call without views)."""
        if not self.hasViews():
            if keySym in ("Return", "KP_Enter"):
                self.effect.applyPendingPreview()
            elif keySym == "BackSpace":
                self.effect.discardPendingPreview()
            elif keySym == "plus":
                self.effect.setTolerance(1.15, relative=True)
                self.effect.finishTaskSynchronously()
            return
        interactor = self.interactor()
        interactor.SetKeySym(keySym)
        interactor.SetControlKey(0)
        interactor.SetAltKey(0)
        interactor.InvokeEvent(vtk.vtkCommand.KeyPressEvent)
        self.waitForComputation()

    #
    # Tests
    #

    def test_registration_and_order(self):
        self.createScene(shape=(32, 32, 32), lesions=(((16, 16, 16), 5, 160.0),))
        self.assertIsNotNone(self.editor.effectByName("Smart fill"))
        names = []
        for index in range(1, len(BUILT_IN_EFFECT_ORDER) + 2):
            effect = self.editor.effectByIndex(index)
            names.append(effect.name if effect else None)
        self.assertEqual(names[:len(BUILT_IN_EFFECT_ORDER)], BUILT_IN_EFFECT_ORDER)
        self.assertEqual(names[len(BUILT_IN_EFFECT_ORDER)], "Smart fill")
        self.assertTrue(self.effect.helpText().startswith("<html>Click inside a lesion to fill it automatically."))
        for objectName in ("SmartFillMode2DRadioButton", "SmartFillMode3DRadioButton", "SmartFillToleranceSlider",
                           "SmartFillAutoToleranceCheckBox", "SmartFillToleranceLabel", "SmartFillLeakageGroupBox",
                           "SmartFillMaxRadiusSpinBox", "SmartFillLeakGuardSpinBox", "SmartFillFillHolesCheckBox",
                           "SmartFillStayInsideBrainCheckBox", "SmartFillApplyOnClickCheckBox", "SmartFillApplyButton",
                           "SmartFillDiscardButton", "SmartFillStatusLabel"):
            self.assertIsNotNone(slicer.util.findChild(self.editor, objectName), objectName)

    def test_defaults_ignore_settings_when_testing(self):
        if not slicer.app.testingEnabled():
            self.skipTest("only meaningful under --testing")
        settings = qt.QSettings()
        settings.setValue("Studio/SmartFill/Mode", "2D")
        settings.setValue("Studio/SmartFill/LeakGuardMm", 3.0)
        settings.setValue("Studio/SmartFill/ApplyOnClick", True)
        try:
            self.createScene(shape=(32, 32, 32), lesions=(((16, 16, 16), 5, 160.0),))
            self.assertEqual(self.effect.parameterValue("Mode"), "3D")
            self.assertAlmostEqual(self.effect.parameterValue("LeakGuardMm"), 1.5)
            self.assertFalse(self.effect.parameterValue("ApplyOnClick"))
            self.assertTrue(self.effect.parameterValue("AutoTolerance"))
            self.assertAlmostEqual(self.effect.parameterValue("MaxRadiusMm"), 100.0)
            # user edits under --testing do not write settings
            self.effect.leakGuardSpinBox.value = 2.0
            self.assertAlmostEqual(self.effect.parameterValue("LeakGuardMm"), 2.0)
            self.assertAlmostEqual(float(settings.value("Studio/SmartFill/LeakGuardMm")), 3.0)
        finally:
            settings.remove("Studio/SmartFill")

    def test_preview_apply_undo_and_dice(self):
        self.createScene()
        seed = (64, 64, 64)
        if self.hasViews():
            self.centerViewsOn(seed)
        self.click(seed)
        self.assertTrue(self.effect.hasPendingPreview())
        self.assertEqual(np.count_nonzero(self.segmentArray()), 0, "the preview must not modify the segment")
        preview = self.previewArray() == 1
        self.assertGreaterEqual(dice(preview, self.truth), 0.95)
        self.assertGreater(self.effect.lastResult["voxels"], 0)

        self.pressKey("Return")
        self.assertFalse(self.effect.hasPendingPreview())
        applied = self.segmentArray()
        self.assertTrue(np.array_equal(applied, preview), "Apply must equal the preview voxel for voxel")

        self.editor.undo()
        self.assertEqual(np.count_nonzero(self.segmentArray()), 0, "one undo state per Apply")
        self.editor.redo()
        self.assertTrue(np.array_equal(self.segmentArray(), applied))

        # Ctrl+click (remove) then Return
        self.click(seed, modifiers=["Control"])
        self.assertTrue((self.previewArray() == 2).any())
        self.pressKey("Return")
        self.assertLess(np.count_nonzero(self.segmentArray()), 0.05 * np.count_nonzero(applied))
        self.editor.undo()
        self.assertTrue(np.array_equal(self.segmentArray(), applied), "one undo state for the remove")

    def test_extend_tolerance_and_discard(self):
        self.createScene(lesions=(((64, 64, 40), 10, 160.0), ((64, 64, 88), 10, 160.0)))
        if self.hasViews():
            self.centerViewsOn((64, 64, 64))
        self.click((64, 64, 40))
        first = self.previewArray() == 1
        self.assertGreater(np.count_nonzero(first), 0)
        self.click((64, 64, 88), modifiers=["Shift"])
        extended = self.previewArray() == 1
        self.assertTrue(np.array_equal(extended & first, first), "Shift+click extends the preview (superset)")
        self.assertGreater(np.count_nonzero(extended), 1.5 * np.count_nonzero(first))

        tolerance = self.effect.lastResult["tolerance"]
        if self.hasViews():
            interactor = self.interactor()
            interactor.SetShiftKey(1)
            self.effect.lastWheelTime = 0.0
            interactor.InvokeEvent(vtk.vtkCommand.MouseWheelForwardEvent)
            interactor.SetShiftKey(0)
            self.waitForComputation()
        else:
            self.effect.setTolerance(1.15, relative=True)
            self.effect.finishTaskSynchronously()
        self.assertAlmostEqual(self.effect.lastResult["tolerance"], tolerance * 1.15, delta=1e-6 * tolerance)
        self.assertFalse(self.effect.lastResult["auto"])
        self.pressKey("plus")
        self.assertAlmostEqual(self.effect.lastResult["tolerance"], tolerance * 1.15 * 1.15, delta=1e-6 * tolerance)
        self.assertTrue(self.effect.hasPendingPreview())

        self.pressKey("BackSpace")
        self.assertFalse(self.effect.hasPendingPreview())
        self.assertEqual(np.count_nonzero(self.segmentArray()), 0)

    def test_progressive_tolerance_update(self):
        """A tolerance update shows the flood at once, then the finalized region; it is never applied or kept unfinished."""
        self.createScene(lesions=(((64, 64, 44), 12, 160.0), ((64, 64, 92), 8, 160.0)))
        effectModule = sys.modules[type(self.effect).__module__]  # the package attribute is the class, not the module
        self.effect.previewAtIjk((44, 64, 64))
        tolerance = self.effect.lastResult["tolerance"]
        self.effect.setTolerance(1.15, relative=True)
        self.effect.finishTaskSynchronously()
        self.assertFalse(self.effect.lastResult["provisional"])
        final = self.previewArray() == 1
        self.assertGreater(np.count_nonzero(final), 0)
        self.assertFalse(final[:, :, 72:].any())
        self.effect.discardPendingPreview()

        budgetSeconds, provisionalMs = effectModule.STEP_BUDGET_SECONDS, effectModule.PROVISIONAL_FINALIZE_MS
        effectModule.STEP_BUDGET_SECONDS = 0.0  # one step per timer callback, so that each state can be observed
        effectModule.PROVISIONAL_FINALIZE_MS = 0.0  # show the flood first whatever the finalization time
        try:
            # repeated steps add up while an update is running
            self.effect.previewAtIjk((44, 64, 64))
            self.effect.setTolerance(1.15, relative=True)
            self.effect.setTolerance(1.15, relative=True)
            self.effect.finishTaskSynchronously()
            self.assertAlmostEqual(self.effect.lastResult["tolerance"], tolerance * 1.15 * 1.15, delta=1e-6 * tolerance)
            self.effect.discardPendingPreview()

            for action in ("extend", "apply"):
                self.effect.previewAtIjk((44, 64, 64))
                self.effect.setTolerance(1.15, relative=True)  # first step: the flood
                self.effect.onTaskTimer()  # next step: the flood is shown before the leak guard, closing and hole filling
                self.assertTrue(self.effect.isComputing())
                self.assertTrue(self.effect.lastResult["provisional"])
                self.assertTrue(self.effect.hasPendingPreview())
                if action == "extend":
                    self.effect.previewAtIjk((92, 64, 64), "extend")
                    self.assertFalse(self.effect.lastResult["provisional"])
                    extended = self.previewArray() == 1
                    self.assertTrue(np.array_equal(extended[:, :, :72], final[:, :, :72]), "the finalized region is kept")
                    self.assertTrue(extended[:, :, 72:].any())
                    self.effect.discardPendingPreview()
                else:
                    self.assertTrue(self.effect.applyPendingPreview())
                    self.assertFalse(self.effect.isComputing())
                    self.assertTrue(np.array_equal(self.segmentArray(), final), "Apply applies the finalized region")
        finally:
            effectModule.STEP_BUDGET_SECONDS, effectModule.PROVISIONAL_FINALIZE_MS = budgetSeconds, provisionalMs

        # without a pending preview and with the automatic tolerance, +/- do not change the stored tolerance
        percent = self.effect.parameterValue("TolerancePercent")
        self.effect.setTolerance(1.15, relative=True)
        self.assertEqual(self.effect.parameterValue("TolerancePercent"), percent)

    def test_shift_drag_pans_without_preview(self):
        if not self.hasViews():
            self.skipTest("needs slice views")
        self.createScene()
        self.centerViewsOn((64, 64, 64))
        red = self.redWidget()
        xyToRas = vtk.vtkMatrix4x4()
        xyToRas.DeepCopy(red.mrmlSliceNode().GetXYToRAS())
        xy = self.xyOf(red, (64, 64, 64))
        slicer.util.clickAndDrag(red, start=xy, end=(xy[0] + 40, xy[1]), steps=10, modifiers=["Shift"])
        slicer.app.processEvents()
        self.waitForComputation()
        self.assertFalse(self.effect.hasPendingPreview())
        changed = any(abs(xyToRas.GetElement(r, c) - red.mrmlSliceNode().GetXYToRAS().GetElement(r, c)) > 1e-6
                      for r in range(4) for c in range(4))
        self.assertTrue(changed, "Shift+drag pans the view")

    def test_deactivation_removes_actors_and_cancels(self):
        if not self.hasViews():
            self.skipTest("needs slice views")
        self.createScene()
        self.centerViewsOn((64, 64, 64))
        red = self.redWidget()
        renderer = self.editor.effectByName("Smart fill").renderer(red)
        self.editor.setActiveEffectByName("")
        baseline = renderer.GetActors2D().GetNumberOfItems()
        self.editor.setActiveEffectByName("Smart fill")
        self.effect = self.editor.activeEffect().self()
        self.click((64, 64, 64))
        self.assertGreater(renderer.GetActors2D().GetNumberOfItems(), baseline)
        # start a computation and deactivate while it runs (one step per timer callback: it is still running)
        effectModule = sys.modules[type(self.effect).__module__]
        budgetSeconds = effectModule.STEP_BUDGET_SECONDS
        effectModule.STEP_BUDGET_SECONDS = 0.0
        try:
            xy = self.xyOf(red, (64, 64, 64))
            slicer.util.clickAndDrag(red, start=xy, end=xy, steps=1)
        finally:
            effectModule.STEP_BUDGET_SECONDS = budgetSeconds
        self.assertTrue(self.effect.isComputing())
        effect = self.effect
        startTime = time.perf_counter()
        self.editor.setActiveEffectByName("")
        deactivateMs = 1000 * (time.perf_counter() - startTime)
        self.assertFalse(effect.isComputing())
        self.assertFalse(effect.hasPendingPreview())
        self.assertEqual(renderer.GetActors2D().GetNumberOfItems(), baseline)
        print(f"deactivate: {deactivateMs:.1f} ms")

    def test_2d_mode_touches_one_slice(self):
        self.createScene()
        self.effect.scriptedEffect.setParameter("Mode", "2D")
        seed = (64, 64, 64)
        if self.hasViews():
            self.centerViewsOn(seed)
        self.click(seed)
        preview = self.previewArray() == 1
        slices = sorted(set(np.argwhere(preview)[:, 0].tolist()))
        self.assertEqual(slices, [64])
        self.assertGreaterEqual(dice(preview[64], self.truth[64]), 0.95)
        self.assertEqual(self.effect.lastResult["mode"], "2D")

    def test_apply_on_click(self):
        self.createScene()
        self.effect.scriptedEffect.setParameter("ApplyOnClick", 1)
        seed = (64, 64, 64)
        if self.hasViews():
            self.centerViewsOn(seed)
            self.click(seed)
        else:
            self.effect.previewAtIjk((seed[2], seed[1], seed[0]))
            self.effect.applyPendingPreview()
        self.assertFalse(self.effect.hasPendingPreview())
        applied = self.segmentArray()
        self.assertGreaterEqual(dice(applied, self.truth), 0.95)
        self.editor.undo()
        self.assertEqual(np.count_nonzero(self.segmentArray()), 0, "one undo state per click")

    def test_save_with_pending_preview(self):
        self.createScene()
        seed = (64, 64, 64)
        if self.hasViews():
            self.centerViewsOn(seed)
        self.click(seed)
        self.pressKey("Return")
        applied = self.segmentArray()
        self.click((64, 64, 30))  # a second, pending preview (background region)
        self.assertTrue(self.effect.hasPendingPreview())
        path = os.path.join(slicer.app.temporaryPath, "SmartFillPendingPreview.seg.nrrd")
        self.assertTrue(slicer.util.saveNode(self.segmentationNode, path))
        loaded = slicer.util.loadSegmentation(path)
        loadedArray = slicer.util.arrayFromSegmentBinaryLabelmap(loaded, loaded.GetSegmentation().GetNthSegmentID(0), self.volumeNode) > 0
        self.assertTrue(np.array_equal(loadedArray, applied), "the saved file contains no preview voxels")
        os.remove(path)

    def test_apply_observers(self):
        self.createScene()
        calls = []

        def observer(info):
            calls.append(info)

        def failingObserver(info):
            raise RuntimeError("expected test failure in an apply observer")

        effectClass = type(self.effect)
        effectClass.addApplyObserver(failingObserver)
        effectClass.addApplyObserver(observer)
        try:
            seed = (64, 64, 64)
            if self.hasViews():
                self.centerViewsOn(seed)
            self.click(seed)
            preview = self.previewArray() == 1
            self.assertTrue(self.effect.applyPendingPreview())
            self.assertEqual(len(calls), 1)
            info = calls[0]
            self.assertEqual(info["addedVoxels"], int(np.count_nonzero(preview)))
            self.assertEqual(info["addedVoxels"], int(np.count_nonzero(self.segmentArray())))
            self.assertEqual(info["removedVoxels"], 0)
            self.assertEqual(info["segmentId"], self.segmentId)
            self.assertIs(info["segmentationNode"], self.segmentationNode)
            self.assertAlmostEqual(info["voxelVolumeMm3"], 1.0)
            for key in ("Mode", "tolerance", "auto", "MaxRadiusMm", "LeakGuardMm"):
                self.assertIn(key, info["params"])
            # remove part: removedVoxels counted
            self.click(seed, modifiers=["Control"])
            self.assertTrue(self.effect.applyPendingPreview())
            self.assertEqual(len(calls), 2)
            self.assertGreater(calls[1]["removedVoxels"], 0)
            self.assertEqual(calls[1]["addedVoxels"], 0)
        finally:
            effectClass.removeApplyObserver(failingObserver)
            effectClass.removeApplyObserver(observer)
        self.assertNotIn(observer, effectClass.applyObservers)

    def test_responsiveness_during_escalation(self):
        if not self.hasViews():
            self.skipTest("needs the main window")
        self.createScene(shape=(256, 256, 256), lesions=(((128, 128, 128), 45, 160.0),))
        seed = (128, 128, 143)  # off-centre: the first pass (50 mm) reaches its limit and the radius is extended
        self.centerViewsOn(seed)
        self.click((128, 128, 60))  # warm-up (pays first-call costs), then discard
        self.pressKey("BackSpace")
        intervals = []
        last = [time.perf_counter()]

        def onProbe():
            now = time.perf_counter()
            intervals.append(1000 * (now - last[0]))
            last[0] = now

        probe = qt.QTimer()
        probe.setInterval(10)
        probe.connect("timeout()", onProbe)
        probe.start()
        slicer.app.processEvents()
        last[0] = time.perf_counter()
        red = self.redWidget()
        xy = self.xyOf(red, seed)
        slicer.util.clickAndDrag(red, start=xy, end=xy, steps=1)
        self.waitForComputation()
        for _ in range(3):
            slicer.app.processEvents()
        probe.stop()
        probe.disconnect("timeout()", onProbe)
        result = self.effect.lastResult
        maxInterval = max(intervals) if intervals else 0.0
        print(f"probe max interval {maxInterval:.1f} ms over {len(intervals)} ticks; maxBlock {result['timingsMs'].get('maxBlock', 0):.1f} ms;"
              f" escalated {result['escalated']}; timings {result['timingsMs']}")
        self.assertTrue(result["escalated"])
        self.assertLessEqual(result["timingsMs"]["maxBlock"], 60.0)
        self.assertLessEqual(maxInterval, 100.0)
        preview = self.previewArray() == 1
        self.assertGreaterEqual(dice(preview, self.truth), 0.9)

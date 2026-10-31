"""Tests of the one-click normalization user interface (A-03b): the Map card, the Draw card and the controller.

They need the Slicer application (main window, BRAINSFit) and run the real normalization job on the synthetic
native subject of ``lnm_normalize_fixture.py`` (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingNormalizeUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

TEMPLATE = "MNI152NLin2009cAsym"
#: Largest gap of the 10 ms probe timer during a run on an idle host (ms); a busy host gets a looser bound
MAX_GAP_IDLE_MS = 100.0
MAX_GAP_BUSY_MS = 400.0


def _wait(condition, timeoutS, step=0.01):
    deadline = time.monotonic() + timeoutS
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        if condition():
            return True
        time.sleep(step)
    slicer.app.processEvents()
    return bool(condition())


def _pump(ms):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


class _GapProbe:
    """A 10 ms QTimer that records the gaps between its ticks (GUI responsiveness)."""

    def __init__(self):
        self.timer = qt.QTimer()
        self.timer.setInterval(10)
        self.timer.connect("timeout()", self._tick)
        self.last = None
        self.gaps = []

    def _tick(self):
        now = time.perf_counter()
        if self.last is not None:
            self.gaps.append((now, (now - self.last) * 1000.0))
        self.last = now

    def maxGapMs(self, until=None, since=None):
        gaps = [gap for when, gap in self.gaps if (until is None or when <= until) and (since is None or when > since)]
        return max(gaps) if gaps else 0.0

    def start(self):
        self.last = None
        self.timer.start()

    def stop(self):
        self.timer.stop()


def _settingsKeys():
    return {str(key) for key in qt.QSettings().allKeys()}


class LesionNetworkMappingNormalizeUITest(unittest.TestCase):
    """Map card (Classic), controller entry points, Stop, reuse, review, edits, Draw card (Studio) and settings."""

    @classmethod
    def setUpClass(cls):
        import numpy as np

        import lnm_normalize_fixture as fixture
        from LesionNetworkMappingLib import nifti
        from LesionNetworkMappingUI import normalize_controller as nc

        cls.nc = nc
        cls.tmp = tempfile.mkdtemp(prefix="lnm-normalize-ui-")
        cls.dataDir = os.path.join(cls.tmp, "data")
        cls._dataDirectory = nc.dataDirectory
        nc.dataDirectory = lambda: cls.dataDir  # never Documents (and never the setting)
        cls.keysBefore = _settingsKeys()
        slicer.mrmlScene.Clear(0)
        subject = fixture.make_subject(os.path.join(cls.tmp, "native"), "sub-01", lesions=("large",))
        fraction, affine, _header = nifti.read_nifti(subject["lesion"])
        maskPath = os.path.join(cls.tmp, "native", "sub-01_lesion.nii.gz")
        nifti.write_nifti(maskPath, (np.asarray(fraction) >= 0.5).astype(np.uint8), affine, sform_code=1, qform_code=1)
        cls.scan = slicer.util.loadVolume(subject["scan"])
        cls.native = slicer.util.loadSegmentation(maskPath)
        cls.native.SetName("sub-01 lesion")
        cls.native.SetReferenceImageGeometryParameterFromVolumeNode(cls.scan)
        segmentation = cls.native.GetSegmentation()
        cls.nativeSegmentId = segmentation.GetNthSegmentID(0)
        segmentation.GetSegment(cls.nativeSegmentId).SetName("Lesion")
        cls.nativeOriginal = slicer.util.arrayFromSegmentBinaryLabelmap(cls.native, cls.nativeSegmentId, cls.scan).copy()
        cls.nativeVoxels = int(np.count_nonzero(cls.nativeOriginal))
        # A template-space segmentation (on the bundled 2 mm brain)
        cls.template = slicer.util.loadVolume(nc.templateFile(TEMPLATE, "fixed"), {"name": "template 2mm"})
        cls.mniSegmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "MNI lesion")
        cls.mniSegmentation.CreateDefaultDisplayNodes()
        cls.mniSegmentation.SetReferenceImageGeometryParameterFromVolumeNode(cls.template)
        cls.mniSegmentId = cls.mniSegmentation.GetSegmentation().AddEmptySegment("Lesion", "Lesion")
        slicer.util.selectModule("LesionNetworkMapping")
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.ctx = cls.widget.ctx
        cls.card = cls.ctx.card("normalize")
        cls.result = None

    @classmethod
    def tearDownClass(cls):
        cls.nc.dataDirectory = cls._dataDirectory
        try:
            from StudioLib import shell

            if shell._shell is not None:
                shell._shell.dispose()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _cardVisible(self):
        _pump(50)
        return not self.card.widget.isHidden()

    def _segmentationNodes(self):
        return {node.GetID() for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")}

    def _volumeNodes(self):
        return {node.GetID() for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")}

    def _setOptions(self, **options):
        self.card.options.update(options)
        self.card.refreshState()

    def _runAndWait(self, timeoutS=120.0, outDir=None, **changes):
        controller = self.card.controller
        done = []
        controller.resultReady.append(done.append)
        failed = []
        controller.failed.append(failed.append)
        try:
            if outDir is not None:
                self.card.controller.start(self.card.request(outDir=outDir, **changes))
            else:
                self.assertTrue(self.card.normalizeButton.enabled, self.card.reasonLabel.text)
                self.card.normalizeButton.click()
            finished = _wait(lambda: done or failed or controller.state in ("cancelled", "failed"), timeoutS)
        finally:
            controller.resultReady.remove(done.append)
            controller.failed.remove(failed.append)
        self.assertTrue(finished, f"no result after {timeoutS} s (state {controller.state})")
        self.assertFalse(failed, f"the normalization failed: {failed[0]['message'] if failed else ''}")
        return done[0]

    #
    # Entry points
    #

    def test_01_entry_points(self):
        """canNormalize() answers without starting anything; offerNormalize() never starts a job; no download under testing."""
        nc = self.nc
        self.assertTrue(nc.canNormalize())
        answer = nc.canNormalize(self.native, self.nativeSegmentId)
        self.assertTrue(answer, answer[1])
        ok, reason = nc.canNormalize(self.mniSegmentation, self.mniSegmentId)
        self.assertFalse(ok)
        self.assertTrue(reason)
        self.assertIs(nc.findSourceVolume(self.native), self.scan)
        runner = self.ctx.runner
        jobsBefore = len(runner.recentJobs())
        self.assertTrue(nc.offerNormalize(self.ctx, self.native, self.nativeSegmentId))
        self.assertTrue(nc.offerNormalize(self.native, self.nativeSegmentId))  # Draw's form, no shell: the Map card
        _pump(200)
        self.assertEqual(len(runner.recentJobs()), jobsBefore)
        self.assertEqual(self.card.controller.state, "idle")
        self.assertIs(self.card.scanSelector.currentNode(), self.scan)
        messages = []
        self.assertIsNone(nc.downloadTemplateFiles(TEMPLATE, ("head",), onFailed=messages.append))
        self.assertTrue(messages and "testing" in messages[0])

    #
    # Map card (Classic: no Studio shell under --testing)
    #

    def test_02_map_card_visibility(self):
        """The card shows for a native seed and hides for a template-space seed."""
        self.ctx.setSeed(self.mniSegmentation, self.mniSegmentId)
        self.assertFalse(self._cardVisible())
        self.assertFalse(self.card.isApplicable())
        self.ctx.setSeed(self.native, self.nativeSegmentId)
        self.assertTrue(self._cardVisible())
        self.assertIs(self.card.scanSelector.currentNode(), self.scan)
        self.assertTrue(self.card.normalizeButton.enabled, self.card.reasonLabel.text)
        # The Map widget shows every card of the seed mode; the card corrects that for a template seed
        self.ctx.setSeed(self.mniSegmentation, self.mniSegmentId)
        self.widget.updateCardVisibility()
        self.assertFalse(self._cardVisible())
        self.ctx.setSeed(self.native, self.nativeSegmentId)
        self.assertTrue(self._cardVisible())

    def test_03_map_normalize_affine(self):
        """One click: a template-space seed on the 1 mm grid, the native lesion untouched, QC shown, GUI responsive."""
        import numpy as np

        nc = self.nc
        self.ctx.setSeed(self.native, self.nativeSegmentId)
        self._setOptions(quality="affine")
        segmentationsBefore = self._segmentationNodes()
        probe = _GapProbe()
        loadingAt = []
        recordLoading = lambda state: state == "loading" and loadingAt.append(time.perf_counter())  # noqa: E731
        self.card.controller.stateChanged.append(recordLoading)
        load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0  # before the job: is the host idle?
        probe.start()
        started = time.monotonic()
        try:
            result = self._runAndWait(timeoutS=90.0)
            seconds = time.monotonic() - started
            _pump(1000)
        finally:
            probe.stop()
            self.card.controller.stateChanged.remove(recordLoading)
        timings = dict(self.card.controller.timings)
        runGap = probe.maxGapMs(until=loadingAt[0] if loadingAt else None)
        resultGap = probe.maxGapMs(since=loadingAt[0]) if loadingAt else 0.0
        print(f"normalize (affine): {seconds:.1f} s, max GUI gap during the run {runGap:.1f} ms, while loading and showing the result {resultGap:.1f} ms "
              f"({len(probe.gaps)} ticks), load {load:.2f}, timings {timings}")
        self.__class__.result = result
        self.assertLessEqual(runGap, MAX_GAP_IDLE_MS if load < 1.0 else MAX_GAP_BUSY_MS)
        # start() only builds the argument list and queues a worker (about 10 ms on an idle host); a busy host (load 2.3) took 70 ms once, so it gets the same looser bound as the other timings
        self.assertLessEqual(timings.get("start_ms", 0.0), 60.0 if load < 1.0 else 200.0)
        self.assertLessEqual(timings.get("nodes_ms", 0.0), 50.0 if load < 1.0 else 100.0)
        self.assertLessEqual(timings.get("progress_max_ms", 0.0), 2.0 if load < 1.0 else 10.0)
        # A new segmentation on the 2009c 1 mm grid is the seed
        segmentation = result.segmentationNode
        self.assertNotIn(segmentation.GetID(), segmentationsBefore)
        self.assertEqual(self.ctx.seed(), (segmentation, "Lesion"))
        from StudioLib import space

        self.assertEqual(space.tableSpace(*nc._gridOf(segmentation)), (TEMPLATE, 1))
        self.assertTrue(space.isMni(segmentation))
        for node in (segmentation, result.volumeNode):
            self.assertEqual(node.GetAttribute(nc.SPACE_ATTRIBUTE), TEMPLATE)
            # A portable reference, never the absolute path (SPEC 9.7 rules 2 and 10)
            self.assertEqual(node.GetAttribute(nc.NORMALIZATION_ATTRIBUTE), f"normalization/{os.path.basename(result.outDir)}/normalization.json")
            self.assertEqual(os.path.normpath(nc.resolveReference(node.GetAttribute(nc.NORMALIZATION_ATTRIBUTE))), os.path.normpath(result.provenancePath))
            self.assertIn(node.GetAttribute(nc.SUGGESTION_ATTRIBUTE), ("looks-ok", "review"))
        self.assertEqual(segmentation.GetAttribute(nc.NORMALIZED_FROM_ATTRIBUTE), f"{self.native.GetID()}|{self.nativeSegmentId}")
        self.assertFalse(result.volumeNode.GetDisplayNode().GetAutoWindowLevel())
        mniVoxels = int(np.count_nonzero(slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, "Lesion", result.volumeNode)))
        self.assertGreater(mniVoxels, 1000)
        # The native lesion is untouched and the card shows the QC panel (it stays visible for its result)
        self.assertEqual(int(np.count_nonzero(slicer.util.arrayFromSegmentBinaryLabelmap(self.native, self.nativeSegmentId, self.scan))), self.nativeVoxels)
        self.assertFalse(nc.isNormalizationResult(self.native))
        self.assertTrue(self._cardVisible())
        self.assertIsNotNone(self.card.widget.findChild("QWidget", "LNMNormalizeQC"))
        self.assertTrue(self.ctx.computeEnabled())
        self.assertFalse(nc.canNormalize(segmentation, "Lesion"))
        # The shared output folder holds no file with absolute local paths (the job log lives in a private folder)
        checker = os.path.join(TEST_DIR, *[".."] * 5, "Utilities", "Scripts", "StudioPrivacyCheck.py")
        if os.path.isfile(checker):
            import importlib.util

            spec = importlib.util.spec_from_file_location("StudioPrivacyCheck", checker)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            lines = []
            self.assertEqual(module.scan([result.outDir], False, lines.append), 0, "\n".join(lines))

    def test_03b_scene_holds_no_absolute_path(self):
        """A saved scene (project, .mrb) holds no absolute local path in the normalization attributes; earlier absolute values are replaced."""
        import re

        nc = self.nc
        self.assertIsNotNone(self.result)

        def sceneXml():
            scene = slicer.mrmlScene
            scene.InvokeEvent(slicer.vtkMRMLScene.StartSaveEvent)  # as File > Save and .mrb writing do: the scene migration runs
            scene.SetSaveToXMLString(1)
            try:
                scene.Commit()
                return scene.GetSceneXMLString()
            finally:
                scene.SetSaveToXMLString(0)

        def lnmValues(xml):
            return re.findall(r"LNM\.(?:Normalization|NormLesionWarp):([^;\"]*)", xml)

        xml = sceneXml()
        values = lnmValues(xml)
        self.assertTrue(values, "no LNM.Normalization attribute in the scene")
        for value in values:
            self.assertFalse(value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value), f"absolute path in a scene attribute: {value}")
        self.assertNotIn(self.tmp, "".join(values))
        # A scene of an earlier version (absolute values) is migrated before it is saved again
        volume = self.result.volumeNode
        reference = volume.GetAttribute(nc.NORMALIZATION_ATTRIBUTE)
        volume.SetAttribute(nc.NORMALIZATION_ATTRIBUTE, self.result.provenancePath)
        volume.SetAttribute(nc.LESION_WARP_ATTRIBUTE, os.path.join(self.result.outDir, "warps", "sub-01_edit-1234_desc-lesion_mask.nii.gz"))
        try:
            for value in lnmValues(sceneXml()):
                self.assertNotIn(self.tmp, value)
            self.assertEqual(volume.GetAttribute(nc.NORMALIZATION_ATTRIBUTE), reference)
            self.assertEqual(volume.GetAttribute(nc.LESION_WARP_ATTRIBUTE), f"normalization/{os.path.basename(self.result.outDir)}/warps/sub-01_edit-1234_desc-lesion_mask.nii.gz")
        finally:
            volume.RemoveAttribute(nc.LESION_WARP_ATTRIBUTE)
            volume.SetAttribute(nc.NORMALIZATION_ATTRIBUTE, reference)
        # References of other computers and of studies
        self.assertEqual(nc.portableReference("/Users/a/Studies/s1/derivatives/normalization/sub-02/normalization.json"), "derivatives/normalization/sub-02/normalization.json")
        self.assertEqual(nc.portableReference("C:\\Users\\a\\Documents\\Slicer Studio\\LNM\\normalization\\sub-02-0a1b2c3d\\normalization.json"), "normalization/sub-02-0a1b2c3d/normalization.json")
        self.assertEqual(nc.portableReference("/data/out/normalization.json"), "out/normalization.json")
        self.assertEqual(nc.resolveReference("derivatives/normalization/sub-02/normalization.json", "/study"), os.path.join("/study", "derivatives", "normalization", "sub-02", "normalization.json"))

    def test_04_rerun_reused(self):
        """Normalizing the same lesion again reuses the result (no new nodes) within 2 s."""
        self.assertIsNotNone(self.result)
        self.ctx.setSeed(self.native, self.nativeSegmentId)
        self.assertTrue(self._cardVisible())
        nodesBefore = (self._segmentationNodes(), self._volumeNodes())
        started = time.monotonic()
        result = self._runAndWait(timeoutS=30.0)
        seconds = time.monotonic() - started
        print(f"rerun (reused): {seconds:.2f} s")
        self.assertEqual(result.status, "reused")
        self.assertLessEqual(seconds, 2.0 if (os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0) < 1.0 else 6.0)
        self.assertEqual((self._segmentationNodes(), self._volumeNodes()), nodesBefore)
        self.assertIs(result.segmentationNode, self.result.segmentationNode)

    def test_05_stop(self):
        """Stop during the registration: cancelled within 3 s, no new node."""
        self.ctx.setSeed(self.native, self.nativeSegmentId)
        self._setOptions(quality="standard")
        controller = self.card.controller
        nodesBefore = (self._segmentationNodes(), self._volumeNodes())
        previous = controller.result
        self.assertIsNotNone(previous)
        stages = []

        def onProgress(fraction, text, eta):
            stages.append(controller.stage)

        controller.progressChanged.append(onProgress)
        try:
            self.card.start(outDir=os.path.join(self.tmp, "stop-run"))
            self.assertTrue(_wait(lambda: controller.stage == "affine", 60.0), f"no affine stage (state {controller.state})")
            _pump(500)
            stopped = time.monotonic()
            self.card.progress.stopButton.click()
            self.assertTrue(_wait(lambda: controller.state == "cancelled", 10.0), controller.state)
            seconds = time.monotonic() - stopped
        finally:
            controller.progressChanged.remove(onProgress)
        print(f"stop: cancelled after {seconds:.2f} s")
        self.assertLessEqual(seconds, 3.0)
        self.assertEqual((self._segmentationNodes(), self._volumeNodes()), nodesBefore)
        # "Nothing was changed": the previous result is still the card's result, with its QC and its folder (and no edit)
        _pump(self.nc.EDIT_DEBOUNCE_MS + 700)
        self.assertIs(controller.result, previous)
        self.assertEqual(controller.outDir, previous.outDir)
        self.assertFalse(controller.edited)
        self.assertIsNotNone(self.card._qcPanel)
        self._setOptions(quality="affine")

    def test_06_review_restore(self):
        """Review in views changes the layers (template behind, scan at 50 %); Done reviewing restores them exactly."""
        controller = self.card.controller
        if controller.result is None:
            controller.result = self.result
        before = controller.compositeState()
        volumesBefore = self._volumeNodes()
        self.assertTrue(controller.reviewInViews(self.result))
        during = controller.compositeState()
        self.assertNotEqual(during, before)
        for _background, foreground, _label, opacity, _labelOpacity in during.values():
            self.assertEqual(foreground, self.result.volumeNode.GetID())
            self.assertAlmostEqual(opacity, 0.5)
        display = self.result.segmentationNode.GetDisplayNode()
        self.assertFalse(display.GetVisibility2DFill())
        self.assertTrue(display.GetVisibility2DOutline())
        self.assertTrue(controller.restoreViews())
        self.assertEqual(controller.compositeState(), before)
        self.assertEqual(self._volumeNodes(), volumesBefore)
        self.assertTrue(display.GetVisibility2DFill())

    def test_06b_review_uses_open_user_template(self):
        """The review shows the open volume of the user's template (no second copy), and names a copy it must read by the template name."""
        from StudioLib import importer, projects, templates

        controller = self.card.controller
        root = os.path.join(self.tmp, "studio-root")
        environment = os.environ.get(projects.ROOT_ENVIRONMENT)
        os.environ[projects.ROOT_ENVIRONMENT] = root
        opened = []
        volumesBefore = self._volumeNodes()
        try:
            template = templates.addTemplate(self.nc.templateFile(TEMPLATE, "fixed"), mask=self.nc.templateFile(TEMPLATE, "mask"),
                                             name="Lab review brain", space=TEMPLATE)
            self.assertTrue(template.id.startswith("user:"), template.id)
            node = importer.loadTemplateVolume(template)
            opened.append(node)
            self.assertEqual(node.GetAttribute("Studio.TemplateId"), template.id)
            volumesOpen = self._volumeNodes()
            found, created = controller._reviewTemplate(template.id)
            self.assertIs(found, node)
            self.assertFalse(created)
            self.assertEqual(self._volumeNodes(), volumesOpen)
            # Without the open volume the template file is read, named by the template name and never by its raw id
            slicer.mrmlScene.RemoveNode(node)
            opened.clear()
            found, created = controller._reviewTemplate(template.id)
            self.assertTrue(created)
            opened.append(found)
            self.assertEqual(found.GetName(), "Lab review brain template")
            self.assertNotIn("user:", found.GetName())
        finally:
            for node in opened:
                if slicer.mrmlScene.IsNodePresent(node):
                    controller._removeWithHelpers(node)
            for node in list(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")):
                if node.GetID() not in volumesBefore:
                    controller._removeWithHelpers(node)
            if environment is None:
                os.environ.pop(projects.ROOT_ENVIRONMENT, None)
            else:
                os.environ[projects.ROOT_ENVIRONMENT] = environment
            templates.registerGrids(force=True)
            shutil.rmtree(root, ignore_errors=True)

    def test_06c_done_toast_names_user_template(self):
        """The 'Lesion normalized' toast names the user's template by its name, never by its raw id (as the Draw card does)."""
        from StudioLib import projects, templates

        root = os.path.join(self.tmp, "studio-root-toast")
        environment = os.environ.get(projects.ROOT_ENVIRONMENT)
        os.environ[projects.ROOT_ENVIRONMENT] = root
        controller = self.card.controller
        resultBefore = controller.result

        class _Shell:
            toasts = []

            def toast(self, text, _ms=0):
                self.toasts.append(text)

        class _Result:
            segmentationNode = None
            nativeSegmentationNode = None
            segmentId = None

            def __init__(self, templateId):
                self.templateId = templateId

        fake = _Shell()
        try:
            template = templates.addTemplate(self.nc.templateFile(TEMPLATE, "fixed"), mask=self.nc.templateFile(TEMPLATE, "mask"),
                                             name="Lab toast brain", space=TEMPLATE)
            self.assertTrue(template.id.startswith("user:"), template.id)
            self.ctx.shell = lambda: fake
            for templateId, expected in ((template.id, "Lab toast brain"), (TEMPLATE, TEMPLATE)):
                result = _Result(templateId)
                controller.result = result
                self.card._useResult(result)
                controller.result = None  # the deferred showInViews must not run on the stand-in
                self.assertIn(expected, fake.toasts[-1])
                self.assertNotIn("user:", fake.toasts[-1])
            _pump(50)
        finally:
            self.ctx.__dict__.pop("shell", None)  # back to the class method
            controller.result = resultBefore
            if environment is None:
                os.environ.pop(projects.ROOT_ENVIRONMENT, None)
            else:
                os.environ[projects.ROOT_ENVIRONMENT] = environment
            templates.registerGrids(force=True)
            shutil.rmtree(root, ignore_errors=True)

    def test_07_edit_then_update(self):
        """An edit of the native lesion offers Update MNI lesion, which re-warps it within 5 s."""
        import numpy as np

        controller = self.card.controller
        self.assertIsNotNone(controller.result)
        segmentation = controller.result.segmentationNode
        before = int(np.count_nonzero(slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, "Lesion", controller.result.volumeNode)))
        self.__class__.mniVoxels = before
        # Erase the upper half of the native lesion (inside the original: an update, not a re-registration)
        array = slicer.util.arrayFromSegmentBinaryLabelmap(self.native, self.nativeSegmentId, self.scan)
        slices = np.nonzero(array.any(axis=(1, 2)))[0]
        array[slices[len(slices) // 2]:] = 0
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, self.native, self.nativeSegmentId, self.scan)
        self.assertTrue(_wait(lambda: controller.edited, 5.0))
        self.assertFalse(controller.needsReregister)
        self.assertIsNotNone(self.card.message)
        done = []
        started = time.monotonic()
        self.assertTrue(controller.updateMniLesion(done.append))
        self.assertTrue(_wait(lambda: done, 30.0))
        seconds = time.monotonic() - started
        print(f"update MNI lesion: {seconds:.2f} s")
        self.assertEqual(done, [True])
        self.assertLessEqual(seconds, 5.0 if (os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0) < 1.0 else 15.0)
        after = int(np.count_nonzero(slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, "Lesion", controller.result.volumeNode)))
        self.assertLess(after, before * 0.8)
        self.assertFalse(controller.edited)
        # The updated segmentation no longer holds the run's lesion: a reuse of the run must not return it
        self.assertNotEqual(segmentation.GetAttribute(self.nc.FINGERPRINT_ATTRIBUTE), controller.result.fingerprint or "")
        warp = segmentation.GetAttribute(self.nc.LESION_WARP_ATTRIBUTE) or ""
        self.assertFalse(os.path.isabs(warp), warp)
        self.assertTrue(os.path.isfile(self.nc.resolveReference(warp) or ""), warp)
        # An edit far outside the original lesion needs a new registration
        array = slicer.util.arrayFromSegmentBinaryLabelmap(self.native, self.nativeSegmentId, self.scan)
        array[5:9, 5:9, 5:9] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, self.native, self.nativeSegmentId, self.scan)
        self.assertTrue(_wait(lambda: controller.edited, 5.0))
        self.assertTrue(controller.needsReregister)

    def test_07b_revert_then_reuse(self):
        """After Update MNI lesion, reverting the native lesion and normalizing again gives the run's lesion, not the edited one."""
        import numpy as np

        controller = self.card.controller
        edited = controller.result.segmentationNode
        slicer.util.updateSegmentBinaryLabelmapFromArray(self.nativeOriginal, self.native, self.nativeSegmentId, self.scan)
        _pump(800)
        self.ctx.setSeed(self.native, self.nativeSegmentId)
        self.assertTrue(self._cardVisible())
        result = self._runAndWait(timeoutS=30.0)
        self.assertEqual(result.status, "reused")
        # The same MNI lesion node (the Map seed keeps following it) holds the run's lesion again, tagged with the run
        self.assertIs(result.segmentationNode, edited)
        self.assertEqual(edited.GetAttribute(self.nc.FINGERPRINT_ATTRIBUTE), result.fingerprint or "")
        self.assertFalse(edited.GetAttribute(self.nc.LESION_WARP_ATTRIBUTE))
        self.assertIs(result.volumeNode, self.result.volumeNode)  # the warped scan is unchanged and reused
        voxels = int(np.count_nonzero(slicer.util.arrayFromSegmentBinaryLabelmap(result.segmentationNode, "Lesion", result.volumeNode)))
        self.assertLessEqual(abs(voxels - self.mniVoxels), 0.02 * self.mniVoxels)
        self.assertFalse(controller.edited)

    def test_07c_reregister_replaces_previous_result(self):
        """A new registration of the edited lesion (Normalize again / Re-register) replaces the earlier MNI lesion and scan in place.

        Regression (bug bash BB-1): it added "Lesion (…)_1" and a second warped scan, and the Map seed chosen with
        Use for Map stayed on the outdated MNI lesion.
        """
        import numpy as np

        nc = self.nc
        controller = self.card.controller
        previous = controller.result
        self.assertIsNotNone(previous)
        segmentation, volume = previous.segmentationNode, previous.volumeNode
        self.ctx.setSeed(segmentation, "Lesion")  # what Use for Map does
        before = int(np.count_nonzero(slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, "Lesion", volume)))
        nodesBefore = (self._segmentationNodes(), self._volumeNodes())
        # An edit far outside the normalized lesion: a new registration (new fingerprint), not an update
        array = slicer.util.arrayFromSegmentBinaryLabelmap(self.native, self.nativeSegmentId, self.scan)
        nz = np.argwhere(array > 0)
        low = nz.min(axis=0)
        k, j, _i = (int(value) for value in nz.mean(axis=0))
        i0 = max(2, int(low[2]) - 14)
        array[k - 3:k + 3, j - 3:j + 3, i0:i0 + 6] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, self.native, self.nativeSegmentId, self.scan)
        self.assertTrue(_wait(lambda: controller.needsReregister, 5.0))
        done, failed = [], []
        controller.resultReady.append(done.append)
        controller.failed.append(failed.append)
        try:
            self.assertTrue(controller.start(previous.request.copy(quality="affine")))
            self.assertTrue(_wait(lambda: done or failed or controller.state in ("cancelled", "failed"), 90.0), controller.state)
        finally:
            controller.resultReady.remove(done.append)
            controller.failed.remove(failed.append)
        _pump(300)
        try:
            self.assertFalse(failed, failed[0]["message"] if failed else "")
            result = done[0]
            self.assertNotEqual(result.fingerprint, previous.fingerprint)
            # One MNI lesion and one warped scan per native lesion and template: the same nodes, no new ones
            self.assertIs(result.segmentationNode, segmentation)
            self.assertIs(result.volumeNode, volume)
            self.assertEqual((self._segmentationNodes(), self._volumeNodes()), nodesBefore)
            for node in (segmentation, volume):
                self.assertEqual(node.GetAttribute(nc.FINGERPRINT_ATTRIBUTE), result.fingerprint or "")
                self.assertEqual(node.GetAttribute(nc.NORMALIZATION_ATTRIBUTE), result.reference)
            self.assertEqual(segmentation.GetAttribute(nc.NORMALIZED_FROM_ATTRIBUTE), f"{self.native.GetID()}|{self.nativeSegmentId}")
            after = int(np.count_nonzero(slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, "Lesion", volume)))
            self.assertGreater(after, before)  # the seed holds the new registration's lesion (with the added voxels)
            self.assertEqual(self.ctx.seed(), (segmentation, "Lesion"))
        finally:
            # Later tests use the original native lesion
            slicer.util.updateSegmentBinaryLabelmapFromArray(self.nativeOriginal, self.native, self.nativeSegmentId, self.scan)
            _pump(800)

    #
    # Draw card (Studio)
    #

    def test_08_draw_card(self):
        """In Studio's Draw the card shows for a native source only; Use for Map makes the MNI lesion the Map seed."""
        from StudioLib import draw, drawcard_normalize, shell

        self.assertIsNotNone(slicer.util.mainWindow(), "the Draw test needs the main window")
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        _pump(200)
        try:
            editor = slicer.modules.SegmentEditorWidget.editor if hasattr(slicer.modules, "SegmentEditorWidget") else None
            studioShell.switchTo("draw")
            _pump(300)
            editor = slicer.modules.SegmentEditorWidget.editor
            editor.setSegmentationNode(self.native)
            editor.setSourceVolumeNode(self.scan)
            draw.extension().refreshCards()
            _pump(500)
            card = drawcard_normalize.active()
            self.assertIsNotNone(card)
            self.assertFalse(card.widget.isHidden())
            self.assertLessEqual(card.widget.height, 56)  # compact idle state (laid out in the Draw inspector)
            self.assertTrue(self.nc.offerNormalize(self.native, self.nativeSegmentId, studioShell))
            self.assertEqual(card.controller.state, "idle")
            self.assertEqual(studioShell.currentWorkspace(), "draw")  # Draw's call to action stays in Draw
            # "Options in Map…" (the ⋯ menu and the failure banner) shows the Map card, not this Draw card again
            self.assertTrue(card.openInMap())
            _pump(300)
            self.assertEqual(studioShell.currentWorkspace(), "map")
            self.assertTrue(self._cardVisible())
            self.assertIs(self.card.scanSelector.currentNode(), self.scan)
            self.assertEqual(card.controller.state, "idle")  # no job is started
            studioShell.switchTo("draw")
            _pump(300)
            # A template source hides the card
            editor.setSegmentationNode(self.mniSegmentation)
            editor.setSourceVolumeNode(self.template)
            draw.extension().refreshCards()
            _pump(500)
            self.assertTrue(card.widget.isHidden())
            # A finished result (loaded from the Map run's folder): Use for Map
            editor.setSegmentationNode(self.native)
            editor.setSourceVolumeNode(self.scan)
            draw.extension().refreshCards()
            request = self.nc.NormalizeRequest(scanNode=self.scan, segmentationNode=self.native, segmentId=self.nativeSegmentId)
            nodesBefore = self._segmentationNodes()
            self.assertTrue(card.controller.loadResult(self.result.outDir, request))
            self.assertTrue(_wait(lambda: card.controller.state == "done", 30.0), card.controller.state)
            self.assertEqual(self._segmentationNodes(), nodesBefore)  # the Map run's nodes are reused
            _pump(400)
            self.assertFalse(card.widget.isHidden())
            self.assertFalse(card.doneRow.isHidden())
            self.ctx.setSeed(self.native, self.nativeSegmentId)
            card.useForMapButton.click()
            _pump(500)
            self.assertEqual(studioShell.currentWorkspace(), "map")
            self.assertEqual(self.ctx.seed(), (card.controller.result.segmentationNode, "Lesion"))
            # The explicit choice stands while Draw shows the same lesion; another lesion in Draw is adopted (W3-I1)
            studioShell.switchTo("draw")
            _pump(300)
            studioShell.switchTo("map")
            _pump(300)
            self.assertEqual(self.ctx.seed(), (card.controller.result.segmentationNode, "Lesion"))
            studioShell.switchTo("draw")
            _pump(300)
            editor.setSegmentationNode(self.mniSegmentation)
            studioShell.switchTo("map")
            _pump(300)
            self.assertEqual(self.ctx.seed(), (self.mniSegmentation, self.mniSegmentId))
            # Back in Draw: trace on the MNI scan
            studioShell.switchTo("draw")
            _pump(300)
            editor.setSegmentationNode(self.native)
            editor.setSourceVolumeNode(self.scan)
            draw.extension().refreshCards()
            _pump(300)
            self.assertTrue(card.traceOnMni())
            self.assertIs(editor.sourceVolumeNode(), card.controller.result.volumeNode)
            self.assertIs(editor.segmentationNode(), card.controller.result.segmentationNode)
            # Normalize scan only (empty lesion): the done state offers Trace on the MNI scan, not Use for Map
            empty = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "empty lesion")
            empty.CreateDefaultDisplayNodes()
            empty.SetReferenceImageGeometryParameterFromVolumeNode(self.scan)
            emptyId = empty.GetSegmentation().AddEmptySegment("Lesion")
            editor.setSegmentationNode(empty)
            editor.setSourceVolumeNode(self.scan)
            editor.setCurrentSegmentID(emptyId)
            draw.extension().refreshCards()
            _pump(500)
            self.assertFalse(card.widget.isHidden())
            self.assertEqual(card.button.text, "Normalize scan only")
            controllerStart = card.controller.start
            card.controller.start = lambda request: controllerStart(request.copy(quality="affine"))  # shorter than Draw's Standard
            try:
                self.assertTrue(card.start())
                self.assertTrue(_wait(lambda: card.controller.state in ("done", "failed", "cancelled"), 90.0), card.controller.state)
            finally:
                del card.controller.start
            self.assertEqual(card.controller.state, "done", card.controller.failure)
            self.assertIsNone(card.controller.result.nativeSegmentationNode)
            _pump(500)
            self.assertFalse(card.doneRow.isHidden())
            self.assertIsNotNone(card._qcPanel)
            self.assertFalse(card._qcPanel.isHidden())
            self.assertFalse(card.useForMapButton.enabled)
            self.assertEqual(card.button.text, "Normalize again")
            self.assertTrue(card.traceOnMni())
            self.assertIs(editor.sourceVolumeNode(), card.controller.result.volumeNode)
            _pump(300)
            self.assertFalse(card.doneRow.isHidden())
            # The Map card is shown in Studio for a native seed too
            studioShell.switchTo("map")
            self.ctx.setSeed(self.native, self.nativeSegmentId)
            self.assertTrue(self._cardVisible())
        finally:
            studioShell.dispose()
            _pump(200)
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)

    def test_08a_draw_stopped_again_keeps_result(self):
        """Stopping a "Normalize again" run gives the Draw card its previous result back (QC, Use for Map, Trace on the MNI scan)."""
        from StudioLib import draw, drawcard_normalize, shell

        studioShell = shell.Shell(force=True)
        studioShell.apply()
        _pump(200)
        try:
            studioShell.switchTo("draw")
            _pump(300)
            editor = slicer.modules.SegmentEditorWidget.editor
            editor.setSegmentationNode(self.native)
            editor.setSourceVolumeNode(self.scan)
            editor.setCurrentSegmentID(self.nativeSegmentId)
            draw.extension().refreshCards()
            _pump(500)
            card = drawcard_normalize.active()
            self.assertIsNotNone(card)
            controller = card.controller
            request = self.nc.NormalizeRequest(scanNode=self.scan, segmentationNode=self.native, segmentId=self.nativeSegmentId)
            self.assertTrue(controller.loadResult(self.result.outDir, request))
            self.assertTrue(_wait(lambda: controller.state == "done", 30.0), controller.state)
            _pump(400)
            previous = controller.result
            self.assertFalse(card.doneRow.isHidden())
            self.assertIsNotNone(card._qcPanel)
            useForMapBefore = card.useForMapButton.enabled
            # Normalize again as a real run (not reused), then Stop during the registration
            controllerStart = controller.start
            controller.start = lambda request: controllerStart(request.copy(quality="standard", reuse=False, outDir=os.path.join(self.tmp, "draw-stop-run")))
            try:
                self.assertTrue(card.start())
                self.assertTrue(_wait(lambda: controller.stage == "affine", 60.0), f"no affine stage (state {controller.state})")
            finally:
                del controller.start
            self.assertIsNone(controller.result)  # while it runs
            card.progress.stopButton.click()
            self.assertTrue(_wait(lambda: controller.state == "cancelled", 10.0), controller.state)
            # The restored result compares the native lesion again (debounced): the untouched lesion of a loaded result is no edit
            _pump(self.nc.EDIT_DEBOUNCE_MS + 700)
            self.assertIs(controller.result, previous)
            self.assertEqual(controller.outDir, previous.outDir)
            self.assertFalse(controller.edited)
            self.assertFalse(card.doneRow.isHidden())
            self.assertEqual(card.useForMapButton.enabled, useForMapBefore)
            self.assertIsNotNone(card._qcPanel)
            self.assertFalse(card._qcPanel.isHidden())
            self.assertEqual(card.button.text, "Normalize again")
            self.assertIn("Normalized", card.label.text)
            self.assertIsNotNone(card.message)
            self.assertIn("Nothing was changed", card.message.findChild("QLabel", "LNMBannerText").text)
            self.assertTrue(card.traceOnMni())
            self.assertIs(editor.sourceVolumeNode(), previous.volumeNode)
        finally:
            studioShell.dispose()
            _pump(200)
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)

    def test_08b_draw_uses_remembered_options(self):
        """Draw's Normalize to MNI builds its request from the remembered Map options (the user's own template included)."""
        from StudioLib import draw, drawcard_normalize, shell

        nc = self.nc
        settings = qt.QSettings()
        originalAvailable = nc.templateAvailable
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        _pump(200)
        try:
            studioShell.switchTo("draw")
            _pump(300)
            editor = slicer.modules.SegmentEditorWidget.editor
            editor.setSegmentationNode(self.native)
            editor.setSourceVolumeNode(self.scan)
            editor.setCurrentSegmentID(self.nativeSegmentId)
            draw.extension().refreshCards()
            _pump(500)
            card = drawcard_normalize.active()
            self.assertIsNotNone(card)
            started = []
            card.controller.start = lambda request: started.append(request) or True
            try:
                # Nothing remembered: the defaults
                settings.remove(nc.LAST_OPTIONS_SETTING)
                self.assertTrue(card.start())
                self.assertEqual(started[-1].templateId, nc.DEFAULT_TEMPLATE)
                self.assertEqual(started[-1].quality, "standard")
                # Remembered user template (image and mask present), quality and margin
                settings.setValue(nc.LAST_OPTIONS_SETTING, json.dumps({"templateId": "user:lab-mni-1-5-mm", "quality": "affine", "modality": "t1", "dilateMm": 5}))
                nc.templateAvailable = lambda templateId: True
                self.assertTrue(card.start())
                self.assertEqual(started[-1].templateId, "user:lab-mni-1-5-mm")
                self.assertEqual(started[-1].quality, "affine")
                self.assertEqual(started[-1].modality, "t1")
                self.assertEqual(started[-1].dilateMm, 5.0)
                self.assertTrue(card.start(scanOnly=True))
                self.assertEqual(started[-1].templateId, "user:lab-mni-1-5-mm")
                # The remembered template is gone (or has no mask): the default template, the other options stay
                nc.templateAvailable = lambda templateId: not nc.isUserTemplate(templateId)
                self.assertTrue(card.start())
                self.assertEqual(started[-1].templateId, nc.DEFAULT_TEMPLATE)
                self.assertEqual(started[-1].quality, "affine")
                # "Automatic" (no template stored) is the default template here
                settings.setValue(nc.LAST_OPTIONS_SETTING, json.dumps({"templateId": None, "quality": "fine"}))
                self.assertTrue(card.start())
                self.assertEqual(started[-1].templateId, nc.DEFAULT_TEMPLATE)
                self.assertEqual(started[-1].quality, "fine")
            finally:
                nc.templateAvailable = originalAvailable
                settings.remove(nc.LAST_OPTIONS_SETTING)
                del card.controller.start
        finally:
            studioShell.dispose()
            _pump(200)
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)

    def test_08c_draw_result_belongs_to_its_segment(self):
        """With two lesions in one segmentation, the result of one is not shown (nor used for Map) for the other.

        Regression (bug bash BB-1): after "Lesion 2" was normalized, selecting "Lesion" still showed "Normalized" and
        Use for Map, which made the warped "Lesion 2" the Map seed; its MNI segment was named "Lesion" as well.
        """
        from StudioLib import draw, drawcard_normalize, shell

        nc = self.nc
        segmentation = self.native.GetSegmentation()
        secondId = segmentation.AddEmptySegment("", "Lesion 2")
        slicer.util.updateSegmentBinaryLabelmapFromArray(self.nativeOriginal, self.native, secondId, self.scan)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        _pump(200)
        made = []
        try:
            studioShell.switchTo("draw")
            _pump(300)
            editor = slicer.modules.SegmentEditorWidget.editor
            editor.setSegmentationNode(self.native)
            editor.setSourceVolumeNode(self.scan)
            editor.setCurrentSegmentID(self.nativeSegmentId)
            draw.extension().refreshCards()
            _pump(500)
            card = drawcard_normalize.active()
            self.assertIsNotNone(card)
            controller = card.controller
            request = nc.NormalizeRequest(scanNode=self.scan, segmentationNode=self.native, segmentId=self.nativeSegmentId)
            self.assertTrue(controller.loadResult(self.result.outDir, request))
            self.assertTrue(_wait(lambda: controller.state == "done", 30.0), controller.state)
            _pump(400)
            self.assertIn("Normalized", card.label.text)
            self.assertFalse(card.doneRow.isHidden())
            # The other segment is current (the card follows the editor by itself): it has no MNI copy
            editor.setCurrentSegmentID(secondId)
            _pump(drawcard_normalize.REFRESH_DELAY_MS + 300)
            self.assertEqual(card.label.text, "Native space · Map needs MNI")
            self.assertEqual(card.button.text, "Normalize to MNI")
            self.assertTrue(card.doneRow.isHidden())
            self.ctx.setSeed(self.native, secondId)
            self.assertFalse(card.useForMap())
            self.assertEqual(self.ctx.seed(), (self.native, secondId))
            self.assertEqual(studioShell.currentWorkspace(), "draw")
            # Back on the normalized segment: its result again
            editor.setCurrentSegmentID(self.nativeSegmentId)
            _pump(drawcard_normalize.REFRESH_DELAY_MS + 300)
            self.assertIn("Normalized", card.label.text)
            self.assertFalse(card.doneRow.isHidden())
            # The MNI lesion of "Lesion 2" is named after it (the segment ID stays "Lesion")
            loaded = {"outDir": os.path.join(self.tmp, "second-lesion"), "record": {"template": {"id": TEMPLATE}, "fingerprint": "second-lesion"}, "outputs": {}}
            before = self._segmentationNodes()
            second = controller.createNodes(loaded, "done", nc.NormalizeRequest(scanNode=self.scan, segmentationNode=self.native, segmentId=secondId))
            made = [slicer.mrmlScene.GetNodeByID(nodeId) for nodeId in self._segmentationNodes() - before]
            self.assertEqual(second.segmentId, nc.LESION_SEGMENT)
            self.assertEqual(second.segmentationNode.GetSegmentation().GetSegment(second.segmentId).GetName(), "Lesion 2")
            self.assertTrue(second.segmentationNode.GetName().startswith("Lesion 2 ("), second.segmentationNode.GetName())
            self.assertEqual(second.segmentationNode.GetAttribute(nc.NORMALIZED_FROM_ATTRIBUTE), f"{self.native.GetID()}|{secondId}")
        finally:
            for node in made:
                slicer.mrmlScene.RemoveNode(node)
            segmentation.RemoveSegment(secondId)
            studioShell.dispose()
            _pump(200)
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)

    def test_08d_draw_normalize_again_on_mni_scan(self):
        """After "Trace on the MNI scan", Normalize again redoes the native scan and lesion of the result (not the warped scan).

        Regression (bug bash BB-1): the request used the editor's MNI scan and MNI lesion, the engine refused it
        (already on the template grid) and the failed run dropped the card's result (QC, Use for Map, Trace on the MNI scan).
        """
        from StudioLib import draw, drawcard_normalize, shell

        studioShell = shell.Shell(force=True)
        studioShell.apply()
        _pump(200)
        try:
            studioShell.switchTo("draw")
            _pump(300)
            editor = slicer.modules.SegmentEditorWidget.editor
            editor.setSegmentationNode(self.native)
            editor.setSourceVolumeNode(self.scan)
            editor.setCurrentSegmentID(self.nativeSegmentId)
            draw.extension().refreshCards()
            _pump(500)
            card = drawcard_normalize.active()
            self.assertIsNotNone(card)
            controller = card.controller
            request = self.nc.NormalizeRequest(scanNode=self.scan, segmentationNode=self.native, segmentId=self.nativeSegmentId)
            self.assertTrue(controller.loadResult(self.result.outDir, request))
            self.assertTrue(_wait(lambda: controller.state == "done", 30.0), controller.state)
            _pump(400)
            previous = controller.result
            self.assertTrue(card.traceOnMni())
            self.assertIs(editor.sourceVolumeNode(), previous.volumeNode)
            self.assertIs(editor.segmentationNode(), previous.segmentationNode)
            self.assertEqual(card.button.text, "Normalize again")
            started = []
            controller.start = lambda request: started.append(request) or True
            try:
                self.assertTrue(card.start())
            finally:
                del controller.start
            self.assertEqual(len(started), 1)
            self.assertIs(started[0].scanNode, self.scan)
            self.assertIs(started[0].segmentationNode, self.native)
            self.assertEqual(started[0].segmentId, self.nativeSegmentId)
            # Nothing was started for real: the result and its card stay
            self.assertIs(controller.result, previous)
            self.assertFalse(card.doneRow.isHidden())
            # The native scan of the result is gone: no request, a readable message, the result stays
            originalRequest = previous.request
            gone = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "closed native scan")
            previous.request = originalRequest.copy(scanNode=gone)
            slicer.mrmlScene.RemoveNode(gone)
            started.clear()
            controller.start = lambda request: started.append(request) or True
            try:
                self.assertFalse(card.start())
            finally:
                del controller.start
                previous.request = originalRequest
            self.assertEqual(started, [])
            self.assertIn("no longer open", card.message.findChild("QLabel", "LNMBannerText").text)
            self.assertIs(controller.result, previous)
        finally:
            studioShell.dispose()
            _pump(200)
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)

    #
    # Settings
    #

    def test_09_settings(self):
        """Slicer.ini gains no normalization key unless "Remember these options" is ticked."""
        keys = _settingsKeys()
        self.assertFalse([key for key in keys - self.keysBefore if key.startswith("Studio/LNM")])
        self.card.showOptions()
        _pump(100)
        popover = self.card.optionsPopover
        self.assertEqual(popover.objectName, "LNMNormalizeOptions")
        self.assertFalse(self.card.headButton.isHidden() and not self.nc.headTemplateAvailable())
        self.card.marginSpinBox.setValue(4.0)
        self.assertNotIn(self.nc.LAST_OPTIONS_SETTING, _settingsKeys())
        self.card.rememberCheckBox.setChecked(True)
        self.assertIn(self.nc.LAST_OPTIONS_SETTING, _settingsKeys())
        self.card.rememberCheckBox.setChecked(False)
        self.assertNotIn(self.nc.LAST_OPTIONS_SETTING, _settingsKeys())
        popover.close()
        self.assertFalse([key for key in _settingsKeys() - self.keysBefore if key.startswith("Studio/LNM")])

    def test_09b_template_list_follows_user_templates(self):
        """Options > Template is rebuilt each time the popover opens: an added user template is offered, a removed one is not."""
        nc = self.nc
        original = nc.userTemplates
        shown = []

        def offered():
            popover = self.card.showOptions()
            _pump(50)
            texts = [self.card.templateCombo.itemText(i) for i in range(self.card.templateCombo.count)]
            shown.append(self.card.templateCombo.currentData)
            popover.close()
            return texts

        try:
            nc.userTemplates = lambda: [("user:first", "First (with mask)")]
            self.card.options["templateId"] = None
            self.assertTrue(any("First (with mask) (yours)" in text for text in offered()))
            nc.userTemplates = lambda: [("user:second", "Second (added later)")]
            texts = offered()
            self.assertTrue(any("Second (added later) (yours)" in text for text in texts), texts)
            self.assertFalse(any("First (with mask)" in text for text in texts), texts)
            self.assertEqual(sum("(yours)" in text for text in texts), 1, texts)
            # The chosen template stays selected across reopenings
            self.card.options["templateId"] = "user:second"
            offered()
            self.assertEqual(shown[-1], "user:second")
            nc.userTemplates = lambda: []
            texts = offered()
            self.assertFalse(any("(yours)" in text for text in texts), texts)
            self.assertEqual(shown[-1], "")  # a removed template falls back to Automatic
        finally:
            nc.userTemplates = original
            self.card.options["templateId"] = None

    #
    # Scene close (last: it clears the scene)
    #

    def test_10_scene_close_during_run(self):
        """Closing the scene while the job runs stops it, and nothing of that lesion reaches the new scene."""
        self.ctx.setSeed(self.native, self.nativeSegmentId)
        self.assertTrue(self._cardVisible())
        controller = self.card.controller
        self.assertTrue(controller.start(self.card.request(outDir=os.path.join(self.tmp, "close-run"), reuse=False)))
        self.assertTrue(_wait(lambda: controller.state == "running", 60.0), controller.state)
        _pump(1000)
        slicer.mrmlScene.Clear(0)
        self.assertTrue(_wait(lambda: not controller.isBusy(), 30.0), controller.state)
        _pump(500)
        self.assertEqual(controller.state, "cancelled")
        self.assertIsNone(controller.result)
        self.assertEqual(slicer.util.getNodesByClass("vtkMRMLSegmentationNode"), [])
        self.assertEqual(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"), [])
        # A finished (or stopped) card is reset by the next close
        slicer.mrmlScene.Clear(0)
        self.assertEqual(controller.state, "idle")
        self.assertIsNone(self.card._qcPanel)

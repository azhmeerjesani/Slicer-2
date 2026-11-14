"""Tests of the VLSM tab's minimum-overlap guard and of the progress row's failure line (BB-1).

A minimum overlap above the number of lesions holds Run VLSM with a reason that says what to change, a failed job's
reason stays on screen in the tab's reason label, and a failure line under a progress bar wraps (and is the tooltip)
instead of being cut mid-word. They need the Slicer application and use the synthetic study of
``lnm_pipeline_fixture.py`` (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingVlsmUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

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


def _pump(ms):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


class _FailedJob:
    """The parts of a failed ``jobrunner.Job`` that ``ProgressRow.update`` reads."""

    title = "VLSM"
    state = "failed"
    progress = 0.4
    stopRequested = False
    failureReason = "The VLSM is blocked: No voxel is lesioned in at least 30 of the 25 lesions (30 lesions): lower the minimum overlap, or add lesions"

    def isQueued(self):
        return False

    def isRunning(self):
        return False

    def isFinished(self):
        return True

    def succeeded(self):
        return False


class LesionNetworkMappingVlsmUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import lnm_pipeline_fixture as fixture

        from LesionNetworkMapping import LesionNetworkMappingLogic
        from LesionNetworkMappingUI import card_pipeline

        cls.tmp = tempfile.mkdtemp(prefix="lnm vlsm ui ")
        cls.fx = fixture.make_fixture(os.path.join(cls.tmp, "f1"), extra_rows=False)
        slicer.util.selectModule("LesionNetworkMapping")
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.bundle = LesionNetworkMappingLogic.registerConnectome(cls.fx["bundle"])
        LesionNetworkMappingLogic.setTermsAccepted(cls.bundle, True)
        cls.widget.refreshConnectomes(select=cls.bundle.path)
        cls.widget.openCohortStudy(cls.fx["study"])
        card_pipeline.setCohortSeed(cls.widget)
        cls.vlsm = cls.widget.ctx.card("cohort").tab("vlsm")
        _pump(100)

    @classmethod
    def tearDownClass(cls):
        try:
            from StudioLib import study_ui

            study_ui.closeStudy()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_minimum_overlap_above_the_lesions_holds_run(self):
        tab = self.vlsm
        tab.refresh()
        _pump(50)
        n = tab.design.n
        self.assertGreater(n, 0)
        tab.minLesionsSpinBox.setValue(n + 5)
        tab.refresh()
        _pump(50)
        enabled, reason = tab.runState()
        self.assertFalse(enabled, "Run VLSM is enabled with a minimum overlap above the number of lesions")
        self.assertIn("minimum overlap", reason.lower())
        self.assertFalse(tab.runButton.enabled)
        self.assertIsNone(tab.run())
        self.assertIn(f"≥ {n + 5} of {n} lesions", str(tab.minOverlapLabel.text))
        tab.minLesionsSpinBox.setValue(1)
        tab.refresh()
        _pump(50)
        self.assertEqual(tab.blockingSettings(), [])

    def test_failure_line_wraps(self):
        from LesionNetworkMappingUI import widgets

        # The row lives in a host that Python owns and deletes at once at the end (``del host``). Qt must not delete the
        # row's widget while Python owns it: Stop is connected to a bound method of the row, so deleting the Stop button
        # frees the row and its Python-owned ``widget`` wrapper, which would delete the widget a second time while it is
        # being destroyed (a deferred delete of it ran at the application's quit and crashed the process).
        host = qt.QWidget()
        host.setObjectName("LNMTestFailHost")
        qt.QVBoxLayout(host).setContentsMargins(0, 0, 0, 0)
        row = widgets.progressRow(objectNamePrefix="LNMTestFail")
        host.layout().addWidget(row.widget)
        row.label.setWordWrap(False)  # the Cohort tabs' choice for a running job's line
        row.label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        host.setFixedWidth(340)
        host.show()
        row.setJob(_FailedJob())
        _pump(50)
        text = str(row.label.text)
        self.assertTrue(text.startswith("Failed: The VLSM is blocked"), text)
        self.assertTrue(row.label.wordWrap)
        self.assertIn(text, str(row.label.toolTip))  # the style may wrap a tooltip in <p>
        self.assertGreater(row.label.height, row.label.fontMetrics().height() + 4, "the failure line is on one line")
        row.reset()
        self.assertFalse(row.label.wordWrap, "the owner's choice is not restored after the failure")
        self.assertEqual(str(row.label.toolTip), "")
        row.setJob(None)
        host.hide()
        del row, host  # the host is Python-owned and parentless: PythonQt deletes it, and the row with it, now
        _pump(50)

    def test_failed_job_reason_stays_on_screen(self):
        tab = self.vlsm
        tab.minLesionsSpinBox.setValue(1)
        tab.refresh()
        _pump(50)
        self.assertTrue(tab.runState()[0])  # Run is allowed, so showState() alone leaves nothing in the reason label
        tab.onDone(_FailedJob())
        _pump(50)
        text = str(tab.reasonLabel.text)
        self.assertIn("VLSM failed: The VLSM is blocked", text)
        self.assertIn("lower the minimum overlap", text)
        self.assertTrue(tab.reasonLabel.wordWrap)
        self.assertTrue(tab.reasonLabel.isVisibleTo(tab.widget), "the failure reason is hidden by the run state")
        tab.showState()
        _pump(50)
        self.assertEqual(str(tab.reasonLabel.text), "")

    def test_result_names_the_subjects_not_analysed(self):
        # A lesion off the template grid and a listwise exclusion leave the result's n: the summary says so (BB-1)
        tab = self.vlsm
        record = {"test": "continuous", "n": 20, "contrast": {"spec": "two_sided", "columns": ["intercept", "score"]},
                  "excluded": {"outside the template2mm grid": ["sub-04"], "missing score": ["sub-03", "sub-06", "sub-10"]}, "n_excluded": 4,
                  "checks": [{"level": "info", "code": "lesions_not_loaded", "message": "1 subject(s) are not analysed"}]}
        tab.showResult(self.tmp, record)
        _pump(50)
        text = str(tab.summaryLabel.text)
        self.assertIn("n = 20", text)
        self.assertIn("4 subject(s) not analysed", text)
        self.assertIn("outside the template2mm grid: 1 (sub-04)", text)
        self.assertIn("missing score: 3 (sub-03, sub-06, sub-10)", text)
        record["excluded"], record["n_excluded"] = {}, 0
        tab.showResult(self.tmp, record)
        _pump(50)
        self.assertNotIn("not analysed", str(tab.summaryLabel.text))

    def test_result_states_the_fdr_floor(self):
        # "FDR q ≤ 0.05: 0 voxels" next to FWE-significant voxels: the summary states the FDR floor, as the Contrast tab
        # does (BB-1: vlsm.json's fdr note was not shown)
        tab = self.vlsm
        note = ("The smallest attainable p is 1/(K+1) = 0.001 (K = 999); BH-FDR reaches q ≤ 0.05 only when at least 259 voxels reach it. "
                "Use 10,000 permutations, or Export for PALM (tail acceleration), for smaller p-values.")
        record = {"test": "continuous", "n": 30, "contrast": {"spec": "two_sided", "columns": ["intercept", "score"]},
                  "min_attainable_p": 0.001, "fwe": {"n_significant": 13, "min_p": 0.002},
                  "fdr": {"enabled": True, "n_significant": 0, "floor_voxels": 259, "voxels_at_floor": 40, "note": note}}
        tab.showResult(self.tmp, record)
        _pump(50)
        text = str(tab.summaryLabel.text)
        self.assertIn("FDR q ≤ 0.05: 0 voxels", text)
        self.assertIn("only when at least 259 voxels reach it", text)
        record["fdr"]["enabled"] = False
        tab.showResult(self.tmp, record)
        _pump(50)
        self.assertNotIn("259 voxels", str(tab.summaryLabel.text))

    def test_other_study_takes_the_previous_map_out_of_the_views(self):
        # BB-1: after a VLSM of study 1, "Other study…" hid the tab's result but left study 1's VLSM map in every slice
        # view, where it could be read as study 2's result. The map of the previous scope now leaves the views.
        import json

        import numpy as np
        from LesionNetworkMappingLib import nifti
        from LesionNetworkMappingUI import card_pipeline, cohort_contrast

        tab, widget = self.vlsm, self.widget
        card = widget.ctx.card("cohort")
        study2 = os.path.join(self.tmp, "study two")
        shutil.copytree(self.fx["study"], study2)
        with open(os.path.join(study2, "study.json"), encoding="utf-8") as f:
            record = json.load(f)
        record["name"] = "study two"
        with open(os.path.join(study2, "study.json"), "w", encoding="utf-8") as f:
            json.dump(record, f)
        path = os.path.join(self.tmp, "vlsm_desc-tstat_map.nii.gz")
        data = np.zeros(self.fx["fine_shape"], dtype=np.float32)
        data[2:6, 2:6, 2:6] = 4.0
        nifti.write_nifti(path, data, self.fx["fine_affine"])

        def foregrounds():
            return {node.GetForegroundVolumeID() for node in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode")}

        tab.refresh()
        _pump(50)
        storedOutputs = tab.outputs
        try:
            tab.outputs = [("vlsmTestTstat", "Statistic (T)", path)]
            volume = tab.showMap(0)
            _pump(100)
            self.assertIsNotNone(volume)
            volumeId = volume.GetID()
            self.assertIn(volumeId, foregrounds(), "the VLSM map is not shown")
            # The same study again (a refresh, another control) keeps the map in the views
            tab.refresh()
            card.refresh()
            _pump(50)
            self.assertIn(volumeId, foregrounds(), "a refresh of the same study took the map out of the views")
            widget.openCohortStudy(study2)
            _pump(300)
            tab.refresh()
            _pump(100)
            self.assertIsNone(tab.result)
            self.assertNotIn(volumeId, foregrounds(), "study 1's VLSM map is still shown with study 2 open")
        finally:
            tab.outputs = storedOutputs
            widget.openCohortStudy(self.fx["study"])
            card_pipeline.setCohortSeed(widget)
            for node in list(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")):
                if node.GetAttribute(cohort_contrast.GROUP_VOLUME_ATTRIBUTE) == "1":
                    slicer.mrmlScene.RemoveNode(node)
            card.groupVolumeId = None
            tab.refresh()
            _pump(100)
            shutil.rmtree(study2, ignore_errors=True)

    def test_map_space_is_the_study_grid_not_the_connectome(self):
        # BB-1: a VLSM map got Studio.Space from the selected connectome (connectome.json "space"), although VLSM runs on
        # the study's lesions on the study's template grid; its NIfTI export then declared the connectome's space. The
        # map now carries the space vlsm.json records (or none, so that the grid is recognised from the file).
        from unittest import mock

        import numpy as np
        from LesionNetworkMappingLib import nifti
        from LesionNetworkMappingUI import cohort_contrast

        tab, widget = self.vlsm, self.widget
        card = widget.ctx.card("cohort")
        contrast = card.tab("contrast")
        path = os.path.join(self.tmp, "vlsm_space_tstat_map.nii.gz")
        data = np.zeros(self.fx["fine_shape"], dtype=np.float32)
        data[2:6, 2:6, 2:6] = 4.0
        nifti.write_nifti(path, data, self.fx["fine_affine"])
        tab.refresh()
        _pump(50)
        stored = (tab.outputs, tab.result, contrast.outputs, contrast.result)
        try:
            with mock.patch.object(widget.logic, "connectomeSpaceName", return_value="MNI152NLin6Asym"):
                tab.outputs = [("vlsmTestTstat", "Statistic (T)", path)]
                tab.result = (self.tmp, {"status": "done", "space": "MNI152NLin2009cAsym"})
                volume = tab.showMap(0)
                self.assertIsNotNone(volume)
                self.assertEqual(volume.GetAttribute("Studio.Space"), "MNI152NLin2009cAsym")
                # A space that is no template space leaves none on the reused volume
                tab.result = (self.tmp, {"status": "done", "space": "native"})
                volume = tab.showMap(0)
                self.assertIsNone(volume.GetAttribute("Studio.Space"))
                # The Contrast tab's maps are on the connectome's grid: they keep its space
                contrast.outputs = [("contrastTestTstat", "Statistic (T)", path)]
                contrast.result = (self.tmp, {"status": "done"})
                volume = contrast.showMap(0)
                self.assertEqual(volume.GetAttribute("Studio.Space"), "MNI152NLin6Asym")
        finally:
            tab.outputs, tab.result, contrast.outputs, contrast.result = stored
            for node in list(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")):
                if node.GetAttribute(cohort_contrast.GROUP_VOLUME_ATTRIBUTE) == "1":
                    slicer.mrmlScene.RemoveNode(node)
            card.groupVolumeId = None
            tab.refresh()
            _pump(100)


class LesionNetworkMappingSingleLevelUITest(unittest.TestCase):
    """A cases-only study has one group level: the Contrast and VLSM tabs do not offer "case vs case"."""

    @classmethod
    def setUpClass(cls):
        import lnm_pipeline_fixture as fixture

        from LesionNetworkMapping import LesionNetworkMappingLogic
        from LesionNetworkMappingUI import card_pipeline

        cls.tmp = tempfile.mkdtemp(prefix="lnm single level ui ")
        cls.fx = fixture.make_fixture(os.path.join(cls.tmp, "f1"), n_cases=12, n_controls=0, extra_rows=False)
        slicer.util.selectModule("LesionNetworkMapping")
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.bundle = LesionNetworkMappingLogic.registerConnectome(cls.fx["bundle"])
        LesionNetworkMappingLogic.setTermsAccepted(cls.bundle, True)
        cls.widget.refreshConnectomes(select=cls.bundle.path)
        cls.widget.openCohortStudy(cls.fx["study"])
        card_pipeline.setCohortSeed(cls.widget)
        cls.card = cls.widget.ctx.card("cohort")
        _pump(100)

    @classmethod
    def tearDownClass(cls):
        try:
            from StudioLib import study_ui

            study_ui.closeStudy()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _check(self, tab):
        tab.refresh()
        _pump(100)
        a, b = str(tab.levelACombo.currentText), str(tab.levelBCombo.currentText)
        self.assertNotEqual(a, b, "the Levels combos read the same level twice")
        self.assertEqual(tab.levelBCombo.count, 0)
        self.assertFalse(tab.levelACombo.enabled)
        self.assertFalse(tab.levelBCombo.enabled)
        self.assertEqual([str(tab.contrastGroup.button(i).text) for i in (0, 1)], ["A > B", "B > A"], "a button reads 'case > case'")
        self.assertFalse(tab.contrastGroup.button(0).enabled)
        self.assertFalse(tab.contrastGroup.button(1).enabled)
        self.assertTrue(tab.contrastGroup.button(2).enabled)  # the other contrasts stay as they are
        enabled, reason = tab.runState()
        self.assertFalse(enabled)
        checks = str(tab.checksLabel.text)
        self.assertIn("1 level(s)", checks)  # the blocking reason is the red line under the controls ...
        self.assertNotIn("1 level(s)", reason)  # ... and is not repeated under Run
        self.assertIn("design checks", reason)
        self.assertEqual(str(tab.reasonLabel.text), reason)

    def test_contrast_tab(self):
        self._check(self.card.tab("contrast"))

    def test_vlsm_tab(self):
        self._check(self.card.tab("vlsm"))


if __name__ == "__main__":
    unittest.main()

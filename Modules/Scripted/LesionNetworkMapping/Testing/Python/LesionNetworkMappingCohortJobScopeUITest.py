"""A Cohort tab's job that ends after another study was opened is not shown as the open study's result (BB-1).

Study A runs a Contrast (GLM) or VLSM job; study B is opened in the Cohort seed while it runs. When A's job ends, the
tab (the same object for both studies) must not show A's result, load A's map or say "the maps are in the Cohort card":
the result stays in A's folder, and the tab shows it again when A is opened. It needs the Slicer application and uses
two synthetic studies of ``lnm_pipeline_fixture.py`` (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCohortJobScopeUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

try:
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


def _waitFor(condition, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        slicer.app.processEvents()
        time.sleep(0.02)
    return condition()


def _under(path, folder):
    path, folder = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(folder))
    return path == folder or path.startswith(folder + os.sep)


class LesionNetworkMappingCohortJobScopeUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import lnm_pipeline_fixture as fixture

        from LesionNetworkMapping import LesionNetworkMappingLogic

        cls.tmp = tempfile.mkdtemp(prefix="lnm job scope ")
        cls.fxA = fixture.make_fixture(os.path.join(cls.tmp, "a"), extra_rows=False, study_name="Study A")
        cls.fxB = fixture.make_fixture(os.path.join(cls.tmp, "b"), extra_rows=False, study_name="Study B")
        slicer.util.selectModule("LesionNetworkMapping")
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.bundle = LesionNetworkMappingLogic.registerConnectome(cls.fxA["bundle"])
        LesionNetworkMappingLogic.setTermsAccepted(cls.bundle, True)
        cls.widget.refreshConnectomes(select=cls.bundle.path)

    @classmethod
    def tearDownClass(cls):
        try:
            from StudioLib import study_ui

            study_ui.closeStudy()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _open(self, fx):
        from LesionNetworkMappingUI import card_pipeline

        self.widget.openCohortStudy(fx["study"])
        card_pipeline.setCohortSeed(self.widget)
        _pump(100)
        return self.widget.ctx.card("cohort")

    def _resultVolumes(self, title):
        return [node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode") if node.GetName().startswith(f"{title}:")]

    def _check(self, key, mapFirst):
        card = self._open(self.fxA)
        if mapFirst:
            job = card.mapLesions()
            self.assertIsNotNone(job, "the lesions of study A cannot be mapped")
            self.assertTrue(_waitFor(job.isFinished, 600), "the cohort maps job of study A did not end")
            self.assertTrue(job.succeeded(), getattr(job, "failureReason", ""))
            _pump(200)
        tab = card.tab(key)
        tab.refresh()
        _pump(50)
        tab.permSpinBox.setValue(20000)  # long enough for study B to be opened while it runs
        if hasattr(tab, "tfceCheckBox"):
            tab.tfceCheckBox.setChecked(False)
        tab.refresh()
        _pump(50)
        for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            if node.GetName().startswith(f"{tab.TITLE}:"):
                slicer.mrmlScene.RemoveNode(node)
        self.assertTrue(tab.runState()[0], tab.runState())
        job = tab.run()
        self.assertIsNotNone(job, f"the {tab.TITLE} job of study A did not start")
        folderA = tab.jobFolder
        self.assertTrue(_under(folderA, self.fxA["study"]), folderA)

        shell = tab.ctx.shell()
        toasts = []
        if shell is not None:
            realToast = shell.toast
            shell.toast = lambda text, *args, **kwargs: (toasts.append(str(text)), realToast(text, *args, **kwargs))
        try:
            cardB = self._open(self.fxB)
            self.assertIs(cardB.tab(key), tab)
            self.assertFalse(job.isFinished(), f"the {tab.TITLE} job of study A ended before study B was opened")
            self.assertIsNone(tab.result)
            self.assertTrue(_waitFor(job.isFinished, 600), f"the {tab.TITLE} job of study A did not end")
            _pump(500)
        finally:
            if shell is not None:
                del shell.toast
        self.assertTrue(job.succeeded(), getattr(job, "failureReason", ""))
        shown = tab.result[0] if tab.result else None
        self.assertFalse(shown and _under(shown, self.fxA["study"]), f"study B's {tab.TITLE} tab shows the result of study A: {shown}")
        self.assertFalse(tab.results.isVisibleTo(tab.widget) and shown is None, "the result panel is shown without a result")
        self.assertEqual(self._resultVolumes(tab.TITLE), [], "the map of study A's job is loaded while study B is open")
        self.assertFalse([text for text in toasts if "the maps are in the Cohort card" in text], toasts)
        if shell is not None:
            self.assertTrue([text for text in toasts if "Study A" in text], toasts)

        # Study A again: the tab shows the job's result from its folder
        self._open(self.fxA)
        tab.refresh()
        _pump(100)
        self.assertIsNotNone(tab.result, f"study A's {tab.TITLE} result is not shown when study A is opened again")
        self.assertTrue(_under(tab.result[0], folderA), tab.result[0])

    def test_contrast_job_stays_with_its_study(self):
        self._check("contrast", mapFirst=True)

    def test_vlsm_job_stays_with_its_study(self):
        self._check("vlsm", mapFirst=False)


if __name__ == "__main__":
    unittest.main()

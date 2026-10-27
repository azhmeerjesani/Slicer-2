"""Choosing a connectome refreshes the Result card at once, and the demo's "Done" chip gives way to Map's own chip.

After "Use the demo" (or a registered bundle chosen in the combo) the Result card kept "Choose a connectome to resolve the
threshold rule." until Preview/All was clicked, and Map's chip ("Ready") was not published after the job runner's "Done"
chip expired. It needs the Slicer application (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingReadyStateTest

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


def _pump(ms):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


class LesionNetworkMappingReadyStateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingLib import connectome

        cls.tmp = tempfile.mkdtemp(prefix="lnm ready state ")
        cls.path = os.path.abspath(connectome.make_synthetic(os.path.join(cls.tmp, "ready.lnmc"), shape=(8, 9, 8), n_subjects=6, runs=(12, 12), name="Ready state")["path"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        slicer.mrmlScene.Clear(0)
        slicer.util.selectModule("LesionNetworkMapping")
        self.widget = slicer.modules.LesionNetworkMappingWidget
        self.logic = self.widget.logic
        self.logic.registerConnectome(self.path)

    def tearDown(self):
        paths = [p for p in self.logic.registeredConnectomes() if os.path.abspath(p) != self.path]
        import qt

        from LesionNetworkMapping import CONNECTOMES_SETTING

        qt.QSettings().setValue(CONNECTOMES_SETTING, paths)
        slicer.mrmlScene.Clear(0)

    def test_planned_threshold_line_after_choosing_a_connectome(self):
        widget = self.widget
        widget._selectConnectome("")
        _pump(100)
        self.assertIn("Choose a connectome", str(widget.derivedLabel.text))
        widget.refreshConnectomes(select=self.path)
        _pump(200)
        self.assertIsNotNone(widget.bundle)
        self.assertNotIn("Choose a connectome", str(widget.derivedLabel.text))
        self.assertTrue(str(widget.derivedLabel.text).startswith("Planned:"), str(widget.derivedLabel.text))

    def test_family_notice_is_shown_outside_the_collapsing_seed_card(self):
        """The family notice (A-10) is repeated in the Compute and Result cards: the Seed card collapses once the lesion is done."""
        widget = self.widget
        notice = "The lesion is on MNI152NLin2009bAsym, the connectome on MNI152NLin2009cAsym: the template families are a few millimetres apart."
        widget.seedCheck = {"ok": True, "kind": "mni", "space": ("MNI152NLin2009bAsym", 1.5), "volumeMl": 2.0, "qc": None, "notice": notice, "reason": ""}
        widget._showSeed(None, None)
        widget._updateSteps()
        _pump(100)
        for name in ("computeFamilyLabel", "resultFamilyLabel"):
            label = getattr(widget, name)
            self.assertEqual(str(label.text), notice, name)
            self.assertFalse(label.isHidden(), name)
        widget.seedCheck = {"ok": True, "kind": "mni", "space": ("MNI152NLin2009cAsym", 1.0), "volumeMl": 2.0, "qc": None, "notice": "", "reason": ""}
        widget._showSeed(None, None)
        for name in ("computeFamilyLabel", "resultFamilyLabel"):
            self.assertTrue(getattr(widget, name).isHidden(), name)
            self.assertEqual(str(getattr(widget, name).text), "", name)

    def test_demo_done_republishes_the_chip_after_the_runner_releases_it(self):
        from LesionNetworkMappingUI import jobrunner

        widget = self.widget
        calls = []
        widget._refreshStatusChip = lambda: calls.append(1)  # shadows the method on this instance only
        saved = (jobrunner.FINISHED_CHIP_MS, self.logic.demoPath)
        jobrunner.FINISHED_CHIP_MS = 30
        self.logic.demoPath = lambda: self.path

        class Job:
            state = "done"

            def succeeded(self):
                return True

        try:
            widget._onDemoDone(Job())
            self.assertEqual(calls, [])
            _pump(500)
            self.assertEqual(len(calls), 1)
        finally:
            jobrunner.FINISHED_CHIP_MS, self.logic.demoPath = saved
            del widget._refreshStatusChip


if __name__ == "__main__":
    unittest.main()

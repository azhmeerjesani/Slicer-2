"""Test that the Calibrate card marks a calibration of another design "Out of date" (BB-1).

The card kept showing the green "FWER ok" chip and the checklist evidence of a calibration after the Contrast design
changed (a covariate turned off), and again after a reopen, because the run folder is named by the contrast label only
and the card never compared the record's design with the configured one (bug
pipeline-calibration-controls-gui:calibration-result-not-out-of-date-after-design-change). The card now shows the newest
calibration of the current design, else the newest one with an "Out of date" chip and no checklist evidence.

It needs the Slicer application (the card is built on a stand-in Map context; nothing is computed)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCalibrateStaleUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
import tempfile
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class _Tab:
    def __init__(self, conn):
        self.conn = conn
        self.design = None

    def spec(self):
        return self.design.spec

    def connectomeDir(self):
        return self.conn

    def currentMapsTsv(self):
        return os.path.join(self.conn, "maps.tsv")


class _Cohort:
    def __init__(self, tab):
        self._tab = tab

    def tab(self, key):
        return self._tab if key == "contrast" else None


class _Runner:
    def isBusy(self):
        return False


class _Bundle:
    n_voxels = 1000
    path = None


class _Ctx:
    def __init__(self, study, cohort):
        self.study = study
        self.bundle = _Bundle()
        self.runner = _Runner()
        self.bundleChanged = []
        self.studyChanged = []
        self._cohort = cohort

    def card(self, key):
        return self._cohort if key == "cohort" else None


class LesionNetworkMappingCalibrateStaleUITest(unittest.TestCase):
    def setUp(self):
        import numpy as np
        from LesionNetworkMappingLib import calibrate, runs
        from LesionNetworkMappingLib import design as designlib
        from LesionNetworkMappingLib.study import Study

        self.calibrate, self.runs = calibrate, runs
        slicer.util.selectModule("LesionNetworkMapping")
        self.tmp = tempfile.mkdtemp(prefix="lnm calibrate stale ")
        rng = np.random.default_rng(5)
        self.study = Study.create(os.path.join(self.tmp, "study"), name="study")
        for index in range(16):
            group = "case" if index < 8 else "control"
            self.study.add_subject(f"sub-{group}{index:03d}", {"group": group, "volume_ml": f"{float(rng.uniform(2, 60)):.3f}"})
        self.study.save()
        self.withVolume = designlib.build_design(self.study, designlib.DesignSpec(test="two_group", levels=["case", "control"], inference={"n_perm": 1000}))
        self.without = designlib.build_design(self.study, designlib.DesignSpec(test="two_group", levels=["case", "control"], covariates=[], volume_off_reason="tested without volume",
                                                                               inference={"n_perm": 1000}))
        self.conn = os.path.join(self.study.path, "derivatives", "lnm", "conn-Synthetic")
        os.makedirs(self.conn)
        self.tab = _Tab(self.conn)
        self.card = None

    def tearDown(self):
        if self.card is not None:
            self.card.cleanup()
            self.card.widget.delete()  # deleteLater() of a parentless card made the application exit abnormally
            self.card = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def calibrated(self, design, name, mtime):
        out = os.path.join(self.conn, "calibration", name)
        os.makedirs(out, exist_ok=True)
        run_id = self.runs.register(self.study.path, "calibration", out, params={"name": name})
        record = {"format": self.calibrate.FORMAT_NAME, "version": self.calibrate.FORMAT_VERSION, "run_id": run_id, "status": "done", "design": self.calibrate._design_info(design),
                  "fwer": {"level": "consistent", "chip": "ok", "fwe": self.calibrate.rate_summary(1, 20), "text": "Empirical FWER 0.05"}}
        path = os.path.join(out, self.calibrate.CALIBRATION_JSON)
        self.calibrate.write_record(path, record)
        os.utime(path, (mtime, mtime))
        self.runs.update(self.study.path, run_id, status="done")
        return run_id

    def assertCurrent(self, card, current):
        self.assertFalse(card.results.isHidden(), "no calibration is shown")
        self.assertEqual(card.resultCurrent, current)
        self.assertEqual(card.staleChip.isHidden(), current, card.evidenceLabel.text)
        if current:
            self.assertIn("Checklist (item 8", card.evidenceLabel.text)
            self.assertEqual(card.fwerChip.property("chipLevel"), "ok")
        else:
            self.assertIn("Out of date", card.evidenceLabel.text)
            self.assertNotIn("Checklist (item 8", card.evidenceLabel.text)
            self.assertNotEqual(card.fwerChip.property("chipLevel"), "ok")  # it does not vouch for the current design
        self.assertTrue(card.runButton.enabled, card.runButton.toolTip)  # "Calibrate this analysis" stays offered

    def test_design_change_and_reopen(self):
        from LesionNetworkMappingUI import card_calibrate

        self.calibrated(self.withVolume, "caseVsControl", 1_000_000)
        self.tab.design = self.withVolume
        self.card = card = card_calibrate.CalibrateCard(_Ctx(self.study, _Cohort(self.tab)))
        self.assertIn("covariates: volume_ml", card.designLabel.text)
        self.assertCurrent(card, True)
        # The volume covariate turned off in the Contrast tab: the same folder name, another design
        self.tab.design = self.without
        card.refresh()
        self.assertIn("covariates: none", card.designLabel.text)
        self.assertCurrent(card, False)
        # Reopen (the card forgets its result): the old design's calibration is still out of date
        card.result = None
        card.refresh()
        self.assertCurrent(card, False)
        # Back to the calibrated design
        self.tab.design = self.withVolume
        card.refresh()
        self.assertCurrent(card, True)
        # A newer calibration of the other design: each design shows its own, the older one included
        self.calibrated(self.without, "caseVsControlNoVolume", 2_000_000)
        card.result = None
        card.refresh()
        self.assertCurrent(card, True)
        self.assertEqual(os.path.basename(card.result[0]), "caseVsControl")
        self.tab.design = self.without
        card.refresh()
        self.assertCurrent(card, True)
        self.assertEqual(os.path.basename(card.result[0]), "caseVsControlNoVolume")

    def test_log_volume_and_welch_are_another_design(self):
        """log(volume) or the Aspin-Welch v statistic is another design than the plain t calibration (BB-1
        calibration-stays-current-after-log-volume-or-welch-change); the design lines name them.
        """
        from LesionNetworkMappingLib import design as designlib
        from LesionNetworkMappingUI import card_calibrate

        def build(**kwargs):
            return designlib.build_design(self.study, designlib.DesignSpec(test="two_group", levels=["case", "control"], inference={"n_perm": 1000}, **kwargs))

        self.calibrated(self.withVolume, "caseVsControl", 1_000_000)
        self.tab.design = self.withVolume
        self.card = card = card_calibrate.CalibrateCard(_Ctx(self.study, _Cohort(self.tab)))
        self.assertCurrent(card, True)
        self.tab.design = build(transforms={"volume_ml": "log"})
        card.refresh()
        self.assertIn("covariates: volume_ml (log)", card.designLabel.text)
        self.assertCurrent(card, False)
        self.assertIn("covariates: volume_ml ·", card.evidenceLabel.text)  # the calibrated design: untransformed
        self.tab.design = build(variance_groups=True)
        card.refresh()
        self.assertIn("Aspin-Welch v", card.designLabel.text)
        self.assertCurrent(card, False)
        self.tab.design = self.withVolume
        card.refresh()
        self.assertCurrent(card, True)


    def test_settings_of_a_stopped_run_are_restored_after_reopen(self):
        """After a reopen the spin boxes show the settings of the stopped run, and a notice says that it continues (BB-1
        calibrate-settings-not-restored-after-reopen-resume-restarts-silently).
        """
        import json

        from LesionNetworkMappingUI import card_calibrate

        out = os.path.join(self.conn, "calibration", "caseVsControl")
        os.makedirs(out)
        params = {"k": 40, "inner_perm": 100, "sims": 5, "effects": [0.3]}
        with open(os.path.join(out, self.calibrate.RESUME_NAME), "w", encoding="utf-8") as f:
            json.dump({"done": ["fwer:0"], "data": {"params": params, "fwer": [], "planted": {}}}, f)
        self.tab.design = self.withVolume
        self.card = card = card_calibrate.CalibrateCard(_Ctx(self.study, _Cohort(self.tab)))
        self.assertEqual((card.kSpin.value, card.innerSpin.value, card.simsSpin.value), (40, 100, 5))
        self.assertEqual(card.effects(), [0.3])
        self.assertIn("stopped calibration", card.noticeLabel.text)
        self.assertFalse(card.noticeLabel.isHidden())
        # Restored once: the user's later change stays
        card.kSpin.setValue(60)
        card.refresh()
        self.assertEqual(card.kSpin.value, 60)


    def test_stop_notice_belongs_to_the_design_of_the_run(self):
        """The stop notice and the Stopped progress row show only for the design that was stopped (BB-1
        calibrate-stopped-notice-stays-after-design-switch).
        """
        from LesionNetworkMappingUI import card_calibrate

        self.calibrated(self.withVolume, "caseVsControl", 1_000_000)
        self.tab.design = self.withVolume
        self.card = card = card_calibrate.CalibrateCard(_Ctx(self.study, _Cohort(self.tab)))
        self.tab.design = self.without
        card.refresh()
        identity = card.designIdentity(*card.currentSpec()[:2])
        card._setNotice("Stopped. Calibrate again with the same settings to continue.", identity)
        card.progress.widget.show()
        card.refresh()
        self.assertFalse(card.noticeLabel.isHidden())
        self.tab.design = self.withVolume
        card.refresh()
        self.assertTrue(card.noticeLabel.isHidden())
        self.assertTrue(card.progress.widget.isHidden())
        self.assertCurrent(card, True)
        self.tab.design = self.without
        card.refresh()
        self.assertFalse(card.noticeLabel.isHidden())


if __name__ == "__main__":
    unittest.main()

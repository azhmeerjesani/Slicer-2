"""Tests of the locked analysis plan in the Map module (L-39): the Cohort card's Contrast and Symptom analyses.

After the plan is locked (Recipe header), a change of the permutations or covariates of the Contrast or Symptom tab is
shown in the header's reason row, holds that analysis's Run (not the rest of Map) until its reason is recorded, and is
recorded as a deviation of that analysis ("contrast.n_perm"); "Use the plan's settings" sets the tab back.

They need the Slicer application and use the synthetic study of ``lnm_pipeline_fixture.py`` (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingPlanUITest

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

HYPOTHESES = {"primary": {"kind": "contrast", "contrast": "caseGtControl"}, "rois": [], "alpha": 0.05, "n_perm": 5000, "seeds": {"permutation": 0}}


def _pump(ms):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


class LesionNetworkMappingPlanUITest(unittest.TestCase):
    """One fixture study with the Ferguson 2019 recipe's plan locked; the tests run in name order and build on each other."""

    @classmethod
    def setUpClass(cls):
        import lnm_pipeline_fixture as fixture

        from LesionNetworkMapping import LesionNetworkMappingLogic
        from LesionNetworkMappingLib import recipes
        from LesionNetworkMappingUI import card_pipeline

        cls.recipes = recipes
        cls.cp = card_pipeline
        cls.tmp = tempfile.mkdtemp(prefix="lnm plan ui ")
        cls.fx = fixture.make_fixture(os.path.join(cls.tmp, "f1"), extra_rows=False)
        cls.study = cls.fx["study"]
        qt.QSettings().remove(card_pipeline.OPTIONS_SETTING)
        slicer.util.selectModule("LesionNetworkMapping")
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.bundle = LesionNetworkMappingLogic.registerConnectome(cls.fx["bundle"])
        LesionNetworkMappingLogic.setTermsAccepted(cls.bundle, True)  # the settings key, as "I accept" does
        cls.widget.refreshConnectomes(select=cls.bundle.path)
        cls.widget.openCohortStudy(cls.study)
        card_pipeline.setCohortSeed(cls.widget)
        cls.ctx = cls.widget.ctx
        cls.header = cls.ctx.card("recipe")
        cls.contrast = cls.ctx.card("cohort").tab("contrast")
        cls.symptom = cls.ctx.card("cohort").tab("symptom")
        recipe = next(recipe for recipe in cls.header.recipes if recipe.id == "classic_ferguson2019_adapted")
        assert cls.header.applyRecipe(recipe)
        cls.plan = cls.header.lockPlan(HYPOTHESES)
        _pump(100)

    @classmethod
    def tearDownClass(cls):
        try:
            from StudioLib import study_ui

            study_ui.closeStudy()
        except Exception:
            pass
        qt.QSettings().remove(cls.cp.OPTIONS_SETTING)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # Helpers

    def _refresh(self, tab):
        tab.refresh()  # the debounced refresh of a control change, now
        self.header.refresh()
        _pump(50)

    def _pendingText(self):
        return "; ".join(change["description"] for change in self.header.pending)

    # Tests

    def test_1_plan_defaults_are_not_deviations(self):
        """With the tabs at the plan's settings nothing waits for a reason and Run is not held by the plan."""
        self.assertEqual(self.recipes.plan_status(self.study)["state"], "locked")
        self._refresh(self.contrast)
        self._refresh(self.symptom)
        self.assertEqual(self.header.pending, [], self._pendingText())
        self.assertTrue(self.header.prompt.isHidden())
        self.assertNotIn("analysis plan", self.contrast.runState()[1])

    def test_2_contrast_permutations_wait_for_a_reason(self):
        """Permutations 1000 instead of the plan's 5000: prompted, Run held (button and run()), nothing else blocked."""
        contrast = self.contrast
        contrast.permSpinBox.setValue(1000)
        self._refresh(contrast)
        self.assertEqual([change["field"] for change in self.header.pending], ["contrast.n_perm"], self._pendingText())
        self.assertIn("n_perm 1000 instead of 5000", self._pendingText())
        self.assertFalse(self.header.prompt.isHidden(), "the reason row is not shown")
        enabled, reason = contrast.runState()
        self.assertFalse(enabled)
        self.assertIn("n_perm 1000 instead of 5000", reason)
        self.assertFalse(contrast.runButton.enabled)
        self.assertIsNone(contrast.run(), "the GLM started without the reason for the change from the locked plan")
        self.assertIsNone(contrast.job)
        # Only this analysis waits: Compute and Run everything follow the plan, which this change does not touch
        self.assertTrue(self.ctx.computeEnabled(), self.ctx.computeDisabledReasons())
        self.assertIsNone(self.header.checkBeforeCompute())
        self.assertEqual(self.recipes.read_deviations(self.study), [])

    def test_3_record_the_reason(self):
        """Record: one deviation "contrast.n_perm" with its reason; the plan Run everything follows is unchanged."""
        records = self.header.recordPending("Fewer permutations for a first look")
        self.assertEqual(len(records), 1)
        deviations = self.recipes.read_deviations(self.study)
        self.assertEqual([(item["field"], item["old"], item["new"], item["reason"]) for item in deviations],
                         [("contrast.n_perm", 5000, 1000, "Fewer permutations for a first look")])
        self.assertTrue(deviations[0]["description"].startswith("Contrast GLM: n_perm 1000 instead of 5000"), deviations[0]["description"])
        self.assertEqual(self.recipes.effective_parameters(self.study)["n_perm"], 5000)
        self._refresh(self.contrast)
        self.assertEqual(self.header.pending, [], self._pendingText())
        self.assertNotIn("analysis plan", self.contrast.runState()[1])
        self.assertEqual(self.recipes.plan_status(self.study)["deviations"], 1)
        # The Symptom analysis still follows the plan's 5000
        self.assertEqual(self.recipes.analysis_parameters(self.study, "symptom")["n_perm"], 5000)

    def test_4_volume_off_and_use_the_plans_settings(self):
        """Lesion volume off (with its reason) is a covariate change; "Use the plan's settings" sets the tab back."""
        contrast = self.contrast
        contrast.covariates.setChecked("volume_ml", False)
        contrast.volumeReasonEdit.setText("matched volumes")
        contrast.permSpinBox.setValue(5000)
        self._refresh(contrast)
        fields = sorted(change["field"] for change in self.header.pending)
        self.assertEqual(fields, ["contrast.design.covariates", "contrast.n_perm"], self._pendingText())
        self.assertFalse(contrast.runState()[0])
        self.header.restorePlanSettings()
        self._refresh(contrast)
        self.assertEqual(contrast.nPerm(), 1000, "the plan with this analysis's recorded deviation")
        self.assertIn("volume_ml", contrast.covariates.checked())
        self.assertEqual(self.header.pending, [], self._pendingText())
        self.assertEqual(len(self.recipes.read_deviations(self.study)), 1, "Use the plan's settings recorded something")

    def test_5_symptom_permutations_wait_for_a_reason(self):
        """The Symptom tab's permutations are compared with the plan too."""
        symptom = self.symptom
        symptom.permSpinBox.setValue(2000)
        self._refresh(symptom)
        self.assertEqual([change["field"] for change in self.header.pending], ["symptom.n_perm"], self._pendingText())
        enabled, reason = symptom.runState()
        self.assertFalse(enabled)
        self.assertIn("n_perm 2000 instead of 5000", reason)
        self.assertTrue(self.ctx.computeEnabled(), self.ctx.computeDisabledReasons())
        symptom.permSpinBox.setValue(5000)
        self._refresh(symptom)
        self.assertEqual(self.header.pending, [], self._pendingText())

    def test_6_rule_change_still_blocks_compute(self):
        """A change of what Run everything follows (the threshold rule) still blocks every computation."""
        rule = {"kind": "top_pct", "value": 5.0, "statistic": "Fz", "sign": "both"}
        before = self.header.rule()
        self.widget.setRule(rule)
        self.header.refresh()
        _pump(50)
        try:
            self.assertEqual([change["field"] for change in self.header.pending], ["rule"], self._pendingText())
            self.assertFalse(self.ctx.computeEnabled())
            self.assertIsNotNone(self.header.checkBeforeCompute())
        finally:
            self.widget.setRule(before)
            self.header.refresh()
            _pump(50)
        self.assertTrue(self.ctx.computeEnabled(), self.ctx.computeDisabledReasons())


if __name__ == "__main__":
    unittest.main()

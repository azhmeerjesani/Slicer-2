"""Tests of the locked analysis plan in the Map module (L-39): the Null models card.

After a plan with null models is locked (Recipe header), the Nulls card follows the plan's kind, K, mode and seed; a
kind outside the planned kinds or another K is shown in the header's reason row, holds the card's Run (not the rest of
Map) until its reason is recorded, and is recorded as a deviation of the card ("nulls_card.nulls.k"); bug
recipes-plan-lock-roi-gui:nulls-card-kind-and-k-changes-after-lock-not-deviations.

They need the Slicer application and use the synthetic study of ``lnm_pipeline_fixture.py`` (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingNullsPlanUITest

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

HYPOTHESES = {"primary": {"kind": "contrast", "contrast": "A>B"}, "rois": [], "alpha": 0.05, "n_perm": 5000, "seeds": {"permutation": 0}}


def _pump(ms):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


class LesionNetworkMappingNullsPlanUITest(unittest.TestCase):
    """One fixture study with the van den Heuvel 2026 recipe's plan locked (nulls exact, rl_independent, K 1000); the
    tests run in name order and build on each other.
    """

    @classmethod
    def setUpClass(cls):
        import lnm_pipeline_fixture as fixture

        from LesionNetworkMapping import LesionNetworkMappingLogic
        from LesionNetworkMappingLib import recipes
        from LesionNetworkMappingUI import card_pipeline

        cls.recipes = recipes
        cls.cp = card_pipeline
        cls.tmp = tempfile.mkdtemp(prefix="lnm nulls plan ui ")
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
        cls.nulls = cls.ctx.card("nulls")
        # Start away from the plan: locking sets the card to the plan's settings
        cls.nulls.kSpin.setValue(500)
        cls.nulls.seedSpin.setValue(9)
        recipe = next(recipe for recipe in cls.header.recipes if recipe.id == "vdh2026_standard_settings")
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

    def _refresh(self):
        self.nulls.refresh()  # the debounced refresh of a control change, now
        self.header.refresh()
        _pump(50)

    def _pendingText(self):
        return "; ".join(change["description"] for change in self.header.pending)

    # Tests

    def test_1_card_follows_the_plan(self):
        """Locking sets the card to the plan's kind, K and seed; nothing waits for a reason."""
        self.assertEqual(self.plan["parameters"]["nulls"]["kinds"], ["rl_independent"])
        self._refresh()
        self.assertEqual(self.nulls.kind, "rl_independent")
        self.assertEqual(self.nulls.kValue(), 1000)
        self.assertEqual(int(self.nulls.seedSpin.value), 0)
        self.assertEqual(self.header.pending, [], self._pendingText())
        self.assertNotIn("analysis plan", self.nulls.runState()[1])

    def test_2_k_change_waits_for_a_reason(self):
        """K 200 instead of the plan's 1000: prompted in the header, the card's Run held, nothing else blocked."""
        self.nulls.kSpin.setValue(200)
        self._refresh()
        self.assertEqual([change["field"] for change in self.header.pending], ["nulls_card.nulls.k"], self._pendingText())
        self.assertIn("Null models card: null cohorts 200 instead of 1000", self._pendingText())
        self.assertFalse(self.header.prompt.isHidden(), "the reason row is not shown")
        enabled, reason = self.nulls.runState()
        self.assertFalse(enabled)
        self.assertIn("null cohorts 200 instead of 1000", reason)
        self.assertFalse(self.nulls.runButton.enabled)
        self.assertIsNone(self.nulls.run(), "the null models started without the reason for the change from the locked plan")
        self.assertIsNone(self.nulls.job)
        self.assertTrue(self.ctx.computeEnabled(), self.ctx.computeDisabledReasons())
        self.assertIsNone(self.header.checkBeforeCompute())
        self.assertEqual(self.recipes.read_deviations(self.study), [])

    def test_3_kind_outside_the_plan_waits_for_a_reason(self):
        """A kind that is not planned is a change too (the repro's rl_cohort_shift: in exact mode its null cohorts are
        the K_pool shared shifts, so K is reported as what the run would use).
        """
        self.nulls.selectKind("rl_cohort_shift")
        self._refresh()
        self.assertEqual([change["field"] for change in self.header.pending], ["nulls_card.nulls.kinds", "nulls_card.nulls.k"], self._pendingText())
        self.assertIn("null kinds rl_independent, rl_cohort_shift instead of rl_independent", self._pendingText())
        self.assertIn("null cohorts 20 instead of 1000", self._pendingText())
        self.assertFalse(self.nulls.runState()[0])
        self.assertIsNone(self.nulls.run())
        self.nulls.selectKind("rl_strength")
        self._refresh()
        self.assertIn("null kinds rl_independent, rl_strength instead of rl_independent", self._pendingText())
        self.assertIn("null cohorts 200 instead of 1000", self._pendingText())
        self.assertFalse(self.nulls.runState()[0])

    def test_4_record_the_reason(self):
        """Record: the card's deviations are logged with the reason; Run is no longer held by the plan."""
        expected = [change["field"] for change in self.header.pending]
        records = self.header.recordPending("A cohort-shift check with fewer null cohorts")
        self.assertEqual([record["field"] for record in records], expected)
        deviations = self.recipes.read_deviations(self.study)
        self.assertEqual([item["field"] for item in deviations], expected)
        self.assertTrue(all(item["reason"] == "A cohort-shift check with fewer null cohorts" for item in deviations))
        self.assertEqual(self.recipes.effective_parameters(self.study)["nulls"]["kinds"], ["rl_independent"], "Run everything's plan is unchanged")
        self._refresh()
        self.assertEqual(self.header.pending, [], self._pendingText())
        self.assertNotIn("analysis plan", self.nulls.runState()[1])
        # The planned kind stays allowed (with the recorded K)
        self.nulls.selectKind("rl_independent")
        self.nulls.kSpin.setValue(200)
        self._refresh()
        self.assertEqual(self.header.pending, [], self._pendingText())


if __name__ == "__main__":
    unittest.main()

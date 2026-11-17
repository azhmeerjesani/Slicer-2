"""Tests that the threshold rule restored with a scene or project reaches every card of the Map module.

Reopening a project restores the rule only into the parameter node (``thresholdRule``) and the Result card controls. The
cards read it from the shared context (``ctx.rule``), which stayed None, so the Robust card fell back to FWE (Bonferroni):
its primary and the ``--rule`` of its sweep, replicate, split-half and influence jobs differed from the rule the Result
card showed (bug analysis-persistence:robust-card-rule-reset-to-fwe-after-reopen).

They need the Slicer application (no network, no connectome)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingRuleRestoreUITest

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

TOP_5 = {"kind": "top_pct", "value": 5.0, "statistic": "T", "sign": "both"}
CUSTOM_T_10 = {"kind": "custom_t", "value": 10.0, "reference_n": 120}


def _pump(ms):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


class LesionNetworkMappingRuleRestoreUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingLib import thresholds

        cls.thresholds = thresholds
        cls.tmp = tempfile.mkdtemp(prefix="lnm rule restore ")
        slicer.util.selectModule("LesionNetworkMapping")
        _pump(50)
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.ctx = cls.widget.ctx
        cls.ruleBefore = cls.widget.currentRule()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.widget.setRule(cls.ruleBefore)
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _restored(self, rule):
        """What a reopened project does: the rule comes back in the parameter node only, the context starts empty."""
        self.ctx.rule = None
        self.widget._parameterNode.thresholdRule = self.thresholds.rule_to_json(rule)
        self.widget.updateGUIFromParameterNode()
        _pump(50)

    def _assertCardsUse(self, rule):
        rule = self.thresholds.normalize_rule(rule)
        self.assertEqual(self.widget.currentRule(), rule, "the parameter node does not hold the restored rule")
        self.assertEqual(self.widget.ruleCombo.currentData, rule["kind"], "the Result card does not show the restored rule")
        self.assertEqual(self.ctx.rule, rule, "the context keeps another rule than the Result card")
        robust = self.ctx.card("robust")
        self.assertIsNotNone(robust)
        self.assertEqual(robust.rule(), rule, "the Robust card's primary is not the Result card's rule")
        robust.showSettings()
        from LesionNetworkMappingLib import robustness

        self.assertIn(robustness.sweep_label(rule), robust.primaryLabel.text)
        nulls = self.ctx.card("nulls")
        if nulls is not None:
            self.assertEqual(nulls.rule(), rule)

    def test_1_restored_top_pct_reaches_the_robust_card(self):
        self._restored(TOP_5)
        self._assertCardsUse(TOP_5)
        self.assertNotIn("FWE", self.ctx.card("robust").primaryLabel.text)

    def test_2_restored_custom_t_reaches_the_robust_card(self):
        self._restored(CUSTOM_T_10)
        self._assertCardsUse(CUSTOM_T_10)

    def test_3_robust_card_reads_the_result_card_when_the_context_is_empty(self):
        """Even before the parameter node is shown again, the Robust card reads the Result card's rule (as the others do)."""
        self.widget.setRule(TOP_5)
        self.ctx.rule = None
        self.assertEqual(self.ctx.card("robust").rule(), self.thresholds.normalize_rule(TOP_5))
        self.widget.setRule(TOP_5)

    def test_4_scene_round_trip(self):
        """Save the scene with a Top 5% rule, start empty and load it again while Map is shown: the cards use Top 5%."""
        self.widget.setRule(TOP_5)
        path = os.path.join(self.tmp, "rule.mrb")
        self.assertTrue(slicer.util.saveScene(path))
        slicer.mrmlScene.Clear(0)
        _pump(50)
        self.ctx.rule = None
        slicer.util.loadScene(path)
        _pump(200)
        self._assertCardsUse(TOP_5)


if __name__ == "__main__":
    unittest.main()

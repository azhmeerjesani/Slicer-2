"""After a restart the VLSM tab shows its newest result again, controls and covariates included (BB-1).

After a restart the tab showed the newest VLSM result under default controls (Groups, two-sided), so Run VLSM would
run another analysis than the one on screen, and the summary never named the extra covariates (bug
vlsm-tab-controls-reset-after-restart-and-covariates-not-shown).
It needs the Slicer application and a synthetic study of ``lnm_pipeline_fixture.py`` (no network, nothing is computed)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingVlsmRestoreUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import json
import os
import shutil
import sys
import tempfile
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


class LesionNetworkMappingVlsmRestoreUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import lnm_pipeline_fixture as fixture

        from LesionNetworkMapping import LesionNetworkMappingLogic

        cls.tmp = tempfile.mkdtemp(prefix="lnm vlsm restore ")
        cls.fx = fixture.make_fixture(os.path.join(cls.tmp, "a"), extra_rows=False, study_name="Study A")
        slicer.util.selectModule("LesionNetworkMapping")
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.bundle = LesionNetworkMappingLogic.registerConnectome(cls.fx["bundle"])
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

    def test_controls_and_covariates_follow_the_shown_result(self):
        from LesionNetworkMappingLib import design as designlib
        from LesionNetworkMappingLib import vlsm
        from LesionNetworkMappingUI import card_pipeline

        self.widget.openCohortStudy(self.fx["study"])
        card_pipeline.setCohortSeed(self.widget)
        slicer.app.processEvents()
        tab = self.widget.ctx.card("cohort").tab("vlsm")
        tab.refresh()
        self.assertEqual(tab.testKind(), "groups")
        self.assertTrue(tab.scoreCombo.count, "the fixture has no score column")
        score = tab.scoreCombo.itemData(0)
        spec = designlib.DesignSpec(
            test="continuous", group_column=None, score_column=score, contrast="positive", covariates=[designlib.VOLUME_COLUMN],
            transforms={designlib.VOLUME_COLUMN: "log"}, inference={"n_perm": 5000, "fwe": True, "fdr": True, "tfce": False})
        folder = os.path.join(tab.connectomeDir(), "restoreScorePos")
        os.makedirs(folder, exist_ok=True)
        spec.save(os.path.join(folder, "design.json"))
        record = {"status": "done", "test": "continuous", "n": 12, "contrast": {"spec": "positive", "columns": ["x", score]},
                  "covariates": [designlib.VOLUME_COLUMN, "age"], "volume_covariate": True}
        with open(os.path.join(folder, vlsm.VLSM_JSON), "w", encoding="utf-8") as f:
            json.dump(record, f)
        # The restart: nothing shown, the controls at their defaults
        tab.result, tab.outputs = None, []
        tab.refresh()
        slicer.app.processEvents()
        self.assertIsNotNone(tab.result, "the newest result is not shown again")
        self.assertEqual(tab.testKind(), "score", "the controls keep their defaults, not the design of the result shown")
        self.assertEqual(tab.contrast(), "positive")
        self.assertEqual(tab.spec().test, "continuous")
        self.assertEqual(tab.spec().transforms, {designlib.VOLUME_COLUMN: "log"})
        self.assertTrue(tab.fdrCheckBox.checked)
        self.assertIn("age", tab.summaryLabel.text, "the summary does not name the covariates of the result")

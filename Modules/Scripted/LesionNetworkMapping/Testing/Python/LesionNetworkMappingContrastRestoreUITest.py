"""After a restart the Contrast tab shows its newest result again and its controls take that result's design (BB-1).

The tab showed the newest GLM result but its controls fell back to their defaults (log volume off, ...), so the
Calibrate card compared its calibration with a design other than the group analysis on screen and said "current" (checklist
item 8) while Report F8 said "out of date" (bug calibrate-card-current-while-checklist-says-out-of-date-after-reopen).
It needs the Slicer application and a synthetic study of ``lnm_pipeline_fixture.py`` (no network, nothing is computed)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingContrastRestoreUITest

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


class LesionNetworkMappingContrastRestoreUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import lnm_pipeline_fixture as fixture

        from LesionNetworkMapping import LesionNetworkMappingLogic

        cls.tmp = tempfile.mkdtemp(prefix="lnm contrast restore ")
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

    def test_controls_follow_the_shown_result(self):
        from LesionNetworkMappingLib import design as designlib
        from LesionNetworkMappingLib import glm
        from LesionNetworkMappingUI import card_pipeline

        self.widget.openCohortStudy(self.fx["study"])
        card_pipeline.setCohortSeed(self.widget)
        slicer.app.processEvents()
        tab = self.widget.ctx.card("cohort").tab("contrast")
        tab.refresh()
        self.assertFalse(tab.logVolumeCheckBox.checked)
        # A finished GLM made with log(volume) on (the state before a restart)
        spec = tab.spec()
        spec.transforms = {designlib.VOLUME_COLUMN: "log"}
        spec = designlib.DesignSpec.from_dict(spec.to_dict())
        folder = os.path.join(tab.outputDir(), "caseVsControl") if not tab.outputDir().endswith("caseVsControl") else tab.outputDir()
        os.makedirs(folder, exist_ok=True)
        spec.save(os.path.join(folder, "design.json"))
        with open(os.path.join(folder, glm.GLM_JSON), "w", encoding="utf-8") as f:
            json.dump({"status": "done"}, f)
        # The restart: no result shown, the controls at their defaults
        tab.result, tab.outputs = None, []
        self.assertFalse(tab.logVolumeCheckBox.checked)
        tab.refresh()
        slicer.app.processEvents()
        self.assertIsNotNone(tab.result, "the newest result is not shown again")
        self.assertTrue(tab.logVolumeCheckBox.checked, "the controls keep their defaults, not the design of the result shown")
        self.assertEqual(tab.spec().transforms, {designlib.VOLUME_COLUMN: "log"})

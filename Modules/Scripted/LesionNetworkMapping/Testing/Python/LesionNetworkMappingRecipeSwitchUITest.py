"""Tests of the Recipe header of the Map module when the user switches between studies with open plans (L-39).

Each study remembers its recipe (``derivatives/lnm/recipe.json``), but the threshold rule and the Run everything options
(``Studio/LNM/Pipeline/Options``) are not kept per study. Opening a study with a remembered recipe sets them to that
recipe's again, and a study without one does not show another study's recipe (bug
lnm-cohort:recipe-options-not-restored-when-switching-studies).

They need the Slicer application (no network, no connectome)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingRecipeSwitchUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
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

QUICK_LOOK = "quicklook_boes2015"
COMPREHENSIVE = "comprehensive_battery"


def _pump(ms):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


class LesionNetworkMappingRecipeSwitchUITest(unittest.TestCase):
    """Two studies with open plans: A uses Quick look, B Comprehensive; the tests run in name order and build on each other."""

    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingLib import recipes
        from LesionNetworkMappingLib.study import Study
        from LesionNetworkMappingUI import card_pipeline

        cls.recipes = recipes
        cls.cp = card_pipeline
        cls.tmp = tempfile.mkdtemp(prefix="lnm recipe switch ")
        cls.studyA = os.fspath(Study.create(os.path.join(cls.tmp, "Study A"), name="Study A").path)
        cls.studyB = os.fspath(Study.create(os.path.join(cls.tmp, "Study B"), name="Study B").path)
        qt.QSettings().remove(card_pipeline.OPTIONS_SETTING)
        slicer.util.selectModule("LesionNetworkMapping")
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.ruleBefore = cls.widget.ctx.card("recipe").rule()
        cls.ctx = cls.widget.ctx
        cls.ctx.setStudy(None)
        cls.widget.cohortStudy = None
        _pump(50)
        cls.header = cls.ctx.card("recipe")

    @classmethod
    def tearDownClass(cls):
        try:
            from StudioLib import study_ui

            study_ui.closeStudy()
        except Exception:
            pass
        try:
            cls.ctx.setStudy(None)
            cls.widget.cohortStudy = None
            if cls.ruleBefore is not None:
                cls.widget.setRule(cls.ruleBefore)
        except Exception:
            pass
        qt.QSettings().remove(cls.cp.OPTIONS_SETTING)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # Helpers

    def _open(self, path):
        self.assertIsNotNone(self.widget.openCohortStudy(path))
        _pump(100)
        self.header.refresh()
        self.assertEqual(os.path.normcase(os.path.abspath(self.header.studyPath())), os.path.normcase(os.path.abspath(path)))

    def _pick(self, recipeId):
        """Choose a recipe through the combo (the user's path)."""
        index = self.header.combo.findData(recipeId)
        self.assertGreaterEqual(index, 0, recipeId)
        self.header.onRecipeActivated(index)
        _pump(50)
        self.assertEqual((self.recipes.selected_recipe(self.header.studyPath()) or {}).get("id"), recipeId)

    def _quickLookStepsOnly(self):
        options = self.cp.loadOptions()
        quick = self.header.recipeById(QUICK_LOOK)
        self.assertEqual(options.get("nulls"), "off", "Run everything would run null models, which Quick look leaves out")
        self.assertEqual(options.get("specificity"), "off", "Run everything would run specificity, which Quick look leaves out")
        self.assertEqual(self.header.currentSteps(), list(quick.steps))
        self.assertEqual(self.recipes.diff(quick, self.header.currentParameters()), [])

    # Tests

    def test_1_quick_look_on_a(self):
        """A picks Quick look: Run everything runs its steps only."""
        self._open(self.studyA)
        self.assertIsNone(self.header.recipe)
        self._pick(QUICK_LOOK)
        self._quickLookStepsOnly()

    def test_2_b_does_not_show_a_recipe(self):
        """B has no remembered recipe: the combo shows none (not A's) and the status says so."""
        self._open(self.studyB)
        self.assertIsNone(self.recipes.selected_recipe(self.studyB))
        shown, comboIndex, comboText = self.header.recipe, self.header.combo.currentIndex, self.header.combo.currentText
        self._pick(COMPREHENSIVE)  # first, so that the next test starts from B's options whatever this one finds
        options = self.cp.loadOptions()
        self.assertNotEqual(options.get("nulls"), "off")
        self.assertNotEqual(options.get("specificity"), "off")
        self.assertIsNone(shown, "B shows the recipe chosen in study A")
        self.assertEqual(comboIndex, 0, comboText)

    def test_3_reopen_a_follows_its_recipe(self):
        """Reopening A sets Run everything back to Quick look's steps; no change the user did not make is shown."""
        self._open(self.studyA)
        self.assertIsNotNone(self.header.recipe)
        self.assertEqual(self.header.recipe.id, QUICK_LOOK)
        self.assertEqual(self.header.combo.currentData, QUICK_LOOK)
        self._quickLookStepsOnly()
        self.assertNotIn("Custom", self.header.statusLabel.text)
        self.assertEqual((self.recipes.selected_recipe(self.studyA) or {}).get("id"), QUICK_LOOK)
        self.assertEqual((self.recipes.selected_recipe(self.studyB) or {}).get("id"), COMPREHENSIVE)

    def test_4_recipe_before_any_study_carries_over(self):
        """A recipe picked while no study is open is still shown in the first study opened (it has none of its own)."""
        self.ctx.setStudy(None)
        self.widget.cohortStudy = None
        _pump(50)
        self.header.refresh()
        self.assertIsNone(self.header.studyPath())
        self.header.applyRecipe(self.header.recipeById(QUICK_LOOK))
        from LesionNetworkMappingLib.study import Study

        studyC = os.fspath(Study.create(os.path.join(self.tmp, "Study C"), name="Study C").path)
        self._open(studyC)
        self.assertIsNotNone(self.header.recipe)
        self.assertEqual(self.header.recipe.id, QUICK_LOOK)
        self._quickLookStepsOnly()


if __name__ == "__main__":
    unittest.main()

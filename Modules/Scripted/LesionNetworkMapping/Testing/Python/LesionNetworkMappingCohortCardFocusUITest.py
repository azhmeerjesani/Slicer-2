"""Test of the Map module's card focus (bug cohort-only-cards-shown-on-lesion-page).

Search "Calibrate this analysis", the Circuits card's "Open Validate" and the Compare card's "Test for differences" focus cards
of Seed = Cohort (Calibrate, Validate, Cohort). They made those cards visible on the single-lesion page while the seed stayed
Lesion. A card that the current seed mode does not offer is now focused by switching to the seed mode that offers it (Cohort
first), as "Run everything" does; a card of the current mode is focused where it is.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCohortCardFocusUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import qt  # noqa: F401
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

COHORT_ONLY = ("calibrate", "validate", "cohort")


class CohortCardFocusUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        cls.widget = slicer.modules.LesionNetworkMappingWidget

    def _setMode(self, mode):
        widget = self.widget
        index = widget.seedModes.index(mode)
        widget.seedModeGroup.button(index).setChecked(True)
        widget.onSeedModeClicked(index)
        self.assertEqual(widget.seedMode(), mode)

    def _shown(self, cardId):
        return self.widget._cardWidgets[cardId][1].isVisible()

    def _hidden(self, cardId):
        return self.widget._cardWidgets[cardId][1].isHidden()

    def setUp(self):
        self._setMode("lesion")

    def tearDown(self):
        self._setMode("lesion")

    def test_1_lesion_page_hides_the_cohort_only_cards(self):
        for cardId in COHORT_ONLY:
            self.assertTrue(self._hidden(cardId), cardId)

    def test_2_focus_of_a_cohort_card_switches_to_cohort(self):
        for cardId in COHORT_ONLY:
            self._setMode("lesion")
            self.assertTrue(self.widget.ctx.focusCard(cardId), cardId)
            self.assertEqual(self.widget.seedMode(), "cohort", cardId)
            self.assertFalse(self._hidden(cardId), cardId)
            self.assertEqual(self.widget.seedModeGroup.checkedId(), self.widget.seedModes.index("cohort"))

    def test_3_circuits_and_compare_actions_do_not_leave_cohort_cards_on_the_lesion_page(self):
        self.widget.ctx.card("circuits").openValidate()
        self.assertEqual(self.widget.seedMode(), "cohort")
        self.assertFalse(self._hidden("validate"))
        self._setMode("lesion")
        self.widget.ctx.card("compare").testDifferences()
        self.assertEqual(self.widget.seedMode(), "cohort")
        self.assertFalse(self._hidden("cohort"))

    def test_4_a_card_of_the_current_mode_keeps_the_mode(self):
        self.assertTrue(self.widget.ctx.focusCard("compare"))
        self.assertEqual(self.widget.seedMode(), "lesion")
        self.assertFalse(self._hidden("compare"))
        for cardId in COHORT_ONLY:
            self.assertTrue(self._hidden(cardId), cardId)

    def test_5_a_lesion_card_from_the_cohort_page_goes_back_to_lesion(self):
        self._setMode("cohort")
        self.assertTrue(self.widget.ctx.focusCard("export"))
        self.assertEqual(self.widget.seedMode(), "lesion")
        self.assertFalse(self._hidden("export"))

    def test_6_an_unknown_card_is_not_found_and_the_mode_stays(self):
        self.assertFalse(self.widget.ctx.focusCard("no-such-card"))
        self.assertEqual(self.widget.seedMode(), "lesion")

    def test_7_a_pending_focus_retry_on_a_destroyed_card_is_silent(self):
        # The 0/150/500 ms retries of focusCard outlive the card at quit (bug focus-card-retry-timers-traceback-after-quit)
        widget = self.widget
        card = qt.QWidget()
        widget._cardWidgets["destroyed-card-test"] = (None, card)
        try:
            card.delete()
            widget._focusCardLater("destroyed-card-test")
        finally:
            widget._cardWidgets.pop("destroyed-card-test", None)


if __name__ == "__main__":
    unittest.main()

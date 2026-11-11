"""Test of the counts in the Seed Coords / Seed Stim summaries, the "seed maps ready" toast and the Cohort card status of the
Map module (bug seedset-counts-plural-only).

They read "1 studies · 1 foci", "1 patients · 1 rows", "1 seed maps ready" and "Lesion maps: 1 lesions" for one item. Each
count is now singular or plural on its own.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingSeedSetCountsTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class SeedSetCountsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        import LesionNetworkMapping
        from LesionNetworkMappingUI import card_cohort

        cls.module = LesionNetworkMapping
        cls.cohort = card_cohort

    def test_coordinates_summary(self):
        text = self.module.coordinatesSummaryText
        self.assertEqual(text(1, 1), "1 study · 1 focus")
        self.assertEqual(text(1, 2), "1 study · 2 foci")
        self.assertEqual(text(3, 1), "3 studies · 1 focus")
        self.assertEqual(text(3, 5), "3 studies · 5 foci")

    def test_stimulation_summary(self):
        text = self.module.stimulationSummaryText
        self.assertEqual(text(1, 1), "1 patient · 1 row")
        self.assertEqual(text(1, 2), "1 patient · 2 rows")
        self.assertEqual(text(2, 2), "2 patients · 2 rows")

    def test_seed_maps_ready_toast(self):
        self.assertEqual(self.module.seedMapsReadyText(1), "1 seed map ready · see the Cohort card")
        self.assertEqual(self.module.seedMapsReadyText(4), "4 seed maps ready · see the Cohort card")
        self.assertEqual(self.module.seedMapsReadyText("?"), "? seed maps ready · see the Cohort card")

    def test_cohort_card_status(self):
        text = self.cohort.cohortMapsStatusText
        self.assertEqual(text(1, "X", 120, 0), "Lesion maps: 1 lesion · conn-X · N=120 (0 from the cache)")
        self.assertEqual(text(5, "X", 120, 2), "Lesion maps: 5 lesions · conn-X · N=120 (2 from the cache)")

    def test_summary_labels_of_the_widget(self):
        widget = slicer.modules.LesionNetworkMappingWidget
        items = list(getattr(widget, "stimulationItems", []) or [])
        try:
            widget.stimulationItems = [{"participant_id": "p1", "site": "a", "points": []}]
            widget._showStimulation()
            self.assertEqual(widget.stimSummaryLabel.text, "1 patient · 1 row")
            from LesionNetworkMappingLib.coords import Focus

            # Talairach sites (a "space" column) are announced and marked; they are converted to MNI when the seeds are made
            widget.stimulationItems = [{"participant_id": "p1", "points": [Focus(-30.0, -55.0, 10.0, space="TAL")]},
                                       {"participant_id": "p2", "points": [Focus(-2.0, 40.0, 20.0)]}]
            widget._showStimulation()
            self.assertIn("1 Talairach site will be converted to MNI (Lancaster et al. 2007)", widget.stimSummaryLabel.text)
            self.assertEqual(widget.stimTable.item(0, 1).text(), "(-30, -55, 10) Talairach")
            self.assertEqual(widget.stimTable.item(1, 1).text(), "(-2, 40, 20)")
        finally:
            widget.stimulationItems = items
            widget._showStimulation()


if __name__ == "__main__":
    unittest.main()

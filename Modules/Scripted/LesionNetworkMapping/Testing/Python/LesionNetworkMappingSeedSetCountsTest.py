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

    def test_cohort_card_calls_seed_sets_seeds(self):
        text = self.cohort.cohortMapsStatusText
        self.assertEqual(text(1, "X", 120, 0, seeds=True), "Seed maps: 1 seed · conn-X · N=120 (0 from the cache)")
        self.assertEqual(text(4, "X", 120, 0, seeds=True), "Seed maps: 4 seeds · conn-X · N=120 (0 from the cache)")
        widget = slicer.modules.LesionNetworkMappingWidget
        original = widget.seedMode
        try:
            for mode, expected in (("coordinates", True), ("stimulation", True), ("cohort", False), ("lesion", False)):
                widget.seedMode = lambda mode=mode: mode
                self.assertEqual(self.cohort._seedSetMode(), expected, mode)
        finally:
            del widget.seedMode
            self.assertEqual(widget.seedMode, original)

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

    def test_stimulation_rows_with_different_outcomes(self):
        # Bug stim-duplicate-participant-conflicting-outcomes-last-row-wins: the table showed sub-01's last outcome on both
        # of its rows and Map used it silently. Each row now shows its own outcome and mapping is refused with a reason.
        import os
        import tempfile

        widget = slicer.modules.LesionNetworkMappingWidget
        node = widget._parameterNode
        saved = (list(widget.stimulationItems or []), widget.stimulationSource, list(widget.stimulationColumns or []), node.seedMode)
        folder = tempfile.mkdtemp()
        try:
            node.seedMode = "stimulation"
            path = os.path.join(folder, "stim_dup.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("participant_id,x,y,z,outcome\nsub-01,-12,-14,-6,40\nsub-01,12,-14,-6,80\nsub-02,10,10,10,30\n")
            self.assertIsNotNone(widget.importStimulation(path))
            self.assertEqual(widget.stimOutcomeCombo.currentData, "outcome")
            self.assertEqual([widget.stimTable.item(row, 2).text() for row in range(3)], ["40", "80", "30"])
            enabled, reason = widget.seedSetComputeState()
            self.assertFalse(enabled)
            self.assertIn("sub-01 has different outcomes: 40, 80", reason)
            # Mapping anyway (prepareSeedSet, on a small stand-in grid) is refused, naming both values
            import types

            import numpy as np
            from LesionNetworkMappingLib.geometry import Grid

            bundle, widget.bundle = widget.bundle, types.SimpleNamespace(grid=Grid((20, 24, 20), np.array([[2.0, 0, 0, -19.0], [0, 2.0, 0, -23.0], [0, 0, 2.0, -19.0], [0, 0, 0, 1.0]])),
                                                                     mask_indices=None)
            try:
                with self.assertRaisesRegex(ValueError, "sub-01 has two different outcomes: 40 and 80"):
                    widget.prepareSeedSet(os.path.join(folder, "run"), checkMask=False)
            finally:
                widget.bundle = bundle
            # Without an outcome column there is nothing to disagree about
            widget.stimOutcomeCombo.setCurrentIndex(0)
            self.assertEqual(widget.stimulationOutcomeConflict(), "")
        finally:
            widget.stimulationItems, widget.stimulationSource, widget.stimulationColumns = saved[0], saved[1], saved[2]
            node.seedMode = saved[3]
            widget._showStimulation()
            import shutil

            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

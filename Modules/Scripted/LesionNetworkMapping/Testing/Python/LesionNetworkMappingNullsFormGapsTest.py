"""The Nulls card's settings rows keep an even gap when rows are hidden (BB-1).

The rows (K, K_pool, rank rule, seed, strength map) were in a QFormLayout and showSettings() hid the ones a kind or mode
does not use. A QFormLayout still reserves the spacing of a hidden row, so the gaps grew with each hidden row (bug
map-nulls-card-numerics:nulls-card-settings-form-blank-gaps-hidden-rows).

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingNullsFormGapsTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class LesionNetworkMappingNullsFormGapsTest(unittest.TestCase):
    def test_gaps_between_visible_rows_are_even(self):
        from LesionNetworkMappingUI import card_nulls

        card = card_nulls.NullsCard.__new__(card_nulls.NullsCard)
        card.onSettingsChanged = lambda: None
        card.onKPoolChanged = lambda value: None
        host = qt.QWidget()
        layout = qt.QVBoxLayout(host)
        card._buildSettings(layout)
        host.resize(400, 400)
        host.show()
        rows = [(card.kLabel, card.kSpin), (card.kPoolLabel, card.kPoolSpin), (card.topLabel, card.topSpin),
                (card.seedLabel, card.seedSpin), (card.strengthLabel, card.strengthCombo)]
        try:
            for hidden in ((), (1,), (2,), (1, 2), (1, 2, 4), (4,), (2, 4)):
                for index, row in enumerate(rows):
                    for widget in row:
                        widget.setVisible(index not in hidden)
                slicer.app.processEvents()
                fields = [row[1] for index, row in enumerate(rows) if index not in hidden]
                tops = [(field.mapTo(host, qt.QPoint(0, 0)).y(), field.height) for field in fields]
                gaps = [tops[i + 1][0] - (tops[i][0] + tops[i][1]) for i in range(len(tops) - 1)]
                self.assertLessEqual(max(gaps) - min(gaps), 3, f"uneven gaps {gaps} with rows {hidden} hidden")
                self.assertLessEqual(max(gaps), 12, f"blank gap {gaps} with rows {hidden} hidden")
        finally:
            host.deleteLater()


if __name__ == "__main__":
    unittest.main()

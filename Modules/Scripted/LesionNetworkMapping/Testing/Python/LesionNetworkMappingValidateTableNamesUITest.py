"""Test that the Validate card's baselines table keeps two reference-map rows apart on the narrow inspector (BB-1).

Every reference-map row starts with "Reference: ". The name column (Stretch) is only about 133 px wide in the inspector
and the table elided on the right, so "Reference: aphasia_map_stroke" and "Reference: aphasia_map_tumor" both read
"Reference: apha..." and only the tooltip told them apart (bug lnm-reference-fastindex-paths:
validate-table-reference-names-elided-indistinguishable). The table now elides in the middle: the end of the name,
the part that differs, stays visible.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingValidateTableNamesUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import time
import unittest

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


def _pump(ms):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


def _metric(value):
    return {"value": value, "ci": [value - 0.1, value + 0.05], "p_perm": 0.01}


def _predictor(name, role, value):
    return {"name": name, "role": role, "status": "computed", "label": ("Reference map " + name[4:]) if name.startswith("ref:") else name,
            "metrics": {"auc": _metric(value), "r": _metric(value), "r2": _metric(value * value), "or_per_sd": _metric(2.0)}}


class ValidateTableNamesUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        _pump(50)
        cls.card = slicer.modules.LesionNetworkMappingWidget.ctx.card("validate")

    def record(self, nameA, nameB):
        return {"outcome_type": "continuous", "primary_metric": "r2", "permutation": {"K": 99}, "bootstrap": {"B": 20},
                "predictors": [_predictor("circuit", "circuit", 0.96), _predictor("volume", "baseline", -0.05),
                               _predictor("ref:" + nameA, "baseline", 0.96), _predictor("ref:" + nameB, "baseline", -0.05)],
                "incremental": {"delta": 0.98, "ci": [0.78, 1.16], "p_perm": 0.01, "text": ""}}

    def test_reference_rows_stay_distinguishable_when_elided(self):
        card = self.card
        self.assertIsNotNone(card, "the Map module has no Validate card")
        for name in ("showSummary", "showNotes", "showFigure"):  # only the table is under test
            setattr(card, name, lambda *args, **kwargs: None)
        try:
            card.showResult("", self.record("aphasia_map_stroke", "aphasia_map_tumor"))
            _pump(50)
            table = card.tableWidget
            self.assertEqual(table.textElideMode, qt.Qt.ElideMiddle, "a long name loses its end")
            metrics = qt.QFontMetrics(table.font)
            width = table.columnWidth(0) - 12
            names = [table.item(row, 0).text() for row in range(table.rowCount)]
            self.assertIn("Reference: aphasia_map_stroke", names)
            self.assertIn("Reference: aphasia_map_tumor", names)
            # Force the narrow inspector name column of the report (about 133 px) whatever the test window is
            width = min(width, 120)
            shown = [metrics.elidedText(text, table.textElideMode, width) for text in names]
            references = [text for name, text in zip(names, shown, strict=True) if name.startswith("Reference: ")]
            self.assertEqual(len(references), 2)
            self.assertEqual(len(set(references)), 2, f"two reference maps read the same: {references}")
        finally:
            for name in ("showSummary", "showNotes", "showFigure"):
                try:
                    delattr(card, name)
                except AttributeError:
                    pass

    def test_delta_row_label_is_not_elided(self):
        """The bold last row 'Δ vs volume' read 'Δ vs v... alone' on the narrow inspector (bug validate-delta-row-label-elided-1366)."""
        card = self.card
        for name in ("showSummary", "showNotes", "showFigure"):
            setattr(card, name, lambda *args, **kwargs: None)
        try:
            card.showResult("", self.record("a", "b"))
            _pump(50)
            table = card.tableWidget
            item = table.item(table.rowCount - 1, 0)
            font = qt.QFont(table.font)
            font.setBold(True)
            # The name column is about 105 px of text on a 1366x768 inspector (the label was cut at that width)
            shown = qt.QFontMetrics(font).elidedText(item.text(), table.textElideMode, 105)
            self.assertEqual(shown, item.text(), "the last row label is cut")
        finally:
            for name in ("showSummary", "showNotes", "showFigure"):
                try:
                    delattr(card, name)
                except AttributeError:
                    pass


if __name__ == "__main__":
    unittest.main()

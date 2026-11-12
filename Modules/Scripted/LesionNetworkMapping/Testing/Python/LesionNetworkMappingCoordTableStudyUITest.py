"""Test that the Coordinates foci table keeps the Study column readable (BB-1).

The table gave x, y, z and Space the same width as Study, so "studyA-1" and "studyB-c2" both read "stud..." (bug
coords-table-study-names-elided-indistinguishable). Study now takes the free width, the short columns fit their
content, and every cell has the full study name as its tooltip.

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCoordTableStudyUITest
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


class CoordTableStudyUITest(unittest.TestCase):
    def test_study_column_is_widest_and_has_tooltips(self):
        from LesionNetworkMappingLib import coords

        slicer.util.selectModule("LesionNetworkMapping")
        _pump(50)
        widget = slicer.modules.LesionNetworkMappingWidget
        foci = [coords.Focus(-30, -55, 10, "studyA-1"), coords.Focus(30, 20, 5, "studyB-c2", "TAL")]
        widget.setCoordinates(foci)
        table = widget.coordTable
        table.setMaximumWidth(300)
        table.show()
        _pump(100)
        widths = [table.columnWidth(c) for c in range(5)]
        self.assertGreater(widths[0], max(widths[1:]), f"Study is not the widest column: {widths}")
        metrics = qt.QFontMetrics(table.font)
        needed = max(metrics.boundingRect(t).width() for t in ("studyA-1", "studyB-c2"))
        self.assertGreaterEqual(widths[0], needed, f"Study cut: {widths}")
        self.assertEqual([table.item(r, 0).toolTip() for r in range(2)], ["studyA-1", "studyB-c2"])
        self.assertEqual(table.item(1, 4).toolTip(), "studyB-c2")
        widget.setCoordinates([])


if __name__ == "__main__":
    unittest.main()

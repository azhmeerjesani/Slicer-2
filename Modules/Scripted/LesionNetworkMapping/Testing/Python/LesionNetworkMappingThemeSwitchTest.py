"""A run-time theme switch recolors the Map elements that took their colors once.

After the Appearance switch (Dark to Light or back) the T values of the peaks table, the "Read more" link of the
disclaimer line and the "⋯" button next to "Get a connectome…" kept the colors of the old theme (1.7-2.8:1 contrast).
It needs the Slicer application (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingThemeSwitchTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import re
import time
import unittest

try:
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


class LesionNetworkMappingThemeSwitchTest(unittest.TestCase):
    def setUp(self):
        try:
            from StudioLib import ui
        except ImportError:
            self.skipTest("the Studio module is not part of this build")

        self.ui = ui
        application = slicer.app
        style = application.style()
        self.previousStyleName = style.objectName if style is not None else ""
        self.addCleanup(self._restoreStyle)
        slicer.mrmlScene.Clear(0)
        slicer.util.selectModule("LesionNetworkMapping")
        self.widget = slicer.modules.LesionNetworkMappingWidget
        slicer.util.mainWindow().show()
        _pump(300)

    def _applyStyle(self, name):
        """Switch the application style and palette at run time, like the Appearance panel (no setting is written)."""
        import qt

        application = slicer.app
        style = qt.QStyleFactory.create(name)
        if style is None:
            self.skipTest(f"style {name} is not available")
        application.removeEventFilter(application.style())
        application.setStyle(style)
        application.installEventFilter(application.style())
        application.setPalette(style.standardPalette())
        _pump(800)  # the palette listeners run once per event loop iteration after the change

    def _restoreStyle(self):
        if self.previousStyleName:
            self._applyStyle(self.previousStyleName)
        slicer.mrmlScene.Clear(0)

    def _readMoreColor(self):
        match = re.search(r"color:\s*(#[0-9a-fA-F]{6})", self.line.findChild("QLabel", "LNMDisclaimer").text)
        return match.group(1).lower()

    def test_peaks_link_and_more_button_follow_a_theme_switch(self):
        import qt
        from LesionNetworkMappingUI import widgets

        widget, ui = self.widget, self.ui
        table = widget.peaksTable
        widget._peaks = [{"value": 9.0}, {"value": -6.0}]
        table.setRowCount(2)
        column = 2  # the value column of the peaks table
        for row, peak in enumerate(widget._peaks):
            item = qt.QTableWidgetItem(f"{peak['value']:.2f}")
            item.setForeground(qt.QBrush(widgets.valueColor(peak["value"])))
            table.setItem(row, column, item)
        self.line = widgets.disclaimerLine()  # the one in the Studio inspector footer exists only while Map is the workspace
        self.addCleanup(self.line.delete)
        self.line.show()
        more = slicer.util.mainWindow().findChild("QToolButton", "LNMConnectomeMoreButton")
        self.assertIsNotNone(more)

        for theme in ("Light", "Dark", "Light"):
            self._applyStyle(f"{theme} Slicer")
            dark = ui.isDark()
            self.assertEqual(dark, theme == "Dark", f"switched to {theme}")
            index = 0 if dark else 1
            for row, kind in enumerate(("hot", "cold")):
                color = table.item(row, column).foreground().color().name()
                self.assertEqual(color, widgets.VALUE_COLORS[kind][index], f"{theme}: {kind} value of the peaks table")
            self.assertEqual(self._readMoreColor(), ui.token("accent-fg").name().lower(), f"{theme}: Read more link")
            # The button's palette is the application's: a stylesheet of its own would freeze the old theme's palette
            self.assertEqual(more.palette.color(qt.QPalette.Window).lightness() < 128, dark, f"{theme}: palette of the more button")


if __name__ == "__main__":
    unittest.main()

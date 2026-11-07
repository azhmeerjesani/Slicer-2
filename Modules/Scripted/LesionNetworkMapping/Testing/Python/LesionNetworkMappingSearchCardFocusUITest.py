"""Test that Search entries of the Map cards scroll to their card when run from another workspace (bug
map-card-search-entries-from-other-workspace-not-scrolled-to-card).

"Compare maps", "Add a reference map" and "Calibrate this analysis" run in Library switched to Map and called ``focusCard()``
before the Map inspector was laid out, so the inspector scroll was wrong (the Calibrate card at y=3064 in a 928 px inspector) and
the focus stayed in a slice view. ``focusCard()`` now repeats once the layout has settled.

Needs the Slicer application with the Studio shell::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingSearchCardFocusUITest

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


def _pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        slicer.app.processEvents()
        time.sleep(0.01)


class SearchCardFocusUITest(unittest.TestCase):
    def setUp(self):
        from StudioLib import shell

        mainWindow = slicer.util.mainWindow()
        if mainWindow is None:
            self.skipTest("needs the main window")
        slicer.util.selectModule("LesionNetworkMapping")
        self.geometry = mainWindow.saveGeometry()
        mainWindow.resize(1600, 1000)  # a tall inspector (the headless default is too small to show a card)
        self.shell = shell.Shell(force=True)
        self.shell.apply()
        mainWindow.activateWindow()
        mainWindow.raise_()
        _pump(0.5)
        self.dock = mainWindow.findChild("QDockWidget", "PanelDockWidget")
        self.widget = slicer.modules.LesionNetworkMappingWidget

    def tearDown(self):
        shell = getattr(self, "shell", None)
        if shell is not None:
            shell.switchTo("library")
            _pump(0.3)
            shell.dispose()
        mainWindow = slicer.util.mainWindow()
        if mainWindow is not None and getattr(self, "geometry", None) is not None:
            mainWindow.restoreGeometry(self.geometry)
        _pump(0.3)

    def _check(self, entryId, cardId):
        from StudioLib import palette

        self.shell.switchTo("library")
        _pump(1)
        self.assertTrue(palette.runEntry(self.shell, entryId))
        _pump(1.5)
        card = self.widget.ctx.card(cardId)
        cardWidget = getattr(card, "widget", card)
        self.assertEqual(self.shell.currentWorkspace(), "map")
        self.assertTrue(cardWidget.isVisible(), cardId)
        top = cardWidget.mapTo(self.dock, qt.QPoint(0, 0)).y()
        self.assertTrue(0 <= top <= self.dock.height - 60, "%s card top %d in inspector of %d" % (cardId, top, self.dock.height))
        focus = qt.QApplication.focusWidget()
        self.assertTrue(focus is not None and (focus == cardWidget or cardWidget.isAncestorOf(focus)), "the card has the focus")

    def test_calibrate_from_library(self):
        self._check("lnm.calibrate", "calibrate")

    def test_compare_from_library(self):
        self._check("lnm.compare", "compare")

    def test_reference_from_library(self):
        self._check("lnm.compare.reference", "compare")

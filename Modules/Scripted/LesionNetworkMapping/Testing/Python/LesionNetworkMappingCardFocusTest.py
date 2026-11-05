"""Test of the keyboard focus of the Map inspector cards (bug map-card-headers-focus-invisible).

The header of a card (``widgets.card()``, a ``ctkCollapsibleButton`` with the "card" slicerRole) is a Tab stop (Space opens
and closes the card), but qSlicerStyle drew a focused header exactly like an unfocused one: a keyboard user lost the focus
on every card header (USABILITY 9.2: a 2 px ring for keyboard focus, U-03: focus is visible everywhere). The style now draws
the ring around the 44 px title row. The detail line of the Report card (``LNMReportDetail``) was a second invisible stop:
it is a Tab stop only while it shows evidence links (Qt marks the focused link).

Needs the Slicer application (with qSlicerStyle for the ring)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCardFocusTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import types
import unittest

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


def _pump(count=10):
    for _ in range(count):
        slicer.app.processEvents()


def _changedPixels(first, second):
    first = first.convertToFormat(qt.QImage.Format_RGB32)
    second = second.convertToFormat(qt.QImage.Format_RGB32)
    count = 0
    for y in range(min(first.height(), second.height())):
        for x in range(min(first.width(), second.width())):
            if first.pixel(x, y) != second.pixel(x, y):
                count += 1
    return count


class CardFocusTest(unittest.TestCase):
    def setUp(self):
        from LesionNetworkMappingUI import widgets

        self.window = qt.QWidget()
        self.window.setFixedWidth(330)
        layout = qt.QVBoxLayout(self.window)
        self.before = qt.QPushButton("before")
        layout.addWidget(self.before)
        self.cards = []
        for name in ("LNMTestCardA", "LNMTestCardB"):
            cardButton = widgets.card("Card " + name[-1], name, collapsed=name.endswith("B"))
            body = qt.QVBoxLayout(cardButton)
            body.addWidget(qt.QLabel("Body"))
            layout.addWidget(cardButton)
            self.cards.append(cardButton)
        self.window.show()
        self.window.activateWindow()
        _pump()

    def tearDown(self):
        self.window.hide()
        self.window.delete()
        _pump()

    def _grabHeader(self, cardButton):
        _pump()
        topLeft = cardButton.mapTo(self.window, qt.QPoint(0, 0))
        return self.window.grab(qt.QRect(topLeft.x(), topLeft.y(), cardButton.width, 44)).toImage()

    def test_card_header_shows_keyboard_focus(self):
        if not slicer.app.style().inherits("qSlicerStyle"):
            self.skipTest("needs the Slicer application style")
        self.window.setAttribute(qt.Qt.WA_KeyboardFocusChange, True)  # as after a Tab key press
        for cardButton in self.cards:
            self.assertTrue(int(cardButton.focusPolicy) & int(qt.Qt.TabFocus), "the card header is a Tab stop")
            cardButton.setFocus(qt.Qt.TabFocusReason)
            _pump()
            if not cardButton.hasFocus():
                self.skipTest("the window cannot take the keyboard focus here")
            focused = self._grabHeader(cardButton)
            self.before.setFocus(qt.Qt.OtherFocusReason)
            _pump()
            plain = self._grabHeader(cardButton)
            # A 2 px ring along the 330 x 44 px title row: far more than the hover or the antialiasing of a corner
            self.assertGreater(_changedPixels(focused, plain), 2 * (cardButton.width + 44), cardButton.objectName)
            # Space on the focused header toggles the card: the focus ring is on a working control
            cardButton.setFocus(qt.Qt.TabFocusReason)
            _pump()
            collapsed = cardButton.collapsed
            qt.QApplication.sendEvent(cardButton, qt.QKeyEvent(qt.QEvent.KeyPress, qt.Qt.Key_Space, qt.Qt.NoModifier, " "))
            qt.QApplication.sendEvent(cardButton, qt.QKeyEvent(qt.QEvent.KeyRelease, qt.Qt.Key_Space, qt.Qt.NoModifier, " "))
            _pump()
            self.assertNotEqual(cardButton.collapsed, collapsed)

    def test_mouse_focus_shows_no_ring(self):
        """The ring is for keyboard focus only (USABILITY 9.2): a click on the header leaves it as it was."""
        if not slicer.app.style().inherits("qSlicerStyle"):
            self.skipTest("needs the Slicer application style")
        cardButton = self.cards[0]
        self.window.setAttribute(qt.Qt.WA_KeyboardFocusChange, False)
        self.before.setFocus(qt.Qt.OtherFocusReason)
        plain = self._grabHeader(cardButton)
        cardButton.setFocus(qt.Qt.MouseFocusReason)
        _pump()
        if not cardButton.hasFocus():
            self.skipTest("the window cannot take the keyboard focus here")
        self.assertEqual(_changedPixels(plain, self._grabHeader(cardButton)), 0)


class ReportDetailFocusTest(unittest.TestCase):
    def test_detail_line_is_a_tab_stop_only_with_links(self):
        from LesionNetworkMappingUI import card_report

        card = card_report.ReportCard(types.SimpleNamespace(runChanged=[], studyChanged=[]))
        try:
            tab = int(qt.Qt.TabFocus)
            label = card.detailLabel
            self.assertEqual(label.objectName, "LNMReportDetail")
            self.assertFalse(int(label.focusPolicy) & tab, "no links: not a (invisible) Tab stop")
            card._setDetail('<b>A1</b><br>value<br>Evidence: <a href="file:x">x</a>')
            self.assertTrue(int(label.focusPolicy) & tab, "with an evidence link Tab reaches the link")
            card._setDetail("<b>A1</b><br>value<br>Evidence: none")
            self.assertFalse(int(label.focusPolicy) & tab)
            card.onSelectionChanged()  # no row selected: the line stays as it was and is no stop
            self.assertFalse(int(label.focusPolicy) & tab)
        finally:
            card.cleanup()
            card.widget.deleteLater()


if __name__ == "__main__":
    unittest.main()

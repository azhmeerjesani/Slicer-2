"""Test of the research-use line of the Map footer (bug map-research-line-first-tab-stop-inert).

"Read more" was a link inside the text of the "Research use only. ..." label (``LinksAccessibleByKeyboard``): the label
took the focus on the first Tab with no link selected, nothing showed the focus and Enter did nothing; only a second Tab
selected "Read more". A Python event filter that selected the link on the first stop crashed the application at shutdown
(the label's text control sends a ChildRemoved event through the filter while Python is finalized). "Read more" is now a
real link-styled button (``LNMDisclaimerReadMore``): one Tab stop (Tab or Shift+Tab) with a focus ring, Enter or Space
opens it, and the label itself is not a stop (USABILITY 9.1: no invisible focus stops).

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingDisclaimerFocusTest

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


def _pump(count=10):
    for _ in range(count):
        slicer.app.processEvents()


class DisclaimerFocusTest(unittest.TestCase):
    def setUp(self):
        from LesionNetworkMappingUI import widgets

        self.widgets = widgets
        self.opened = []
        self.original = widgets.showDisclaimer
        widgets.showDisclaimer = lambda anchor=None: self.opened.append(anchor)  # the popover would block the test
        self.window = qt.QWidget()
        layout = qt.QVBoxLayout(self.window)
        self.before = qt.QPushButton("before")
        self.after = qt.QPushButton("after")
        self.line = widgets.disclaimerLine()
        for child in (self.before, self.line, self.after):
            layout.addWidget(child)
        self.label = self.line.findChild("QLabel", "LNMDisclaimer")
        self.button = self.line.findChild("QPushButton", "LNMDisclaimerReadMore")
        self.window.show()
        self.window.activateWindow()
        _pump()

    def tearDown(self):
        self.widgets.showDisclaimer = self.original
        self.window.hide()
        self.window.delete()
        _pump()

    def _key(self, key, modifiers=qt.Qt.NoModifier):
        widget = slicer.app.focusWidget()
        qt.QApplication.sendEvent(widget, qt.QKeyEvent(qt.QEvent.KeyPress, key, modifiers))
        qt.QApplication.sendEvent(widget, qt.QKeyEvent(qt.QEvent.KeyRelease, key, modifiers))
        _pump()

    def test_read_more_is_a_button_and_the_label_no_stop(self):
        self.assertIsNotNone(self.label)
        self.assertIsNotNone(self.button, "Read more is a real button of the line")
        self.assertEqual(self.button.text, "Read more")
        self.assertTrue(self.button.isVisible())
        self.assertTrue(int(self.button.focusPolicy) & int(qt.Qt.TabFocus), "Read more is a Tab stop")
        self.assertFalse(int(self.label.focusPolicy) & int(qt.Qt.TabFocus), "the text itself is not a (invisible) Tab stop")
        self.assertEqual(self.label.text, self.widgets.DISCLAIMER_LINE)
        stops = [child for child in self.line.findChildren("QWidget") if int(child.focusPolicy) & int(qt.Qt.TabFocus)]
        self.assertEqual(stops, [self.button], "the line has exactly one Tab stop")
        # The popover opens under the line (its anchor is the label) on a click too
        self.button.click()
        self.assertEqual(self.opened, [self.label])

    def test_first_tab_stop_is_read_more_and_enter_opens_it(self):
        self.before.setFocus(qt.Qt.OtherFocusReason)
        _pump()
        self.assertIs(slicer.app.focusWidget(), self.before)
        self._key(qt.Qt.Key_Tab)
        self.assertIs(slicer.app.focusWidget(), self.button, "one Tab reaches Read more")
        self._key(qt.Qt.Key_Return)
        self.assertEqual(self.opened, [self.label], "Enter on the first stop opens Read more")
        self._key(qt.Qt.Key_Space)
        self.assertEqual(self.opened, [self.label, self.label], "Space opens Read more too")
        # Read more is the only stop of the line: the next Tab leaves it
        self._key(qt.Qt.Key_Tab)
        self.assertIs(slicer.app.focusWidget(), self.after, "the next Tab goes on to the next control")

    def test_backward_first_stop_is_read_more_and_enter_opens_it(self):
        self.after.setFocus(qt.Qt.OtherFocusReason)
        _pump()
        self._key(qt.Qt.Key_Backtab, qt.Qt.ShiftModifier)
        self.assertIs(slicer.app.focusWidget(), self.button, "one Shift+Tab reaches Read more")
        self._key(qt.Qt.Key_Return)
        self.assertEqual(self.opened, [self.label], "Enter on the first backward stop opens Read more")
        self._key(qt.Qt.Key_Backtab, qt.Qt.ShiftModifier)
        self.assertIs(slicer.app.focusWidget(), self.before, "the next Shift+Tab goes on to the previous control")

    def test_the_label_has_no_keyboard_link_and_no_event_filter(self):
        """The text label neither takes keyboard links (the two-stop label) nor carries a Python event filter (the first
        fix did: it crashed the application at shutdown, when the label's destruction sent events through the filter).
        """
        import inspect

        flags = int(self.label.property("textInteractionFlags"))  # property(): QFlags read the same on Qt 5 and Qt 6
        self.assertEqual(flags & int(qt.Qt.LinksAccessibleByKeyboard), 0)
        self.assertEqual(int(self.label.focusPolicy), int(qt.Qt.NoFocus))
        code = [line for line in inspect.getsource(self.widgets.disclaimerLine).splitlines() if not line.lstrip().startswith("#")]
        self.assertNotIn("installEventFilter", "\n".join(code))


if __name__ == "__main__":
    unittest.main()

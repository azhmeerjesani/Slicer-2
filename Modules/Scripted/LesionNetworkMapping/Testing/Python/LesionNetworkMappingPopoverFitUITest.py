"""The Recipe header's popovers (Deviations, recipe details) stay on the screen (bug: 'Deviations (n)' at the right of the header
opened a 380 px popover past the right screen edge, cutting the deviation reasons mid-word).

It needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingPopoverFitUITest

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


def _available(widget):
    screen = widget.screen() if hasattr(widget, "screen") else None
    if screen is None:
        screen = qt.QGuiApplication.screens()[0]
    area = screen.availableGeometry
    return area() if callable(area) else area


LONG_TEXT = "<b>Deviations from the locked plan</b><br>" + "<br>".join(
    f"· change {i} instead of planned <i>(reason: " + " ".join(["word"] * 30) + ")</i>" for i in range(4)
)


class LesionNetworkMappingPopoverFitUITest(unittest.TestCase):
    def _showNear(self, corner):
        """Show a popover anchored on a button at the given screen corner ('right', 'bottomRight'); return (frame geometry, anchor rect, area)."""
        from LesionNetworkMappingUI import card_recipe

        top = qt.QWidget()
        top.setWindowFlags(qt.Qt.FramelessWindowHint)
        self.addCleanup(top.deleteLater)
        self.addCleanup(top.close)
        area = _available(top)
        right, bottom = area.x() + area.width(), area.y() + area.height()
        y = bottom - 50 if corner == "bottomRight" else area.y() + 200
        top.setGeometry(right - 120, y, 120, 40)
        button = qt.QPushButton("Deviations (4)", top)
        button.setGeometry(10, 5, 105, 30)
        top.show()
        _pump(200)
        popover = card_recipe.Popover(top, "LNMTestPopover")
        self.addCleanup(popover.frame.hide)
        popover.show(button, LONG_TEXT)
        _pump(200)
        origin = button.mapToGlobal(qt.QPoint(0, 0))
        anchor = qt.QRect(origin.x(), origin.y(), button.width, button.height)
        return popover.frame.geometry, anchor, area

    def assertInside(self, frame, area):
        self.assertGreaterEqual(frame.x(), area.x(), f"popover {frame} starts left of the screen {area}")
        self.assertLessEqual(frame.x() + frame.width(), area.x() + area.width(), f"popover {frame} passes the screen's right edge {area}")
        self.assertGreaterEqual(frame.y(), area.y(), f"popover {frame} starts above the screen {area}")
        self.assertLessEqual(frame.y() + frame.height(), area.y() + area.height(), f"popover {frame} passes the screen's bottom edge {area}")

    def test_anchor_at_the_right_edge_keeps_the_popover_on_screen(self):
        frame, anchor, area = self._showNear("right")
        self.assertInside(frame, area)
        self.assertEqual(frame.y(), anchor.y() + anchor.height(), "the popover opens below its button")
        self.assertEqual(frame.x() + frame.width(), anchor.x() + anchor.width(), "the popover is right-aligned with its button")

    def test_anchor_at_the_bottom_right_opens_above(self):
        frame, anchor, area = self._showNear("bottomRight")
        self.assertInside(frame, area)
        self.assertLessEqual(frame.y() + frame.height(), anchor.y(), "the popover opens above its button when there is no room below")

    def test_position_keeps_left_alignment_when_it_fits(self):
        from LesionNetworkMappingUI import card_recipe

        top = qt.QWidget()
        top.setWindowFlags(qt.Qt.FramelessWindowHint)
        self.addCleanup(top.deleteLater)
        self.addCleanup(top.close)
        area = _available(top)
        top.setGeometry(area.x() + 100, area.y() + 100, 200, 40)
        top.show()
        _pump(200)
        point = card_recipe.Popover.position(top, area, card_recipe.POPOVER_WIDTH, 200)
        origin = top.mapToGlobal(qt.QPoint(0, 0))
        self.assertEqual((point.x(), point.y()), (origin.x(), origin.y() + top.height))
        unclamped = card_recipe.Popover.position(top, None, card_recipe.POPOVER_WIDTH, 200)
        self.assertEqual((unclamped.x(), unclamped.y()), (origin.x(), origin.y() + top.height))


if __name__ == "__main__":
    unittest.main()

"""Test that the deferred default-seed adoption of the Map module does nothing once the widget is torn down (bug
map-adopt-default-seed-traceback-at-exit: a traceback at app exit when a segmentation was added just before).

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingAdoptSeedAtExitTest
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class AdoptSeedAtExitTest(unittest.TestCase):
    def test_adopt_after_cleanup_is_silent(self):
        slicer.util.selectModule("LesionNetworkMapping")
        widget = slicer.modules.LesionNetworkMappingWidget
        self.assertFalse(widget._widgetDestroyed())
        widget._closed = True
        try:
            widget._adoptDefaultSeed()  # must neither raise nor touch the GUI
        finally:
            widget._closed = False

    def test_adopt_with_destroyed_group_is_silent(self):
        slicer.util.selectModule("LesionNetworkMapping")
        widget = slicer.modules.LesionNetworkMappingWidget
        group = widget.seedModeGroup

        class Dead:
            def __getattr__(self, name):
                raise ValueError("Trying to call '%s' on a destroyed QButtonGroup object" % name)

        widget.seedModeGroup = Dead()
        try:
            self.assertTrue(widget._widgetDestroyed())
            widget._adoptDefaultSeed()
        finally:
            widget.seedModeGroup = group

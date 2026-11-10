"""The Result card's Peak region column keeps to the atlases of the map's template family.

``LesionNetworkMappingWidget._peakSpace`` used ``geometry.detect_space(grid)``'s (space, resolution) pair as the space;
``atlases.peak_regions`` read that pair as an "unknown" family and took every cached atlas, so a 2009c map was named
by the FSL-family atlases (Harvard-Oxford, Schaefer NLin6) that the Clusters card refuses for it.

They need the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingPeakSpaceUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import time
import types
import unittest
from unittest import mock

try:
    import qt  # noqa: F401
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


def wait(condition, seconds=30.0):
    deadline = time.time() + seconds
    while not condition() and time.time() < deadline:
        slicer.app.processEvents()
        time.sleep(0.02)
    return condition()


class LesionNetworkMappingPeakSpaceUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        slicer.app.processEvents()

    def test_peak_space_is_the_space_label(self):
        from LesionNetworkMappingLib import atlases, geometry

        widget = slicer.modules.LesionNetworkMappingWidget
        grid2009 = geometry.GRID_BY_ID["TF2009-2mm"].grid
        gridFsl = geometry.GRID_BY_ID["NLin6-2mm"].grid
        self.assertEqual(widget._peakSpace(grid2009), "MNI152NLin2009cAsym")
        self.assertEqual(widget._peakSpace(gridFsl), "MNI152NLin6Asym")
        self.assertIsNone(widget._peakSpace(types.SimpleNamespace(shape=(10, 11, 12), affine=gridFsl.affine)))
        self.assertIsNone(widget._peakSpace(None))

        # The lookup hands that label to peak_regions, whose family filter then refuses the other family's atlases
        self.assertEqual(atlases.space_family(widget._peakSpace(grid2009)), "2009")
        spaces = []
        with mock.patch.object(atlases, "peak_regions", lambda points, space=None, cache=None, atlases=None: spaces.append(space)), \
                mock.patch.object(widget, "_showPeakRegions", lambda peaks, regions: None):
            widget._lookupPeakRegions([{"world": (0.0, 0.0, 0.0)}], grid2009)
            self.assertTrue(wait(lambda: getattr(widget, "_peakRegionJob", None) is None))
        self.assertEqual(spaces, ["MNI152NLin2009cAsym"])


if __name__ == "__main__":
    unittest.main()

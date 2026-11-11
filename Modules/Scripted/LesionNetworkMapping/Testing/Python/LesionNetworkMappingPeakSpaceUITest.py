"""The Result card's Peak region column keeps to the atlases of the map's template family.

``LesionNetworkMappingWidget._peakSpace`` used ``geometry.detect_space(grid)``'s (space, resolution) pair as the space;
``atlases.peak_regions`` read that pair as an "unknown" family and took every cached atlas, so a 2009c map was named
by the FSL-family atlases (Harvard-Oxford, Schaefer NLin6) that the Clusters card refuses for it. It also told the
space from the grid only, so a map on a non-standard grid whose sidecar declares its space was named by every family.

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

    def test_peak_space_prefers_the_declared_space_on_a_nonstandard_grid(self):
        """A 2009c map on a 3 mm grid (no standard grid) whose sidecar declares MNI152NLin2009cAsym: the Peak region
        keeps to the 2009c atlases, as the Clusters card does (it read None from the grid and mixed every family).
        """
        import numpy as np

        from LesionNetworkMappingLib import geometry
        from LesionNetworkMappingUI.card_clusters import labelFamily, volumeSpace

        widget = slicer.modules.LesionNetworkMappingWidget
        affine = np.array([[-3.0, 0, 0, 96.0], [0, 3.0, 0, -132.0], [0, 0, 3.0, -78.0], [0, 0, 0, 1.0]])
        grid = types.SimpleNamespace(shape=(65, 77, 65), affine=affine)
        self.assertIsNone(geometry.detect_space((grid.shape, grid.affine)))
        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "PeakSpaceDeclared")
        volume.SetAttribute("Studio.Space", "MNI152NLin2009cAsym")
        saved = (widget.loadedMap, widget.ctx.resultVolume)
        try:
            widget.loadedMap = {"path": None, "statistic": "T", "n": None, "name": "declared", "label": "declared",
                                "siblings": {}, "space": "MNI152NLin2009cAsym"}
            widget.ctx.resultVolume = volume
            self.assertEqual(widget._peakSpace(grid), "MNI152NLin2009cAsym")
            # The same source as the Clusters card's label family
            self.assertEqual(volumeSpace(volume), widget._peakSpace(grid))
            self.assertEqual(labelFamily(types.SimpleNamespace(grid=grid, space=volumeSpace(volume))), "2009")

            # Declared only on the volume (a map adopted from a scene): the volume's Studio.Space is used
            widget.loadedMap = dict(widget.loadedMap, space=None)
            self.assertEqual(widget._peakSpace(grid), "MNI152NLin2009cAsym")

            # Nothing declared: the grid decides (None for this grid), as before
            volume.RemoveAttribute("Studio.Space")
            self.assertIsNone(widget._peakSpace(grid))
        finally:
            widget.loadedMap, widget.ctx.resultVolume = saved
            slicer.mrmlScene.RemoveNode(volume)


if __name__ == "__main__":
    unittest.main()

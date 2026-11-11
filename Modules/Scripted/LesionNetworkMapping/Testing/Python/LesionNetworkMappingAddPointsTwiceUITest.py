"""Test that Coords > Add points skips the points already in the list (bug add-points-twice-duplicates-foci-in-study).

Pressing Add points twice with the same point list added each point again, so the study had twice its foci, and the
random-coordinate control (which keeps each study's number of foci) drew twice as many random foci for it. Points
already in the list are now skipped and the summary says so, as Import coordinates… does for a file imported twice.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingAddPointsTwiceUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class AddPointsTwiceUITest(unittest.TestCase):
    def setUp(self):
        slicer.mrmlScene.Clear(0)
        slicer.util.selectModule("LesionNetworkMapping")
        slicer.app.processEvents()
        self.widget = slicer.modules.LesionNetworkMappingWidget
        self.widget.setCoordinates([], None)
        self.points = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "My points")
        self.points.AddControlPoint(-40, -20, 50)
        self.points.AddControlPoint(40, -20, 50)
        self.widget.coordMarkupsSelector.setCurrentNode(self.points)
        slicer.app.processEvents()

    def tearDown(self):
        self.widget.setCoordinates([], None)
        slicer.mrmlScene.Clear(0)

    def test_the_same_points_are_added_once(self):
        from LesionNetworkMappingLib import coords

        self.widget.coordAddMarkupsButton.click()
        slicer.app.processEvents()
        self.assertEqual(len(self.widget.coordinateFoci), 2)
        self.widget.coordAddMarkupsButton.click()
        slicer.app.processEvents()
        foci = list(self.widget.coordinateFoci)
        self.assertEqual(len(foci), 2)
        self.assertEqual({name: len(members) for name, members in coords.group_by_study(foci).items()}, {"My points": 2})
        self.assertIn("2 foci already in the list were skipped", self.widget.coordSummaryLabel.text)

    def test_a_moved_or_new_point_is_still_added(self):
        self.assertEqual(len(self.widget.addMarkupsCoordinates(self.points)), 2)
        self.points.AddControlPoint(0, 10, 20)
        added = self.widget.addMarkupsCoordinates(self.points)
        self.assertEqual([(focus.x, focus.y, focus.z) for focus in added], [(0.0, 10.0, 20.0)])
        self.assertEqual(len(self.widget.coordinateFoci), 3)
        self.assertIn("2 foci already in the list were skipped", self.widget.coordSummaryLabel.text)
        # The same positions under another study name are other foci
        self.assertEqual(len(self.widget.addMarkupsCoordinates(self.points, study="Other study")), 3)
        self.assertEqual(len(self.widget.coordinateFoci), 6)


if __name__ == "__main__":
    unittest.main()

"""Test that the Robust card checks the coordinates file of the sphere variants when it is chosen.

A file with a word in a coordinate or a wrong header used to be accepted (Run stayed enabled) and the job then failed
with a raw "Analysis failed: ValueError: could not convert string to float: 'left'" that named neither file, row nor
column (bug map-robust-card:robust-coordinates-file-errors-raw-and-late). Now the file is read when it is chosen: a bad
file is not kept, Run says why (file, row, column) and the notice shows the same text.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingRobustCoordinatesUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

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


class RobustCoordinatesUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        _pump(50)
        cls.robust = slicer.modules.LesionNetworkMappingWidget.ctx.card("robust")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-robust-coords-")

    def tearDown(self):
        self.robust.setCoordinates(None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        _pump(50)

    def write(self, text, name="seeds.tsv"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def sphereState(self):
        """runState("seeds") with the sphere variants ticked, as if a study and a connectome were chosen."""
        for variant, check in self.robust.variantChecks.items():
            check.setChecked(variant.startswith("sphere"))
        with mock.patch.object(self.robust, "ctx", mock.MagicMock(study=object(), bundle=object())):
            return self.robust.runState("seeds")

    def test_a_bad_value_is_refused_when_the_file_is_chosen(self):
        self.robust.setCoordinates(self.write("participant_id\tx\ty\tz\nsub-01\tleft\t2\t3\n"))
        _pump(50)
        self.assertIsNone(self.robust._coordinates)
        enabled, reason = self.sphereState()
        self.assertFalse(enabled)
        for part in ("seeds.tsv", "row 1", "column x", "'left'"):
            self.assertIn(part, reason)
        self.assertNotIn(self.tmp, reason)
        self.assertEqual(self.robust.notice, reason)

    def test_a_wrong_header_is_refused_and_a_good_file_clears_the_message(self):
        self.robust.setCoordinates(self.write("subject\tx\ty\tz\nsub-01\t1\t2\t3\n"))
        self.assertIn("participant_id", self.sphereState()[1])
        good = self.write("participant_id\tx\ty\tz\nsub-01\t1\t2\t3\n", "good.tsv")
        self.robust.setCoordinates(good)
        _pump(50)
        self.assertEqual(self.robust._coordinates, good)
        self.assertIsNone(self.robust._coordinatesError)
        self.assertEqual(self.robust.notice, "")
        self.assertNotIn("participant_id", self.sphereState()[1])


if __name__ == "__main__":
    unittest.main()

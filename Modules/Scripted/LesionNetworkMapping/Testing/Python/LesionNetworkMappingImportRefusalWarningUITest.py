"""Test that refusing a user's coordinate or stimulation file is a warning, not a program error.

importCoordinates and importStimulation used slicer.util.errorDisplay, which logs at ERROR and counts up the top-bar
error badge (bug map-single:coordinate-and-stimulation-file-refusals-logged-as-errors-light-badge). A problem with the
user's own file is shown with warningDisplay, as the Library does.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingImportRefusalWarningUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class ImportRefusalWarningUITest(unittest.TestCase):
    def setUp(self):
        slicer.util.selectModule("LesionNetworkMapping")
        slicer.app.processEvents()
        self.widget = slicer.modules.LesionNetworkMappingWidget
        self.tmp = tempfile.mkdtemp(prefix="lnm-import-refusal-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def empty(self, name):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        return path

    def check(self, method, name):
        with mock.patch.object(slicer.util, "errorDisplay") as err, mock.patch.object(slicer.util, "warningDisplay") as warn:
            result = method(self.empty(name))
        self.assertIsNone(result)
        err.assert_not_called()
        warn.assert_called_once()

    def test_unreadable_coordinate_file_is_a_warning(self):
        self.check(self.widget.importCoordinates, "empty.csv")

    def test_unreadable_stimulation_file_is_a_warning(self):
        self.check(self.widget.importStimulation, "stim_empty.csv")


if __name__ == "__main__":
    unittest.main()

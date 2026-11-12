"""Test that a focus repeated inside one file is not reported as "already in the list" (bug coords-in-file-duplicate-reported-already-in-list).

Importing a file that lists the same focus twice into an empty list said "1 focus already in the list was skipped".
The note now counts foci already in the list and foci repeated in the file separately.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingInFileDuplicateNoteUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class InFileDuplicateNoteUITest(unittest.TestCase):
    def setUp(self):
        slicer.util.selectModule("LesionNetworkMapping")
        slicer.app.processEvents()
        self.widget = slicer.modules.LesionNetworkMappingWidget
        self.widget.setCoordinates([], None)

    def tearDown(self):
        self.widget.setCoordinates([], None)

    def _foci(self):
        from LesionNetworkMappingLib.coords import Focus

        return [Focus(-2, 40, 20, "Jones 2012"), Focus(-2, 40, 20, "Jones 2012"), Focus(10, 20, 30, "Jones 2012"), Focus(1, 2, 3, "Lee 2015")]

    def test_a_repeat_inside_the_file_is_not_already_in_the_list(self):
        foci = self._foci()
        self.assertEqual(len(self.widget._appendNewFoci(foci, None)), 3)
        text = self.widget.coordSummaryLabel.text
        self.assertIn("1 duplicate focus in the file was skipped", text)
        self.assertNotIn("already in the list", text)

    def test_both_cases_are_counted_separately(self):
        foci = self._foci()
        self.widget._appendNewFoci(foci, None)
        self.assertEqual(self.widget._appendNewFoci(foci, None), [])
        text = self.widget.coordSummaryLabel.text
        self.assertIn("3 foci already in the list were skipped", text)
        self.assertIn("1 duplicate focus in the file was skipped", text)


if __name__ == "__main__":
    unittest.main()

"""Test of the "Not mapped" line of the Map module's Cohort seed (bug cohort-not-mapped-missing-lesion-singular).

The line read "Not mapped: 3 missing lesion" (the raw reason after a bare count). The count now follows the reason, so
that no reason is wrongly singular or plural.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCohortExcludedTextTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class CohortExcludedTextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        import LesionNetworkMapping

        cls.module = LesionNetworkMapping

    def test_count_follows_the_reason(self):
        text = self.module.cohortExcludedText({"missing lesion": ["sub-01", "sub-02", "sub-03"], "include=0": ["sub-04"], "qc=fail": []})
        self.assertEqual(text, "Not mapped: missing lesion (3) · include=0 (1)")
        self.assertNotIn("3 missing lesion", text)

    def test_nothing_excluded(self):
        self.assertEqual(self.module.cohortExcludedText({}), "")
        self.assertEqual(self.module.cohortExcludedText({"missing lesion": []}), "")

    def test_label_of_the_cohort_seed(self):
        widget = slicer.modules.LesionNetworkMappingWidget
        study, info = widget.cohortStudy, widget.cohortInfo
        try:
            class _Study:
                name = "S"
                path = ""

            widget.cohortStudy = _Study()
            widget.cohortInfo = {"n": 0, "lesions": [], "native": [], "groups": {}, "excluded": {"missing lesion": ["a", "b", "c"]}}
            widget._showCohort()
            self.assertEqual(widget.cohortExcludedLabel.text, "Not mapped: missing lesion (3)")
        finally:
            widget.cohortStudy, widget.cohortInfo = study, info
            widget._showCohort()


if __name__ == "__main__":
    unittest.main()

"""Test that Methods describes a null-model run in words, not as a key dump (bug methods-null-models-paragraph-raw-key-dump).

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingMethodsNullsTextTest
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

SUMMARY = {"kind": "rl_independent", "mode": "exact_pooled", "k": 1000, "n_lesions": 6, "n_fwe_05": 0, "min_attainable_p": 0.000999}


class MethodsNullsTextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        from LesionNetworkMappingLib import export, methods, nulls

        cls.methods, cls.export, cls.nulls = methods, export, nulls

    def test_sentence_is_readable(self):
        text = self.methods.nulls_text(SUMMARY)
        for key in ("rl_independent", "exact_pooled", "n_fwe_05", "min_attainable_p", "n_lesions"):
            self.assertNotIn(key, text)
        self.assertIn("independent relocation", text)
        self.assertIn("exact pooled", text)
        self.assertIn("1000", text)
        self.assertIn("6 case lesions", text)
        self.assertIn("no voxel reached PFWE <= 0.05", text)
        self.assertIn("smallest attainable p = 0.000999", text)

    def test_missing_values_are_not_recorded(self):
        text = self.methods.nulls_text({"kind": "rl_independent"})
        self.assertIn(self.export.NOT_RECORDED, text)
        self.assertEqual(self.methods.nulls_text(None), "")

    def test_citation_resolves_to_the_reference_list(self):
        for caption in (self.methods.NULL_CAPTION, self.nulls.CAPTION):
            self.assertNotIn("2026a", caption)
            self.assertNotIn("2026b", caption)
        self.assertEqual(len([v for v in self.export.CITATIONS.values() if "van den Heuvel" in v]), 1)

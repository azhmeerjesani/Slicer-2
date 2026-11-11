"""Test of the readable stratum names in the fast index gate texts (bug fast-gate-texts-show-raw-stratum-ids).

The gate sentence, the fast-mode reason and the Nulls parity line showed the internal ids ("white-matter-supervoxel",
"brainstem"); they now use the names of the parity table.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingFastGateTextTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class FastGateTextTest(unittest.TestCase):
    def test_strata_text(self):
        from LesionNetworkMappingUI import manager_fastindex

        self.assertEqual(manager_fastindex.strataText(["brainstem", "white-matter-supervoxel", "other"]), "brain stem, white matter (supervoxel), other")
        self.assertEqual(manager_fastindex.strataText(None), "")

    def test_failing_text(self):
        from LesionNetworkMappingUI import manager_fastindex

        gate = {"strata": {"brainstem": {"passed": False, "reason": "median partial r 0.61 < 0.80"}}, "size_bins": {"lt1ml": {"passed": False, "reason": "x"}}}
        text = manager_fastindex.failingText(gate)
        self.assertIn("brain stem (median partial r 0.61 < 0.80)", text)
        self.assertIn("< 1 mL (x)", text)
        self.assertNotIn("brainstem", text)

    def test_nulls_parity_line(self):
        from LesionNetworkMappingUI import card_nulls

        record = {"approximate": True, "parity": {"allowed_strata": ["white-matter-supervoxel"], "constants": {"partial_r": 0.8, "top10_dice": 0.7}}}
        cls = next(getattr(card_nulls, n) for n in dir(card_nulls) if hasattr(getattr(card_nulls, n), "parityText"))
        text = cls.parityText(record)
        self.assertIn("white matter (supervoxel)", text)
        self.assertNotIn("white-matter-supervoxel", text)

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

    def test_missing_atlases_are_named_by_catalog_title(self):
        # Bug fastindex-missing-atlases-shown-as-catalog-ids: "Not downloaded yet" listed ids such as tian-s2-2mm.
        from LesionNetworkMappingLib import fetch
        from LesionNetworkMappingUI import manager_fastindex

        title = fetch.find_resource("tian-s2-2mm", ("atlases",)).title
        self.assertNotEqual(title, "tian-s2-2mm")
        self.assertEqual(manager_fastindex.atlasTitle("tian-s2-2mm"), title)
        self.assertEqual(manager_fastindex.atlasTitle("no-such-atlas"), "no-such-atlas")

    def test_gate_notes_have_no_raw_ids_or_python_command(self):
        # Bug fastindex-gate-text-raw-stratum-ids-and-python-command: the gate text repeated the failures with raw ids and named a command.
        from LesionNetworkMappingLib import fastmap
        from LesionNetworkMappingUI import manager_fastindex

        lesions = [{"name": f"l{i}", "stratum": s, "size_bin": None, "partial_r": 0.1, "top10_dice": 0.1} for s in ("brainstem", "white-matter-supervoxel") for i in range(6)]
        gate = fastmap.gate({"lesions": lesions, "size_axis": False, "reference": None}, min_lesions=2)
        self.assertTrue(any("not allowed for brainstem" in m for m in gate["messages"]))
        notes = " ".join(manager_fastindex.gateNotes(gate))
        self.assertNotIn("brainstem", notes)
        self.assertNotIn("white-matter-supervoxel", notes)
        self.assertNotIn("not allowed for", notes)
        self.assertNotIn("python", notes)
        self.assertIn("Manage… → Build reference maps", notes)

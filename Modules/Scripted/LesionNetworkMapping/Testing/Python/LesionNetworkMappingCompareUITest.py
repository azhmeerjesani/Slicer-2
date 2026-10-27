"""Test that an exported Compare card comparison is recorded in the run registry, so checklist item G10 sees it.

The Compare card writes ``derivatives/lnm/compare/<time>`` and registers a done run of kind "comparison" (its summary
counts the reference maps of the library). A comparison of maps of this study only does not make G10 "done"; one with a
reference map of the library does.

It needs the Slicer application and uses the synthetic study of ``lnm_pipeline_fixture.py`` (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCompareUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
import sys
import tempfile
import unittest

try:
    import qt  # noqa: F401
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)


class LesionNetworkMappingCompareUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import lnm_pipeline_fixture as fixture

        from LesionNetworkMapping import LesionNetworkMappingLogic

        cls.tmp = tempfile.mkdtemp(prefix="lnm compare ui ")
        cls.fx = fixture.make_fixture(os.path.join(cls.tmp, "f1"), extra_rows=False)
        cls.study = cls.fx["study"]
        slicer.util.selectModule("LesionNetworkMapping")
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.bundle = LesionNetworkMappingLogic.registerConnectome(cls.fx["bundle"])
        LesionNetworkMappingLogic.setTermsAccepted(cls.bundle, True)
        cls.widget.refreshConnectomes(select=cls.bundle.path)
        cls.widget.openCohortStudy(cls.study)
        cls.card = cls.widget.ctx.card("compare")

    @classmethod
    def tearDownClass(cls):
        try:
            from StudioLib import study_ui

            study_ui.closeStudy()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _compare(self, withReference):
        import numpy as np

        from LesionNetworkMappingLib import refmaps
        from LesionNetworkMappingUI.card_compare import MapSource

        rng = np.random.default_rng(1)
        base = rng.standard_normal(200)
        maps = {"Group T": base + rng.standard_normal(200), "Other": base + rng.standard_normal(200)}
        sources = [MapSource("group:a", "Group T", "group", None, {"kind": "group", "desc": "T"}),
                   MapSource("group:b", "Other", "group", None, {"kind": "group", "desc": "Other"})]
        if withReference:
            maps["Pain reference"] = base + rng.standard_normal(200)
            sources.append(MapSource("reference:nv-1", "Pain reference", "reference", None,
                                     {"kind": "neurovault", "id": "nv-1", "title": "Pain reference", "licence": "CC0-1.0", "condition": "pain", "sha256": "ab" * 32}))
        self.card.comparison = refmaps.compare(maps, None)
        self.card.comparedSources = sources
        self.card.tests = {}
        self.card.top = []

    def _g10(self):
        from LesionNetworkMappingLib import checklist

        return next(item for item in checklist.evaluate(self.study) if item["id"] == "G10")

    def test_1_study_only_comparison_is_registered_but_not_cross_condition(self):
        from LesionNetworkMappingLib import runs

        self.assertEqual(self._g10()["status"], "not_run")
        self._compare(withReference=False)
        out = self.card.export()
        self.assertTrue(out and os.path.isfile(os.path.join(out, "compare.json")), self.card.notice)
        records = [record for record in runs.load(self.study).values() if record["kind"] == "comparison"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "done")
        self.assertEqual(records[0]["id"], self.card.lastRunId)
        self.assertEqual(runs.read_summary(self.study, records[0])["n_reference_maps"], 0)
        self.assertEqual(self._g10()["status"], "not_run")

    def test_2_comparison_with_a_library_map_makes_g10_done(self):
        from LesionNetworkMappingLib import runs

        self._compare(withReference=True)
        out = self.card.export()
        self.assertTrue(out and os.path.isfile(os.path.join(out, "compare.json")), self.card.notice)
        record = runs.load(self.study)[self.card.lastRunId]
        self.assertEqual(record["kind"], "comparison")
        self.assertEqual(runs.read_summary(self.study, record)["n_reference_maps"], 1)
        self.assertNotIn(self.tmp, str(record["dir"]))  # relative to the study
        item = self._g10()
        self.assertEqual(item["status"], "done", item)
        self.assertEqual(item["evidence"], [self.card.lastRunId])


if __name__ == "__main__":
    unittest.main()

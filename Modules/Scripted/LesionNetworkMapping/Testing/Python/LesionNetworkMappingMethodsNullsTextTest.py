"""Test that Methods describes a null-model run in words, not as a key dump (bug methods-null-models-paragraph-raw-key-dump).

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingMethodsNullsTextTest
"""

import json
import os
import shutil
import tempfile
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

RULE = {"kind": "top_pct", "value": 10, "statistic": "Fz", "sign": "both"}
PARITY = {
    "source": "computed",
    "n": 12,
    "constants": {"partial_r": 0.8, "top10_dice": 0.6},
    "strata": {"cortical": {"n": 8, "median_partial_r": 0.91, "median_top10_dice": 0.72, "passed": True, "reason": "passed"},
               "subcortical": {"n": 3, "median_partial_r": 0.62, "median_top10_dice": 0.41, "passed": False, "reason": "median partial r 0.62 < 0.80"}},
    "size_bins": {},
    "excluded": [{"name": "sub-07", "stratum": "subcortical", "size_bin": None, "message": "Excluded from fast-mode statistics."}],
}
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

    def test_status_rule_k_pool_seed_and_parity(self):
        """Bug methods-nulls-paragraph-omits-status-rule-parity: addendum 3.6 status tag, rule, K_pool, seed, parity numbers."""
        fast = self.methods.nulls_text(
            {"kind": "rl_overlap", "mode": "approximate", "k": 200, "n_lesions": 11, "n_fwe_05": 0, "min_attainable_p": 1 / 201},
            {"kind": "rl_overlap", "mode": "approximate", "k": 200, "k_pool": 100, "rule": RULE, "sign": "both", "overlap": 90.0},
            {"null": 12345},
            {"kind_status": self.nulls.KIND_INFO["rl_overlap"]["status"], "parity": PARITY},
        )
        self.assertIn("single preprint (Cui 2026), experimental", fast)
        self.assertIn("Top x%", fast)
        self.assertIn("value 10", fast)
        self.assertIn("≥ 90% overlap", fast)
        self.assertIn("seed 12345", fast)
        self.assertIn("parity gate on 12 exactly mapped lesions", fast)
        self.assertIn("median partial r >= 0.8", fast)
        self.assertIn("cortical n = 8, median partial r 0.91, median top-10% Dice 0.72 (passed)", fast)
        self.assertIn("(failed)", fast)
        self.assertIn("1 lesion left out by the gate (sub-07)", fast)
        self.assertNotIn("K_pool", fast)  # the fast overlap-preserving null has no K_pool (as in its sidecar)
        exact = self.methods.nulls_text(
            {"kind": "rl_independent", "mode": "exact_pooled", "k": 1000, "n_lesions": 11, "n_fwe_05": 0, "min_attainable_p": 0.000999},
            {"kind": "rl_independent", "mode": "exact_pooled", "k": 1000, "k_pool": 20, "rule": RULE, "sign": "pos", "overlap": 75.0},
            {"null": 7},
        )
        self.assertIn("[status: established]", exact)  # from nulls.KIND_INFO when there is no nulls.json
        self.assertIn("pooled exact null (K_pool = 20 per lesion)", exact)
        self.assertIn("K_pool = 20 relocations per lesion", exact)
        self.assertIn("seed 7", exact)
        self.assertNotIn("parity", exact)

    def test_methods_sections_use_params_seeds_and_nulls_json(self):
        from LesionNetworkMappingLib import runs

        root = tempfile.mkdtemp(prefix="lnm-methods-nulls-")
        self.addCleanup(shutil.rmtree, root, True)
        out = os.path.join(root, "derivatives", "lnm", "nulls", "rl_overlap")
        os.makedirs(out)
        run_id = runs.register(root, "nulls", out, params={"kind": "rl_overlap", "mode": "approximate", "k": 200, "k_pool": 100, "rule": RULE, "sign": "both", "overlap": 90.0},
                               seeds={"null": 4242}, software_versions={})
        with open(os.path.join(out, self.nulls.NULLS_JSON), "w", encoding="utf-8") as f:
            json.dump({"run_id": run_id, "kind": "rl_overlap", "kind_status": self.nulls.KIND_INFO["rl_overlap"]["status"], "parity": PARITY}, f)
        runs.update(root, run_id, status="done", summary={"kind": "rl_overlap", "mode": "approximate", "k": 200, "n_lesions": 11, "n_fwe_05": 0, "min_attainable_p": 1 / 201})
        text = self.methods.render(self.methods.methods_sections(root), fmt="txt")
        paragraph = next(line for line in text.split("\n") if line.startswith("Null models"))
        for expected in ("experimental", "Top x%", "seed 4242", "parity gate on 12 exactly mapped lesions", "left out by the gate (sub-07)"):
            self.assertIn(expected, paragraph)

    def test_rl_strength_names_the_strength_map(self):
        """Bug rl-strength-abs-described-as-signed-muc-methods-omit-strength-map."""
        summary = {**SUMMARY, "kind": "rl_strength"}
        signed = self.methods.nulls_text(summary, params={"kind": "rl_strength", "strength": "signed"})
        absolute = self.methods.nulls_text(summary, params={"kind": "rl_strength", "strength": "abs"})
        self.assertIn("signed mean connectivity", signed)
        self.assertIn("absolute hubness", absolute)
        self.assertNotIn("hubness", signed)
        self.assertIn("absolute hubness", self.methods.nulls_text(summary, record={"design": {"strength": "abs"}}))
        self.assertIn(self.export.NOT_RECORDED, self.methods.nulls_text(summary))
        self.assertIn("hubness", self.nulls.kind_description("rl_strength", "abs"))
        self.assertNotIn("signed", self.nulls.kind_description("rl_strength", "abs"))
        self.assertIn("signed mean connectivity", self.nulls.kind_description("rl_strength", "signed"))
        self.assertEqual(self.nulls.kind_description("rl_independent", "abs"), self.nulls.KIND_INFO["rl_independent"]["description"])

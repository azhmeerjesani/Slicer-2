"""Tests of the GLM design builder (design.py) and the PALM round-trip (palm.py).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingDesignTest.py -v
"""

import dataclasses
import itertools
import json
import math
import os
import shlex
import shutil
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import design as designlib
from LesionNetworkMappingLib import nifti, palm
from LesionNetworkMappingLib.geometry import mask_indices
from LesionNetworkMappingLib.study import Study

DesignSpec = designlib.DesignSpec


def _codes(check_list, level=None):
    return [check.code for check in check_list if level is None or check.level == level]


def _designs(permutations, labels):
    """Distinct designs tested by data-row index arrays P (Y[P] against X is Y against X[P⁻¹])."""
    labels = np.asarray(labels)
    return {tuple(labels[np.argsort(permutation)].tolist()) for permutation in permutations}


class _StudyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-design-test-")
        self.rng = np.random.default_rng(1234)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def make_study(self, rows, name="study", roles=None):
        study = Study.create(self.path(name), name=name)
        for sid, values in rows:
            study.add_subject(sid, values)
        for column, role in (roles or {}).items():
            study.set_column_role(column, role)
        return study

    def volumes(self, n):
        return np.round(self.rng.uniform(5.0, 40.0, n), 2)

    def nested_study(self, n_cases=8, n_controls=12):
        """Literature cases (one dataset, 2D tracings) against a control-lesion database (3D)."""
        rows = []
        volumes = 5.0 + 1.37 * self.rng.permutation(n_cases + n_controls)  # distinct
        for index in range(n_cases):
            # Case reports need an approved review before analyses use them (Study.review_required)
            rows.append((f"sub-c{index:02d}", {"group": "case", "volume_ml": volumes[index], "dataset": "literature", "source_type": "case_report", "dimensionality": "2D", "review_status": "approved"}))
        for index in range(n_controls):
            rows.append((f"sub-k{index:02d}", {"group": "control", "volume_ml": volumes[n_cases + index], "dataset": "ctrldb", "source_type": "control_db", "dimensionality": "3D", "review_status": "approved"}))
        return self.make_study(rows, "nested")

    def crossed_study(self, datasets=("d1", "d2", "d3"), per_level=5):
        """Every dataset holds cases and controls (symptom-anchored, within-cohort)."""
        rows = []
        for dataset in datasets:
            for level in ("case", "control"):
                for index in range(per_level):
                    rows.append((f"sub-{dataset}{level[:2]}{index}", {"group": level, "volume_ml": float(self.rng.uniform(5, 40)), "dataset": dataset, "source_type": "own_scan"}))
        return self.make_study(rows, "crossed")


class DesignSpecTest(_StudyTestCase):
    def test_defaults_and_round_trip(self):
        spec = DesignSpec()
        self.assertEqual(spec.test, "two_group")
        self.assertEqual(spec.covariates, ["volume_ml"])
        self.assertEqual(spec.tails, 2)
        self.assertEqual(spec.n_perm, designlib.DEFAULT_N_PERM)
        self.assertEqual(spec.inference["fwe"], True)
        self.assertEqual(spec.inference["tfce"], False)
        data = spec.to_dict()
        self.assertEqual(data["format"], "slicer-lnm-design")
        self.assertEqual(data["version"], 1)
        data["futureKey"] = {"x": 1}
        again = DesignSpec.from_dict(json.loads(json.dumps(data)))
        self.assertEqual(again.to_dict(), data)  # unknown keys are kept
        self.assertEqual(DesignSpec(contrast="A>B").tails, 1)
        self.assertEqual(DesignSpec(contrast="F").tails, 1)
        path = spec.save(self.path("design.json"))
        self.assertEqual(DesignSpec.load(path).to_dict(), spec.to_dict())

    def test_contrast_change_after_save(self):
        # Tails are derived from the contrast: a spec that was saved (tails resolved) can change its contrast
        path = DesignSpec(inference={"n_perm": 2000}).save(self.path("design.json"))
        data = DesignSpec.load(path).to_dict()
        self.assertEqual(data["inference"]["tails"], 2)
        for contrast, tails in (("A>B", 1), ("B>A", 1), ("F", 1), ("two_sided", 2)):
            with self.subTest(contrast=contrast):
                edited = DesignSpec.from_dict({**data, "contrast": contrast})
                self.assertEqual(edited.tails, tails)
                self.assertEqual(edited.n_perm, 2000)
                replaced = dataclasses.replace(DesignSpec.load(path), contrast=contrast)
                self.assertEqual(replaced.tails, tails)
                again = DesignSpec.load(replaced.save(self.path(f"design-{tails}.json")))
                self.assertEqual(dataclasses.replace(again, contrast="two_sided").tails, 2)
        self.assertEqual(DesignSpec(contrast="A>B", inference={"tails": 2}).tails, 1)  # a stored value is replaced

    def test_invalid_specs(self):
        for kwargs in (
            {"test": "paired"},
            {"contrast": "positive"},
            {"test": "continuous"},  # no score column
            {"test": "continuous", "score_column": "moca", "contrast": "A>B"},
            {"inference": {"n_perm": 999}},
            {"inference": {"tails": 3}},
            {"transforms": {"volume_ml": "sqrt"}},
            {"permute": "sometimes"},
            {"input": "R"},
            {"levels": ["case", "case"]},
            {"levels": ["a", "b", "c"]},  # two-group
            {"levels": ["case"]},  # group tests need 2 levels
            {"test": "one_sample", "levels": []},
            {"test": "one_sample", "group_column": None, "levels": ["case"]},
            {"covariates": ["group"]},
            {"covariates": ["age", "age"]},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(designlib.DesignError):
                DesignSpec(**kwargs)
        with self.assertRaises(designlib.DesignError):
            DesignSpec.from_dict({"format": "slicer-lnm-design", "version": 2})
        with self.assertRaises(designlib.DesignError):
            DesignSpec.from_dict({"format": "other"})
        with self.assertRaises(designlib.DesignError):
            DesignSpec.from_dict({"format": "slicer-lnm-design", "version": "1.0"})


class CodingTest(_StudyTestCase):
    def coding_study(self):
        rows = []
        volumes = [4.0, 9.0, 16.0, 25.0, 36.0, 49.0, 64.0, 81.0, 100.0, 121.0, 144.0, 169.0]
        ages = [50, 61, 47, 70, 66, 58, 52, 73, 69, 44, 60, 55]
        sexes = ["M", "F", "F", "M", "F", "M", "M", "F", "M", "F", "F", "M"]
        for index in range(12):
            group = "case" if index < 6 else "control"
            rows.append((f"sub-{index:02d}", {"group": group, "volume_ml": volumes[index], "age": ages[index], "sex": sexes[index]}))
        return self.make_study(rows, "coding"), np.array(volumes), np.array(ages, dtype=float), sexes

    def test_dummy_coding_and_centring(self):
        study, volumes, ages, sexes = self.coding_study()
        spec = DesignSpec(covariates=["volume_ml", "age", "sex"], transforms={"volume_ml": "log"}, inference={"n_perm": 1000})
        design = designlib.build_design(study, spec)
        self.assertEqual(design.columns, ["intercept", "group[case]", "log(volume_ml)", "age", "sex[F]"])
        np.testing.assert_array_equal(design.X[:, 0], 1.0)
        np.testing.assert_array_equal(design.X[:, 1], [1.0] * 6 + [0.0] * 6)  # the last level (control) is the reference
        np.testing.assert_allclose(design.X[:, 2], np.log(volumes) - np.log(volumes).mean())
        np.testing.assert_allclose(design.X[:, 3], ages - ages.mean())
        self.assertAlmostEqual(float(design.X[:, 3].mean()), 0.0)
        np.testing.assert_array_equal(design.X[:, 4], [1.0 if sex == "F" else 0.0 for sex in sexes])  # the first level (M) is the reference
        np.testing.assert_array_equal(design.C, [[0, 1, 0, 0, 0]])
        self.assertEqual(design.nuisance, [0, 2, 3, 4])
        self.assertEqual(design.levels, ["case", "control"])
        self.assertEqual(design.n_per_level, {"case": 6, "control": 6})
        self.assertEqual(design.rank, 5)
        self.assertEqual(design.stat, "t")
        self.assertEqual(design.contrast_names, ["casevscontrol"])
        self.assertEqual(design.design_type, "within_cohort")
        # B>A flips the sign; A>B keeps it one-tailed
        flipped = designlib.build_design(study, DesignSpec(contrast="B>A", covariates=["volume_ml"], inference={"n_perm": 1000}))
        np.testing.assert_array_equal(flipped.C, [[0, -1, 0]])
        self.assertEqual(flipped.tails, 1)

    def test_cell_means_f(self):
        study, _, _, _ = self.coding_study()
        design = designlib.build_design(study, DesignSpec(contrast="F", covariates=["volume_ml"]))
        self.assertEqual(design.columns, ["group[case]", "group[control]", "volume_ml"])
        np.testing.assert_array_equal(design.C, [[1, -1, 0]])
        self.assertEqual(design.stat, "F")
        self.assertNotIn("no_intercept", _codes(design.checks))  # cell means span the constant
        self.assertEqual(design.nuisance, [2])

    def test_anova_levels_and_exclusions(self):
        rows = []
        arms = ["a", "b", "c"]
        for index in range(18):
            values = {"arm": arms[index % 3], "volume_ml": 10.0 + index, "group": "case"}
            rows.append((f"sub-{index:02d}", values))
        rows.append(("sub-x1", {"arm": "d", "volume_ml": 3.0}))
        rows.append(("sub-x2", {"arm": "a", "volume_ml": "n/a"}))
        rows.append(("sub-x3", {"arm": "b", "volume_ml": 4.0, "include": "0"}))
        study = self.make_study(rows, "anova", roles={"arm": "group"})
        design = designlib.build_design(study, DesignSpec(test="anova", group_column="arm", levels=["a", "b", "c"], contrast="F"))
        self.assertEqual(design.n, 18)
        self.assertEqual(design.C.shape, (2, 4))
        np.testing.assert_array_equal(design.C, [[1, 0, -1, 0], [0, 1, -1, 0]])
        self.assertEqual(design.contrast_names, ["a-c", "b-c"])
        self.assertEqual(design.excluded, {"include=0": ["sub-x3"], "missing volume_ml": ["sub-x2"], "arm not in levels (a, b, c)": ["sub-x1"]})
        self.assertIn("excluded", _codes(design.checks, "info"))
        # Treatment coding with the first two levels compared
        two = designlib.build_design(study, DesignSpec(test="anova", group_column="arm", levels=["a", "b", "c"], contrast="A>B"))
        self.assertEqual(two.columns, ["intercept", "arm[a]", "arm[b]", "volume_ml"])
        np.testing.assert_array_equal(two.C, [[0, 1, -1, 0]])
        # The variance groups (v statistic) are two-group only
        blocked = designlib.build_design(study, DesignSpec(test="anova", group_column="arm", contrast="F", variance_groups=True))
        self.assertIn("variance_groups_two_group_only", _codes(blocked.checks, "block"))

    def test_unknown_columns_and_no_subjects(self):
        study, _, _, _ = self.coding_study()
        with self.assertRaises(designlib.DesignError):
            designlib.build_design(study, DesignSpec(covariates=["nonexistent"]))
        empty = designlib.build_design(study, DesignSpec(levels=["case", "patient"], covariates=["volume_ml"]))
        self.assertIn("empty_level", _codes(empty.checks, "block"))
        self.assertEqual(empty.excluded, {"group not in levels (case, patient)": [f"sub-{index:02d}" for index in range(6, 12)]})
        with self.assertRaises(designlib.DesignError):
            designlib.build_design(study, DesignSpec(levels=["x", "y"]))  # no subject left
        with self.assertRaises(designlib.DesignError):
            designlib.build_design(study, DesignSpec(covariates=["sex"], transforms={"sex": "log"}))

    def test_continuous_and_one_sample(self):
        rows = [(f"sub-{index:02d}", {"group": "case", "volume_ml": 5.0 + index, "moca": 20 + (index * 7) % 9}) for index in range(14)]
        study = self.make_study(rows, "scores", roles={"moca": "score"})
        design = designlib.build_design(study, DesignSpec(test="continuous", score_column="moca", contrast="negative"))
        self.assertEqual(design.design_type, "continuous")
        self.assertEqual(design.columns, ["intercept", "moca", "volume_ml"])
        np.testing.assert_array_equal(design.C, [[0, -1, 0]])
        self.assertEqual(design.n_per_level, {"all": 14})
        self.assertNotIn("one_sample", _codes(design.checks))
        one = designlib.build_design(study, DesignSpec(test="one_sample"))
        self.assertEqual(one.design_type, "one_sample")
        self.assertEqual(one.exchange, "sign_flip")
        self.assertEqual(one.unique_perms, 2**14)
        warnings = [check for check in one.checks if check.code == "one_sample"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].level, "warn")
        self.assertEqual(warnings[0].message, "One-sample test: no comparison group; one-sample LNM maps converge on connectome degree (Petersen 2026)")

    def test_one_sample_of_one_level(self):
        rows = [(f"sub-{index:02d}", {"group": "case" if index < 10 else "control", "volume_ml": 5.0 + index}) for index in range(16)]
        study = self.make_study(rows, "cases")
        everyone = designlib.build_design(study, DesignSpec(test="one_sample"))
        self.assertEqual(everyone.n, 16)
        cases = designlib.build_design(study, DesignSpec(test="one_sample", levels=["case"]))
        self.assertEqual(cases.n, 10)
        self.assertEqual(cases.excluded, {"group not in levels (case)": [f"sub-{index:02d}" for index in range(10, 16)]})
        self.assertEqual(cases.unique_perms, 2**10)
        self.assertEqual(cases.columns, ["intercept", "volume_ml"])


class NestingTest(_StudyTestCase):
    def test_nested_study_is_free_exchange_with_limitations(self):
        study = self.nested_study()
        design = designlib.build_design(study, DesignSpec())
        self.assertEqual(design.design_type, "cases_vs_control_database")
        self.assertEqual(design.permute, "free")
        self.assertIsNone(design.blocks)
        self.assertEqual(design.block_column, "dataset")
        self.assertIn("nested", design.permute_reason)
        self.assertEqual(design.crosstab, {"literature": {"case": 8, "control": 0}, "ctrldb": {"case": 0, "control": 12}})
        joined = " | ".join(design.limitations)
        for column in ("dataset", "source_type", "dimensionality"):
            self.assertIn(f"{column} (Cramér's V = 1.00 with group) cannot be adjusted for in this design", joined)
        self.assertTrue(design.ok, [check.message for check in design.blocking])
        self.assertIn("nested", _codes(design.checks, "info"))
        self.assertIn("small_group", _codes(design.checks, "warn"))  # 8 cases
        self.assertNotIn("dimensionality_imbalance", _codes(design.checks))  # collinear: a limitation instead
        self.assertEqual(design.unique_perms, math.factorial(20))
        self.assertFalse(design.exhaustive)
        self.assertEqual(design.min_p, 1 / 5000)

    def test_collinear_dataset_covariate_blocks_and_is_kept(self):
        study = self.nested_study()
        design = designlib.build_design(study, DesignSpec(covariates=["volume_ml", "dataset"]))
        self.assertIn("dataset[literature]", design.columns)  # never dropped
        blocking = {check.code: check for check in design.blocking}
        self.assertIn("collinear_covariate", blocking)
        self.assertIn("every dataset holds a single group level", blocking["collinear_covariate"].message)
        self.assertIn("rank_deficient", blocking)
        self.assertFalse(design.ok)

    def test_within_on_nested_blocks(self):
        study = self.nested_study()
        design = designlib.build_design(study, DesignSpec(permute="within"))
        nested = [check for check in design.checks if check.code == "nested_blocks"]
        self.assertEqual([check.level for check in nested], ["block"])
        self.assertIn("restrict the analysis to blocks that hold both levels", nested[0].message)

    def test_restrict_to_mixed_blocks(self):
        rows = []
        for index in range(8):
            rows.append((f"sub-a{index}", {"group": "case" if index % 2 else "control", "volume_ml": 5.0 + index, "dataset": "mixed"}))
        for index in range(4):
            rows.append((f"sub-b{index}", {"group": "case", "volume_ml": 9.0 + index, "dataset": "casesonly"}))
        study = self.make_study(rows, "partial")
        nested = designlib.build_design(study, DesignSpec())
        self.assertEqual(nested.design_type, "cases_vs_control_database")
        info = next(check for check in nested.checks if check.code == "nested")
        self.assertEqual(info.detail["mixed_blocks"], ["mixed"])
        restricted = designlib.build_design(study, DesignSpec(restrict_to_mixed_blocks=True))
        self.assertEqual(restricted.design_type, "within_cohort")
        self.assertEqual(restricted.excluded, {"dataset holds a single level of group": [f"sub-b{index}" for index in range(4)]})
        self.assertEqual(restricted.n, 8)


    def test_partly_nested_names_the_single_level_blocks(self):
        rows = []
        for dataset in ("d1", "d2"):
            for index in range(8):
                rows.append((f"sub-{dataset}{index}", {"group": "case" if index % 2 else "control", "volume_ml": 5.0 + index, "dataset": dataset}))
        rows.append(("sub-extra", {"group": "case", "volume_ml": 7.5, "dataset": "d3"}))
        design = designlib.build_design(self.make_study(rows, "extra"), DesignSpec())
        self.assertEqual(design.design_type, "cases_vs_control_database")
        self.assertIn("dataset d3 holds a single level of group", design.permute_reason)
        self.assertNotIn("group is nested in dataset", design.permute_reason)
        info = next(check for check in design.checks if check.code == "nested")
        self.assertEqual(info.detail["single_level_blocks"], ["d3"])

    def test_missing_implicit_block_does_not_decide_nesting(self):
        rows = []
        for dataset in ("d1", "d2"):
            for index in range(10):
                rows.append((f"sub-{dataset}{index:02d}", {"group": "case" if index % 2 else "control", "volume_ml": 5.0 + index, "dataset": dataset}))
        rows += [(f"sub-x{index}", {"group": "control", "volume_ml": 9.5 + index}) for index in range(2)]  # no dataset
        design = designlib.build_design(self.make_study(rows, "nodataset"), DesignSpec())
        self.assertEqual(design.block_column, "dataset")
        self.assertEqual(design.design_type, "within_cohort")
        self.assertEqual(design.permute, "within")
        self.assertEqual(design.n, 22)  # kept: an implicit block column is not required listwise
        self.assertEqual(design.crosstab["n/a"], {"case": 0, "control": 2})
        warning = next(check for check in design.checks if check.code == "missing_block")
        self.assertEqual(warning.level, "warn")
        self.assertIn("2 subject(s) have no dataset", warning.message)
        # The n/a block is still a block: its rows are permuted among themselves only
        missing = np.array([label == "n/a" for label in design.block_labels])
        for order in itertools.islice(designlib.design_shufflings(design), 50):
            self.assertTrue(np.all(missing[order] == missing))
        # An explicit block column is required listwise
        explicit = designlib.build_design(self.make_study(rows, "nodataset2"), DesignSpec(block_column="dataset"))
        self.assertEqual(explicit.n, 20)
        self.assertNotIn("missing_block", _codes(explicit.checks))


class CrossedTest(_StudyTestCase):
    def test_within_block_permutations_never_cross_blocks(self):
        study = self.crossed_study()
        design = designlib.build_design(study, DesignSpec(inference={"n_perm": 1000, "seed": 7}))
        self.assertEqual(design.design_type, "within_cohort")
        self.assertEqual(design.permute, "within")
        self.assertIn("Within-dataset permutation", design.permute_reason)
        self.assertEqual(design.limitations, [])
        self.assertIsNotNone(design.blocks)
        self.assertEqual(design.unique_perms, math.factorial(10) ** 3)  # the volume covariate makes every row distinct
        self.assertTrue(design.ok, [check.message for check in design.blocking])
        blocks = design.blocks
        shufflings = list(designlib.design_shufflings(design))
        self.assertEqual(len(shufflings), 1000)
        np.testing.assert_array_equal(shufflings[0], np.arange(design.n))
        crossing_labels = 0
        for permutation in shufflings:
            self.assertEqual(sorted(permutation.tolist()), list(range(design.n)))
            np.testing.assert_array_equal(blocks[permutation], blocks)
            crossing_labels += int(np.any(np.array(design.group_labels)[permutation] != np.array(design.group_labels)))
        self.assertGreater(crossing_labels, 900)  # labels do move within blocks
        # Reproducible from the seed
        again = list(designlib.design_shufflings(design))
        self.assertTrue(all(np.array_equal(a, b) for a, b in zip(shufflings, again, strict=True)))

    def test_whole_block_permutations(self):
        blocks = np.repeat([1, 2, 3, 4], 3)
        for permutation in itertools.islice(designlib.permutations(12, blocks, "whole", 1000, 3), 30):
            moved = permutation.reshape(4, 3)
            for row in moved:
                self.assertEqual(len(set(blocks[row].tolist())), 1)  # a block lands as a unit
                self.assertEqual(row.tolist(), sorted(row.tolist()))
        # Unique counts: 4 blocks with 2 distinct patterns (2 + 2)
        labels = ["a", "b", "b"] * 2 + ["b", "a", "b"] * 2
        self.assertEqual(designlib.unique_count(12, blocks, labels, mode="whole"), 6)
        enumerated = list(designlib.permutations(12, blocks, "whole", 5000, 0, labels))
        self.assertEqual(len(enumerated), 6)
        self.assertEqual(len(_designs(enumerated, labels)), 6)
        for permutation in enumerated:
            for row in permutation.reshape(4, 3):
                self.assertEqual(len(set(blocks[row].tolist())), 1)
        with self.assertRaises(ValueError):
            designlib.unique_count(5, [1, 1, 2, 2, 2], None, mode="whole")

    def test_unbalanced_blocks_fall_back_to_free(self):
        rows = []
        for dataset in ("d1", "d2"):
            for index in range(6):
                rows.append((f"sub-{dataset}{index}", {"group": "case" if index else "control", "volume_ml": 5.0 + index, "dataset": dataset}))
        design = designlib.build_design(self.make_study(rows, "unbalanced"), DesignSpec())
        self.assertEqual(design.design_type, "within_cohort")
        self.assertEqual(design.permute, "free")
        self.assertIn("blocks_not_used", _codes(design.checks, "warn"))


class PermutationTest(_StudyTestCase):
    def test_exhaustive_two_groups_of_four(self):
        labels = ["case"] * 4 + ["control"] * 4
        self.assertEqual(designlib.unique_count(8, None, labels), 70)
        self.assertEqual(designlib.unique_count(8, None, None), math.factorial(8))
        permutations = list(designlib.permutations(8, None, "free", 5000, 0, labels))
        self.assertEqual(len(permutations), 70)
        np.testing.assert_array_equal(permutations[0], np.arange(8))
        self.assertEqual(len(_designs(permutations, labels)), 70)
        # Within blocks: C(4, 2) * C(4, 2)
        blocks = np.array([1, 1, 1, 1, 2, 2, 2, 2])
        labels = ["a", "a", "b", "b"] * 2
        self.assertEqual(designlib.unique_count(8, blocks, labels), 36)
        within = list(designlib.permutations(8, blocks, "within", 5000, 0, labels))
        self.assertEqual(len(within), 36)
        self.assertEqual(len(_designs(within, labels)), 36)
        for permutation in within:
            np.testing.assert_array_equal(blocks[permutation], blocks)

    def test_random_permutations_are_seeded(self):
        a = list(designlib.permutations(30, None, "free", 1000, 5))
        b = list(designlib.permutations(30, None, "free", 1000, 5))
        c = list(designlib.permutations(30, None, "free", 1000, 6))
        self.assertEqual(len(a), 1000)
        self.assertTrue(all(np.array_equal(x, y) for x, y in zip(a, b, strict=True)))
        self.assertFalse(all(np.array_equal(x, y) for x, y in zip(a[1:], c[1:], strict=True)))
        np.testing.assert_array_equal(c[0], np.arange(30))

    def test_sign_flips(self):
        flips = list(designlib.sign_flips(10, 5000, 0))
        self.assertEqual(len(flips), 1024)
        np.testing.assert_array_equal(flips[0], np.ones(10))
        self.assertEqual(len({tuple(f.tolist()) for f in flips}), 1024)
        random = list(designlib.sign_flips(20, 1000, 3))
        self.assertEqual(len(random), 1000)
        self.assertTrue(all(set(np.unique(f).tolist()) <= {-1.0, 1.0} for f in random))

    def two_group(self, n_per_group, name):
        rows = [(f"sub-{level[:2]}{index}", {"group": level, "volume_ml": 10.0}) for level in ("case", "control") for index in range(n_per_group)]
        return self.make_study(rows, name)

    def test_design_permutation_counts(self):
        spec = DesignSpec(covariates=[], volume_off_reason="synthetic test")
        eight = designlib.build_design(self.two_group(4, "eight"), spec)
        self.assertEqual(eight.unique_perms, 70)
        self.assertIn("too_few_permutations", _codes(eight.checks, "block"))
        self.assertFalse(eight.ok)
        self.assertIn("volume_off", _codes(eight.checks, "info"))
        fourteen = designlib.build_design(self.two_group(7, "fourteen"), spec)
        self.assertEqual(fourteen.unique_perms, 3432)
        self.assertTrue(fourteen.exhaustive)
        self.assertEqual(fourteen.n_perm, 3432)
        self.assertAlmostEqual(fourteen.min_p, 1 / 3432)
        self.assertIn("exhaustive", _codes(fourteen.checks, "warn"))
        self.assertEqual(len(list(designlib.design_shufflings(fourteen))), 3432)
        sixteen = designlib.build_design(self.two_group(8, "sixteen"), spec)
        self.assertEqual(sixteen.unique_perms, 12870)
        self.assertFalse(sixteen.exhaustive)
        self.assertEqual(sixteen.n_perm, 5000)


class ExhaustiveOrientationTest(_StudyTestCase):
    """Exhaustive permutations are data-row arrays (Y[P]) whose relabellings X[P⁻¹] are all distinct."""

    def five_and_five(self):
        rows = [(f"sub-{level[:2]}{index}", {"group": level, "volume_ml": 10.0}) for level in ("case", "control") for index in range(5)]
        return designlib.build_design(self.make_study(rows, "fivefive"), DesignSpec(covariates=[], volume_off_reason="orientation test"))

    def test_all_designs_distinct(self):
        labels = ["case"] * 5 + ["control"] * 5
        permutations = list(designlib.permutations(10, None, "free", 5000, 0, labels))
        self.assertEqual(len(permutations), math.comb(10, 5))
        np.testing.assert_array_equal(permutations[0], np.arange(10))
        self.assertEqual(len(_designs(permutations, labels)), 252)
        # Within blocks (C(5, 2)² = 100) and whole blocks (6 blocks with patterns 3 + 2 + 1: 60)
        blocks = np.repeat([1, 2], 5)
        within_labels = ["a", "a", "b", "b", "b"] * 2
        within = list(designlib.permutations(10, blocks, "within", 5000, 0, within_labels))
        self.assertEqual(len(within), 100)
        self.assertEqual(len(_designs(within, within_labels)), 100)
        blocks = np.repeat([1, 2, 3, 4, 5, 6], 2)
        whole_labels = ["a", "b"] * 3 + ["b", "a"] * 2 + ["b", "b"]
        whole = list(designlib.permutations(12, blocks, "whole", 5000, 0, whole_labels))
        self.assertEqual(len(whole), designlib.unique_count(12, blocks, whole_labels, mode="whole"))
        self.assertEqual(len(_designs(whole, whole_labels)), 60)

    def test_exhaustive_p_matches_brute_force(self):
        from LesionNetworkMappingLib import glm

        design = self.five_and_five()
        self.assertTrue(design.exhaustive)
        self.assertEqual(design.unique_perms, 252)
        Y = self.rng.standard_normal((design.n, 12))
        Y[: design.n // 2, :4] += 1.5  # a few voxels with an effect, so small p values are exercised
        # Brute force: every assignment of 5 of the 10 subjects to the first level, data unchanged
        brute = []
        for chosen in itertools.combinations(range(design.n), 5):
            relabelled = design.X.copy()
            relabelled[:, 1] = 0.0
            relabelled[list(chosen), 1] = 1.0
            brute.append(glm.fit(Y, relabelled, design.C).stat)
        brute = np.abs(np.array(brute))
        # The design's enumeration applied as data rows Y[P] against the unchanged design
        enumerated = np.abs(np.array([glm.fit(Y[permutation], design.X, design.C).stat for permutation in designlib.design_shufflings(design)]))
        self.assertEqual(enumerated.shape, brute.shape)
        np.testing.assert_allclose(np.sort(enumerated, axis=0), np.sort(brute, axis=0), rtol=1e-10, atol=1e-12)
        observed = np.abs(glm.fit(Y, design.X, design.C).stat)
        tolerance = 1e-9 * observed + 1e-12
        exact = np.sum(brute >= observed - tolerance, axis=0) / 252.0
        from_design = np.sum(enumerated >= observed - tolerance, axis=0) / 252.0
        np.testing.assert_array_equal(from_design, exact)
        self.assertLess(exact.min(), 0.05)
        # glm.shufflings yields the same arrays, and its exhaustive p is the brute-force p
        iterator, count, exhaustive = glm.shufflings(design)
        listed = list(iterator)
        self.assertEqual((count, exhaustive), (252, True))
        self.assertTrue(all(np.array_equal(a, b) for a, b in zip(listed, designlib.design_shufflings(design), strict=True)))
        result = glm.run(design, Y, dtype=np.float64)
        self.assertTrue(result.exhaustive)
        np.testing.assert_allclose(result.p_unc, exact, rtol=0, atol=1e-12)


class ChecksTest(_StudyTestCase):
    def test_vif(self):
        rows = []
        for index in range(24):
            case = index < 12
            age = (70.0 if case else 40.0) + self.rng.normal(0, 2.0)
            rows.append((f"sub-{index:02d}", {"group": "case" if case else "control", "volume_ml": float(self.rng.uniform(5, 40)), "age": round(age, 3)}))
        study = self.make_study(rows, "vif")
        design = designlib.build_design(study, DesignSpec(covariates=["volume_ml", "age"]))
        self.assertIn("vif", _codes(design.checks, "warn"))
        # VIF of age = 1 / (1 - R²) of age on the other columns
        X = design.X
        others = X[:, [0, 1, 2]]
        age = X[:, 3]
        residual = age - others @ np.linalg.lstsq(others, age, rcond=None)[0]
        r2 = 1 - residual @ residual / np.sum((age - age.mean()) ** 2)
        self.assertAlmostEqual(design.vif["age"], 1 / (1 - r2), places=6)
        self.assertGreater(design.vif["group[case]"], 5)
        self.assertLess(design.vif["volume_ml"], 2)
        self.assertNotIn("intercept", design.vif)

    def test_matrix_checks(self):
        n = 12
        g = np.array([1.0] * 6 + [0.0] * 6)
        no_intercept = designlib.matrix_checks(g[:, None], [1.0])
        self.assertIn("no_intercept", _codes(no_intercept, "block"))
        demeaned = designlib.matrix_checks((g - g.mean())[:, None], [1.0])
        self.assertIn("no_intercept", _codes(demeaned, "block"))
        intercept_only = designlib.matrix_checks(np.ones((n, 1)), [1.0])
        self.assertEqual(_codes(intercept_only), ["one_sample"])
        cell_means_mean = designlib.matrix_checks(np.column_stack([g, 1 - g]), [1.0, 0.0])
        self.assertEqual(_codes(cell_means_mean), ["one_sample"])  # tests the mean of group A against 0
        good = designlib.matrix_checks(np.column_stack([np.ones(n), g]), [0.0, 1.0])
        self.assertEqual(good, [])
        duplicate = designlib.matrix_checks(np.column_stack([np.ones(n), g, 2 * g]), [0.0, 1.0, 0.0], ["intercept", "g", "g2"])
        self.assertIn("rank_deficient", _codes(duplicate, "block"))
        self.assertIn("contrast_not_estimable", _codes(duplicate, "block"))
        rank = next(check for check in duplicate if check.code == "rank_deficient")
        self.assertEqual(rank.detail["columns"], ["g", "g2"])
        nan = designlib.matrix_checks(np.column_stack([np.ones(n), np.where(g, np.nan, 0.0)]), [0.0, 1.0])
        self.assertEqual(_codes(nan), ["non_finite_design"])
        self.assertIn("no_residual_df", _codes(designlib.matrix_checks(np.column_stack([np.ones(2), [0.0, 1.0]]), [0.0, 1.0])))

    def test_volume_checks(self):
        rows = []
        volumes = [1.0] * 10 + [2.0] * 10 + [200.0]
        for index, volume in enumerate(volumes):
            rows.append((f"sub-{index:02d}", {"group": "case" if index % 2 else "control", "volume_ml": volume}))
        study = self.make_study(rows, "volume")
        design = designlib.build_design(study, DesignSpec())
        self.assertIn("volume_skew", _codes(design.checks, "warn"))
        logged = designlib.build_design(study, DesignSpec(transforms={"volume_ml": "log"}))
        self.assertNotIn("volume_skew", _codes(logged.checks))
        off = designlib.build_design(study, DesignSpec(covariates=[]))
        self.assertIn("volume_off", _codes(off.checks, "warn"))
        reasoned = designlib.build_design(study, DesignSpec(covariates=[], volume_off_reason="volumes not comparable across sources"))
        self.assertIn("volume_off", _codes(reasoned.checks, "info"))
        self.assertNotIn("volume_off", _codes(reasoned.checks, "warn"))

    def test_dimensionality_imbalance(self):
        rows = []
        for index in range(30):
            case = index < 15
            two_d = (index % 3 != 0) if case else (index % 5 == 0)
            rows.append((f"sub-{index:02d}", {"group": "case" if case else "control", "volume_ml": 5.0 + index, "dimensionality": "2D" if two_d else "3D"}))
        study = self.make_study(rows, "dims")
        design = designlib.build_design(study, DesignSpec())
        self.assertIn("dimensionality_imbalance", _codes(design.checks, "warn"))
        covaried = designlib.build_design(study, DesignSpec(covariates=["volume_ml", "dimensionality"]))
        self.assertNotIn("dimensionality_imbalance", _codes(covaried.checks))
        self.assertIn("dimensionality[3D]", covaried.columns)

    def test_maps_and_pooling_checks(self):
        study = self.crossed_study()
        design = designlib.build_design(study, DesignSpec())
        maps = self.rng.normal(0, 1, (design.n, 200)).astype(np.float32)
        cases = np.array([label == "case" for label in design.group_labels])
        wide = maps.copy()
        wide[cases] *= 3.0  # variance ratio 9
        self.assertIn("variance_ratio", _codes(designlib.map_checks(design, wide), "warn"))
        self.assertEqual(designlib.map_checks(design, maps), [])
        broken = maps.copy()
        broken[2, 5] = np.nan
        self.assertIn("non_finite_maps", _codes(designlib.map_checks(design, broken), "block"))
        by_sid = {sid: wide[index] for index, sid in enumerate(design.sids)}
        with_maps = designlib.build_design(study, DesignSpec(), maps=by_sid)
        self.assertIn("variance_ratio", _codes(with_maps.checks, "warn"))
        runs = [{"bundle": {"manifest_sha256": "a" * 64}, "subject_ids_sha256": sha * 64, "status": "done"} for sha in ("1", "2")]
        pooled = designlib.build_design(study, DesignSpec(), run_metas=runs)
        self.assertIn("not_poolable", _codes(pooled.checks, "block"))
        self.assertIn("different subject lists", next(check for check in pooled.checks if check.code == "not_poolable").message)


class SaveAndPreviewTest(_StudyTestCase):
    def test_save_design_and_preview(self):
        study = self.crossed_study()
        design = designlib.build_design(study, DesignSpec(covariates=["volume_ml"], inference={"n_perm": 2000, "tfce": True}))
        json_path, tsv_path = designlib.save_design(design, self.path("out"))
        spec, resolved = designlib.read_design_json(json_path)
        self.assertEqual(spec.to_dict(), design.spec.to_dict())
        self.assertEqual(resolved["design_type"], "within_cohort")
        self.assertEqual(resolved["permute"], "within")
        self.assertEqual(resolved["sids"], design.sids)
        self.assertIsNone(resolved["unique_perms"])  # (10!)^3 > 2^53
        self.assertAlmostEqual(resolved["unique_perms_log10"], 3 * math.log10(math.factorial(10)))
        with open(tsv_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0].split("\t"), ["participant_id", *design.columns])
        self.assertEqual(len(lines), design.n + 1)
        np.testing.assert_allclose([float(value) for value in lines[1].split("\t")[1:]], design.X[0], rtol=1e-9)
        preview = designlib.design_preview(design)
        text = json.dumps(preview, allow_nan=False)
        self.assertIn("matrix", json.loads(text))
        for key in ("matrix", "columns", "rank", "vif", "unique_perms", "min_p", "checks", "design_type", "limitations", "permute_reason"):
            self.assertIn(key, preview)
        self.assertEqual(len(preview["matrix"]), design.n)


def _write_mask(path, shape=(6, 7, 5)):
    affine = np.diag([-2.0, 2.0, 2.0, 1.0])
    affine[:3, 3] = [10.0, -12.0, -8.0]
    mask = np.zeros(shape, dtype=np.uint8)
    mask[1:5, 1:6, 1:4] = 1
    mask[2, 3, 2] = 0
    nifti.write_nifti(path, mask, affine)
    return mask.astype(bool), affine


class PalmExportTest(_StudyTestCase):
    def setUp(self):
        super().setUp()
        self.mask_path = self.path("mask.nii.gz")
        self.mask, self.affine = _write_mask(self.mask_path)
        self.V = int(self.mask.sum())

    def export(self, design, name):
        maps = self.rng.normal(0, 1, (design.n, self.V)).astype(np.float32)
        out = self.path(name)
        return palm.export_palm(design, maps, self.mask_path, out), maps, out

    def command(self, out, script, program):
        with open(os.path.join(out, script), encoding="utf-8") as f:
            line = next(line for line in f if line.startswith(program + " "))
        return shlex.split(line)

    def palm_tokens(self, out):
        return self.command(out, "run_palm.sh", "palm")

    def test_two_sided_crossed_export(self):
        design = designlib.build_design(self.crossed_study(), DesignSpec(inference={"n_perm": 2000, "tfce": True, "seed": 3}))
        paths, maps, out = self.export(design, "palm-crossed")
        for name in ("fz_4d.nii.gz", "mask.nii.gz", "design.csv", "contrasts.csv", "eb.csv", "design.mat", "design.con", "design.grp", "run_palm.sh", "run_randomise.sh", "README.md", "subjects.tsv", "design.json", "palm_export.json"):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
        self.assertNotIn("vg.csv", paths)
        tokens = self.palm_tokens(out)
        self.assertIn("-twotail", tokens)
        self.assertEqual(tokens[tokens.index("-eb") : tokens.index("-eb") + 3], ["-eb", "eb.csv", "-within"])
        self.assertNotIn("-vg", tokens)
        self.assertEqual(tokens[tokens.index("-n") + 1], "2000")
        self.assertEqual(tokens[tokens.index("-T") : tokens.index("-T") + 7], ["-T", "-tfce_H", "2", "-tfce_E", "0.5", "-tfce_C", "26"])
        for flag in ("-logp", "-corrcon", "-fdr"):
            self.assertIn(flag, tokens)
        self.assertEqual(tokens[-2:], ["-o", "out/palm"])
        self.assertTrue(os.access(os.path.join(out, "run_palm.sh"), os.X_OK))
        # The files parse back to the in-app design
        np.testing.assert_allclose(palm.read_csv_matrix(os.path.join(out, "design.csv")), design.X, rtol=1e-9)
        np.testing.assert_array_equal(palm.read_csv_matrix(os.path.join(out, "contrasts.csv")), design.C)
        np.testing.assert_array_equal(palm.read_csv_matrix(os.path.join(out, "eb.csv"))[:, 0], design.blocks)
        mat, header = palm.read_vest(os.path.join(out, "design.mat"))
        np.testing.assert_allclose(mat, design.X, rtol=1e-9)
        self.assertEqual(header["NumWaves"], str(design.p))
        self.assertEqual(header["NumPoints"], str(design.n))
        self.assertIn("PPheights", header)
        con, header = palm.read_vest(os.path.join(out, "design.con"))
        np.testing.assert_array_equal(con, np.vstack([design.C, -design.C]))  # randomise is one-tailed
        self.assertEqual(header["NumContrasts"], "2")
        grp, _ = palm.read_vest(os.path.join(out, "design.grp"))
        np.testing.assert_array_equal(grp[:, 0], design.blocks)
        with open(os.path.join(out, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("design.grp` holds exchangeability blocks for randomise (`-e`), not PALM variance groups", readme)
        self.assertIn("Research use only", readme)
        # The 4D image holds the maps in design order on the exact mask grid
        data, affine, header = nifti.read_nifti(os.path.join(out, "fz_4d.nii.gz"))
        self.assertEqual(data.shape, (*self.mask.shape, design.n))
        np.testing.assert_allclose(affine, self.affine)
        self.assertEqual(header["datatype"], 16)
        indices = mask_indices(self.mask)
        recovered = np.moveaxis(data, -1, 0).reshape(design.n, -1)[:, indices]
        np.testing.assert_array_equal(recovered, maps)
        self.assertEqual(float(np.abs(np.moveaxis(data, -1, 0).reshape(design.n, -1)[:, ~self.mask.reshape(-1)]).max()), 0.0)
        mask_data, mask_affine, _ = nifti.read_nifti(os.path.join(out, "mask.nii.gz"))
        np.testing.assert_array_equal(mask_data != 0, self.mask)
        np.testing.assert_allclose(mask_affine, self.affine)
        with open(os.path.join(out, "subjects.tsv"), encoding="utf-8") as f:
            subjects = [line.split("\t")[1] for line in f.read().splitlines()[1:]]
        self.assertEqual(subjects, design.sids)
        self.assertEqual(_codes(palm.validate_palm_export(out), "block"), [])
        randomise = self.command(out, "run_randomise.sh", "randomise")
        self.assertEqual(randomise[0], "randomise")
        self.assertIn("-e", randomise)
        self.assertIn("--seed=3", randomise)

    def test_one_tailed_variance_groups_and_free_export(self):
        study = self.nested_study(n_cases=12, n_controls=14)
        design = designlib.build_design(study, DesignSpec(contrast="A>B", variance_groups=True))
        self.assertEqual(design.stat, "v")
        self.assertEqual(design.permute, "free")
        paths, _, out = self.export(design, "palm-nested")
        tokens = self.palm_tokens(out)
        self.assertNotIn("-twotail", tokens)
        self.assertNotIn("-eb", tokens)
        self.assertEqual(tokens[tokens.index("-vg") + 1], "vg.csv")
        np.testing.assert_array_equal(palm.read_csv_matrix(paths["vg.csv"])[:, 0], [1 if label == "case" else 2 for label in design.group_labels])
        self.assertNotIn("-T", tokens)
        grp, _ = palm.read_vest(os.path.join(out, "design.grp"))
        np.testing.assert_array_equal(grp[:, 0], np.ones(design.n))  # no blocks: all ones, as FEAT writes it
        with open(os.path.join(out, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("## Limitations", readme)
        self.assertEqual(_codes(palm.validate_palm_export(out), "block"), [])
        # Without variance groups there is no -vg
        plain = designlib.build_design(study, DesignSpec(contrast="A>B"))
        _, _, out = self.export(plain, "palm-plain")
        self.assertNotIn("-vg", self.palm_tokens(out))
        self.assertFalse(os.path.exists(os.path.join(out, "vg.csv")))

    def test_f_and_one_sample_exports(self):
        rows = [(f"sub-{index:02d}", {"arm": "abc"[index % 3], "volume_ml": 10.0 + index, "group": "case"}) for index in range(18)]
        study = self.make_study(rows, "anova", roles={"arm": "group"})
        design = designlib.build_design(study, DesignSpec(test="anova", group_column="arm", contrast="F"))
        paths, _, out = self.export(design, "palm-f")
        tokens = self.palm_tokens(out)
        self.assertEqual(tokens[tokens.index("-f") : tokens.index("-f") + 3], ["-f", "ftests.csv", "-fonly"])
        np.testing.assert_array_equal(palm.read_csv_matrix(paths["ftests.csv"]), [[1, 1]])
        fts, header = palm.read_vest(os.path.join(out, "design.fts"))
        np.testing.assert_array_equal(fts, [[1, 1]])
        self.assertEqual((header["NumWaves"], header["NumContrasts"]), ("2", "1"))  # as FEAT writes it
        self.assertNotIn("NumPoints", header)
        self.assertNotIn("-twotail", tokens)
        self.assertEqual(_codes(palm.validate_palm_export(out), "block"), [])
        one = designlib.build_design(study, DesignSpec(test="one_sample", covariates=[], volume_off_reason="test"))
        _, _, out = self.export(one, "palm-one")
        tokens = self.palm_tokens(out)
        self.assertIn("-ise", tokens)
        self.assertIn("-twotail", tokens)
        randomise = self.command(out, "run_randomise.sh", "randomise")
        self.assertIn("-1", randomise)
        checks = palm.validate_palm_export(out)
        self.assertEqual(_codes(checks, "block"), [])
        self.assertIn("one_sample", _codes(checks, "warn"))

    def test_validation_catches_edited_exports(self):
        design = designlib.build_design(self.crossed_study(), DesignSpec())
        _, _, out = self.export(design, "palm-edit")
        # Dropping the intercept (the Petersen 2026 PALM error)
        X = design.X[:, 1:]
        with open(os.path.join(out, "design.csv"), "w", encoding="utf-8") as f:
            f.write("".join(",".join("%.10g" % value for value in row) + "\n" for row in X))
        with open(os.path.join(out, "contrasts.csv"), "w", encoding="utf-8") as f:
            f.write("1,0\n")
        codes = _codes(palm.validate_palm_export(out), "block")
        self.assertIn("no_intercept", codes)
        self.assertIn("export_mismatch", codes)
        # A command that lost -twotail no longer matches the in-app design
        _, _, out = self.export(design, "palm-edit2")
        script = os.path.join(out, "run_palm.sh")
        with open(script, encoding="utf-8") as f:
            text = f.read()
        with open(script, "w", encoding="utf-8") as f:
            f.write(text.replace(" -twotail", ""))
        messages = [check.message for check in palm.validate_palm_export(out) if check.level == "block"]
        self.assertTrue(any("-twotail" in message for message in messages), messages)
        # Whole- instead of within-block permutation, and a different permutation count
        for old, new, flag in ((" -within", " -whole", "-whole"), (" -n 5000", " -n 10", "-n 10"), (" -fdr", "", "-fdr")):
            with self.subTest(edit=new):
                _, _, out = self.export(design, f"palm-edit-{flag.strip('-').replace(' ', '')}")
                script = os.path.join(out, "run_palm.sh")
                with open(script, encoding="utf-8") as f:
                    text = f.read()
                self.assertIn(old, text)
                with open(script, "w", encoding="utf-8") as f:
                    f.write(text.replace(old, new))
                messages = [check.message for check in palm.validate_palm_export(out) if check.code == "export_mismatch"]
                self.assertTrue(any(flag in message for message in messages), messages)

    def test_export_refuses_blocked_designs_and_bad_maps(self):
        blocked = designlib.build_design(self.nested_study(), DesignSpec(covariates=["volume_ml", "dataset"]))
        with self.assertRaises(ValueError):
            palm.export_palm(blocked, np.zeros((blocked.n, self.V), np.float32), self.mask_path, self.path("x"))
        design = designlib.build_design(self.crossed_study(), DesignSpec())
        with self.assertRaises(ValueError):
            palm.export_palm(design, np.zeros((design.n, self.V + 1), np.float32), self.mask_path, self.path("y"))
        maps = np.zeros((design.n, self.V), np.float32)
        maps[0, 0] = np.inf
        with self.assertRaises(ValueError):
            palm.export_palm(design, maps, self.mask_path, self.path("z"))
        with self.assertRaises(ValueError):
            palm.export_palm(design, np.zeros((design.n, self.V), np.float32), self.mask_path, self.path("w"), spec=DesignSpec(contrast="A>B"))
        paths = palm.export_palm(design, np.zeros((design.n, self.V), np.float32), self.mask_path, self.path("v"), spec=DesignSpec(inference={"n_perm": 10000}))
        self.assertIn("-n", self.palm_tokens(self.path("v")))
        self.assertEqual(self.palm_tokens(self.path("v"))[self.palm_tokens(self.path("v")).index("-n") + 1], "10000")


class PalmImportTest(_StudyTestCase):
    def setUp(self):
        super().setUp()
        self.mask_path = self.path("mask.nii.gz")
        self.mask, self.affine = _write_mask(self.mask_path)
        self.out = self.path("palm", "out")
        os.makedirs(self.out)
        self.p = self.rng.uniform(1e-4, 1.0, self.mask.shape)
        self.t = self.rng.normal(0, 2, self.mask.shape)

    def write(self, name, data):
        nifti.write_nifti(os.path.join(self.out, name), data.astype(np.float32), self.affine)

    def write_outputs(self, logp, contrasts=(1,)):
        for contrast in contrasts:
            self.write(f"palm_vox_tstat_c{contrast}.nii.gz", self.t * contrast)
            for key in ("vox_tstat_fwep", "tfce_tstat_fwep", "vox_tstat_fdrp", "vox_tstat_uncp"):
                self.write(f"palm_{key}_c{contrast}.nii.gz", -np.log10(self.p) if logp else self.p)

    def test_logp_from_config(self):
        self.write_outputs(logp=True)
        with open(os.path.join(self.out, "palm_palmconfig.txt"), "w", encoding="utf-8") as f:
            f.write("# Configuration file for PALM.\n# Version alpha119\n-i fz_4d.nii.gz\n-m mask.nii.gz\n-d design.csv\n-t contrasts.csv\n-n 5000\n-logp\n-twotail\n-o out/palm\n")
        result = palm.import_palm(self.path("palm"))
        self.assertTrue(result["logp"])
        self.assertEqual(result["logp_source"], "config")
        self.assertEqual(result["command"], "palm -i fz_4d.nii.gz -m mask.nii.gz -d design.csv -t contrasts.csv -n 5000 -logp -twotail -o out/palm")
        self.assertEqual(result["stat"], "tstat")
        self.assertEqual(sorted(result["maps"]), ["tfce_fwep", "vox_fdrp", "vox_fwep", "vox_stat", "vox_uncp"])
        for key in ("vox_fwep", "tfce_fwep", "vox_fdrp", "vox_uncp"):
            np.testing.assert_allclose(result["maps"][key], self.p, rtol=1e-5)
        np.testing.assert_allclose(result["maps"]["vox_stat"], self.t, rtol=1e-6)
        np.testing.assert_allclose(result["affine"], self.affine)
        masked = palm.import_palm(self.path("palm"), mask_path=self.mask_path)
        np.testing.assert_allclose(masked["maps"]["vox_fwep"], self.p.reshape(-1)[mask_indices(self.mask)], rtol=1e-5)

    def test_logp_from_value_range(self):
        self.write_outputs(logp=True, contrasts=(1, 2))
        result = palm.import_palm(self.path("palm", "out"))
        self.assertTrue(result["logp"])
        self.assertEqual(result["logp_source"], "range")
        self.assertIsNone(result["command"])
        self.assertEqual(sorted(result["contrasts"]), [1, 2])
        np.testing.assert_allclose(result["contrasts"][2]["vox_stat"], 2 * self.t, rtol=1e-6)
        np.testing.assert_allclose(result["contrasts"][2]["vox_uncp"], self.p, rtol=1e-5)

    def test_plain_p_and_missing_outputs(self):
        self.write_outputs(logp=False)
        result = palm.import_palm(self.path("palm"))
        self.assertFalse(result["logp"])
        np.testing.assert_allclose(result["maps"]["vox_fwep"], self.p, rtol=1e-6)
        with self.assertRaises(FileNotFoundError):
            palm.import_palm(self.path("palm"), prefix="other")


if __name__ == "__main__":
    unittest.main()

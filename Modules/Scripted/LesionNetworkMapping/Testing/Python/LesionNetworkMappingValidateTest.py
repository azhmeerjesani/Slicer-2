"""Tests of the validation engine (validate.py) and the lesion scores (scores.py): leakage, folds (LODO fallback,
LOO warning), metrics (AUC formula, negative out-of-sample R²), mandatory baselines, calibrated permutation p,
the incremental CI of a useless circuit, frozen-circuit application and the command line.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingValidateTest.py -v
"""

import io
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np
from scipy import sparse

from LesionNetworkMappingLib import connectome, jobs, nifti, reference, runs, scores, similarity, validate
from LesionNetworkMappingLib.export import file_sha256, subjects_sha256
from LesionNetworkMappingLib.study import Study

NEUTRAL_FORBIDDEN = ("significant", "predicts", "specific", "proves", "confirms", "fails")


def _absolute_strings(value):
    """The strings of a JSON value that are absolute local paths (shared records hold none, SPEC 9.7 rule 10)."""
    if isinstance(value, dict):
        return [item for child in value.values() for item in _absolute_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _absolute_strings(child)]
    if not isinstance(value, str):
        return []
    option, has_value, option_value = value.partition("=")
    return [value] if os.path.isabs(value) or (has_value and option.startswith("-") and os.path.isabs(option_value)) else []


def _cohort(n=48, V=300, effect=0.0, seed=0, datasets=None, occupancy=True, reference=True, outcome_type="binary", volume_effect=0.0):
    """Synthetic cohort: Fz-like maps with a planted outcome pattern of size ``effect`` on the first V/8 voxels."""
    rng = np.random.default_rng(seed)
    if outcome_type == "binary":
        y = np.zeros(n)
        y[rng.permutation(n)[: n // 2]] = 1.0
    else:
        y = rng.standard_normal(n)
    volume = np.exp(1.0 + 0.4 * rng.standard_normal(n) + volume_effect * (y - y.mean()))
    pattern = np.zeros(V)
    pattern[: V // 8] = 1.0
    mu = rng.standard_normal(V)
    pc1 = rng.standard_normal(V)
    maps = 0.25 + 0.08 * rng.standard_normal((n, V)) + 0.03 * np.outer(rng.standard_normal(n), mu) + effect * np.outer(y - y.mean(), pattern)
    occ = sparse.random(n, V, density=0.04, random_state=seed + 1, format="csr") if occupancy else None
    return validate.ValidationData(
        sids=[f"sub-{index:03d}" for index in range(n)],
        maps=maps.astype(np.float32),
        outcome=y,
        outcome_type=outcome_type,
        volume=volume,
        datasets=datasets,
        occupancy=occ,
        reference={"mu_r": mu, "pc1": pc1} if reference else None,
        reference_reason=None if reference else "no reference maps in this test",
        occupancy_reason=None if occupancy else "no occupancy in this test",
    )


class ScoresTest(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(3)
        self.V = 200

    def test_damage_mean_and_percentage(self):
        circuit = self.rng.standard_normal(self.V)
        indices = np.array([3, 10, 50, 120])
        values = np.array([1.0, 0.5, 0.25, 1.0])
        dense = np.zeros(self.V)
        dense[indices] = values
        expected = float(values @ circuit[indices])
        for occupancy in ((indices, values), dense, sparse.csr_matrix(dense[None, :])):
            self.assertAlmostEqual(scores.damage_score(occupancy, circuit), expected, places=12)
            self.assertAlmostEqual(scores.mean_in_lesion(occupancy, circuit), expected / values.sum(), places=12)
        threshold = float(np.sort(np.abs(circuit[indices]))[1])
        inside = np.abs(circuit[indices]) >= threshold
        self.assertAlmostEqual(scores.pct_lesion_in_circuit((indices, values), circuit, threshold), 100 * values[inside].sum() / values.sum(), places=10)
        roi = np.zeros(self.V)
        roi[[3, 50]] = 1.0
        self.assertAlmostEqual(scores.pct_lesion_in_circuit((indices, values), roi, 1.0, sign="pos"), 100 * 1.25 / 2.75, places=10)
        self.assertTrue(math.isnan(scores.mean_in_lesion((np.array([], dtype=int), np.array([])), circuit)))
        with self.assertRaises(ValueError):
            scores.damage_score((np.array([self.V]), np.array([1.0])), circuit)

    def test_similarity_score_and_partial(self):
        a, b = self.rng.standard_normal((2, self.V))
        covariates = self.rng.standard_normal((2, self.V))
        score = scores.similarity_score(a, b, covariates)
        self.assertAlmostEqual(score.r, similarity.pearson(a, b), places=12)
        self.assertAlmostEqual(score.z, math.atanh(score.r), places=12)
        self.assertAlmostEqual(score.partial_r, similarity.partial_spatial_correlation(a, b, covariates), places=12)
        self.assertTrue(math.isnan(scores.similarity_score(a, b).partial_r))

    def test_batched_scores_equal_single_scores(self):
        Y = (0.3 + 0.1 * self.rng.standard_normal((6, self.V))).astype(np.float32)
        C = self.rng.standard_normal((3, self.V))
        covariates = self.rng.standard_normal((2, self.V))
        r, partial = scores.similarity_matrix(Y, C, covariates, chunk=37)
        for i in range(6):
            for j in range(3):
                single = scores.similarity_score(Y[i], C[j], covariates)
                self.assertAlmostEqual(r[i, j], single.r, places=6)
                self.assertAlmostEqual(partial[i, j], single.partial_r, places=6)
        occupancy = sparse.random(6, self.V, density=0.05, random_state=2, format="csr")
        damage = scores.damage_matrix(occupancy, C)
        for i in range(6):
            self.assertAlmostEqual(damage[i, 1], scores.damage_score(occupancy[i], C[1]), places=10)
        roi = scores.top_pct_roi(C[0], 10)
        self.assertEqual(int(np.count_nonzero(roi)), 20)
        self.assertTrue(np.all(np.abs(C[0][roi != 0]) >= np.abs(C[0][roi == 0]).max()))
        np.testing.assert_array_equal(np.sign(C[0][roi != 0]), roi[roi != 0])
        self.assertAlmostEqual(scores.roi_matrix(Y, roi[None, :])[2, 0], scores.roi_connectivity(Y[2], roi), places=6)
        self.assertAlmostEqual(scores.roi_connectivity(Y[2], roi != 0), float(Y[2][roi != 0].mean()), places=6)


class FoldsTest(unittest.TestCase):
    def test_stratified_kfold(self):
        y = np.array([1.0] * 20 + [0.0] * 40)
        folds = validate.make_folds("kfold", y, None, seed=1, k=10, repeats=5)
        self.assertEqual((folds.scheme, folds.repeats, folds.k), ("kfold", 5, 10))
        for repeat in range(5):
            row = folds.assignment[repeat]
            self.assertTrue(np.all(row >= 0))
            for fold in range(10):
                members = row == fold
                self.assertEqual(int(members.sum()), 6)
                self.assertEqual(int(y[members].sum()), 2)  # stratified: 2 cases per fold
        self.assertFalse(np.array_equal(folds.assignment[0], folds.assignment[1]))

    def test_lodo_used_when_every_dataset_holds_both_classes(self):
        y = np.tile([1.0, 0.0], 15)
        datasets = ["A"] * 10 + ["B"] * 10 + ["C"] * 10
        folds = validate.make_folds("auto", y, datasets, seed=0)
        self.assertEqual(folds.scheme, "lodo")
        self.assertEqual(folds.labels[0], ["A", "B", "C"])
        self.assertIn("LODO", folds.reason)
        self.assertIsNone(folds.message)

    def test_lodo_fallback(self):
        y = np.array([1.0] * 10 + [0.0] * 10 + [1.0, 0.0] * 5)
        datasets = ["cases"] * 10 + ["controls"] * 10 + ["mixed"] * 10  # literature cases vs a control database
        for scheme in ("auto", "lodo"):
            folds = validate.make_folds(scheme, y, datasets, seed=0, k=5, repeats=2)
            self.assertEqual(folds.scheme, "kfold")
            self.assertIn("LODO not possible: dataset cases, controls hold one class", folds.reason)
            self.assertEqual(folds.message is not None, scheme == "lodo")
        scores_y = np.arange(25, dtype=float)
        folds = validate.make_folds("lodo", scores_y, ["A"] * 15 + ["B"] * 10 + ["C"] * 0, seed=0, outcome_type="continuous", k=5, repeats=1)
        self.assertEqual(folds.scheme, "lodo")
        folds = validate.make_folds("lodo", scores_y, ["A"] * 16 + ["B"] * 9, seed=0, outcome_type="continuous", k=5, repeats=1)
        self.assertEqual(folds.scheme, "kfold")
        self.assertIn("fewer than 10 subjects", folds.message)
        self.assertIn("no dataset column", validate.make_folds("auto", y, None, seed=0).reason)

    def test_loo_warning_and_bootstrap_units(self):
        y = np.tile([1.0, 0.0], 6)
        folds = validate.make_folds("loo", y, None, seed=0)
        self.assertEqual(folds.scheme, "loo")
        self.assertIn(validate.LOO_WARNING, folds.warnings)
        self.assertEqual(folds.n_folds(), 12)
        counts = np.array([2, 0, 1, 1, 3, 0, 1, 1, 0, 2, 1, 0])
        folds = validate.make_folds("kfold", y, None, seed=0, k=3, repeats=2, counts=counts)
        self.assertTrue(np.all(folds.assignment[:, counts == 0] == -1))
        self.assertTrue(np.all(folds.assignment[:, counts > 0] >= 0))  # each unit (all its copies) has one fold


class MetricsTest(unittest.TestCase):
    def test_auc_formula(self):
        rng = np.random.default_rng(4)
        s = np.round(rng.standard_normal(40), 1)  # ties
        y = (rng.random(40) < 0.4).astype(float)
        w = rng.integers(1, 4, 40).astype(float)
        pos, neg = np.flatnonzero(y == 1), np.flatnonzero(y == 0)
        numerator = sum(w[i] * w[j] * (1.0 if s[i] > s[j] else 0.5 if s[i] == s[j] else 0.0) for i in pos for j in neg)
        self.assertAlmostEqual(validate.auc(s, y, w), numerator / (w[pos].sum() * w[neg].sum()), places=12)
        self.assertAlmostEqual(validate.auc(np.repeat(s, w.astype(int)), np.repeat(y, w.astype(int))), validate.auc(s, y, w), places=12)
        self.assertAlmostEqual(validate.auc(-s, y), 1.0 - validate.auc(s, y), places=12)
        self.assertEqual(validate.auc([1, 2, 3], [1, 1, 0]), 0.0)
        self.assertTrue(math.isnan(validate.auc([1, 2], [1, 1])))

    def test_out_of_sample_r2_can_be_negative(self):
        y = np.array([1.0, 2.0, 3.0, 4.0])
        baseline = np.full(4, 2.5)
        self.assertAlmostEqual(validate.oos_r2(y, y, baseline), 1.0)
        self.assertAlmostEqual(validate.oos_r2(y, baseline, baseline), 0.0)
        worse = np.array([4.0, 3.0, 2.0, 1.0])
        self.assertAlmostEqual(validate.oos_r2(y, worse, baseline), 1.0 - 20.0 / 5.0)
        self.assertLess(validate.oos_r2(y, worse, baseline), 0.0)
        # A null cohort: the held-out predictions are worse than the training mean
        data = _cohort(n=40, effect=0.0, seed=5, outcome_type="continuous", occupancy=False, reference=False)
        result = validate.validate(data, "glm", scheme="kfold", k=5, repeats=2, n_boot=0, n_perm=0, seed=1)
        values = [item["metrics"]["r2"]["value"] for item in result.predictors if item["status"] == "computed"]
        self.assertTrue(all(np.isfinite(values)))
        self.assertLess(min(values), 0.0)

    def test_bootstrap_interval_contains_the_observed_value(self):
        rng = np.random.default_rng(8)
        values = rng.normal(0.0, 0.05, 400)
        details = validate.interval_details(values, 0.0)
        self.assertEqual(details["method"], "bc")
        np.testing.assert_allclose(details["ci"], details["ci_percentile"], atol=0.01)  # centred: BC = percentile
        shifted = validate.interval_details(values - 0.03, 0.0)
        self.assertEqual(shifted["method"], "bc")
        self.assertGreater(shifted["ci"][0], shifted["ci_percentile"][0])  # shifted towards the observed value
        # Few resamples: the plain percentile interval, widened to contain the observed value
        few = validate.interval_details([0.04, 0.06, 0.09], -0.01)
        self.assertEqual(few["method"], "percentile+observed")
        self.assertLessEqual(few["ci"][0], -0.01)
        # Observed beyond every bootstrap value (|z0| clipped): the BC interval is widened to contain it
        beyond = validate.interval_details(values, 0.5)
        self.assertEqual(beyond["method"], "bc+observed")
        self.assertEqual(beyond["ci"][1], 0.5)
        self.assertTrue(math.isnan(validate.interval_details([0.1], 0.1)["ci"][0]))

    def test_verdict_follows_the_sign_of_the_difference(self):
        self.assertIn("does not exceed", validate.verdict_text(-0.01, [0.04, 0.09], "lesion-volume baseline"))
        self.assertIn("does not exceed", validate.verdict_text(0.01, [-0.09, -0.04], "lesion-volume baseline"))
        self.assertIn("exceeds", validate.verdict_text(0.06, [0.04, 0.09], "lesion-volume baseline"))

    def test_logistic_fits(self):
        rng = np.random.default_rng(6)
        X = np.column_stack([np.ones(80), rng.standard_normal((80, 2))])
        y = (rng.random(80) < 1 / (1 + np.exp(-(X @ [0.3, 1.0, -0.5])))).astype(float)
        w = rng.integers(1, 3, 80).astype(float)
        beta = validate.fit_logistic(X, y, w)
        mu = 1 / (1 + np.exp(-X @ beta))
        np.testing.assert_allclose(X.T @ (w * (y - mu)) - validate.RIDGE * beta, 0, atol=1e-7)
        batch = validate.fit_logistic_batch(np.stack([X, X]), y, np.stack([w, np.ones(80)]))
        np.testing.assert_allclose(batch[0], beta, atol=1e-6)
        np.testing.assert_allclose(batch[1], validate.fit_logistic(X, y), atol=1e-6)
        z = (X[:, 1] - X[:, 1].mean()) / X[:, 1].std()
        slope = validate.fit_logistic(np.column_stack([np.ones(80), z]), y)[1]
        self.assertAlmostEqual(validate.odds_ratio_per_sd(X[:, 1], y), math.exp(slope), places=6)
        separated = validate.fit_logistic_batch(np.stack([np.column_stack([np.ones(6), [-3, -2, -1, 1, 2, 3]])]), np.array([0, 0, 0, 1, 1, 1.0]), np.ones((1, 6)))
        self.assertTrue(np.all(np.isfinite(separated)))


class LeakageTest(unittest.TestCase):
    def test_assert_disjoint(self):
        validate.assert_disjoint(np.array([True, False, True]), np.array([1]))
        with self.assertRaises(validate.LeakageError):
            validate.assert_disjoint(np.array([True, True, False]), np.array([1]))
        with self.assertRaises(validate.LeakageError):
            validate.assert_disjoint([0, 2, 5], [5, 7])

    def _check_scores(self, data, circuit, counts=None):
        """Held-out lesions are scored with the circuit of their fold's training lesions only, and training lesions
        with the circuit that leaves their inner block out, both equal to circuits built directly (validate.fold_map).
        """
        y = data.outcome
        counts = np.ones(data.n) if counts is None else counts
        folds = validate.make_folds("kfold", y, data.datasets, seed=3, outcome_type=data.outcome_type, k=4, repeats=2, counts=counts)
        prep = validate._prepare(data, circuit, None, 10.0)
        out = validate._cv_runs(prep, [validate._Run(y, counts.astype(float), folds, 11)], keep=True)[0]
        scored = out["folds_scored"]
        checked = 0
        for (repeat, fold), values in zip(scored["items"], scored["circuit"]["circuit"], strict=True):
            row = folds.assignment[repeat]
            test = np.flatnonzero(row == fold)
            train = (row >= 0) & (row != fold)
            outer = validate.fold_map(data, y, train, circuit, weights=counts)
            # Poisoning the held-out maps does not change the circuit they are scored with
            poisoned = validate.ValidationData(**{**data.__dict__, "maps": data.maps.copy()})
            poisoned.maps[test] = 50.0 * np.random.default_rng(fold).standard_normal((test.size, data.maps.shape[1]))
            np.testing.assert_allclose(validate.fold_map(poisoned, y, train, circuit, weights=counts), outer, rtol=1e-10, atol=1e-10)
            for j in test:
                expected = validate.scores.roi_connectivity(data.maps[j], outer) if circuit == "topk" else math.atanh(np.clip(similarity.pearson(data.maps[j], outer), -1, 1))
                self.assertAlmostEqual(values[j], expected, places=3)
                checked += 1
            block = scored["blocks"][repeat]
            member = np.flatnonzero(train)[0]
            inner = validate.fold_map(data, y, train & (block != block[member]), circuit, weights=counts)
            expected = validate.scores.roi_connectivity(data.maps[member], inner) if circuit == "topk" else math.atanh(np.clip(similarity.pearson(data.maps[member], inner), -1, 1))
            self.assertAlmostEqual(values[member], expected, places=3)
        self.assertGreater(checked, 0)

    def test_held_out_lesion_never_in_its_circuit(self):
        data = _cohort(n=36, V=160, effect=0.05, seed=7, occupancy=False, reference=False)
        for circuit in ("glm", "slnm"):
            self._check_scores(data, circuit)

    def test_bootstrap_copies_never_cross_folds(self):
        data = _cohort(n=36, V=160, effect=0.05, seed=8, occupancy=False, reference=False)
        counts = validate.bootstrap_counts(np.random.default_rng(2), data.outcome, None, "binary")
        self._check_scores(data, "glm", counts.astype(float))

    def test_location_map_from_training_lesions_only(self):
        data = _cohort(n=30, V=120, effect=0.0, seed=9, occupancy=True, reference=False)
        y = data.outcome
        folds = validate.make_folds("kfold", y, None, seed=2, k=3, repeats=1)
        prep = validate._prepare(data, "glm", None, 10.0)
        out = validate._cv_runs(prep, [validate._Run(y, np.ones(data.n), folds, 4)], keep=True)[0]
        occupancy = data.occupancy.toarray()
        for (repeat, fold), values in zip(out["folds_scored"]["items"], out["folds_scored"]["location"], strict=True):
            row = folds.assignment[repeat]
            train = (row >= 0) & (row != fold)
            location = occupancy[train & (y == 1)].mean(axis=0) - occupancy[train & (y == 0)].mean(axis=0)
            for j in np.flatnonzero(row == fold):
                self.assertAlmostEqual(values[j], float(occupancy[j] @ location), places=10)

    def test_topk_rois_from_training_lesions(self):
        data = _cohort(n=32, V=200, effect=0.06, seed=10, occupancy=False, reference=False)
        self._check_scores(data, "topk")


class ValidationTest(unittest.TestCase):
    def test_baselines_always_present(self):
        names = {"circuit", "circuit_partial", "volume", "mu_r", "pc1", "location"}
        bare = validate.validate(_cohort(occupancy=False, reference=False), "glm", scheme="kfold", k=4, repeats=1, n_boot=3, n_perm=3)
        full = validate.validate(_cohort(), "glm", scheme="kfold", k=4, repeats=1, n_boot=3, n_perm=3)
        for result, computed in ((bare, {"circuit", "volume"}), (full, names)):
            rows = {item["name"]: item for item in result.predictors}
            self.assertEqual(set(rows), names)
            self.assertEqual({name for name, item in rows.items() if item["status"] == "computed"}, computed)
            for item in rows.values():
                self.assertEqual(set(item["metrics"]), {"auc", "or_per_sd"})
                if item["status"] != "computed":
                    self.assertTrue(item["reason"])
            self.assertEqual({entry["baseline"] for entry in result.comparisons}, {"volume", "mu_r", "pc1", "location"})
        self.assertIn("the degree baseline was not computed: no reference maps in this test", bare.verdicts)
        data = _cohort()
        data.refmaps = {"other": np.random.default_rng(1).standard_normal(300)}
        result = validate.validate(data, "slnm", scheme="kfold", k=4, repeats=1, n_boot=2, n_perm=0)
        self.assertIn("ref:other", {item["name"] for item in result.predictors})
        self.assertIn("ref:other", {entry["baseline"] for entry in result.comparisons})
        record = result.to_dict()
        self.assertFalse(record["circuit"]["permutation_thresholds_in_folds"])
        json.dumps(jobs.to_json(record))

    def test_planted_effect_and_neutral_verdicts(self):
        data = _cohort(n=60, effect=0.08, seed=11)
        result = validate.validate(data, "glm", scheme="kfold", k=5, repeats=2, n_boot=20, n_perm=19, seed=2)
        circuit = result.predictor("circuit")["metrics"]["auc"]
        self.assertGreater(circuit["value"], 0.8)
        self.assertAlmostEqual(circuit["p_perm"], 1 / 20)
        self.assertLessEqual(circuit["ci"][0], circuit["value"])
        self.assertGreaterEqual(circuit["ci"][1], circuit["value"])
        self.assertTrue(result.verdicts[0].startswith("volume + circuit "))
        for text in result.verdicts:
            for word in NEUTRAL_FORBIDDEN:
                self.assertNotIn(word, text.lower())
        self.assertEqual(
            validate.verdict_text(0.01, [-0.04, 0.06], "degree baseline"),
            "the circuit does not exceed the degree baseline (ΔAUC 0.01, 95% CI \u22120.04 to 0.06)",
        )
        self.assertIn("exceeds the PC1 baseline", validate.verdict_text(0.2, [0.1, 0.3], "PC1 baseline"))
        self.assertIn("falls below", validate.verdict_text(-0.2, [-0.3, -0.1], "PC1 baseline"))
        self.assertIn("cannot be compared", validate.verdict_text(float("nan"), [float("nan")] * 2, "PC1 baseline"))

    def test_lodo_fallback_in_validate(self):
        datasets = ["cases"] * 12 + ["controls"] * 12 + ["mixed"] * 24
        data = _cohort(n=48, seed=12, datasets=datasets)
        data.outcome = np.array([1.0] * 12 + [0.0] * 12 + [1.0, 0.0] * 12)
        result = validate.validate(data, "glm", scheme="lodo", k=4, repeats=1, n_boot=2, n_perm=2)
        self.assertEqual(result.folds.scheme, "kfold")
        self.assertTrue(result.warnings[0].startswith("LODO not possible: dataset cases, controls hold one class"))
        both = _cohort(n=48, seed=12, datasets=["A", "B", "C"] * 16)
        result = validate.validate(both, "glm", scheme="auto", n_boot=2, n_perm=2)
        self.assertEqual(result.folds.scheme, "lodo")
        loo = validate.validate(_cohort(n=16, V=100, seed=1), "glm", scheme="loo", n_boot=0, n_perm=0)
        self.assertIn(validate.LOO_WARNING, loo.warnings)

    def test_permutation_p_is_calibrated_under_the_null(self):
        K = 39
        p_values = []
        for seed in range(40):
            data = _cohort(n=32, V=120, effect=0.0, seed=100 + seed, occupancy=False, reference=False)
            result = validate.validate(data, "glm", scheme="kfold", k=4, repeats=1, n_boot=0, n_perm=K, seed=seed)
            p = result.predictor("circuit")["metrics"]["auc"]["p_perm"]
            self.assertGreaterEqual(p, 1 / (K + 1))
            p_values.append(p)
        p_values = np.array(p_values)
        self.assertLess(np.mean(p_values <= 0.1), 0.25)
        self.assertLess(np.mean(p_values <= 0.05), 0.2)
        self.assertGreater(p_values.mean(), 0.35)
        self.assertLess(p_values.mean(), 0.65)

    def test_incremental_ci_covers_zero_for_a_useless_circuit(self):
        data = _cohort(n=80, V=200, effect=0.0, seed=21, occupancy=False, reference=False, volume_effect=1.5)
        result = validate.validate(data, "glm", scheme="kfold", k=5, repeats=2, n_boot=60, n_perm=0, seed=3)
        volume = result.predictor("volume")["metrics"]["auc"]["value"]
        self.assertGreater(volume, 0.7)  # volume carries the outcome
        incremental = result.incremental
        self.assertLessEqual(incremental["ci"][0], 0.0)
        self.assertGreaterEqual(incremental["ci"][1], 0.0)
        self.assertIn("does not exceed the lesion-volume baseline", incremental["text"])
        self.assertLess(abs(incremental["delta"]), 0.1)

    def test_continuous_outcome(self):
        data = _cohort(n=48, effect=0.06, seed=13, outcome_type="continuous")
        result = validate.validate(data, "slnm", scheme="kfold", k=4, repeats=2, n_boot=10, n_perm=9)
        circuit = result.predictor("circuit")["metrics"]
        self.assertEqual(set(circuit), {"r", "r2"})
        self.assertGreater(circuit["r"]["value"], 0.5)
        self.assertEqual(result.incremental["metric"], "r2")
        self.assertIn("ΔR²", result.incremental["text"])


class FrozenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-validate-frozen-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_frozen_metrics(self):
        rng = np.random.default_rng(14)
        y = np.array([1.0] * 20 + [0.0] * 20)
        values = y + rng.standard_normal(40)
        metrics = validate.frozen_metrics(values, y, "binary", n_boot=200, seed=1)
        self.assertAlmostEqual(metrics["auc"]["value"], validate.auc(values, y))
        self.assertLess(metrics["auc"]["ci"][0], metrics["auc"]["value"])
        self.assertGreater(metrics["auc"]["ci"][1], metrics["auc"]["value"])
        continuous = validate.frozen_metrics(values, values + rng.standard_normal(40), "continuous", n_boot=50)
        self.assertEqual(set(continuous), {"r"})

    def test_apply_frozen(self):
        fixture = CommandLineTest._write_fixture(self.tmp, n=24, seed=15)
        circuit = np.zeros(fixture["shape"], dtype=np.float32)
        circuit.reshape(-1)[fixture["indices"]] = fixture["pattern"]
        path = os.path.join(self.tmp, "circuit-frozen_desc-T_map.nii.gz")
        nifti.write_nifti(path, circuit, fixture["affine"])
        with self.assertRaises(ValueError):
            validate.apply_frozen(path, "0" * 64, fixture["maps_tsv"], fixture["participants"], "group", "binary", mask=fixture["mask"])
        result = validate.apply_frozen(path, file_sha256(path), fixture["maps_tsv"], fixture["participants"], "group", "binary", mask=fixture["mask"], n_boot=50)
        self.assertEqual(result["n"], 24)
        maps = result["data"].maps
        for index in (0, 5, 23):
            self.assertAlmostEqual(result["scores"][index], math.atanh(similarity.pearson(maps[index], fixture["pattern"])), places=5)
        self.assertGreater(result["metrics"]["auc"]["value"], 0.8)
        self.assertEqual(len(result["metrics"]["auc"]["ci"]), 2)
        self.assertIn("auc", result["volume_metrics"])


class CommandLineTest(unittest.TestCase):
    SHAPE = (8, 8, 8)

    @classmethod
    def _grid(cls):
        ijk = np.indices(cls.SHAPE).reshape(3, -1).T
        mask = ((((ijk - 3.5) / 3.6) ** 2).sum(axis=1) <= 1.0).reshape(cls.SHAPE)
        affine = np.diag([2.0, 2.0, 2.0, 1.0])
        affine[:3, 3] = -7.0
        return mask, affine

    @classmethod
    def _write_fixture(cls, folder, n=30, seed=16, study=False, bundle=None):
        """maps.tsv with per-lesion AvgFz maps (each in its cache-like folder with weights.npz), a mask and participants.

        :param bundle: (bundle path, subject list sha256): the maps are this bundle's (its mask, cohortmaps.json).
        """
        rng = np.random.default_rng(seed)
        mask, affine = cls._grid()
        indices = np.flatnonzero(mask.reshape(-1))
        V = indices.size
        mask_path = os.path.join(bundle[0], "mask.nii.gz") if bundle else os.path.join(folder, "mask.nii.gz")
        if not bundle:
            nifti.write_nifti(mask_path, mask.astype(np.uint8), affine)
        pattern = np.zeros(V)
        pattern[: V // 6] = 1.0
        group = ["case" if index % 2 == 0 else "control" for index in range(n)]
        if study:
            study_obj = Study.create(os.path.join(folder, "study"), name="study")
            for index in range(n):
                study_obj.add_subject(f"sub-{index:02d}", {"group": group[index], "dataset": "site1" if index < n // 2 else "site2", "age": 50 + index})
            study_obj.save()
            participants = os.path.join(study_obj.path, "participants.tsv")
            maps_dir = os.path.join(study_obj.path, "derivatives", "lnm", "cohortmaps-1")
        else:
            participants = os.path.join(folder, "participants.tsv")
            with open(participants, "w", encoding="utf-8") as f:
                f.write("participant_id\tgroup\tdataset\tage\n")
                for index in range(n):
                    f.write(f"sub-{index:02d}\t{group[index]}\t{'site1' if index < n // 2 else 'site2'}\t{50 + index}\n")
            maps_dir = os.path.join(folder, "cohortmaps")
        os.makedirs(maps_dir)
        lines = ["participant_id\tAvgFz\tT\tvolume_ml\tmanifest_sha256\tsubject_ids_sha256\tstatistic\tsupersample\tprobabilistic\tlibrary_version"]
        for index in range(n):
            sid = f"sub-{index:02d}"
            entry = os.path.join(maps_dir, "cache", f"entry{index:02d}")
            os.makedirs(entry)
            values = 0.25 + 0.08 * rng.standard_normal(V) + (0.06 if group[index] == "case" else -0.06) * pattern
            volume = np.zeros(mask.size, dtype=np.float32)
            volume[indices] = values
            nifti.write_nifti(os.path.join(entry, "lesion_desc-AvgRFz_funcmap.nii.gz"), volume.reshape(cls.SHAPE), affine)
            lesion = rng.choice(V, size=6, replace=False)
            np.savez(os.path.join(entry, "weights.npz"), indices=lesion.astype(np.int32), values=np.ones(6, dtype=np.float32), n_voxels=np.int64(V))
            lines.append(f"{sid}\tcache/entry{index:02d}/lesion_desc-AvgRFz_funcmap.nii.gz\tn/a\t{rng.uniform(1, 20):.3f}\tabc\t{bundle[1] if bundle else '0' * 64}\tT\t2\tfalse\t1.0")
        maps_tsv = os.path.join(maps_dir, "maps.tsv")
        with open(maps_tsv, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        if bundle:
            with open(os.path.join(maps_dir, "cohortmaps.json"), "w", encoding="utf-8") as f:
                json.dump({"format": "slicer-lnm-cohortmaps", "run_id": None, "status": "done", "bundle": {"name": "Synth Conn", "path": bundle[0]}}, f)
        return {"maps_tsv": maps_tsv, "participants": participants, "mask": mask_path, "shape": cls.SHAPE, "affine": affine, "indices": indices, "pattern": pattern, "maps_dir": maps_dir}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-validate-cli-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_in_process(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = jobs.run_job(validate.main, argv, job="validate", exit=False, orphan=False, signals=False)
        jobs.reset_cancel()
        return code, [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]

    def test_end_to_end(self):
        fixture = self._write_fixture(self.tmp, study=True)
        out = os.path.join(os.path.dirname(fixture["maps_dir"]), "validation")
        refmap = os.path.join(self.tmp, "other_map.nii.gz")
        nifti.write_nifti(refmap, np.random.default_rng(1).standard_normal(self.SHAPE).astype(np.float32), fixture["affine"])
        argv = ["--maps", fixture["maps_tsv"], "--participants", fixture["participants"], "--outcome", "group", "--type", "binary", "--circuit", "glm", "--covariates", "age", "--mask", fixture["mask"], "--refmaps", refmap, "--out", out, "--k", "3", "--repeats", "2", "--boot", "4", "--perm", "4", "--threads", "1"]
        code, events = self.run_in_process(argv)
        self.assertEqual(code, 0, events[-1])
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds[-1], "done")
        estimate = [event for event in events if event["event"] == "stage" and event.get("name") == "bootstrap"]
        self.assertEqual(len(estimate), 1)
        self.assertIsNotNone(estimate[0]["eta"])
        self.assertTrue(any(event["event"] == "progress" and event.get("eta") is not None for event in events))
        warnings = [event["message"] for event in events if event["event"] == "warning"]
        self.assertTrue(any("not computed" in message for message in warnings))  # no reference maps: μC and PC1 rows say why
        self.assertEqual(sorted(os.listdir(out)), ["folds.tsv", "scores.tsv", "validation.json"])
        with open(os.path.join(out, "validation.json"), encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(_absolute_strings(record), [])
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["scheme"]["used"], "lodo")  # both sites hold both classes
        self.assertEqual(record["inputs"]["refmaps"], ["../../../../other_map.nii.gz"])
        rows = {item["name"]: item for item in record["predictors"]}
        self.assertEqual(set(rows), {"circuit", "circuit_partial", "volume", "mu_r", "pc1", "location", "ref:other_map"})
        self.assertEqual(rows["location"]["status"], "computed")
        self.assertEqual(rows["mu_r"]["status"], "not computed")
        self.assertEqual(record["covariates"], ["volume_ml", "age"])
        self.assertEqual(record["outcome"]["positive"], "case")
        resolved = validate.read_record(os.path.join(out, "validation.json"))
        self.assertEqual(resolved["inputs"]["maps_tsv"], fixture["maps_tsv"])
        with open(os.path.join(out, "folds.tsv"), encoding="utf-8") as f:
            folds = f.read().splitlines()
        self.assertEqual(folds[0], "participant_id\trepeat\tfold\tfold_label")
        self.assertEqual(len(folds), 1 + 30)  # LODO: one repeat
        with open(os.path.join(out, "scores.tsv"), encoding="utf-8") as f:
            header = f.readline().rstrip("\n").split("\t")
        self.assertIn("circuit_score", header)
        self.assertIn("location_prediction", header)
        registry = runs.load(os.path.dirname(os.path.dirname(os.path.dirname(fixture["maps_dir"]))))
        validation = [record for record in registry.values() if record["kind"] == "validation"]
        self.assertEqual(len(validation), 1)
        self.assertEqual(validation[0]["status"], "done")

    def test_reference_baselines_with_a_bundle(self):
        mask, affine = self._grid()
        rng = np.random.default_rng(5)
        bundle_path = os.path.join(self.tmp, "synth.lnmc")
        writer = connectome.ConnectomeWriter(bundle_path, mask, affine, name="Synth Conn", overwrite=True)
        n_voxels = int(mask.sum())
        for s in range(4):
            writer.add_subject(f"sub-{s + 1:02d}", [rng.standard_normal((n_voxels, 30)) + 50.0 for _ in range(2)])
        writer.finalize()
        bundle = connectome.ConnectomeBundle.open(bundle_path)
        reference.build(bundle, n_seeds=40, k=3, power_iters=2, register=False, emit=lambda *args, **kwargs: None)
        sha = subjects_sha256(bundle.subject_ids)
        bundle.close()
        fixture = self._write_fixture(self.tmp, n=24, bundle=(bundle_path, sha))
        out = os.path.join(self.tmp, "validation")
        code, events = self.run_in_process(["--maps", fixture["maps_tsv"], "--participants", fixture["participants"], "--outcome", "group", "--type", "binary", "--circuit", "topk", "--score", "roi", "--scheme", "kfold", "--k", "3", "--repeats", "1", "--out", out, "--boot", "3", "--perm", "3", "--threads", "1"])
        self.assertEqual(code, 0, events[-1])
        with open(os.path.join(out, "validation.json"), encoding="utf-8") as f:
            record = json.load(f)
        rows = {item["name"]: item for item in record["predictors"]}
        self.assertEqual(rows["mu_r"]["status"], "computed")
        self.assertEqual(rows["pc1"]["status"], "computed")
        self.assertNotIn("circuit_partial", rows)  # ROI connectivity: no partial similarity row
        self.assertEqual(record["reference"]["definition"], "lnm-reference-v1")
        self.assertEqual(record["circuit"]["thresholding"], "top 10% of |t| (a fixed rank rule)")
        self.assertEqual(_absolute_strings(record), [])

    def _rewrite_datasets(self, participants, filler):
        with open(participants, encoding="utf-8") as f:
            lines = f.read().splitlines()
        with open(participants, "w", encoding="utf-8") as f:
            f.write(lines[0] + "\n")
            for index, line in enumerate(lines[1:]):
                parts = line.split("\t")
                parts[2] = filler(index)
                f.write("\t".join(parts) + "\n")

    def test_dataset_column_with_missing_values(self):
        fixture = self._write_fixture(self.tmp)
        common = {"mask": fixture["mask"], "memmap_path": None}

        def load(dataset_column=None):
            events = []
            loaded = validate.load_data(fixture["maps_tsv"], fixture["participants"], "group", "binary", dataset_column=dataset_column, emit=lambda event, **fields: events.append((event, fields)), **common)
            return loaded["data"], loaded["info"], [fields for event, fields in events if event == "warning"]

        # Every site filled in (both hold both classes): LODO; --dataset none: one cohort
        data, info, _ = load()
        self.assertEqual(info["dataset_column"], "dataset")
        self.assertEqual(validate.lodo_check(data.outcome, data.datasets, "binary")[0], True)
        data, info, _ = load("none")
        self.assertIsNone(data.datasets)
        self.assertIsNone(info["dataset_column"])
        self.assertEqual(data.n, 30)
        # An empty dataset column is not picked: one cohort, nobody excluded
        self._rewrite_datasets(fixture["participants"], lambda index: "n/a")
        data, info, warnings = load()
        self.assertEqual((data.n, data.datasets, info["dataset_column"], info["excluded"]), (30, None, None, {}))
        self.assertEqual(warnings, [])
        # Half filled in: the subjects without a dataset form one of their own (with a warning), as in the glm/slnm design
        self._rewrite_datasets(fixture["participants"], lambda index: "n/a" if index % 2 else "site1")
        data, info, warnings = load()
        self.assertEqual(data.n, 30)
        self.assertEqual(info["excluded"], {})
        self.assertEqual(sorted(set(data.datasets)), ["n/a", "site1"])
        self.assertEqual(info["dataset_missing"], 15)
        self.assertEqual([item["code"] for item in warnings], ["missing_dataset"])
        self.assertIn("15 subject(s) have no dataset", warnings[0]["message"])
        # Given explicitly: the subjects without a dataset are excluded (listwise, reported)
        data, info, warnings = load("dataset")
        self.assertEqual(data.n, 15)
        self.assertEqual(len(info["excluded"]["missing dataset"]), 15)
        # The command line uses all 30 subjects; each site holds one class, so LODO falls back to k-fold
        out = os.path.join(self.tmp, "validation")
        argv = ["--maps", fixture["maps_tsv"], "--participants", fixture["participants"], "--outcome", "group", "--type", "binary", "--circuit", "glm", "--mask", fixture["mask"], "--k", "3", "--repeats", "1", "--boot", "0", "--perm", "0", "--threads", "1", "--out", out]
        code, events = self.run_in_process(argv)
        self.assertEqual(code, 0, events[-1])
        with open(os.path.join(out, "validation.json"), encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(record["outcome"]["n"], 30)
        self.assertEqual(record["outcome"]["dataset_missing"], 15)
        self.assertEqual(record["scheme"]["used"], "kfold")
        self.assertIn("LODO not possible", record["scheme"]["reason"])
        self.assertIn("missing_dataset", [event.get("code") for event in events if event["event"] == "warning"])
        estimate = [event for event in events if event["event"] == "stage" and event.get("name") == "bootstrap"]
        self.assertEqual(estimate[0]["estimate"], "no bootstrap or permutation reruns")

    def test_map_range_warning(self):
        fixture = self._write_fixture(self.tmp, n=12)
        path = os.path.join(fixture["maps_dir"], "cache", "entry03", "lesion_desc-AvgRFz_funcmap.nii.gz")
        data, affine, _ = nifti.read_nifti(path)
        nifti.write_nifti(path, (np.asarray(data) * 500.0).astype(np.float32), affine)
        events = []
        validate.load_data(fixture["maps_tsv"], fixture["participants"], "group", "binary", mask=fixture["mask"], emit=lambda event, **fields: events.append(fields))
        ranges = [fields["message"] for fields in events if fields.get("code") == "map_range"]
        self.assertEqual(len(ranges), 1)
        self.assertIn("1 map(s) have |Fz| above 10", ranges[0])
        self.assertIn("sub-03", ranges[0])

    def test_frozen_and_usage(self):
        fixture = self._write_fixture(self.tmp)
        circuit = np.zeros(self.SHAPE, dtype=np.float32)
        circuit.reshape(-1)[fixture["indices"]] = fixture["pattern"]
        path = os.path.join(self.tmp, "circuit.nii.gz")
        nifti.write_nifti(path, circuit, fixture["affine"])
        out = os.path.join(self.tmp, "frozen")
        base = ["--maps", fixture["maps_tsv"], "--participants", fixture["participants"], "--outcome", "group", "--type", "binary", "--mask", fixture["mask"], "--out", out, "--threads", "1"]
        code, events = self.run_in_process([*base, "--frozen", path, "--frozen-sha256", file_sha256(path), "--boot", "30"])
        self.assertEqual(code, 0, events[-1])
        with open(os.path.join(out, "validation.json"), encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(record["mode"], "frozen")
        self.assertEqual(record["frozen"]["sha256"], file_sha256(path))
        self.assertEqual(_absolute_strings(record), [])
        self.assertGreater(record["metrics"]["auc"]["value"], 0.8)
        self.assertEqual(record["frozen"]["score"], "similarity")
        # A top-x% circuit is scored as ROI connectivity by default, as in the cross-validation
        code, events = self.run_in_process([*base, "--frozen", path, "--frozen-sha256", file_sha256(path), "--boot", "10", "--circuit", "topk"])
        self.assertEqual(code, 0, events[-1])
        with open(os.path.join(out, "validation.json"), encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual((record["frozen"]["score"], record["frozen"]["top_pct"]), ("roi", 10.0))
        code, events = self.run_in_process([*base, "--frozen", path, "--frozen-sha256", "1" * 64])
        self.assertEqual(code, jobs.EXIT_USAGE)
        self.assertIn("not the frozen", events[-1]["message"])
        code, events = self.run_in_process(base)
        self.assertEqual(code, jobs.EXIT_USAGE)
        self.assertIn("--circuit is required", events[-1]["message"])


if __name__ == "__main__":
    unittest.main()

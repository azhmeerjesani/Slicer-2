"""Tests of symptom lesion network mapping (slnm.py): partial r, Spearman, block permutation, LODO,
convergence (Stouffer, I²), control-map comparisons with raw-input permutation, and the command line.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingSlnmTest.py -v
"""

import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np
from scipy import stats as scipy_stats

from LesionNetworkMappingLib import connectome, glm, jobs, nifti, reference, runs, similarity, slnm
from LesionNetworkMappingLib import design as designlib
from LesionNetworkMappingLib.export import subjects_sha256
from LesionNetworkMappingLib.study import Study


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))


def _partial_corr(x, Y, Z):
    """Partial correlation of x with every column of Y given Z (least-squares residuals, then Pearson)."""
    n = len(x)
    design = np.column_stack([np.ones(n), Z]) if Z is not None and np.size(Z) else np.ones((n, 1))
    rx = x - design @ np.linalg.lstsq(design, x, rcond=None)[0]
    rY = Y - design @ np.linalg.lstsq(design, Y, rcond=None)[0]
    return (rx @ rY) / np.sqrt((rx @ rx) * np.einsum("nv,nv->v", rY, rY))


class _StudyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-slnm-test-")
        self.rng = np.random.default_rng(12)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def make_study(self, rows, name="study"):
        study = Study.create(self.path(name), name=name)
        for sid, values in rows:
            study.add_subject(sid, values)
        study.save()
        return study

    def score_rows(self, sizes, rng=None):
        """Subjects of datasets with given sizes: nihss, age, volume_ml and dataset columns."""
        rng = rng or self.rng
        rows = []
        for d, size in enumerate(sizes):
            for index in range(size):
                rows.append((f"sub-d{d}s{index:02d}", {"nihss": int(rng.integers(0, 25)), "age": float(rng.uniform(30, 80)), "volume_ml": float(rng.uniform(2, 60)), "dataset": f"site{d + 1}"}))
        return rows


class PartialRTest(_StudyTestCase):
    def test_partial_r_equals_residualisation(self):
        study = self.make_study(self.score_rows([30]))
        sdesign = slnm.slnm_design(study, "nihss", covariates=["volume_ml", "age"])
        design = sdesign.design
        self.assertEqual(design.columns, ["intercept", "nihss", "volume_ml", "age"])
        self.assertEqual(design.design_type, "continuous")
        n = design.n
        x = design.X[:, 1]
        Z = design.X[:, 2:]
        Y = self.rng.standard_normal((n, 40)) + np.outer(x, np.linspace(0, 0.2, 40)) + np.outer(Z[:, 0], np.ones(40)) * 0.05
        result = slnm.run(sdesign, Y, n_perm=200, seed=3, dtype=np.float64)
        self.assertEqual(result.effect_kind, "partial_r")
        expected = _partial_corr(x, Y, Z)
        np.testing.assert_allclose(result.effect, expected, rtol=1e-10, atol=1e-12)
        np.testing.assert_allclose(result.effect, result.stat / np.sqrt(result.stat**2 + result.df), rtol=1e-12)
        self.assertEqual(result.df, n - 4)
        np.testing.assert_allclose(slnm.partial_r_map(x, Y, Z), expected, rtol=1e-10, atol=1e-12)
        np.testing.assert_allclose(slnm.ScoreNull(x, Y, Z).observed, expected, rtol=1e-6, atol=1e-7)
        # A score that does not vary once the covariates are removed gives 0, not NaN
        self.assertTrue(np.all(slnm.partial_r_map(Z[:, 0], Y, Z) == 0))
        # FWE p and FDR q from the permutations; the parametric p is not part of the result
        self.assertEqual(result.n_perm, 200)
        self.assertTrue(np.all((result.p_fwe >= 1 / 200) & (result.p_fwe <= 1)))
        self.assertTrue(np.all(result.q_fdr >= result.p_unc - 1e-12))

    def test_flip(self):
        study = self.make_study(self.score_rows([20]))
        plain = slnm.slnm_design(study, "nihss")
        flipped = slnm.slnm_design(study, "nihss", flip=True)
        self.assertTrue(flipped.flip)
        np.testing.assert_array_equal(flipped.x, -plain.x)
        np.testing.assert_array_equal(flipped.raw_X, plain.raw_X)
        Y = self.rng.standard_normal((plain.n, 25))
        a = slnm.run(plain, Y, n_perm=100, seed=1, dtype=np.float64)
        b = slnm.run(flipped, Y, n_perm=100, seed=1, dtype=np.float64)
        np.testing.assert_allclose(b.effect, -a.effect, atol=1e-12)
        np.testing.assert_allclose(b.p_fwe, a.p_fwe)  # two-sided inference does not depend on the sign

    def test_spearman_equals_ranked_pearson(self):
        rows = self.score_rows([25])
        rows[3][1]["nihss"] = rows[4][1]["nihss"]  # ties share their average rank
        study = self.make_study(rows)
        Y = self.rng.standard_normal((25, 30)) ** 3
        Y[:, 0] = Y[0, 0]  # a constant voxel
        Y[:5, 1] = 1.0  # tied map values
        sdesign = slnm.slnm_design(study, "nihss", covariates=[], volume_off_reason="spearman test", method="spearman")
        scores = np.array([float(values["nihss"]) for _, values in rows])
        result = slnm.run(sdesign, Y, n_perm=100, seed=1, dtype=np.float64)
        rho = np.array([scipy_stats.spearmanr(scores, Y[:, v]).statistic if v else 0.0 for v in range(Y.shape[1])])
        np.testing.assert_allclose(result.effect, rho, rtol=1e-10, atol=1e-12)
        np.testing.assert_array_equal(slnm.rank_maps(Y, chunk=7), scipy_stats.rankdata(Y, axis=0).astype(np.float32))
        # With covariates: the partial Pearson correlation of the ranked score, covariates and maps
        ranked = slnm.slnm_design(study, "nihss", covariates=["volume_ml", "age"], method="spearman")
        self.assertEqual([ranked.design.columns[i] for i in ranked.ranked_columns], ["nihss", "volume_ml", "age"])
        result = slnm.run(ranked, Y, n_perm=100, seed=1, dtype=np.float64)
        covariates = np.array([[values["volume_ml"], values["age"]] for _, values in rows])
        expected = _partial_corr(scipy_stats.rankdata(scores), scipy_stats.rankdata(Y, axis=0)[:, 1:], scipy_stats.rankdata(covariates, axis=0))
        np.testing.assert_allclose(result.effect[1:], expected, rtol=1e-9)
        self.assertEqual(result.effect[0], 0.0)
        with self.assertRaises(ValueError):
            slnm.adjust_design(designlib.build_design(study, designlib.DesignSpec(test="one_sample")))

    def test_block_permutation_respected(self):
        study = self.make_study(self.score_rows([12, 12, 12]))
        sdesign = slnm.slnm_design(study, "nihss")
        design = sdesign.design
        self.assertEqual((design.permute, design.block_column), ("within", "dataset"))
        blocks = design.blocks
        iterator, count, exhaustive = glm.shufflings(design, n_perm=300, seed=2)
        listed = list(iterator)
        self.assertEqual((len(listed), count, exhaustive), (300, 300, False))
        for perm in listed:
            self.assertTrue(np.array_equal(blocks[perm], blocks))
        perms, exhaustive = slnm.null_permutations(design.n, blocks, design.permute, 200, seed=5)
        self.assertEqual(len(perms), 199)
        self.assertFalse(exhaustive)
        for perm in perms:
            self.assertTrue(np.array_equal(blocks[perm], blocks))
        self.assertGreater(sum(not np.array_equal(perm, np.arange(design.n)) for perm in perms), 190)
        # The GLM run uses exactly these within-block shufflings (brute-force refits of the partial r)
        Y = self.rng.standard_normal((design.n, 12))
        result = slnm.run(sdesign, Y, n_perm=300, seed=2, dtype=np.float64)
        Z = design.X[:, 2:]
        nuisance = np.column_stack([np.ones(design.n), Z])
        Yr = Y - nuisance @ np.linalg.lstsq(nuisance, Y, rcond=None)[0]
        fitted = Y - Yr
        null_max = []
        for perm in listed[1:]:
            null_max.append(np.max(np.abs(_partial_corr(design.X[:, 1], Yr[perm] + fitted, Z))))
        observed = np.abs(result.effect)
        expected = (1 + (np.asarray(null_max)[None, :] >= observed[:, None] - 1e-9).sum(axis=1)) / 300
        np.testing.assert_allclose(result.p_fwe, expected)
        # Datasets of one subject cannot be permuted within: the design falls back to free exchange and says so
        free = slnm.slnm_design(self.make_study(self.score_rows([12, 1]), "single"), "nihss")
        self.assertEqual(free.design.permute, "free")
        self.assertIn("blocks_not_used", [check.code for check in free.design.checks])


class ConvergenceFormulaTest(unittest.TestCase):
    def test_fisher_combination_stouffer_and_i2(self):
        r = np.array([[0.3, 0.0, -0.2, 0.5], [0.1, 0.0, -0.2, 0.9999999], [0.5, 0.0, -0.2, -0.4]])
        n = np.array([20, 50, 12])
        k = np.array([1, 2, 1])
        z = np.arctanh(np.clip(r, -slnm.RMAX, slnm.RMAX))
        weights = n - 3
        np.testing.assert_allclose(slnm.fisher_combine(r, n), np.tanh((weights[:, None] * z).sum(0) / weights.sum()))
        scaled = z * np.sqrt(n - 3 - k)[:, None]
        np.testing.assert_allclose(slnm.stouffer(r, n, k), scaled.sum(0) / np.sqrt(3))
        w = n - 3 - k
        mean = (w[:, None] * z).sum(0) / w.sum()
        q = (w[:, None] * (z - mean) ** 2).sum(0)
        i2, q_out = slnm.i_squared(r, n, k)
        np.testing.assert_allclose(q_out, q)
        expected = np.where(q > 0, np.maximum(0.0, (q - 2) / np.where(q > 0, q, 1)), 0.0)
        np.testing.assert_allclose(i2, expected)
        self.assertEqual(i2[1], 0.0)  # identical maps: Q = 0, no heterogeneity
        self.assertEqual(i2[2], 0.0)
        # A textbook check: two studies with z 0 and 1 and weights 10 and 10: Q = 5, I² = (5 - 1) / 5
        i2, q = slnm.i_squared(np.tanh(np.array([[0.0], [1.0]])), np.array([14, 14]), np.array([1, 1]))
        self.assertAlmostEqual(float(q[0]), 5.0)
        self.assertAlmostEqual(float(i2[0]), 0.8)


class LodoTest(_StudyTestCase):
    def _data(self, sizes, effect, rng):
        study = self.make_study(self.score_rows(sizes, rng), name=f"lodo{rng.integers(1 << 30)}")
        sdesign = slnm.slnm_design(study, "nihss", covariates=["volume_ml"])
        n_voxels = 120
        pattern = np.sin(np.linspace(0, 6, n_voxels))
        shared = rng.standard_normal(n_voxels)  # every map shares a connectome-like component
        Y = rng.standard_normal((sdesign.n, n_voxels)) + np.outer(rng.standard_normal(sdesign.n), shared) + effect * np.outer(sdesign.raw_X[:, 1] / 7.0, pattern)
        return sdesign, Y

    def test_weights_and_combination(self):
        rng = np.random.default_rng(3)
        sdesign, Y = self._data([12, 15, 20, 6], 1.0, rng)
        events = []
        result = slnm.lodo(sdesign, Y, n_perm=200, seed=4, on_batch=lambda done, total: events.append((done, total)))
        self.assertTrue(result.ok)
        self.assertEqual(result.datasets, ["site1", "site2", "site3"])
        self.assertEqual(result.n, [12, 15, 20])
        self.assertEqual(result.k, [1, 1, 1])
        self.assertIn("site4", result.skipped)
        self.assertTrue(any("site4" in message for message in result.warnings))
        labels = np.array(sdesign.design.block_labels)
        for d, label in enumerate(result.datasets):
            rows = np.flatnonzero(labels == label)
            expected = _partial_corr(sdesign.raw_X[rows, 1], Y[rows], sdesign.raw_X[rows, 2:])
            np.testing.assert_allclose(result.maps[d], expected, rtol=1e-9, atol=1e-12)
        weights = np.array([9.0, 12.0, 17.0])
        z = np.arctanh(result.maps)
        np.testing.assert_allclose(result.combined, np.tanh((weights[:, None] * z).sum(0) / weights.sum()), rtol=1e-12)
        np.testing.assert_allclose(result.matrix, np.corrcoef(result.maps), atol=1e-12)
        self.assertAlmostEqual(result.mean_offdiag, np.mean([result.matrix[0, 1], result.matrix[0, 2], result.matrix[1, 2]]))
        for d in range(3):
            others = [e for e in range(3) if e != d]
            combined = np.tanh((weights[others, None] * z[others]).sum(0) / weights[others].sum())
            self.assertAlmostEqual(result.lodo_r[d], np.corrcoef(result.maps[d], combined)[0, 1], places=12)
        np.testing.assert_allclose(result.stouffer_z, slnm.stouffer(result.maps, [12, 15, 20], [1, 1, 1]))
        self.assertEqual(result.n_perm, 200)
        self.assertEqual(events[-1], (199, 199))
        self.assertLessEqual(result.p_perm, 0.02)  # a planted shared effect converges
        summary = result.summary()
        json.dumps(summary, allow_nan=False)
        self.assertEqual(summary["weights"], [9, 12, 17])
        self.assertIn("descriptive", summary["convergence"]["label"])
        # Spearman: per-dataset ranks within each dataset
        spearman = slnm.adjust_design(sdesign.design, "spearman")
        spearman.raw_X = sdesign.raw_X
        ranked = slnm.lodo(spearman, Y, n_perm=50, seed=4)
        rows = np.flatnonzero(labels == "site2")
        expected = _partial_corr(scipy_stats.rankdata(sdesign.raw_X[rows, 1]), scipy_stats.rankdata(Y[rows], axis=0), scipy_stats.rankdata(sdesign.raw_X[rows, 2:], axis=0))
        np.testing.assert_allclose(ranked.maps[1], expected, rtol=1e-9)

    def test_too_few_datasets(self):
        rng = np.random.default_rng(4)
        sdesign, Y = self._data([12, 5], 0.0, rng)
        result = slnm.lodo(sdesign, Y, n_perm=50)
        self.assertFalse(result.ok)
        self.assertTrue(any("at least 2 datasets" in message for message in result.warnings))
        json.dumps(result.summary(), allow_nan=False)
        path = slnm.write_lodo_tsv(self.path("lodo.tsv"), result)
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0].split("\t")[:5], ["dataset", "n", "k", "weight", "status"])
        self.assertTrue(lines[1].startswith("site2\tn/a"))


class NullCalibrationTest(_StudyTestCase):
    def test_raw_input_permutation_p_is_calibrated(self):
        rng = np.random.default_rng(21)
        n, n_voxels, reps, n_perm = 16, 150, 300, 100
        study = self.make_study(self.score_rows([n], rng))
        sdesign = slnm.slnm_design(study, "nihss", covariates=["volume_ml"])
        smooth = np.cumsum(rng.standard_normal((2, n_voxels)), axis=1)  # the reference maps [mu_r, pc1]
        p_raw, p_partial = [], []
        for rep in range(reps):
            # Maps biased towards a shared component (as connectome maps are towards PC1); no score effect
            Y = rng.standard_normal((n, n_voxels)) + np.outer(rng.standard_normal(n), smooth[1])
            sdesign.design.X[:, 1] = rng.standard_normal(n)
            main = slnm.partial_r_map(sdesign.x, Y, sdesign.covariates)
            control = slnm.partial_r_map(rng.standard_normal(n), Y, sdesign.covariates)
            item = slnm.compare(sdesign, Y, main, {"control": control}, covariates=smooth, n_perm=n_perm, seed=rep)[0]
            p_raw.append(item["p_perm_r"])
            p_partial.append(item["p_perm_partial"])
        for values in (np.array(p_raw), np.array(p_partial)):
            # Valid permutation p: P(p <= a) <= a; and close to a for a calibrated test (3 binomial SDs)
            for alpha in (0.1, 0.25, 0.5):
                rate = float(np.mean(values <= alpha + 1e-12))
                self.assertLessEqual(abs(rate - alpha), 3 * math.sqrt(alpha * (1 - alpha) / reps) + 0.01, (alpha, rate))
            self.assertGreater(scipy_stats.kstest(values, "uniform").pvalue, 1e-3)

    def test_lodo_block_permutation_p_is_calibrated(self):
        rng = np.random.default_rng(8)
        study = self.make_study(self.score_rows([10, 10, 10], rng))
        sdesign = slnm.slnm_design(study, "nihss", covariates=[], volume_off_reason="calibration")
        shared = np.cumsum(rng.standard_normal(100))
        values = []
        reps = 200
        for _ in range(reps):
            Y = rng.standard_normal((30, 100)) + np.outer(rng.standard_normal(30), shared)
            sdesign.raw_X[:, 1] = rng.standard_normal(30)
            values.append(slnm.lodo(sdesign, Y, n_perm=60, seed=int(rng.integers(1 << 30))).p_perm)
        values = np.array(values)
        for alpha in (0.1, 0.3):
            rate = float(np.mean(values <= alpha + 1e-12))
            self.assertLessEqual(abs(rate - alpha), 3 * math.sqrt(alpha * (1 - alpha) / reps) + 0.02, (alpha, rate))


class ComparisonTest(_StudyTestCase):
    def test_uses_similarity_and_reference_covariates(self):
        rng = np.random.default_rng(31)
        study = self.make_study(self.score_rows([10, 10], rng))
        sdesign = slnm.slnm_design(study, "nihss")
        self.assertEqual(sdesign.design.permute, "within")
        n_voxels = 200
        covs = np.vstack([np.cumsum(rng.standard_normal(n_voxels)), np.sin(np.linspace(0, 9, n_voxels))])
        Y = rng.standard_normal((sdesign.n, n_voxels)) + np.outer(rng.standard_normal(sdesign.n), covs[1]) + np.outer(sdesign.x / 8, covs[0])
        main = slnm.partial_r_map(sdesign.x, Y, sdesign.covariates)
        control = slnm.partial_r_map(sdesign.x + rng.standard_normal(sdesign.n) * 5, Y, sdesign.covariates)
        out = slnm.compare(sdesign, Y, main, {"age": control}, covariates=covs, n_perm=150, seed=2)[0]
        self.assertAlmostEqual(out["r"], similarity.pearson(main, control), places=12)
        self.assertAlmostEqual(out["partial_r"], similarity.partial_spatial_correlation(main, control, covs), places=12)
        self.assertEqual((out["n_perm"], out["tails"]), (150, 2))
        self.assertRegex(out["text"], r"^r = -?\d\.\d\d → partial\(PC1, μC\) r = -?\d\.\d\d, p_perm = [0-9.e-]+$")
        # The batched null partial r equals similarity.partial_spatial_correlation map by map
        null = slnm.ScoreNull(sdesign.x, Y, sdesign.covariates)
        perms, _ = slnm.null_permutations(sdesign.n, sdesign.design.blocks, "within", 5, seed=2)
        maps = null.maps(perms).astype(np.float64)
        basis = slnm._basis(covs.T, n_voxels)
        residual = similarity.residual_map(control, covs)
        batched = similarity.pearson_rows(slnm._residualize_rows(maps, basis), residual)
        expected = [similarity.partial_spatial_correlation(row, control, covs) for row in maps]
        np.testing.assert_allclose(batched, expected, atol=1e-10)
        # The null maps are the partial r of the permuted scores
        np.testing.assert_allclose(maps[0], _partial_corr(sdesign.x[perms[0]], Y, sdesign.covariates), atol=1e-5)
        # Without reference maps the partial comparison is reported as not computed
        out = slnm.compare(sdesign, Y, main, {"age": control}, covariates=None, n_perm=50, reason="no bundle")[0]
        self.assertIsNone(out["partial_r"])
        self.assertIsNone(out["p_perm_partial"])
        self.assertEqual(out["text"], f"r = {out['r']:.2f}, p_perm = {out['p_perm_r']:.2g} (partial(PC1, μC) not computed: no bundle)")
        self.assertEqual(slnm.comparison_text(0.56, 0.3, 0.11), "r = 0.56 → partial(PC1, μC) r = 0.30, p_perm = 0.11")


class CommandLineTest(_StudyTestCase):
    SHAPE = (10, 10, 10)

    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.mkdtemp(prefix="lnm-slnm-bundle-")
        rng = np.random.default_rng(5)
        ijk = np.indices(cls.SHAPE).reshape(3, -1).T
        mask = (((ijk - 4.5) / 4.6) ** 2).sum(axis=1) <= 1.0
        n_voxels = int(mask.sum())
        hub = np.linalg.norm(ijk[mask] - np.array([3, 5, 5]), axis=1) <= 2.5
        affine = np.diag([2.0, 2.0, 2.0, 1.0])
        affine[:3, 3] = -9.0
        writer = connectome.ConnectomeWriter(os.path.join(cls.shared, "synth.lnmc"), mask.reshape(cls.SHAPE), affine, name="Synth Conn", overwrite=True)
        for s in range(4):
            subject_runs = []
            for _ in range(2):
                x = rng.standard_normal((n_voxels, 30))
                x[hub] += 2.0 * rng.standard_normal(30)
                subject_runs.append(x + 50.0)
            writer.add_subject(f"sub-{s + 1:02d}", subject_runs)
        writer.finalize()
        cls.bundle_path = os.path.join(cls.shared, "synth.lnmc")
        bundle = connectome.ConnectomeBundle.open(cls.bundle_path)
        reference.build(bundle, n_seeds=40, k=3, power_iters=2, register=False, emit=lambda *args, **kwargs: None)
        cls.subjects_sha = subjects_sha256(bundle.subject_ids)
        cls.mask = bundle.mask.copy()
        cls.affine = bundle.affine.copy()
        bundle.close()
        cls.mask_path = os.path.join(cls.bundle_path, "mask.nii.gz")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.shared, ignore_errors=True)

    def setUp(self):
        super().setUp()
        rows = self.score_rows([12, 12, 12])
        rows.append(("sub-nomap", {"nihss": 3, "age": 50.0, "volume_ml": 5.0, "dataset": "site1"}))
        rows.append(("sub-noscore", {"nihss": "n/a", "age": 50.0, "volume_ml": 5.0, "dataset": "site2"}))
        self.study = self.make_study(rows)
        maps_dir = self.path("study", "derivatives", "lnm", "cohortmaps-1")
        os.makedirs(maps_dir)
        lines = ["participant_id\tAvgFz\tT\tvolume_ml\tmanifest_sha256\tsubject_ids_sha256\tstatistic\tsupersample\tprobabilistic\tlibrary_version"]
        pattern = np.zeros(self.SHAPE)
        pattern[2:6, 3:7, 3:7] = 1.0
        shared = self.rng.standard_normal(self.SHAPE)
        for sid, values in rows:
            if sid == "sub-nomap":
                continue
            score = values["nihss"] if values["nihss"] != "n/a" else 0
            fz = 0.3 * self.rng.standard_normal(self.SHAPE) + 0.2 * self.rng.standard_normal() * shared + 0.04 * score * pattern
            path = os.path.join(maps_dir, f"{sid}_desc-AvgRFz_funcmap.nii.gz")
            nifti.write_nifti(path, fz.astype(np.float32), self.affine)
            lines.append(f"{sid}\t{os.path.basename(path)}\tn/a\t{values['volume_ml']}\tabc\t{self.subjects_sha}\tT\t2\tfalse\t1.0")
        self.maps_tsv = os.path.join(maps_dir, "maps.tsv")
        with open(self.maps_tsv, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.cohort_run = runs.register(self.study.path, "cohortmaps", maps_dir, status="done")
        with open(os.path.join(maps_dir, "cohortmaps.json"), "w", encoding="utf-8") as f:
            json.dump({"format": "slicer-lnm-cohortmaps", "run_id": self.cohort_run, "status": "done", "bundle": {"name": "Synth Conn", "path": self.bundle_path}}, f)

    def argv(self, out, *extra):
        return ["--maps", self.maps_tsv, "--participants", os.path.join(self.study.path, "participants.tsv"), "--score", "nihss", "--mask", self.mask_path, "--out", out, "--threads", "1", *extra]

    def run_in_process(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = jobs.run_job(slnm.main, argv, job="slnm", exit=False, orphan=False, signals=False)
        jobs.reset_cancel()
        return code, [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]

    def test_end_to_end(self):
        out = self.path("slnm-out")
        argv = self.argv(out, "--covariates", "volume_ml", "--perm", "200", "--lodo", "--control-scores", "age", "--compare-perm", "100")
        result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.slnm", *argv], check=False, capture_output=True, text=True, env=_env(), cwd=self.tmp, timeout=600)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(events[-1]["event"], "done")
        stages = [event["name"] for event in events if event["event"] == "stage"]
        self.assertEqual(stages, ["maps", "permutations", "permutations", "lodo", "compare", "write"])
        self.assertEqual({event["index"] for event in events if event["event"] == "stage"}, set(range(1, 7)))
        names = sorted(os.listdir(out))
        stem = "group_conn-SynthConn_desc-"
        expected = [f"{stem}slnmNihss{stat}_map.nii.gz" for stat in ("R", "PFwe", "PFdr", "LodoR", "StoufferZ", "ISq", "DsSite1R", "DsSite2R", "DsSite3R")]
        expected += [f"{stem}slnmAge{stat}_map.nii.gz" for stat in ("R", "PFwe", "PFdr")]
        expected += ["circuit-slnmNihss_desc-R_map.nii.gz", "design.json", "design_matrix.tsv", "slnm.json", "slnm_lodo.tsv"]
        self.assertEqual(names, sorted(expected))  # no parametric p, no temporary files left
        mask_header = nifti.read_raw_header(self.mask_path)
        for name in names:
            if name.endswith(".nii.gz"):
                header = nifti.read_raw_header(os.path.join(out, name))
                for field in nifti.GEOMETRY_FIELDS:
                    self.assertTrue(np.array_equal(header[field], mask_header[field]), f"{name}: {field}")
        with open(os.path.join(out, "slnm.json"), encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["format"], "slicer-lnm-slnm")
        self.assertEqual((record["score"], record["method"], record["flip"]), ("nihss", "pearson", False))
        self.assertEqual(record["n"], 36)
        self.assertEqual(record["excluded"], {"missing nihss": ["sub-noscore"], "no map in maps.tsv": ["sub-nomap"]})
        self.assertEqual(record["permutation"]["scheme"], "within")
        self.assertEqual(record["permutation"]["block_column"], "dataset")
        self.assertEqual(record["permutation"]["n_perm"], 200)
        self.assertTrue(record["permutation"]["preview"])
        self.assertIsNone(record["parametric_p"])
        self.assertIn("FWE", record["primary_inference"])
        self.assertEqual(record["inference"]["df"], 36 - 3)
        self.assertGreater(record["inference"]["fwe"]["n_significant"], 0)  # the planted score pattern
        self.assertEqual(record["outputs"]["circuit"], "circuit-slnmNihss_desc-R_map.nii.gz")
        lodo = record["lodo"]
        self.assertTrue(lodo["computed"])
        self.assertEqual(lodo["datasets"], ["site1", "site2", "site3"])
        self.assertEqual(lodo["n"], [12, 12, 12])  # sub-noscore (site2) is excluded listwise
        self.assertEqual(lodo["permutation"]["n_perm"], 100)
        self.assertEqual(len(lodo["matrix"]), 3)
        (control,) = record["controls"]
        self.assertEqual((control["name"], control["desc"]), ("age", "slnmAge"))
        self.assertEqual(control["n"], 36)
        self.assertRegex(control["text"], r"^r = -?\d\.\d\d → partial\(PC1, μC\) r = -?\d\.\d\d, p_perm = ")
        self.assertEqual(record["comparison"]["reference"]["definition"], reference.DEFINITION)
        self.assertEqual(record["comparison"]["reference"]["covariates"], ["mu_r", "pc1"])
        info = [event for event in events if event["event"] == "info" and event.get("code") == "control_comparison"]
        self.assertEqual(info[0]["message"], control["text"])
        # The written maps equal an in-process run of the library on the same design
        study = Study.open(self.study.path)
        subset, _ = glm._subset_study(study, {sid for sid in study.subject_ids if sid != "sub-nomap"}, {})
        sdesign = slnm.slnm_design(subset, "nihss", covariates=["volume_ml"], n_perm=200)
        indices = np.flatnonzero(self.mask.reshape(-1))
        Y = np.stack([np.asarray(nifti.read_nifti(os.path.join(os.path.dirname(self.maps_tsv), f"{sid}_desc-AvgRFz_funcmap.nii.gz"))[0], dtype=np.float32).reshape(-1)[indices] for sid in sdesign.sids])
        expected_r = slnm.partial_r_map(sdesign.x, Y.astype(np.float64), sdesign.covariates)
        written, _, _ = nifti.read_nifti(os.path.join(out, f"{stem}slnmNihssR_map.nii.gz"))
        np.testing.assert_allclose(np.asarray(written).reshape(-1)[indices], expected_r, atol=1e-6)
        circuit, _, _ = nifti.read_nifti(os.path.join(out, "circuit-slnmNihss_desc-R_map.nii.gz"))
        np.testing.assert_array_equal(np.asarray(circuit), np.asarray(written))
        # LODO table
        with open(os.path.join(out, "slnm_lodo.tsv"), encoding="utf-8") as f:
            table = [line.split("\t") for line in f.read().splitlines()]
        self.assertEqual(table[0], ["dataset", "n", "k", "weight", "status", "lodo_r", "lodo_p_perm", "mean_r_others", "r_site1", "r_site2", "r_site3"])
        self.assertEqual([row[0] for row in table[1:]], ["site1", "site2", "site3"])
        # Run registry: kind slnm with the cohortmaps parent
        slnm_runs = [run for run in runs.load(self.study.path).values() if run["kind"] == "slnm"]
        self.assertEqual(len(slnm_runs), 1)
        self.assertEqual(slnm_runs[0]["parents"], [self.cohort_run])
        self.assertEqual(slnm_runs[0]["status"], "done")

    def test_spearman_flip_and_missing_reference(self):
        out = self.path("spearman")
        code, events = self.run_in_process(self.argv(out, "--spearman", "--flip", "--perm", "100", "--control-scores", "age", "--compare-perm", "50", "--bundle", self.path("missing.lnmc")))
        self.assertEqual(code, 0, events[-1])
        self.assertIn("no_reference", [event.get("code") for event in events if event["event"] == "warning"])
        with open(os.path.join(out, "slnm.json"), encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual((record["method"], record["flip"]), ("spearman", True))
        self.assertEqual(record["ranked_columns"], ["nihss", "volume_ml"])
        spec, resolved = designlib.read_design_json(os.path.join(out, "design.json"))
        self.assertEqual(spec.extra["slnm"], {"method": "spearman", "flip": True, "ranked_columns": ["nihss", "volume_ml"]})
        self.assertEqual(resolved["design_type"], "continuous")
        self.assertIsNone(record["controls"][0]["partial_r"])
        self.assertIn("not computed", record["controls"][0]["text"])
        study = Study.open(self.study.path)
        subset, _ = glm._subset_study(study, {sid for sid in study.subject_ids if sid != "sub-nomap"}, {})
        sdesign = slnm.slnm_design(subset, "nihss", method="spearman", flip=True)
        indices = np.flatnonzero(self.mask.reshape(-1))
        Y = np.stack([np.asarray(nifti.read_nifti(os.path.join(os.path.dirname(self.maps_tsv), f"{sid}_desc-AvgRFz_funcmap.nii.gz"))[0], dtype=np.float64).reshape(-1)[indices] for sid in sdesign.sids])
        ranks = scipy_stats.rankdata(Y.astype(np.float32), axis=0)
        expected = -_partial_corr(scipy_stats.rankdata(sdesign.raw_X[:, 1]), ranks, scipy_stats.rankdata(sdesign.raw_X[:, 2:], axis=0))
        written, _, _ = nifti.read_nifti(os.path.join(out, "group_conn-SynthConn_desc-slnmNihssR_map.nii.gz"))
        np.testing.assert_allclose(np.asarray(written).reshape(-1)[indices], expected, atol=1e-6)

    def test_usage_errors(self):
        code, events = self.run_in_process(self.argv(self.path("bad"), "--control-scores", "nihss"))
        self.assertEqual(code, jobs.EXIT_USAGE)
        code, events = self.run_in_process(self.argv(self.path("bad"), "--control-scores", "unknown"))
        self.assertEqual(code, jobs.EXIT_USAGE)
        self.assertIn("unknown", events[-1]["message"])
        code, events = self.run_in_process(self.argv(self.path("bad"), "--covariates", "volume_ml,dataset2"))
        self.assertEqual(code, jobs.EXIT_USAGE)
        self.assertEqual(events[-1]["code"], "design_blocked")

    def test_resume_after_cancel_reproduces_the_uninterrupted_result(self):
        extra = ("--perm", "300", "--batch", "50", "--lodo", "--control-scores", "age", "--compare-perm", "60")
        reference_out = self.path("reference")
        code, _ = self.run_in_process(self.argv(reference_out, *extra))
        self.assertEqual(code, 0)
        out = self.path("resumed")
        cancel_file = self.path("stop")
        original = jobs.emit
        seen = {"progress": 0}

        def emit(event, **fields):
            # Stop during the permutations of the control map (after the main map finished)
            if event == "progress" and fields.get("stage") == "permutations" and fields.get("unit") == "control:age":
                seen["progress"] += 1
                if seen["progress"] == 2:
                    open(cancel_file, "w").close()
            return original(event, **fields)

        with mock.patch.object(jobs, "emit", emit):
            code, events = self.run_in_process(self.argv(out, *extra, "--cancel-file", cancel_file))
        self.assertEqual(code, jobs.EXIT_CANCELLED)
        self.assertEqual(events[-1]["event"], "cancelled")
        with open(os.path.join(out, "slnm.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["status"], "cancelled")
        os.remove(cancel_file)
        code, events = self.run_in_process(self.argv(out, *extra, "--cancel-file", cancel_file, "--resume"))
        self.assertEqual(code, 0, events[-1])
        resumed = [event for event in events if event["event"] == "stage" and event["name"] == "permutations"]
        self.assertEqual([event["resumed"] for event in resumed], [299, 100])
        names = sorted(name for name in os.listdir(reference_out) if name.endswith((".nii.gz", ".tsv")))
        self.assertEqual(names, sorted(name for name in os.listdir(out) if name.endswith((".nii.gz", ".tsv"))))
        for name in names:
            if name.endswith(".nii.gz"):
                a, _, _ = nifti.read_nifti(os.path.join(reference_out, name))
                b, _, _ = nifti.read_nifti(os.path.join(out, name))
                self.assertTrue(np.array_equal(a, b), name)
            else:
                with open(os.path.join(reference_out, name), encoding="utf-8") as f, open(os.path.join(out, name), encoding="utf-8") as g:
                    self.assertEqual(f.read(), g.read(), name)
        self.assertFalse([name for name in os.listdir(out) if name.startswith(".slnm-")])
        statuses = sorted(run["status"] for run in runs.load(self.study.path).values() if run["kind"] == "slnm")
        self.assertEqual(statuses, ["cancelled", "done", "done"])
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", self.subjects_sha))


if __name__ == "__main__":
    unittest.main()

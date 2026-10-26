"""Tests of the QA v2 numerics (qa.py, similarity.py; addendum §3.7).

Map similarity (Pearson, Spearman, partial correlation given covariate maps, the residual "debiased
view", R²) against independent formulas; calibrated percentiles and chips; the reference similarity
and calibrated chip of single-lesion and group maps against a manual calculation on the null voxel
sample; the analytic random-location z-map; and the end-to-end path with a reference built by
reference.py on a small bundle and maps from the engine.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingQaTest.py -v
"""

import inspect
import json
import os
import shutil
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np
from scipy import stats as scipy_stats

from LesionNetworkMappingLib import connectome, engine, qa, reference, similarity

LEGACY_KEYS = {"degree_r", "degree_rho", "degree_level", "null", "null_level"}
REFERENCE_QA_KEYS = {
    "mu_r", "mu_rho", "pc1_r", "pc1_rho", "pc2_r", "pc2_rho", "pc3_r", "pc3_rho", "hub_r", "hub_rho",
    "r2_ref", "partial_pc1_mu", "mu_r_sample", "percentile", "k", "resolution", "level", "text", "null_kind",
}  # fmt: skip


def _corr(a, b):
    return float(np.corrcoef(a, b)[0, 1])


def _manual_residual(y, covariates):
    """Residual of y on [1, covariates] by an explicit least-squares solve of the uncentred design."""
    design = np.column_stack([np.ones(len(y)), *np.atleast_2d(covariates)])
    beta = np.linalg.lstsq(design, y, rcond=None)[0]
    return y - design @ beta


def _precision_partial(a, b, covariates):
    """Partial correlation from the inverse correlation matrix of [a, b, covariates] (no residualisation)."""
    precision = np.linalg.inv(np.corrcoef(np.vstack([a, b, *np.atleast_2d(covariates)])))
    return float(-precision[0, 1] / np.sqrt(precision[0, 0] * precision[1, 1]))


def _synthetic_reference(rng, n_voxels):
    """Reference maps with realistic correlations between them (μC_r, PC1 related; hub related to μC_r)."""
    mu_r = rng.standard_normal(n_voxels)
    pc1 = 0.6 * mu_r + 0.8 * rng.standard_normal(n_voxels)
    maps = {
        "mu_r": mu_r.astype(np.float32),
        "mu_z": (1.1 * mu_r).astype(np.float32),
        "sigma_r": (0.2 + np.abs(rng.standard_normal(n_voxels)) * 0.05).astype(np.float32),
        "hub": (np.abs(mu_r) + 0.3 * rng.standard_normal(n_voxels)).astype(np.float32),
        "pc1": pc1.astype(np.float32),
        "pc2": rng.standard_normal(n_voxels).astype(np.float32),
        "pc3": rng.standard_normal(n_voxels).astype(np.float32),
    }
    return reference.Reference(maps, np.array([3.0, 2.0, 1.0]), {"definition": reference.DEFINITION}, "", "0" * 64)


class SimilarityTest(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(3)

    def test_pearson_and_spearman(self):
        a = self.rng.standard_normal(500)
        b = 0.5 * a + self.rng.standard_normal(500)
        b[[3, 9]] = np.nan
        a[17] = np.inf
        valid = np.isfinite(a) & np.isfinite(b)
        self.assertAlmostEqual(similarity.pearson(a, b), _corr(a[valid], b[valid]), places=12)
        self.assertAlmostEqual(similarity.spearman(a, b), scipy_stats.spearmanr(a[valid], b[valid]).statistic, places=12)
        self.assertEqual(similarity.pearson_spearman(a, b), (similarity.pearson(a, b), similarity.spearman(a, b)))
        # Ties share their average rank
        ties = np.array([3.0, 1.0, 3.0, 2.0, 1.0, 3.0])
        np.testing.assert_array_equal(similarity.ranks(ties), scipy_stats.rankdata(ties))
        self.assertAlmostEqual(similarity.spearman(ties, np.arange(6.0)), scipy_stats.spearmanr(ties, np.arange(6.0)).statistic, places=12)
        # Undefined: constant map, fewer than 3 finite voxels
        self.assertTrue(np.isnan(similarity.pearson(np.ones(10), np.arange(10.0))))
        self.assertTrue(np.isnan(similarity.spearman(np.ones(10), np.arange(10.0))))
        self.assertTrue(np.isnan(similarity.pearson([1.0, 2.0, np.nan], [1.0, 3.0, 2.0])))
        with self.assertRaises(ValueError):
            similarity.pearson(np.zeros(4), np.zeros(5))
        # qa keeps its former private helpers (same implementation)
        self.assertIs(qa._ranks, similarity.ranks)
        self.assertEqual(qa._pearson(a[valid], b[valid]), similarity.pearson(a, b))

    def test_rows(self):
        b = self.rng.standard_normal(300)
        rows = self.rng.standard_normal((4, 300)) + b
        rows[2] = 5.0  # constant row
        rows[1, 7] = np.nan  # excluded for every row
        valid = np.isfinite(rows).all(axis=0)
        r = similarity.pearson_rows(rows, b)
        r2, rho = similarity.pearson_spearman_rows(rows, b)
        for index in (0, 1, 3):
            self.assertAlmostEqual(r[index], _corr(rows[index, valid], b[valid]), places=12)
            self.assertAlmostEqual(r2[index], r[index], places=12)
            self.assertAlmostEqual(rho[index], scipy_stats.spearmanr(rows[index, valid], b[valid]).statistic, places=12)
        self.assertTrue(np.isnan(r[2]) and np.isnan(r2[2]) and np.isnan(rho[2]))
        # Ranks of the rows are cached by content: changing a row in place gives fresh values
        fixed = self.rng.standard_normal((1, 300))
        first = similarity.pearson_spearman_rows(fixed, b)[1][0]
        fixed[0] = -fixed[0]
        self.assertAlmostEqual(similarity.pearson_spearman_rows(fixed, b)[1][0], -first, places=12)

    def test_partial_correlation(self):
        n = 2000
        c1 = self.rng.standard_normal(n)
        c2 = 0.5 * c1 + self.rng.standard_normal(n)
        a = 2.0 * c1 - c2 + self.rng.standard_normal(n) + 7.0
        b = c1 + 0.3 * a + self.rng.standard_normal(n) - 3.0
        covariates = np.vstack([c1, c2])
        partial = similarity.partial_spatial_correlation(a, b, covariates)
        # Residualisation formula (explicit uncentred design) and the precision-matrix formula
        self.assertAlmostEqual(partial, _corr(_manual_residual(a, covariates), _manual_residual(b, covariates)), places=10)
        self.assertAlmostEqual(partial, _precision_partial(a, b, covariates), places=10)
        # One covariate: the textbook first-order formula
        r_ab, r_ac, r_bc = _corr(a, b), _corr(a, c1), _corr(b, c1)
        first_order = (r_ab - r_ac * r_bc) / np.sqrt((1 - r_ac**2) * (1 - r_bc**2))
        self.assertAlmostEqual(similarity.partial_spatial_correlation(a, b, c1), first_order, places=10)
        # No covariates: Pearson; a duplicated, a scaled and a constant covariate change nothing
        self.assertAlmostEqual(similarity.partial_spatial_correlation(a, b, None), _corr(a, b), places=12)
        self.assertAlmostEqual(similarity.partial_spatial_correlation(a, b, np.zeros((0, n))), _corr(a, b), places=12)
        redundant = np.vstack([c1, c2, c1, 3.0 * c2 + 1.0, np.full(n, 4.0)])
        self.assertAlmostEqual(similarity.partial_spatial_correlation(a, b, redundant), partial, places=10)
        # Non-finite voxels in any input are left out
        a2, c2_nan = a.copy(), covariates.copy()
        a2[5] = np.nan
        c2_nan[1, 11] = np.inf
        keep = np.ones(n, dtype=bool)
        keep[[5, 11]] = False
        expected = _precision_partial(a[keep], b[keep], covariates[:, keep])
        self.assertAlmostEqual(similarity.partial_spatial_correlation(a2, b, c2_nan), expected, places=10)
        # A map inside the covariates' span has no partial correlation
        self.assertTrue(np.isnan(similarity.partial_spatial_correlation(2.0 * c1 - c2, b, covariates)))
        self.assertTrue(np.isnan(similarity.partial_spatial_correlation(a[:4], b[:4], covariates[:, :4])))
        with self.assertRaises(ValueError):
            similarity.partial_spatial_correlation(a, b, np.zeros((2, n + 1)))

    def test_residual_map_and_r2(self):
        n = 1500
        covariates = self.rng.standard_normal((2, n))
        values = 3.0 + covariates[0] - 0.5 * covariates[1] + self.rng.standard_normal(n)
        values[[4, 40]] = np.nan
        covariates[1, 99] = np.nan
        residual = similarity.residual_map(values, covariates)
        valid = np.isfinite(values) & np.isfinite(covariates).all(axis=0)
        np.testing.assert_array_equal(np.isnan(residual), ~valid)
        # Orthogonal to the intercept and to each covariate; equal to the explicit least-squares residual
        self.assertLess(abs(residual[valid].sum()), 1e-9)
        for row in covariates:
            self.assertLess(abs(residual[valid] @ row[valid]) / np.linalg.norm(row[valid]), 1e-9)
        np.testing.assert_allclose(residual[valid], _manual_residual(values[valid], covariates[:, valid]), atol=1e-10)
        # R² = 1 - SSres/SStot, the squared multiple correlation; r² for one covariate
        y = values[valid]
        expected = 1.0 - (residual[valid] @ residual[valid]) / ((y - y.mean()) @ (y - y.mean()))
        self.assertAlmostEqual(similarity.r2_explained(values, covariates), expected, places=12)
        self.assertAlmostEqual(similarity.r2_explained(y, covariates[0, valid]), _corr(y, covariates[0, valid]) ** 2, places=12)
        self.assertAlmostEqual(similarity.r2_explained(covariates[0] * 2.0 + 1.0, covariates), 1.0, places=12)
        self.assertTrue(np.isnan(similarity.r2_explained(np.ones(n), covariates)))
        # No covariates: the centred map
        np.testing.assert_allclose(similarity.residual_map(y, None), y - y.mean(), atol=1e-12)
        self.assertEqual(similarity.DEBIASED_LABEL, "descriptive; not an inferential correction")


class CalibrationTest(unittest.TestCase):
    def test_percentile_edge_cases(self):
        nulls = np.array([0.1, 0.2, 0.3, 0.4])
        self.assertEqual(qa.calibrated_percentile(0.5, nulls), (100.0, 4))
        self.assertEqual(qa.calibrated_percentile(0.0, nulls), (0.0, 4))
        self.assertEqual(qa.calibrated_percentile(0.25, nulls), (50.0, 4))
        # Ties count half
        self.assertEqual(qa.calibrated_percentile(0.3, nulls), (62.5, 4))
        self.assertEqual(qa.calibrated_percentile(1.0, np.ones(10)), (50.0, 10))
        self.assertEqual(qa.calibrated_percentile(0.2, [0.2, 0.2, 0.1, 0.5]), (50.0, 4))
        # Non-finite null values are not counted; undefined observed value or no nulls
        self.assertEqual(qa.calibrated_percentile(0.35, [0.1, np.nan, 0.3, np.inf, 0.4]), (200.0 / 3.0, 3))
        self.assertEqual(qa.calibrated_percentile(float("nan"), nulls), (None, 4))
        self.assertEqual(qa.calibrated_percentile(None, nulls), (None, 4))
        self.assertEqual(qa.calibrated_percentile(0.3, []), (None, 0))
        rng = np.random.default_rng(0)
        values = np.round(rng.standard_normal(50), 1)
        for observed in (values[3], 0.05, -3.0, 3.0):
            self.assertAlmostEqual(qa.calibrated_percentile(observed, values)[0], scipy_stats.percentileofscore(values, observed, kind="mean"), places=10)

    def test_levels(self):
        self.assertEqual(qa.calibrated_level(4.9, 50), ("below", "less degree-like than relocated lesions; percentile resolution 2% at K=50"))
        self.assertEqual(qa.calibrated_level(5.0, 50), ("amber", "indistinguishable from relocated lesions on this axis; percentile resolution 2% at K=50"))
        self.assertEqual(qa.calibrated_level(95.0, 50)[0], "amber")
        self.assertEqual(qa.calibrated_level(96.0, 50), ("red", "more degree-like than relocated lesions; percentile resolution 2% at K=50"))
        self.assertEqual(qa.calibrated_level(100.0, 3)[1], "more degree-like than relocated lesions; percentile resolution 33% at K=3")
        self.assertTrue(qa.calibrated_level(50.0, 1000)[1].endswith("percentile resolution 0.1% at K=1000"))
        self.assertEqual(qa.calibrated_level(0.0, 20, "null cohorts")[1], "less degree-like than null cohorts; percentile resolution 5% at K=20")
        self.assertEqual(qa.calibrated_level(None, 0)[0], qa.NOT_CALIBRATED)
        self.assertEqual(qa.calibrated_level(None, 50)[0], qa.NOT_CALIBRATED)
        # The fixed cut-offs stay available as secondary text
        self.assertEqual(qa.chip_level(0.45), ("amber", "moderate"))
        self.assertEqual(qa.null_level(0.8), "amber")

    def test_no_nine_factor_line(self):
        for module in (qa, similarity):
            source = inspect.getsource(module)
            self.assertNotIn("93%", source)
            self.assertNotIn("9-factor", source)


class LesionQaTest(unittest.TestCase):
    N_VOXELS = 4000

    def setUp(self):
        self.rng = np.random.default_rng(11)
        self.reference = _synthetic_reference(self.rng, self.N_VOXELS)
        self.mu = self.reference.maps["mu_r"].astype(np.float64)
        self.null_voxels = np.sort(self.rng.choice(self.N_VOXELS, 1500, replace=False))
        # Map: partly degree-like; nulls: as degree-like as each other, some more, some less
        self.map = 0.5 * self.mu + self.rng.standard_normal(self.N_VOXELS)
        weights = np.linspace(0.0, 1.0, 50)
        self.nulls = weights[:, np.newaxis] * self.mu[self.null_voxels] + self.rng.standard_normal((50, len(self.null_voxels)))

    def test_keys_without_reference_are_unchanged(self):
        degree = self.reference.maps["hub"]
        values = qa.lesion_qa(self.map, degree, self.nulls, self.null_voxels)
        self.assertEqual(set(values), LEGACY_KEYS)
        self.assertEqual(values, qa.lesion_qa(self.map, degree, self.nulls, self.null_voxels, reference=None))
        self.assertEqual(set(qa.lesion_qa(self.map)), LEGACY_KEYS)

    def test_reference_keys_and_values(self):
        values = qa.lesion_qa(self.map, self.reference.maps["hub"], self.nulls, self.null_voxels, reference=self.reference)
        self.assertEqual(set(values), LEGACY_KEYS | {"reference"})
        ref = values["reference"]
        self.assertEqual(set(ref), REFERENCE_QA_KEYS)
        json.dumps(values, allow_nan=False)  # JSON-ready (qa.json)
        maps = self.reference.maps
        for key, name in (("mu_r", "mu"), ("pc1", "pc1"), ("pc2", "pc2"), ("pc3", "pc3"), ("hub", "hub")):
            self.assertAlmostEqual(ref[f"{name}_r"], _corr(self.map, maps[key]), places=10)
            self.assertAlmostEqual(ref[f"{name}_rho"], scipy_stats.spearmanr(self.map, maps[key]).statistic, places=10)
        covariates = np.vstack([maps[key] for key in qa.R2_REFERENCE_KEYS]).astype(np.float64)
        residual = _manual_residual(self.map, covariates)
        centred = self.map - self.map.mean()
        self.assertAlmostEqual(ref["r2_ref"], 1.0 - residual @ residual / (centred @ centred), places=10)
        self.assertAlmostEqual(ref["partial_pc1_mu"]["pc1_given_mu_r"], _precision_partial(self.map, maps["pc1"], maps["mu_r"]), places=10)
        self.assertAlmostEqual(ref["partial_pc1_mu"]["mu_r_given_pc1"], _precision_partial(self.map, maps["mu_r"], maps["pc1"]), places=10)
        # Percentile: r(map, μC_r) among r(null map, μC_r), both on the null voxel sample
        sample_mu = self.reference.restrict(self.null_voxels).maps["mu_r"]
        observed = _corr(self.map[self.null_voxels], sample_mu)
        null_r = np.array([_corr(row, sample_mu) for row in self.nulls])
        expected = 100.0 * (np.count_nonzero(null_r < observed) + 0.5 * np.count_nonzero(null_r == observed)) / len(null_r)
        self.assertAlmostEqual(ref["mu_r_sample"], observed, places=10)
        self.assertAlmostEqual(ref["percentile"], expected, places=10)
        self.assertGreater(ref["percentile"], 5.0)
        self.assertLess(ref["percentile"], 95.0)
        self.assertEqual((ref["k"], ref["resolution"], ref["null_kind"]), (50, 2.0, "relocated"))
        self.assertEqual((ref["level"], ref["text"]), qa.calibrated_level(ref["percentile"], 50))
        self.assertTrue(ref["text"].endswith("percentile resolution 2% at K=50"))
        # Same values from a plain dict of maps and from a boolean null-voxel mask
        mask = np.zeros(self.N_VOXELS, dtype=bool)
        mask[self.null_voxels] = True
        self.assertEqual(qa.lesion_qa(self.map, None, self.nulls, mask, reference=dict(maps))["reference"], ref)
        # reference_similarity of the same map
        sim = qa.reference_similarity(self.map, self.reference)
        self.assertEqual(sim["mu_r"], (ref["mu_r"], ref["mu_rho"]))
        self.assertEqual(sim["r2_ref"], ref["r2_ref"])

    def test_levels_follow_the_nulls(self):
        k = 40
        sample_mu = self.mu[self.null_voxels]
        noise = self.rng.standard_normal((k, len(self.null_voxels)))
        mild = 0.3 * sample_mu + noise
        strong = 0.5 * self.mu + 0.05 * self.rng.standard_normal(self.N_VOXELS)  # much more degree-like than the nulls
        weak = -0.5 * self.mu + self.rng.standard_normal(self.N_VOXELS)
        self.assertEqual(qa.lesion_qa(strong, null_maps=mild, null_voxels=self.null_voxels, reference=self.reference)["reference"]["level"], "red")
        self.assertEqual(qa.lesion_qa(weak, null_maps=mild, null_voxels=self.null_voxels, reference=self.reference)["reference"]["percentile"], 0.0)
        # Without null maps: similarity only, not calibrated
        ref = qa.lesion_qa(strong, reference=self.reference)["reference"]
        self.assertEqual((ref["percentile"], ref["k"], ref["level"]), (None, 0, qa.NOT_CALIBRATED))
        self.assertGreater(ref["mu_r"], 0.99)
        self.assertIsNone(ref["mu_r_sample"])
        # A null voxel NaN in one null map removes that voxel for the observed map and every null map
        nulls = mild.copy()
        nulls[3, 10] = np.nan
        keep = np.ones(len(self.null_voxels), dtype=bool)
        keep[10] = False
        ref = qa.lesion_qa(self.map, null_maps=nulls, null_voxels=self.null_voxels, reference=self.reference)["reference"]
        self.assertAlmostEqual(ref["mu_r_sample"], _corr(self.map[self.null_voxels][keep], sample_mu[keep]), places=10)
        with self.assertRaises(ValueError):
            qa.lesion_qa(self.map, null_maps=mild[:, :10], null_voxels=self.null_voxels, reference=self.reference)
        with self.assertRaises(ValueError):
            qa.reference_similarity(self.map[:10], self.reference)

    def test_reference_with_fewer_maps(self):
        maps = {key: self.reference.maps[key] for key in ("mu_r", "pc1", "sigma_r")}
        sim = qa.reference_similarity(self.map, maps)
        self.assertEqual((sim["pc2"], sim["pc3"], sim["hub"]), ((None, None), (None, None), (None, None)))
        covariates = np.vstack([maps["mu_r"], maps["pc1"]]).astype(np.float64)
        residual = _manual_residual(self.map, covariates)
        centred = self.map - self.map.mean()
        self.assertAlmostEqual(sim["r2_ref"], 1.0 - residual @ residual / (centred @ centred), places=10)
        sim = qa.reference_similarity(self.map, {"mu_r": maps["mu_r"]})
        self.assertEqual(sim["partial_pc1_mu"], {"pc1_given_mu_r": None, "mu_r_given_pc1": None})
        self.assertAlmostEqual(sim["r2_ref"], _corr(self.map, maps["mu_r"]) ** 2, places=10)
        with self.assertRaises(ValueError):
            qa.reference_similarity(self.map, {"pc1": maps["pc1"]})
        # On a voxel selection: (V,) maps and maps already restricted give the same values
        voxels = self.null_voxels
        self.assertEqual(qa.reference_similarity(self.map, self.reference, voxels), qa.reference_similarity(self.map[voxels], self.reference, voxels))
        self.assertAlmostEqual(qa.reference_similarity(self.map, self.reference, voxels)["mu_r"][0], _corr(self.map[voxels], self.mu[voxels]), places=10)

    def test_group_qa(self):
        self.assertIsNone(qa.group_qa(self.map, None))
        ref = qa.group_qa(self.map, self.reference)
        self.assertEqual(set(ref), REFERENCE_QA_KEYS)
        self.assertEqual((ref["level"], ref["percentile"], ref["null_kind"]), (qa.NOT_CALIBRATED, None, "null_cohort"))
        self.assertEqual(ref["text"], "not calibrated (no null cohorts to compare with)")
        null_groups = np.linspace(0.0, 1.0, 20)[:, np.newaxis] * self.mu + self.rng.standard_normal((20, self.N_VOXELS))
        ref = qa.group_qa(self.map, self.reference, null_groups, null_kind="rl_independent")
        null_r = np.array([_corr(row, self.mu) for row in null_groups])
        observed = _corr(self.map, self.mu)
        self.assertAlmostEqual(ref["percentile"], 100.0 * np.count_nonzero(null_r < observed) / 20, places=10)
        self.assertEqual((ref["k"], ref["null_kind"]), (20, "rl_independent"))
        self.assertIn("null cohorts", ref["text"])
        self.assertTrue(ref["text"].endswith("percentile resolution 5% at K=20"))

    def test_analytic_rl_zmap(self):
        avg_r = 0.1 * self.rng.standard_normal(self.N_VOXELS)
        z = qa.analytic_rl_zmap(avg_r, self.reference)
        maps = self.reference.maps
        np.testing.assert_allclose(z, (avg_r - maps["mu_r"].astype(np.float64)) / maps["sigma_r"].astype(np.float64), rtol=1e-12)
        sigma = maps["sigma_r"].copy()
        sigma[5] = 0.0
        avg_r[6] = np.nan
        z = qa.analytic_rl_zmap(avg_r, {"mu_r": maps["mu_r"], "sigma_r": sigma})
        self.assertTrue(np.isnan(z[5]) and np.isnan(z[6]))
        self.assertEqual(np.count_nonzero(np.isnan(z)), 2)
        self.assertIsNone(qa.analytic_rl_zmap(avg_r, {"mu_r": maps["mu_r"]}))
        self.assertIsNone(qa.analytic_rl_zmap(avg_r, None))
        self.assertIn("approximate random-location null", qa.ANALYTIC_RL_LABEL)
        self.assertIn("descriptive", qa.ANALYTIC_RL_LABEL)
        self.assertEqual(qa.R2_REFERENCE_LABEL, "spatial R² on these 4 references")


class ReferenceIntegrationTest(unittest.TestCase):
    """qa.load_reference and lesion_qa with a reference built by reference.py and maps from the engine."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-qa-")
        rng = np.random.default_rng(5)
        shape = (12, 12, 12)
        ijk = np.indices(shape).reshape(3, -1).T
        mask = (((ijk - 5.5) / 5.5) ** 2).sum(axis=1) <= 1.0
        n_voxels = int(mask.sum())
        hub = np.linalg.norm(ijk[mask] - np.array([3, 6, 6]), axis=1) <= 3
        writer = connectome.ConnectomeWriter(os.path.join(cls.tmp, "qa.lnmc"), mask.reshape(shape), np.diag([2.0, 2.0, 2.0, 1.0]), name="QaTest", overwrite=True)
        for s in range(4):
            runs = []
            for _ in range(2):
                x = rng.standard_normal((n_voxels, 30))
                x[hub] += 2.0 * rng.standard_normal(30)
                runs.append(x + 50.0)
            writer.add_subject(f"sub-{s + 1:02d}", runs)
        writer.finalize()
        cls.bundle = connectome.ConnectomeBundle.open(os.path.join(cls.tmp, "qa.lnmc"))
        reference.build(cls.bundle, n_seeds=40, k=3, power_iters=2, register=False, emit=lambda *args, **kwargs: None)

    @classmethod
    def tearDownClass(cls):
        cls.bundle.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_lesion_qa_with_built_reference(self):
        self.assertIsNone(qa.load_reference(os.path.join(self.tmp, "missing.lnmc")))
        ref = qa.load_reference(self.bundle.path)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.definition, reference.DEFINITION)
        weights = np.zeros(self.bundle.n_voxels, dtype=np.float32)
        weights[:6] = 1.0
        relocated = qa.relocated_lesions(weights, self.bundle, k=10, seed=0, sparse=True)
        null_voxels = engine.null_voxel_sample(self.bundle.n_voxels, 200, seed=0)
        maps = engine.compute_network_maps(self.bundle, weights[np.newaxis], null_weights=relocated, null_voxels=null_voxels)
        values = qa.lesion_qa(maps.t[0], ref.maps["hub"], maps.null_t, maps.null_voxels, reference=ref)
        result = values["reference"]
        json.dumps(values, allow_nan=False)
        sample_mu = ref.restrict(maps.null_voxels).maps["mu_r"]
        observed = _corr(maps.t[0][maps.null_voxels], sample_mu)
        null_r = np.array([_corr(row, sample_mu) for row in maps.null_t])
        self.assertEqual(result["k"], len(maps.null_t))
        self.assertAlmostEqual(result["percentile"], 100.0 * (np.count_nonzero(null_r < observed) + 0.5 * np.count_nonzero(null_r == observed)) / len(null_r), places=8)
        self.assertIn(result["level"], ("below", "amber", "red"))
        # The analytic z-map of the mean r map
        z = qa.analytic_rl_zmap(maps.avg_r[0], ref)
        self.assertEqual(z.shape, (self.bundle.n_voxels,))
        self.assertTrue(np.all(np.isfinite(z[ref.maps["sigma_r"] > 0])))


if __name__ == "__main__":
    unittest.main()

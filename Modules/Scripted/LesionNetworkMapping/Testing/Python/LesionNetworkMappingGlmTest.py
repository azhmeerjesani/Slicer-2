"""Tests of the GLM permutation engine (glm.py) and TFCE (tfce.py).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingGlmTest.py -v
"""

import collections
import io
import itertools
import json
import math
import os
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
from scipy import ndimage
from scipy import stats as scipy_stats

from LesionNetworkMappingLib import design as designlib
from LesionNetworkMappingLib import glm, jobs, nifti, runs, tfce
from LesionNetworkMappingLib.study import Study

DesignSpec = designlib.DesignSpec


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))


def _brute_statistics(Y, X, C, shufflings, stat="t", groups=None, sign_flip=False):
    """Statistic of every shuffling by a full refit of Y* = P Rz Y + Hz Y (lstsq / explicit formulas)."""
    n, p = X.shape
    _, Z = glm.nuisance_partition(X, C)
    Hz = Z @ np.linalg.pinv(Z) if Z.shape[1] else np.zeros((n, n))
    Rz = np.eye(n) - Hz
    Yr, Yh = Rz @ Y, Hz @ Y
    XtX_inv = np.linalg.inv(X.T @ X)
    out = []
    for shuffling in shufflings:
        Ys = (np.asarray(shuffling)[:, None] * Yr if sign_flip else Yr[np.asarray(shuffling)]) + Yh
        beta = np.linalg.lstsq(X, Ys, rcond=None)[0]
        residual = Ys - X @ beta
        sse = np.sum(residual**2, axis=0)
        psi = C @ beta
        if stat == "t":
            out.append(psi[0] / np.sqrt(sse / (n - p) * (C @ XtX_inv @ C.T)[0, 0]))
        elif stat == "F":
            middle = np.linalg.inv(C @ XtX_inv @ C.T)
            out.append(np.einsum("iv,ij,jv->v", psi, middle, psi) / C.shape[0] / (sse / (n - p)))
        else:  # Aspin-Welch v (Winkler 2014): W_nn = sum of R_nn in the group / the group's residual sum of squares
            leverage = np.diag(X @ np.linalg.pinv(X))
            values = np.empty(Y.shape[1])
            for voxel in range(Y.shape[1]):
                weights = np.empty(n)
                for level in set(groups.tolist()):
                    rows = groups == level
                    weights[rows] = np.sum(1.0 - leverage[rows]) / np.sum(residual[rows, voxel] ** 2)
                covariance = np.linalg.inv(X.T @ (weights[:, None] * X))
                values[voxel] = psi[0, voxel] / np.sqrt((C @ covariance @ C.T)[0, 0])
            out.append(values)
    return np.array(out)


def _brute_p(null, two_sided=True):
    """(uncorrected p, FWE p) from brute-force statistics (row 0 = the identity, which counts as the "1 +")."""
    magnitude = np.abs(null) if two_sided else null
    observed = magnitude[0]
    tolerance = 1e-9 * np.abs(observed) + 1e-12
    count = np.sum(magnitude[1:] >= observed - tolerance, axis=0)
    maxima = magnitude[1:].max(axis=1)
    fwe = np.sum(maxima[:, None] >= (observed - tolerance)[None, :], axis=0)
    total = null.shape[0]
    return (1.0 + count) / total, (1.0 + fwe) / total


def _reference_tfce(volume, mask, H=2.0, E=0.5, steps=100, connectivity=26, two_sided=True):  # noqa: N803
    """Slow TFCE: breadth-first clusters at every height, one voxel at a time."""
    rank = {6: 1, 18: 2, 26: 3}[connectivity]
    offsets = [offset for offset in itertools.product((-1, 0, 1), repeat=3) if any(offset) and sum(map(abs, offset)) <= rank]

    def positive(values):
        out = np.zeros(values.shape)
        top = values[mask].max() if mask.any() else 0.0
        if top <= 0:
            return out
        dh = top / steps
        for k in range(1, steps + 1):
            h = top if k == steps else k * dh
            above = mask & (values >= h)
            seen = np.zeros(values.shape, dtype=bool)
            for start in zip(*np.nonzero(above), strict=True):
                if seen[start]:
                    continue
                cluster, queue = [], collections.deque([start])
                seen[start] = True
                while queue:
                    voxel = queue.popleft()
                    cluster.append(voxel)
                    for offset in offsets:
                        neighbour = tuple(voxel[axis] + offset[axis] for axis in range(3))
                        if all(0 <= neighbour[axis] < values.shape[axis] for axis in range(3)) and above[neighbour] and not seen[neighbour]:
                            seen[neighbour] = True
                            queue.append(neighbour)
                for voxel in cluster:
                    out[voxel] += len(cluster) ** E * h**H * dh
        return out

    result = positive(volume)
    return result - positive(-volume) if two_sided else result


class FitTest(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(7)

    def test_ols_t_equals_lstsq(self):
        n, V = 15, 40
        X = np.column_stack([np.ones(n), self.rng.standard_normal(n), self.rng.standard_normal(n)])
        Y = self.rng.standard_normal((n, V))
        C = np.array([[0.0, 1.0, -0.5]])
        result = glm.fit(Y, X, C)
        beta, residuals, rank, _ = np.linalg.lstsq(X, Y, rcond=None)
        sigma2 = residuals / (n - rank)
        t = (C @ beta)[0] / np.sqrt(sigma2 * (C @ np.linalg.inv(X.T @ X) @ C.T)[0, 0])
        np.testing.assert_allclose(result.stat, t, rtol=1e-10, atol=1e-12)
        np.testing.assert_allclose(result.beta, beta, rtol=1e-10, atol=1e-12)
        self.assertEqual((result.df, result.rank, result.kind, result.df1), (n - 3, 3, "t", 1))
        # Two rows: F = (psi' (C (X'X)^-1 C')^-1 psi / r) / sigma2
        C2 = np.array([[0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        f = glm.fit(Y, X, C2)
        psi = C2 @ beta
        expected = np.einsum("iv,ij,jv->v", psi, np.linalg.inv(C2 @ np.linalg.inv(X.T @ X) @ C2.T), psi) / 2 / sigma2
        np.testing.assert_allclose(f.stat, expected, rtol=1e-10)
        self.assertEqual(f.kind, "F")
        with self.assertRaises(ValueError):
            glm.fit(Y[:3], X[:3], C)  # no residual degrees of freedom

    def test_beckmann_partition(self):
        n = 14
        X = np.column_stack([np.ones(n), self.rng.standard_normal(n), self.rng.integers(0, 2, n).astype(float)])
        C = np.array([[0.0, 0.0, 1.0]])
        effect, Z = glm.nuisance_partition(X, C)
        self.assertEqual(Z.shape, (n, 2))
        # [effect, Z] spans the space of X, and the contrast estimate is the coefficient of the effect column
        both = np.column_stack([effect, Z])
        self.assertEqual(np.linalg.matrix_rank(np.column_stack([both, X])), 3)
        y = self.rng.standard_normal(n)
        self.assertAlmostEqual(float(np.linalg.lstsq(both, y, rcond=None)[0][0]), float((C @ np.linalg.lstsq(X, y, rcond=None)[0])[0]), places=10)


class FreedmanLaneTest(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(1234)
        n = 12
        self.n = n
        self.group = np.array([1.0] * 6 + [0.0] * 6)
        volume = self.rng.uniform(5, 40, n)
        self.X = np.column_stack([np.ones(n), self.group, volume - volume.mean()])
        self.C = np.array([[0.0, 1.0, 0.0]])
        self.Y = self.rng.standard_normal((n, 50)) + 0.9 * self.group[:, None] * (np.arange(50) < 12) + 0.05 * volume[:, None]
        self.perms = list(designlib.permutations(n, None, "free", 200, seed=11))

    def test_p_equals_brute_force_refits(self):
        null = _brute_statistics(self.Y, self.X, self.C, self.perms)
        np.testing.assert_allclose(null[0], glm.fit(self.Y, self.X, self.C).stat, rtol=1e-10)
        p_unc, p_fwe = _brute_p(null)
        for dtype in (np.float64, np.float32):
            with self.subTest(dtype=dtype.__name__):
                result = glm.permutation_test(self.Y, self.X, self.C, iter(self.perms), stat="t", tails=2, dtype=dtype, batch=7)
                self.assertEqual(result.n_perm, 200)
                self.assertTrue(np.array_equal(result.p_unc, p_unc))
                self.assertTrue(np.array_equal(result.p_fwe, p_fwe))
                np.testing.assert_allclose(result.stat, null[0], rtol=1e-10)
                self.assertAlmostEqual(result.min_p, 1 / 200)
                np.testing.assert_allclose(np.sort(result.max_null), np.sort(np.abs(null[1:]).max(axis=1)), rtol=1e-4 if dtype is np.float32 else 1e-10)
        # One-sided (A>B): max t
        p_unc1, p_fwe1 = _brute_p(null, two_sided=False)
        result = glm.permutation_test(self.Y, self.X, self.C, iter(self.perms), stat="t", tails=1, dtype=np.float64)
        self.assertTrue(np.array_equal(result.p_unc, p_unc1))
        self.assertTrue(np.array_equal(result.p_fwe, p_fwe1))

    def test_projection_identity_sse(self):
        # SSE_b = ||Yr||² - β̃ᵀ(XᵀX)β̃ with β̃ = X⁺ P Yr equals the residual sum of squares of a refit
        engine = glm.FreedmanLane(self.X, self.C, "t", dtype=np.float64)
        prepared = engine.prepare(self.Y)
        _, Z = glm.nuisance_partition(self.X, self.C)
        Yr = self.Y - Z @ np.linalg.pinv(Z) @ self.Y
        np.testing.assert_allclose(prepared.Yr, Yr, atol=1e-12)
        for perm in self.perms[1:6]:
            shuffled = Yr[perm]
            beta = np.linalg.pinv(self.X) @ shuffled
            identity = np.sum(Yr**2, axis=0) - np.einsum("pv,pq,qv->v", beta, self.X.T @ self.X, beta)
            refit = np.sum((shuffled - self.X @ beta) ** 2, axis=0)
            np.testing.assert_allclose(identity, refit, rtol=1e-9, atol=1e-12)
        np.testing.assert_allclose(engine.null_statistics(prepared, self.perms[1:6]), _brute_statistics(self.Y, self.X, self.C, self.perms[1:6]), rtol=1e-9, atol=1e-12)

    def test_f_v_and_sign_flips_match_brute_force(self):
        n = self.n
        levels = np.repeat([0, 1, 2], 4)
        covariate = self.rng.standard_normal(n)
        X3 = np.column_stack([(levels == k).astype(float) for k in range(3)] + [covariate - covariate.mean()])
        C3 = np.array([[1.0, 0.0, -1.0, 0.0], [0.0, 1.0, -1.0, 0.0]])
        null = _brute_statistics(self.Y, X3, C3, self.perms, "F")
        p_unc, p_fwe = _brute_p(null, two_sided=False)
        result = glm.permutation_test(self.Y, X3, C3, iter(self.perms), stat="F", tails=2, dtype=np.float64)
        self.assertEqual(result.tails, 1)  # F is one-sided
        self.assertTrue(np.array_equal(result.p_unc, p_unc))
        self.assertTrue(np.array_equal(result.p_fwe, p_fwe))

        Y = self.Y.copy()
        Y[self.group == 1] *= 3.0  # unequal variances
        groups = self.group + 1
        null = _brute_statistics(Y, self.X, self.C, self.perms[:40], "v", groups=groups)
        p_unc, p_fwe = _brute_p(null)
        result = glm.permutation_test(Y, self.X, self.C, iter(self.perms[:40]), stat="v", tails=2, variance_groups=groups, dtype=np.float64)
        np.testing.assert_allclose(result.stat, null[0], rtol=1e-9)
        self.assertTrue(np.array_equal(result.p_unc, p_unc))
        self.assertTrue(np.array_equal(result.p_fwe, p_fwe))
        engine = glm.FreedmanLane(self.X, self.C, "v", groups, dtype=np.float32)
        np.testing.assert_allclose(engine.null_statistics(engine.prepare(Y), self.perms[1:6]), null[1:6], rtol=1e-4, atol=1e-4)

        X1 = np.column_stack([np.ones(n), covariate - covariate.mean()])
        C1 = np.array([[1.0, 0.0]])
        flips = list(designlib.sign_flips(n, 300, seed=5))
        Y1 = self.Y + 0.4
        null = _brute_statistics(Y1, X1, C1, flips, sign_flip=True)
        p_unc, p_fwe = _brute_p(null)
        result = glm.permutation_test(Y1, X1, C1, iter(flips), stat="t", tails=2, exchange="sign_flip", dtype=np.float64)
        self.assertTrue(np.array_equal(result.p_unc, p_unc))
        self.assertTrue(np.array_equal(result.p_fwe, p_fwe))

    def test_v_without_covariates_is_welch(self):
        X = self.X[:, :2]
        C = np.array([[0.0, 1.0]])
        Y = self.Y.copy()
        Y[self.group == 1] *= 4.0
        engine = glm.FreedmanLane(X, C, "v", self.group, dtype=np.float64)
        a, b = Y[self.group == 1], Y[self.group == 0]
        welch = (a.mean(0) - b.mean(0)) / np.sqrt(a.var(0, ddof=1) / len(a) + b.var(0, ddof=1) / len(b))
        np.testing.assert_allclose(engine.prepare(Y).stat, welch, rtol=1e-10)
        with self.assertRaises(ValueError):
            glm.FreedmanLane(X, C, "v", np.arange(self.n) % 3)  # exactly two variance groups

    def test_identity_first_and_constant_voxels(self):
        with self.assertRaises(ValueError):
            glm.permutation_test(self.Y, self.X, self.C, iter(self.perms[1:]))
        Y = self.Y.copy()
        Y[:, 0] = 3.0  # constant: removed by the intercept in the nuisance
        Y[:, 1] = 0.0
        result = glm.permutation_test(Y, self.X, self.C, iter(self.perms[:50]))
        self.assertEqual(result.stat[0], 0.0)
        self.assertEqual(result.p_unc[0], 1.0)
        self.assertEqual(result.p_fwe[1], 1.0)
        self.assertTrue(np.all(np.isfinite(result.stat)))

    def test_resume_state_reproduces_the_run(self):
        full = glm.permutation_test(self.Y, self.X, self.C, iter(self.perms), batch=16)
        tmp = tempfile.mkdtemp(prefix="lnm-glm-state-")
        batches = []

        def on_batch(state, done, total, elapsed):
            batches.append(done)
            if len(batches) == 2:  # checkpoint after two batches, then stop
                state.save(os.path.join(tmp, "state.npz"))

        try:
            with self.assertRaises(jobs.Cancelled):
                glm.permutation_test(self.Y, self.X, self.C, iter(self.perms), batch=16, on_batch=on_batch, cancel=lambda: len(batches) >= 2)
            self.assertEqual(batches, [16, 32])
            state = glm.NullState.load(os.path.join(tmp, "state.npz"))
            self.assertEqual(state.cursor, 33)
            resumed = glm.permutation_test(self.Y, self.X, self.C, iter(self.perms), batch=16, state=state)
            self.assertTrue(np.array_equal(resumed.p_unc, full.p_unc))
            self.assertTrue(np.array_equal(resumed.p_fwe, full.p_fwe))
            self.assertTrue(np.array_equal(resumed.max_null, full.max_null))
            self.assertIsNone(glm.NullState.load(os.path.join(tmp, "missing.npz")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class _StudyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-glm-test-")
        self.rng = np.random.default_rng(99)

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


class DesignRunTest(_StudyTestCase):
    def test_blocks_respected(self):
        rows = []
        for dataset in ("d1", "d2", "d3"):
            for level in ("case", "control"):
                for index in range(5):
                    rows.append((f"sub-{dataset}{level[:2]}{index}", {"group": level, "volume_ml": float(self.rng.uniform(5, 40)), "dataset": dataset}))
        study = self.make_study(rows)
        design = designlib.build_design(study, DesignSpec())
        self.assertEqual((design.design_type, design.permute), ("within_cohort", "within"))
        iterator, count, exhaustive = glm.shufflings(design, n_perm=300, seed=4)
        listed = list(iterator)
        self.assertEqual((len(listed), count, exhaustive), (300, 300, False))
        blocks = design.blocks
        for perm in listed:
            self.assertTrue(np.array_equal(blocks[perm], blocks))  # never across blocks
        Y = self.rng.standard_normal((design.n, 30))
        result = glm.run(design, Y, n_perm=300, seed=4, dtype=np.float64)
        null = _brute_statistics(Y, design.X, design.C, listed)
        p_unc, p_fwe = _brute_p(null)
        self.assertTrue(np.array_equal(result.p_unc, p_unc))
        self.assertTrue(np.array_equal(result.p_fwe, p_fwe))
        self.assertEqual(result.label, "caseVsControl")

    def test_exhaustive_p_is_exact(self):
        rows = [(f"sub-{level[:2]}{index}", {"group": level, "volume_ml": 10.0 + index}) for level in ("case", "control") for index in range(5)]
        study = self.make_study(rows)
        design = designlib.build_design(study, DesignSpec(covariates=[], volume_off_reason="exhaustive test"))
        self.assertTrue(design.exhaustive)
        self.assertEqual(design.unique_perms, 252)  # C(10, 5)
        Y = self.rng.standard_normal((design.n, 8))
        result = glm.run(design, Y, dtype=np.float64)
        self.assertTrue(result.exhaustive)
        self.assertEqual(result.n_perm, 252)
        self.assertAlmostEqual(result.min_p, 1 / 252)
        # Exact permutation distribution: every assignment of 5 of the 10 subjects to the first level
        X = design.X
        stats = []
        for chosen in itertools.combinations(range(design.n), 5):
            group = np.zeros(design.n)
            group[list(chosen)] = 1.0
            relabelled = X.copy()
            relabelled[:, 1] = group
            stats.append(glm.fit(Y, relabelled, design.C).stat)
        stats = np.abs(np.array(stats))
        observed = np.abs(glm.fit(Y, X, design.C).stat)
        exact = np.sum(stats >= observed - 1e-9 * observed - 1e-12, axis=0) / 252.0
        np.testing.assert_allclose(result.p_unc, exact, rtol=0, atol=1e-12)
        self.assertTrue(np.allclose(result.p_unc * 252, np.round(result.p_unc * 252)))

    def test_exhaustive_within_blocks_is_exact(self):
        # Two datasets of 3 cases and 3 controls: C(6, 3)² = 400 relabellings within the datasets
        rows = [(f"sub-{dataset}{level[:2]}{index}", {"group": level, "volume_ml": 10.0, "dataset": dataset}) for dataset in ("d1", "d2") for level in ("case", "control") for index in range(3)]
        study = self.make_study(rows)
        design = designlib.build_design(study, DesignSpec(covariates=[], volume_off_reason="exhaustive test"))
        self.assertEqual((design.permute, design.unique_perms, design.exhaustive), ("within", 400, True))
        Y = self.rng.standard_normal((design.n, 6))
        result = glm.run(design, Y, dtype=np.float64)
        self.assertEqual(result.n_perm, 400)
        rows_of = [np.flatnonzero(design.blocks == code) for code in (1, 2)]
        stats = []
        for first, second in itertools.product(itertools.combinations(rows_of[0], 3), itertools.combinations(rows_of[1], 3)):
            relabelled = design.X.copy()
            relabelled[:, 1] = 0.0
            relabelled[list(first) + list(second), 1] = 1.0
            stats.append(glm.fit(Y, relabelled, design.C).stat)
        stats = np.abs(np.array(stats))
        observed = np.abs(glm.fit(Y, design.X, design.C).stat)
        exact = np.sum(stats >= observed - 1e-9 * observed - 1e-12, axis=0) / 400.0
        np.testing.assert_allclose(result.p_unc, exact, rtol=0, atol=1e-12)

    def test_type_one_error_is_calibrated(self):
        # Null data with a nuisance (volume) effect: the FWE rate over 200 simulations is within the
        # binomial 95% interval of alpha = 0.05
        n, V, simulations, alpha = 20, 15, 200, 0.05
        group = np.repeat([1.0, 0.0], n // 2)
        rejections = 0
        voxel_rejections = 0
        for simulation in range(simulations):
            rng = np.random.default_rng(10_000 + simulation)
            volume = rng.uniform(5, 40, n)
            X = np.column_stack([np.ones(n), group, volume - volume.mean()])
            Y = 0.1 * volume[:, None] + rng.standard_normal((n, V))
            perms = designlib.permutations(n, None, "free", 200, seed=simulation)
            result = glm.permutation_test(Y, X, np.array([[0.0, 1.0, 0.0]]), perms)
            rejections += int(np.any(result.p_fwe <= alpha))
            voxel_rejections += int(np.count_nonzero(result.p_unc <= alpha))
        low, high = scipy_stats.binom.interval(0.95, simulations, alpha)
        self.assertTrue(low <= rejections <= high, f"FWE rejections {rejections} of {simulations} outside [{low}, {high}]")
        low, high = scipy_stats.binom.interval(0.95, simulations * V, alpha)
        self.assertTrue(low <= voxel_rejections <= high, f"voxel rejections {voxel_rejections} outside [{low}, {high}]")

    def test_effect_sizes(self):
        rows = [(f"sub-{level[:2]}{index}", {"group": level, "volume_ml": float(self.rng.uniform(5, 40)), "score": float(self.rng.normal())}) for level in ("case", "control") for index in range(8)]
        study = self.make_study(rows)
        design = designlib.build_design(study, DesignSpec(covariates=[], volume_off_reason="formula test", contrast="A>B", inference={"n_perm": 1000}))
        Y = self.rng.standard_normal((design.n, 6))
        result = glm.run(design, Y, n_perm=101, dtype=np.float64, allow_blocked=True)
        cases = np.array([label == "case" for label in design.group_labels])
        a, b = Y[cases], Y[~cases]
        df = design.n - 2
        pooled = np.sqrt(((len(a) - 1) * a.var(0, ddof=1) + (len(b) - 1) * b.var(0, ddof=1)) / df)
        hedges = (a.mean(0) - b.mean(0)) / pooled * (1 - 3 / (4 * df - 1))
        self.assertEqual(result.effect_kind, "hedges_g")
        np.testing.assert_allclose(result.effect, hedges, rtol=1e-10)
        self.assertEqual(result.label, "caseGtControl")
        # Partial r of a score with a covariate = the correlation of the residualized score and maps
        design = designlib.build_design(study, DesignSpec(test="continuous", score_column="score", inference={"n_perm": 1000}))
        result = glm.run(design, Y, n_perm=101, dtype=np.float64, allow_blocked=True)
        nuisance = design.X[:, [0, 2]]
        residualize = np.eye(design.n) - nuisance @ np.linalg.pinv(nuisance)
        score, maps = residualize @ design.X[:, 1], residualize @ Y
        partial = np.array([np.corrcoef(score, maps[:, voxel])[0, 1] for voxel in range(Y.shape[1])])
        self.assertEqual(result.effect_kind, "partial_r")
        np.testing.assert_allclose(result.effect, partial, rtol=1e-9)
        self.assertEqual(result.label, "score")
        # F: partial eta² = SS_effect / (SS_effect + SS_error)
        result = glm.permutation_test(Y, np.column_stack([cases, ~cases, np.zeros(design.n) + design.X[:, 2]]).astype(float), np.array([[1.0, -1.0, 0.0]]), designlib.permutations(design.n, None, "free", 50, 1), stat="F", dtype=np.float64)
        eta = glm.effect_size("partial_eta2", result)
        np.testing.assert_allclose(eta, result.stat / (result.stat + result.df), rtol=1e-12)

    def test_fdr_and_discreteness(self):
        p = np.array([0.01, 0.04, 0.03, 0.2, 0.5])
        expected = np.array([0.05, 0.2 / 3, 0.2 / 3, 0.25, 0.5])  # step-up: q(0.03) = min(0.075, q(0.04))
        np.testing.assert_allclose(glm.fdr_bh(p), expected)
        self.assertEqual(glm.fdr_floor_voxels(228_483, 1 / 5001), math.ceil(228_483 / 5001 / 0.05))
        self.assertEqual(glm.fdr_floor_voxels(228_483, 1 / 5001), 914)
        np.testing.assert_allclose(glm.fwe_from_null([3.0, 1.0, -5.0], [0.5, 2.0, 4.0, 6.0]), [3 / 5, 4 / 5, 2 / 5])

    def test_contrast_labels(self):
        rows = [(f"sub-{level[:2]}{index}", {"group": level, "volume_ml": 10.0 + index}) for level in ("case", "control") for index in range(6)]
        study = self.make_study(rows)
        for contrast, label in (("A>B", "caseGtControl"), ("B>A", "controlGtCase"), ("two_sided", "caseVsControl"), ("F", "groupAnova")):
            self.assertEqual(glm.contrast_label(designlib.build_design(study, DesignSpec(contrast=contrast))), label)
        self.assertEqual(glm.contrast_label(designlib.build_design(study, DesignSpec(test="one_sample", contrast="positive"))), "oneSamplePos")


class TfceTest(unittest.TestCase):
    def test_equals_slow_reference(self):
        rng = np.random.default_rng(3)
        for connectivity in (6, 18, 26):
            with self.subTest(connectivity=connectivity):
                volume = ndimage.gaussian_filter(rng.standard_normal((9, 10, 8)), 1.0) * 4.0
                mask = rng.random(volume.shape) > 0.2
                fast = tfce.tfce(volume, mask, steps=25, connectivity=connectivity)
                slow = _reference_tfce(volume, mask, steps=25, connectivity=connectivity)
                np.testing.assert_allclose(fast, slow, rtol=0, atol=1e-6)
                one_sided = tfce.tfce(volume, mask, steps=25, connectivity=connectivity, two_sided=False)
                np.testing.assert_allclose(one_sided, _reference_tfce(volume, mask, steps=25, connectivity=connectivity, two_sided=False), atol=1e-6)
        volume = ndimage.gaussian_filter(rng.standard_normal((7, 8, 6)), 1.0) * 3.0
        np.testing.assert_allclose(tfce.tfce(volume, None, H=1.5, E=0.8, steps=40), _reference_tfce(volume, np.ones(volume.shape, bool), H=1.5, E=0.8, steps=40), atol=1e-6)
        self.assertTrue(np.all(tfce.tfce(-np.abs(volume), None, two_sided=False) == 0))

    def test_grid_and_eta(self):
        mask = np.zeros((20, 22, 18), dtype=bool)
        mask[3:15, 4:20, 2:10] = True
        grid = tfce.TfceGrid(mask)
        self.assertEqual(grid.box_shape, (12, 16, 8))
        self.assertEqual(grid.n_voxels, int(mask.sum()))
        vector = np.linspace(-2, 3, grid.n_voxels)
        full = np.zeros(mask.shape)
        full[mask] = vector
        np.testing.assert_allclose(grid.enhance(vector), tfce.tfce(full, mask)[mask])
        self.assertAlmostEqual(grid.max_enhanced(vector), float(np.abs(tfce.tfce(full, mask)).max()))
        # The ETA scales with the box and the permutations; two-sided is twice one-sided
        reference = tfce.tfce_eta((91, 109, 91), 1, two_sided=False)
        self.assertAlmostEqual(reference, 0.8, places=6)
        self.assertAlmostEqual(tfce.tfce_eta((91, 109, 91), 1000), 1600.0, places=3)
        self.assertAlmostEqual(tfce.tfce_eta(grid, 10, two_sided=False), 10 * 12 * 16 * 8 * tfce.SECONDS_PER_BOX_VOXEL)
        self.assertAlmostEqual(tfce.tfce_eta(mask, 10, two_sided=False), tfce.tfce_eta(grid, 10, two_sided=False))
        with self.assertRaises(ValueError):
            tfce.structure(8)

    def test_tfce_fwe_in_permutations(self):
        rng = np.random.default_rng(5)
        mask = np.zeros((8, 9, 7), dtype=bool)
        mask[1:7, 1:8, 1:6] = True
        grid = tfce.TfceGrid(mask)
        n = 16
        group = np.repeat([1.0, 0.0], 8)
        X = np.column_stack([np.ones(n), group])
        signal = np.zeros(mask.shape)
        signal[2:5, 2:5, 2:4] = 1.5
        Y = rng.standard_normal((n, grid.n_voxels)) + group[:, None] * signal[mask][None, :]
        perms = list(designlib.permutations(n, None, "free", 120, 2))
        result = glm.permutation_test(Y, X, np.array([[0.0, 1.0]]), iter(perms), tfce_grid=grid, dtype=np.float64)
        self.assertEqual(result.tfce_max_null.size, 119)
        expected = []
        for perm in perms[1:]:
            stat = glm.fit(Y[perm], X, np.array([[0.0, 1.0]])).stat  # no nuisance but the intercept: P Rz Y + Hz Y fits as Y[P]
            expected.append(grid.max_enhanced(stat))
        np.testing.assert_allclose(result.tfce_max_null, expected, rtol=1e-8)
        np.testing.assert_allclose(result.tfce_p_fwe, glm.fwe_from_null(result.tfce, expected), rtol=0)
        self.assertLess(result.tfce_p_fwe[np.flatnonzero(signal[mask])].min(), 0.05)


class CommandLineTest(_StudyTestCase):
    SHAPE = (7, 8, 6)

    def setUp(self):
        super().setUp()
        self.affine = np.array([[-2.0, 0, 0, 12.0], [0, 2.0, 0, -14.0], [0, 0, 2.0, -8.0], [0, 0, 0, 1]])
        mask = self.rng.random(self.SHAPE) > 0.3
        self.mask_path = self.path("bundle", "mask.nii.gz")
        os.makedirs(os.path.dirname(self.mask_path))
        nifti.write_nifti(self.mask_path, mask.astype(np.uint8), self.affine, sform_code=4, qform_code=4, descrip="LNM connectome mask")
        self.mask = mask
        rows = []
        n = 8
        volumes = 5.0 + 1.7 * self.rng.permutation(2 * n)
        for index in range(n):
            rows.append((f"sub-c{index:02d}", {"group": "case", "volume_ml": volumes[index] if index else "n/a"}))
        for index in range(n):
            rows.append((f"sub-k{index:02d}", {"group": "control", "volume_ml": volumes[n + index]}))
        rows.append(("sub-x00", {"group": "control", "volume_ml": 12.0}))  # no map
        self.study = self.make_study(rows)
        maps_dir = self.path("study", "derivatives", "lnm", "cohortmaps-1")
        os.makedirs(maps_dir)
        lines = ["participant_id\tAvgFz\tT\tvolume_ml\tmanifest_sha256\tsubject_ids_sha256\tstatistic\tsupersample\tprobabilistic\tlibrary_version"]
        blob = np.zeros(self.SHAPE)
        blob[2:5, 2:6, 1:4] = 1.0
        for sid, values in rows[:-1]:
            case = values["group"] == "case"
            fz = 0.3 * self.rng.standard_normal(self.SHAPE) + (0.6 * blob if case else 0.0)
            fz_path = os.path.join(maps_dir, f"{sid}_desc-AvgRFz_funcmap.nii.gz")
            t_path = os.path.join(maps_dir, f"{sid}_desc-T_funcmap.nii.gz")
            nifti.write_nifti(fz_path, fz.astype(np.float32), self.affine)
            nifti.write_nifti(t_path, (10 * fz).astype(np.float32), self.affine)
            volume = values["volume_ml"] if values["volume_ml"] != "n/a" else 33.3
            lines.append(f"{sid}\t{fz_path}\t{os.path.basename(t_path)}\t{volume}\tabc\tdef\tT\t2\tfalse\t1.0")
        self.maps_tsv = os.path.join(maps_dir, "maps.tsv")
        with open(self.maps_tsv, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.cohort_run = runs.register(self.study.path, "cohortmaps", maps_dir, status="done")
        with open(os.path.join(maps_dir, "cohortmaps.json"), "w", encoding="utf-8") as f:
            json.dump({"format": "slicer-lnm-cohortmaps", "run_id": self.cohort_run, "status": "done", "bundle": {"name": "Synth Conn 1"}}, f)
        self.design_path = DesignSpec(contrast="A>B", inference={"n_perm": 1000, "seed": 3}).save(self.path("design.json"))

    def argv(self, out, *extra):
        return ["--maps", self.maps_tsv, "--participants", os.path.join(self.study.path, "participants.tsv"), "--design", self.design_path, "--mask", self.mask_path, "--out", out, "--threads", "1", *extra]

    def run_in_process(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = jobs.run_job(glm.main, argv, job="glm", exit=False, orphan=False, signals=False)
        jobs.reset_cancel()
        return code, [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]

    def test_end_to_end(self):
        out = self.path("glm-out")
        result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.glm", *self.argv(out, "--perm", "300", "--tfce", "--batch", "32")], check=False, capture_output=True, text=True, env=_env(), cwd=self.tmp, timeout=600)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(events[-1]["event"], "done")
        self.assertIn("progress", [event["event"] for event in events])
        names = sorted(name for name in os.listdir(out) if name.endswith(".nii.gz"))
        stem = "group_conn-SynthConn1_desc-caseGtControl"
        self.assertEqual(names, sorted(f"{stem}{stat}_map.nii.gz" for stat in ("Tstat", "PUnc", "PFwe", "PFdr", "TfcePFwe", "HedgesG")))
        mask_header = nifti.read_raw_header(self.mask_path)
        for name in names:
            header = nifti.read_raw_header(os.path.join(out, name))
            for field in nifti.GEOMETRY_FIELDS:
                self.assertTrue(np.array_equal(header[field], mask_header[field]), f"{name}: {field}")
            self.assertTrue(np.array_equal(header["dim"][:4], mask_header["dim"][:4]))
            self.assertEqual(int(header["datatype"]), 16)
        with open(os.path.join(out, "glm.json"), encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["design_type"], "within_cohort")
        self.assertEqual(record["n"], 16)
        self.assertEqual(record["excluded"], {"no map in maps.tsv": ["sub-x00"]})
        self.assertEqual(record["volume_filled_from_maps"], 1)
        self.assertEqual(record["permutation"]["n_perm"], 300)
        self.assertTrue(record["permutation"]["preview"])
        self.assertFalse(record["permutation"]["exhaustive"])
        self.assertEqual(record["seeds"], {"permutation": 3})
        self.assertAlmostEqual(record["min_p"], 1 / 300)
        self.assertEqual(record["fdr"]["floor_voxels"], math.ceil(int(self.mask.sum()) / 300 / 0.05 - 1e-9))
        self.assertIn("note", record["fdr"])
        self.assertEqual(record["tails"], 1)
        self.assertTrue(record["tfce"]["enabled"])
        self.assertGreater(record["tfce"]["eta_s"], 0)
        self.assertEqual(record["effect_size"]["name"], "HedgesG")
        self.assertIn("n_perm_below_minimum", [check["code"] for check in record["checks"]])
        self.assertEqual(record["inputs"]["cohortmaps_run"], self.cohort_run)
        for key in ("limitations", "timings", "outputs", "fwe", "disclaimer"):
            self.assertIn(key, record)
        self.assertTrue(os.path.isfile(os.path.join(out, "design.json")))
        self.assertTrue(os.path.isfile(os.path.join(out, "design_matrix.tsv")))
        self.assertFalse(os.path.exists(os.path.join(out, glm.MAPS_MEMMAP)))
        registry = runs.load(self.study.path)
        glm_runs = [run for run in registry.values() if run["kind"] == "glm"]
        self.assertEqual(len(glm_runs), 1)
        self.assertEqual(glm_runs[0]["parents"], [self.cohort_run])
        self.assertEqual(glm_runs[0]["status"], "done")
        # The case blob is detected and the statistics are consistent with an in-process fit
        tstat, _, _ = nifti.read_nifti(os.path.join(out, f"{stem}Tstat_map.nii.gz"))
        pfwe, _, _ = nifti.read_nifti(os.path.join(out, f"{stem}PFwe_map.nii.gz"))
        self.assertTrue(np.all(np.asarray(pfwe)[~self.mask] == 1.0))
        self.assertGreater(float(np.asarray(tstat)[2:5, 2:6, 1:4][self.mask[2:5, 2:6, 1:4]].mean()), 2.0)

    def test_blocked_design_exits_2(self):
        design_path = DesignSpec(covariates=["volume_ml", "group2"], inference={"n_perm": 1000}).save(self.path("bad.json"))
        argv = self.argv(self.path("blocked"))
        argv[argv.index("--design") + 1] = design_path
        code, events = self.run_in_process(argv)
        self.assertEqual(code, 2)
        self.assertEqual(events[-1]["event"], "error")
        # A collinear covariate blocks with its explanation
        study = Study.open(self.study.path)
        for sid in study.subject_ids:
            study.set(sid, dataset="lit" if sid.startswith("sub-c") else "ctrl")
        study.save()
        design_path = DesignSpec(covariates=["volume_ml", "dataset"], inference={"n_perm": 1000}).save(self.path("collinear.json"))
        argv[argv.index("--design") + 1] = design_path
        code, events = self.run_in_process(argv)
        self.assertEqual(code, 2)
        self.assertEqual(events[-1]["code"], "design_blocked")
        self.assertIn("collinear", events[-1]["message"])
        self.assertIn("collinear_covariate", [check["code"] for check in events[-1]["checks"]])

    def test_resume_after_cancel_reproduces_the_uninterrupted_result(self):
        reference = self.path("reference")
        code, _ = self.run_in_process(self.argv(reference, "--perm", "1000", "--batch", "50"))
        self.assertEqual(code, 0)
        out = self.path("resumed")
        cancel_file = self.path("stop")
        original = jobs.emit
        seen = {"progress": 0}

        def emit(event, **fields):
            if event == "progress" and fields.get("stage") == "permutations":
                seen["progress"] += 1
                if seen["progress"] == 3:
                    open(cancel_file, "w").close()
            return original(event, **fields)

        with mock.patch.object(jobs, "emit", emit):
            code, events = self.run_in_process(self.argv(out, "--perm", "1000", "--batch", "50", "--cancel-file", cancel_file))
        self.assertEqual(code, jobs.EXIT_CANCELLED)
        self.assertEqual(events[-1]["event"], "cancelled")
        self.assertEqual(events[-1]["done"], 150)
        with open(os.path.join(out, "glm.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["status"], "cancelled")
        self.assertTrue(os.path.exists(os.path.join(out, glm.RESUME_ARRAYS)))
        os.remove(cancel_file)
        code, events = self.run_in_process(self.argv(out, "--perm", "1000", "--batch", "50", "--cancel-file", cancel_file, "--resume"))
        self.assertEqual(code, 0)
        stages = [event for event in events if event["event"] == "stage" and event["name"] == "permutations"]
        self.assertEqual(stages[0]["resumed"], 150)
        names = sorted(name for name in os.listdir(reference) if name.endswith(".nii.gz"))
        self.assertEqual(names, sorted(name for name in os.listdir(out) if name.endswith(".nii.gz")))
        for name in names:
            a, _, _ = nifti.read_nifti(os.path.join(reference, name))
            b, _, _ = nifti.read_nifti(os.path.join(out, name))
            self.assertTrue(np.array_equal(a, b), name)
        with open(os.path.join(reference, "glm.json"), encoding="utf-8") as f:
            first = json.load(f)
        with open(os.path.join(out, "glm.json"), encoding="utf-8") as f:
            second = json.load(f)
        for key in ("fwe", "fdr", "min_p", "stat_range", "n"):
            self.assertEqual(first[key], second[key], key)
        self.assertEqual(second["permutation"]["resumed_from"], 150)
        self.assertFalse(os.path.exists(os.path.join(out, glm.RESUME_ARRAYS)))
        statuses = sorted(run["status"] for run in runs.load(self.study.path).values() if run["kind"] == "glm")
        self.assertEqual(statuses, ["cancelled", "done", "done"])


if __name__ == "__main__":
    unittest.main()

"""Tests of the cohort null suite (nulls.py, addendum §3.3 and §3.6) on a synthetic bundle.

The null kinds must keep what they claim (lesion shapes; the pairwise Dice of the cohort shift exactly; strength-matched
targets; a smaller KS distance after annealing); the permutation p, its floor, the BH q, the max-statistic FWE p and the
bias map are compared with brute force; the pooled exact null must equal the overlap of the drawn relocations mapped
one by one with the engine; and every output must carry the caption and say whether it is exact-pooled or approximate.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingNullsTest.py -v
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import cohort, cohortmaps, connectome, engine, fastmap, glm, jobs, nifti, nulls, reference, runs, stats, thresholds

SHAPE = (20, 24, 20)
N_LESIONS = 12
TOP10 = {"kind": "top_pct", "value": 10, "statistic": "Fz", "sign": "both"}


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""), SLICER_LNM_OFFLINE="1")


def _quiet(*args, **kwargs):
    pass


def _sphere_volume(shape, centre, radius):
    ijk = np.indices(shape).reshape(3, -1).T
    return (np.linalg.norm(ijk - np.asarray(centre, dtype=np.float64), axis=1) <= radius).reshape(shape).astype(np.uint8)


def _ijk(relocator, positions):
    return np.stack(np.unravel_index(relocator.indices[positions], relocator.shape), axis=1)


def _read_map(path, bundle):
    data, _affine, _header = nifti.read_nifti(path)
    return np.asarray(data, dtype=np.float64).reshape(-1)[bundle.mask_indices]


class NullsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-nulls-")
        info = connectome.make_synthetic(os.path.join(cls.tmp, "Synthetic.lnmc"), shape=SHAPE, n_subjects=5, runs=(30, 30), seed=3)
        cls.bundle = connectome.ConnectomeBundle.open(info["path"])
        rng = np.random.default_rng(0)
        centre = (np.array(SHAPE) - 1) / 2.0
        lesions = []
        for index in range(N_LESIONS):
            path = os.path.join(cls.tmp, "lesions", f"sub-{index:02d}_lesion.nii.gz")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            nifti.write_nifti(path, _sphere_volume(SHAPE, centre + rng.integers(-4, 5, 3), rng.uniform(1.2, 2.2)), cls.bundle.affine)
            lesions.append((f"sub-{index:02d}", path))
        cls.lesions = lesions
        cls.maps_dir = os.path.join(cls.tmp, "maps")
        cohortmaps.run(lesions, cls.bundle, cls.maps_dir, emit=_quiet, register=False)
        cls.maps_tsv = os.path.join(cls.maps_dir, "maps.tsv")
        reference.build(cls.bundle, n_seeds=40, k=3, power_iters=2, register=False, emit=_quiet)
        cls.reference = reference.load_reference(cls.bundle)
        cls.cohort = nulls.load_cohort(cls.maps_tsv, cls.bundle, emit=_quiet)
        cls.index = fastmap.build_index(cls.bundle, fastmap.build_tiling(cls.bundle, []), emit=_quiet, register=False)

    @classmethod
    def tearDownClass(cls):
        cls.bundle.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def relocator(self, columns=None, min_inside=None):
        return nulls.Relocator(self.cohort.columns if columns is None else columns, self.bundle, min_inside=min_inside)

    def passing_parity(self):
        """A parity report of the index whose gate passes (approximate maps compared with themselves)."""
        approx = fastmap.approximate_maps(self.cohort.columns, self.index)
        report = fastmap.parity(approx, approx, self.reference, fastmap.lesion_strata(self.cohort.columns, self.index), fastmap.lesion_volumes_ml(self.cohort.columns, self.index), self.cohort.ids)
        report["index"] = {"sha256": self.index.sha256}
        return report

    # Null kinds

    def assert_shape_preserved(self, relocator, lesion, column, shift):
        positions, weights = column
        original = {tuple(v): w for v, w in zip(relocator.ijk[lesion], relocator.columns[lesion][1], strict=True)}
        moved_back = _ijk(relocator, positions) - np.asarray(shift)
        for voxel, weight in zip(map(tuple, moved_back), weights, strict=True):
            self.assertIn(voxel, original)
            self.assertEqual(original[voxel], weight)
        self.assertTrue(np.all(np.diff(positions) > 0), "sorted, unique positions")
        self.assertGreaterEqual(weights.sum(), relocator.min_inside * relocator.total[lesion] * (1 - 1e-9))

    def test_relocations_preserve_shape(self):
        relocator = self.relocator()
        columns, shifts = nulls.relocate_independent(relocator, 6, seed=1)
        for lesion in range(relocator.n_lesions):
            self.assertEqual(len(columns[lesion]), 6)
            self.assertTrue(np.all(np.any(shifts[lesion] != 0, axis=1)))
            for column, shift in zip(columns[lesion], shifts[lesion], strict=True):
                self.assert_shape_preserved(relocator, lesion, column, shift)
        # Independent seeds per lesion: two identical lesions are not moved to the same places
        twins = nulls.Relocator([self.cohort.columns[0], self.cohort.columns[0]], self.bundle)
        _, twin_shifts = nulls.relocate_independent(twins, 6, seed=1)
        self.assertFalse(np.array_equal(twin_shifts[0], twin_shifts[1]))
        strength_columns, strength_shifts, _ = nulls.relocate_strength(relocator, self.reference.maps["mu_r"], 4, seed=2)
        shared, _info = nulls.cohort_shifts(relocator, 4, seed=3)
        placed = nulls.anneal_overlap(relocator, iterations=100, seed=4)
        for lesion in range(relocator.n_lesions):
            for column, shift in zip(strength_columns[lesion], strength_shifts[lesion], strict=True):
                self.assert_shape_preserved(relocator, lesion, column, shift)
            self.assert_shape_preserved(relocator, lesion, placed["columns"][lesion], placed["shifts"][lesion])
            for shift in shared:
                column = relocator.column(lesion, shift)
                moved_back = _ijk(relocator, column[0]) - shift
                self.assertTrue(set(map(tuple, moved_back)) <= set(map(tuple, relocator.ijk[lesion])))

    def test_cohort_shift_preserves_pairwise_dice_exactly(self):
        relocator = self.relocator(min_inside=1.0)
        shifts, info = nulls.cohort_shifts(relocator, 10, seed=5)
        self.assertGreater(len(shifts), 0)
        self.assertTrue(all(value >= 1.0 - 1e-12 for value in info["inside"]))
        observed = nulls.pairwise_dice(relocator.columns, relocator.n_voxels)
        self.assertGreater(np.count_nonzero(observed), 0, "the fixture has overlapping lesions")
        for shift in shifts:
            self.assertTrue(np.any(shift != 0))
            moved = nulls.apply_shift(relocator, shift)
            for lesion, column in enumerate(moved):
                self.assertEqual(column[0].size, relocator.columns[lesion][0].size)
            np.testing.assert_array_equal(nulls.pairwise_dice(moved, relocator.n_voxels), observed)
        # With the default criterion (95% of the cohort's weight inside) the shapes are kept on the grid
        relaxed = self.relocator()
        relaxed_shifts, relaxed_info = nulls.cohort_shifts(relaxed, 10, seed=5)
        self.assertTrue(all(value >= 0.95 - 1e-12 for value in relaxed_info["inside"]))
        self.assertEqual(len({tuple(s) for s in relaxed_shifts}), len(relaxed_shifts), "distinct shifts")

    def test_strength_targets_lie_in_candidate_set(self):
        relocator = self.relocator()
        for mode, key in nulls.STRENGTH_MAPS.items():
            strength = np.asarray(self.reference.maps[key], dtype=np.float64)
            columns, shifts, candidates = nulls.relocate_strength(relocator, strength, 5, seed=6, n_candidates=30)
            values = nulls.lesion_strength(relocator, strength)
            for lesion in range(relocator.n_lesions):
                positions, weights = relocator.columns[lesion]
                self.assertAlmostEqual(values[lesion], float((strength[positions] * weights).sum() / weights.sum()), places=9)
                candidate_ijk = {tuple(v) for v in _ijk(relocator, candidates[lesion])}
                self.assertEqual(len(candidates[lesion]), 30)
                distance = np.abs(strength - values[lesion])
                outside = np.setdiff1d(np.arange(relocator.n_voxels), candidates[lesion])
                self.assertLessEqual(distance[candidates[lesion]].max(), distance[outside].min(), f"{mode}: the candidates are the nearest in strength")
                self.assertGreater(len(shifts[lesion]), 0)
                for shift in shifts[lesion]:
                    self.assertIn(tuple(relocator.centroid[lesion] + shift), candidate_ijk)
                self.assertEqual(len(columns[lesion]), len(shifts[lesion]))

    def test_annealing_reduces_ks_distance(self):
        # A clustered cohort: overlapping lesions, which independent relocation separates
        centre = (np.array(SHAPE) - 1) / 2.0
        columns = []
        for offset in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0), (0, 0, 1), (-1, 0, 0)):
            volume = _sphere_volume(SHAPE, centre + offset, 2.0)
            columns.append(engine.lesion_weights(volume, self.bundle.mask_indices))
        relocator = self.relocator(columns)
        observed = nulls.pairwise_dice(relocator.columns, relocator.n_voxels)
        self.assertGreater(observed.min(), 0.2)
        result = nulls.anneal_overlap(relocator, iterations=nulls.ANNEAL_ITERATIONS, seed=7)
        self.assertGreater(result["ks_initial"], 0.5)
        self.assertLess(result["ks_best"], result["ks_initial"])
        self.assertLess(result["ks_best"], 0.5)
        self.assertGreater(result["accepted"], 0)
        # The returned placement has the reported KS distance and valid shifts
        placed = nulls.pairwise_dice(result["columns"], relocator.n_voxels)
        self.assertAlmostEqual(nulls.ks_distance(nulls.dice_histogram(placed), nulls.dice_histogram(observed)), result["ks_best"], places=12)
        for lesion, shift in enumerate(result["shifts"]):
            self.assertTrue(relocator.valid(lesion, shift[np.newaxis])[0])
        # Histogram bins as numpy.histogram over the lesionset edges
        values = np.array([0.0, 0.05, 0.049, 0.5, 0.99, 1.0])
        np.testing.assert_array_equal(nulls.dice_histogram(values), np.histogram(values, bins=np.asarray(nulls.DICE_EDGES))[0])

    def test_dice_tracker_matches_brute_force(self):
        relocator = self.relocator()
        observed = nulls.pairwise_dice(relocator.columns, relocator.n_voxels)
        sets = [set(p.tolist()) for p in (column[0][column[1] >= 0.5] for column in relocator.columns)]
        brute = [2 * len(a & b) / (len(a) + len(b)) for i, a in enumerate(sets) for b in sets[i + 1:]]
        np.testing.assert_allclose(observed, brute, rtol=0, atol=1e-15)
        # More than 64 lesions use several bit words
        many = [relocator.columns[i % len(relocator.columns)] for i in range(70)]
        dice = nulls.pairwise_dice(many, relocator.n_voxels)
        self.assertEqual(dice.size, 70 * 69 // 2)
        self.assertEqual(dice[N_LESIONS - 1], 1.0)  # the pair (0, 12) in row-major condensed order: lesion 0 and its copy

    # Statistics

    def test_p_formula_and_floor(self):
        rng = np.random.default_rng(8)
        n_lesions, k, n_voxels = 10, 39, 50
        observed = rng.integers(0, n_lesions + 1, size=(2, n_voxels))
        observed[1] = np.minimum(observed[1], n_lesions - observed[0])
        observed[:, 0] = (n_lesions, 0)  # beyond every null: p at the floor
        nulls_counts = []
        for _ in range(k):
            pos = rng.integers(0, 6, size=n_voxels)
            neg = np.minimum(rng.integers(0, 3, size=n_voxels), n_lesions - pos)
            pos[0] = 3
            nulls_counts.append(np.vstack((pos, neg)))
        accumulator = nulls.NullOverlap(observed, n_lesions, 90, "both", k)
        for counts in nulls_counts:
            accumulator.add(counts)
        result = accumulator.finish()
        maps, numbers = result["maps"], result["numbers"]
        totals = np.array([c.sum(axis=0) for c in nulls_counts], dtype=np.float64)
        obs = observed.sum(axis=0).astype(np.float64)
        p = (1 + (totals >= obs).sum(axis=0)) / (k + 1)
        np.testing.assert_allclose(maps["sensNullP"], p, rtol=1e-6)
        self.assertAlmostEqual(float(maps["sensNullP"][0]), 1 / (k + 1), places=7)
        self.assertAlmostEqual(numbers["min_attainable_p"], 1 / (k + 1))
        self.assertGreaterEqual(float(maps["sensNullP"].min()), 1 / (k + 1) - 1e-9)
        np.testing.assert_allclose(maps["sensNullQ"], glm.fdr_bh(p), rtol=1e-6)
        mean, sd = totals.mean(axis=0), totals.std(axis=0, ddof=1)
        z_obs = (obs - mean) / np.maximum(sd, nulls.SD_FLOOR)
        max_null = ((totals - mean) / np.maximum(sd, nulls.SD_FLOOR)).max(axis=1)
        p_fwe = (1 + (max_null[:, np.newaxis] >= z_obs[np.newaxis]).sum(axis=0)) / (k + 1)
        np.testing.assert_allclose(maps["sensNullPFwe"], p_fwe, rtol=1e-6)
        np.testing.assert_allclose(maps["sensNullExcess"], 100 * (obs - mean) / n_lesions, rtol=1e-5, atol=1e-5)
        classic = [(100 * c[0] / n_lesions >= 90) ^ (100 * c[1] / n_lesions >= 90) for c in nulls_counts]
        np.testing.assert_allclose(maps["sensNullBias"], np.mean(classic, axis=0), rtol=1e-6)
        self.assertEqual(numbers["fdr_floor_voxels"], int(np.ceil(n_voxels / (k + 1) / 0.05 - 1e-9)))
        observed_classic = (100 * observed[0] / n_lesions >= 90) ^ (100 * observed[1] / n_lesions >= 90)
        self.assertAlmostEqual(numbers["classic_rule"]["observed_fraction"], observed_classic.mean())
        self.assertIn("at the rule:", numbers["classic_rule"]["text"])
        self.assertIn("random sets:", numbers["classic_rule"]["text"])
        self.assertEqual(nulls.permutation_p(5, 0, 999), 1 / 1000)
        with self.assertRaises(ValueError):
            accumulator.add(nulls_counts[0])

    def test_fpr_text(self):
        text = nulls.fpr_text(90, 0.31, np.linspace(0.22, 0.35, 101))
        dash = chr(0x2013)  # en dash
        self.assertTrue(text.startswith(f"≥90% at the rule: 31% of brain voxels pass; random sets: 28% (5{dash}95%: 23%{dash}34%)"), text)

    def test_rule_values_and_binary_maps_match_cohort(self):
        rows = np.asarray(nulls._load_rows(self.cohort.maps["T"], self.bundle, "T"), dtype=np.float32)
        for rule in (TOP10 | {"statistic": "T"}, {"kind": "custom_t", "value": 2.0}, {"kind": "top_pct", "value": 5, "statistic": "T", "sign": "pos"}):
            values = nulls.rule_values(rows, rule, 5, self.bundle.n_voxels)
            resolved = cohort.resolve_rules(rows, rule, 5, self.bundle.n_voxels)
            np.testing.assert_array_equal(values, [item.value for item in resolved])
            for sign in thresholds.SIGNS:
                parts = nulls.binarize_parts(rows, values, sign)
                result = cohort.group_maps(rows, rule, sign=sign, n=5, legacy=())
                np.testing.assert_array_equal(parts.sum(axis=(0, 1)), result.maps["overlapCount"])
                for row in range(rows.shape[0]):
                    np.testing.assert_array_equal(parts[row].any(axis=0), stats.binarize(rows[row], values[row], sign))
                classic = nulls.classic_pass(parts.sum(axis=0), rows.shape[0], 50, sign)
                np.testing.assert_array_equal(classic, cohort.group_maps(rows, rule, sign=sign, n=5, legacy=(), sensitivity_pct=50).maps["sens50"] != 0)
        with self.assertRaises(ValueError):
            nulls.resolve_rule({"kind": "t_convention", "value": 7}, 5, self.bundle.n_voxels)

    # The run

    def test_pooled_exact_equals_brute_force(self):
        maps_tsv = self.path("tiny", "maps.tsv")
        if not os.path.exists(maps_tsv):
            cohortmaps.run(self.lesions[:3], self.bundle, self.path("tiny"), cache_root=self.path("maps", "cache"), emit=_quiet, register=False)
        out = self.path("tiny-nulls")
        record = nulls.run(self.bundle, out, maps_tsv=maps_tsv, kind="rl_independent", mode="exact", k=25, k_pool=3, rule=TOP10, overlap=60, seed=9, emit=_quiet, register=False)
        self.assertEqual(record["mode"], "exact_pooled")
        self.assertEqual(record["label"], "pooled exact null (3 per lesion)")
        self.assertLess(record["design"]["observed_check"]["max_relative_difference"], 1e-5)
        design = np.load(os.path.join(out, nulls.SETS_NAME))
        sets, shifts, counts = design["sets"], design["shifts"].astype(np.int64), design["shift_counts"]
        tiny = nulls.load_cohort(maps_tsv, self.bundle, emit=_quiet)
        relocator = nulls.Relocator(tiny.columns, self.bundle)
        offsets = np.concatenate(([0], np.cumsum(counts)))[:-1]
        # Brute force: every relocation mapped on its own as a lesion, thresholded, and the drawn ones summed
        relocated = [relocator.column(lesion, shifts[offsets[lesion] + j]) for lesion in range(len(tiny)) for j in range(counts[lesion])]
        weights = np.vstack([nulls._dense(column, self.bundle.n_voxels) for column in relocated])
        fz = engine.compute_network_maps(self.bundle, weights, subjects=tiny.subject_ids).avg_fz
        cuts = [nulls.rule_values(row, TOP10, 5, self.bundle.n_voxels)[0] for row in fz]
        positive = [row >= cut for row, cut in zip(fz, cuts, strict=True)]
        negative = [row <= -cut for row, cut in zip(fz, cuts, strict=True)]
        pos = np.array([sum(positive[column] for column in row) for row in sets], dtype=np.float64)
        neg = np.array([sum(negative[column] for column in row) for row in sets], dtype=np.float64)
        overlaps = pos + neg
        observed_rows = nulls._load_rows(tiny.maps["AvgFz"], self.bundle, "AvgFz")
        observed = cohort.group_maps(observed_rows, TOP10, n=5, legacy=()).maps["overlapCount"].astype(np.float64)
        p = (1 + (overlaps >= observed).sum(axis=0)) / (len(sets) + 1)
        label = engine.connectome_label(self.bundle.name)
        np.testing.assert_allclose(_read_map(os.path.join(out, f"group_conn-{label}_desc-sensNullP_map.nii.gz"), self.bundle), p, rtol=1e-6)
        np.testing.assert_allclose(_read_map(os.path.join(out, f"group_conn-{label}_desc-sensNullExcess_map.nii.gz"), self.bundle), 100 * (observed - overlaps.mean(axis=0)) / 3, atol=1e-4)
        bias = np.mean((100 * pos / 3 >= 60) ^ (100 * neg / 3 >= 60), axis=0)  # the classic rule is sign-specific
        np.testing.assert_allclose(_read_map(os.path.join(out, f"group_conn-{label}_desc-sensNullBias_map.nii.gz"), self.bundle), bias, atol=1e-6)
        # The draws: one relocation per lesion, uniformly from its own pool
        for lesion in range(len(tiny)):
            self.assertTrue(np.all((sets[:, lesion] >= offsets[lesion]) & (sets[:, lesion] < offsets[lesion] + counts[lesion])))
        # The same pool through the API: the same relocations, and every null cohort's overlap equals the brute force
        pool = nulls.build_relocation_pool(tiny.columns, self.bundle, tiny.subject_ids, k_pool=3, rule=TOP10, seed=9, observed=observed_rows)
        self.assertEqual(pool.counts, counts.tolist())
        self.assertLess(max(pool.check), 1e-5)
        for mine, theirs in zip((column for lesion_columns in pool.columns for column in lesion_columns), relocated, strict=True):
            np.testing.assert_array_equal(mine[0], theirs[0])
            np.testing.assert_array_equal(mine[1], theirs[1])
        for row, expected in zip(sets, overlaps, strict=True):
            np.testing.assert_array_equal(pool.overlap(row).sum(axis=0), expected)
        np.testing.assert_array_equal(pool.draw(25, seed=9), sets)

    def test_exact_run_outputs_and_resume(self):
        out = self.path("exact")
        events = []
        record = nulls.run(self.bundle, out, maps_tsv=self.maps_tsv, kind="rl_independent", mode="exact", k=60, k_pool=4, rule=TOP10, emit=lambda e, **f: events.append((e, f)),
                           run_root=self.tmp, budget_bytes=1)
        self.assertEqual(events[0][0], "start")
        self.assertEqual(events[-1][0], "done")
        self.assertEqual([f["name"] for e, f in events if e == "stage"], ["design", "null maps", "null cohorts", "inference", "write"])
        self.assertEqual(record["k"], 60)
        self.assertAlmostEqual(record["min_attainable_p"], 1 / 61)
        self.assertEqual(record["group_qa"]["overlap"]["k"], 60)
        self.assertIn(record["group_qa"]["overlap"]["level"], ("below", "amber", "red", "not_calibrated"))
        maps, voxels, statistic = nulls.load_null_group_maps(out)
        self.assertEqual(maps.shape, (60, voxels.size))
        self.assertEqual(statistic, "Fz")
        registered = runs.load(self.tmp)[record["run_id"]]
        self.assertEqual(registered["kind"], "nulls")
        self.assertEqual(registered["status"], "done")
        self.assertFalse(os.path.exists(os.path.join(out, nulls.WORK_DIR)))
        reference_maps = {desc: _read_map(path, self.bundle) for desc, path in ((d, os.path.join(out, name)) for d, name in record["outputs"].items() if name.endswith(".nii.gz"))}

        # Stopped after the first engine batch (budget 1 byte: one lesion per pass), then resumed: same outputs
        resumed = self.path("resumed")

        class StopAfterFirstBatch:
            def __init__(self):
                self.batches = 0

            def __call__(self):
                return self.batches >= 1

        stop = StopAfterFirstBatch()

        def on_event(event, **fields):
            if event == "progress" and fields.get("stage") == "null maps" and fields.get("done") == fields.get("total") and fields.get("batch"):
                stop.batches += 1

        with self.assertRaises(jobs.Cancelled):
            nulls.run(self.bundle, resumed, maps_tsv=self.maps_tsv, kind="rl_independent", mode="exact", k=60, k_pool=4, rule=TOP10, emit=on_event, cancel=stop, register=False, budget_bytes=1)
        self.assertTrue(os.path.isfile(os.path.join(resumed, nulls.WORK_DIR, "batch-0.npz")))
        again = []
        nulls.run(self.bundle, resumed, maps_tsv=self.maps_tsv, kind="rl_independent", mode="exact", k=60, k_pool=4, rule=TOP10, emit=lambda e, **f: again.append((e, f)),
                  register=False, budget_bytes=1, resume=True)
        engine_batches = [f for e, f in again if e == "progress" and f.get("stage") == "null maps" and f.get("batch") and f.get("total") == 5]  # per-subject progress
        self.assertNotIn(1, [f["batch"] for f in engine_batches], "batch 1 was loaded, not recomputed")
        for desc, values in reference_maps.items():
            np.testing.assert_array_equal(_read_map(os.path.join(resumed, os.path.basename(record["outputs"][desc])), self.bundle), values, err_msg=desc)

    def test_kinds_and_modes(self):
        parity = self.passing_parity()
        for kind, mode, k in (("rl_strength", "exact", 30), ("rl_cohort_shift", "exact", 30), ("rl_independent", "fast", 40), ("rl_cohort_shift", "fast", 20), ("rl_overlap", "fast", 5)):
            record = nulls.run(self.bundle, self.path(f"{kind}-{mode}"), maps_tsv=self.maps_tsv, kind=kind, mode=mode, k=k, k_pool=4 if mode == "exact" else None, rule=TOP10,
                               parity=parity, iterations=150, emit=_quiet, register=False)
            self.assertEqual(record["mode"], nulls.MODE_RECORDS[mode])
            self.assertEqual(record["approximate"], mode == "fast")
            self.assertEqual(record["kind_status"], nulls.KIND_INFO[kind]["status"])
            if kind == "rl_cohort_shift" and mode == "exact":
                self.assertEqual(record["k"], 4)
                self.assertEqual(record["label"], "pooled exact null (4 cohort shifts)")
            else:
                self.assertEqual(record["k"], k)
            if mode == "fast":
                self.assertEqual(record["parity"]["allowed_strata"], ["white-matter-supervoxel"])
                self.assertEqual(record["statistic"], "approximate Fz")
                self.assertIn("never report them as final observed maps", record["note"])
        self.assertEqual(record["design"]["iterations"], 150)
        self.assertLessEqual(record["design"]["ks_best_median"], record["design"]["ks_initial_median"])

    def test_empirical_pool(self):
        pool_dir = self.path("pool")
        os.makedirs(pool_dir, exist_ok=True)
        rng = np.random.default_rng(11)
        centre = (np.array(SHAPE) - 1) / 2.0
        for index in range(8):
            nifti.write_nifti(os.path.join(pool_dir, f"sub-c{index:02d}_lesion.nii.gz"), _sphere_volume(SHAPE, centre + rng.integers(-5, 6, 3), rng.uniform(1.0, 2.5)), self.bundle.affine)
        shutil.copy(self.lesions[0][1], os.path.join(pool_dir, "sub-dup_lesion.nii.gz"))  # a cohort lesion in the pool is left out
        events = []
        record = nulls.run(self.bundle, self.path("pool-exact"), maps_tsv=self.maps_tsv, kind="empirical_pool", mode="exact", k=50, pool=pool_dir, pool_matches=3, rule=TOP10,
                           emit=lambda e, **f: events.append((e, f)), register=False, cache=self.path("maps", "cache"))
        self.assertEqual(record["design"]["pool"], 8)
        self.assertEqual(record["label"], "pooled exact null (8 control lesions, 3 volume-matched per lesion)")
        self.assertTrue(any(e == "warning" and f.get("code") == "pool" for e, f in events))
        cohort_volumes, pool_volumes = [1.0, 2.0, 8.0], [0.5, 1.1, 1.9, 2.2, 7.0, 9.0, 30.0]
        sets, candidates = nulls.volume_matched_sets(cohort_volumes, pool_volumes, 40, n_match=2, seed=3)
        for lesion, volume in enumerate(cohort_volumes):
            distance = [abs(np.log(v) - np.log(volume)) for v in pool_volumes]
            self.assertEqual(sorted(candidates[lesion].tolist()), sorted(np.argsort(distance, kind="stable")[:2].tolist()))
        for row in sets:
            for lesion, choice in enumerate(row):
                self.assertIn(choice, candidates[lesion])
            self.assertEqual(len(set(row.tolist())), 3, "no control lesion twice in a null cohort")

    def test_study_source_uses_the_cases(self):
        from LesionNetworkMappingLib.study import Study

        study = Study.create(self.path("study"), space="MNI152NLin6Asym")
        for index, (sid, lesion) in enumerate(self.lesions[:6]):
            target = self.path("study", "lesions", f"{sid}_space-MNI152NLin6Asym_desc-lesion_mask.nii.gz")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(lesion, target)
            study.add_subject(sid, {"lesion": study.relpath(target), "group": "case" if index < 4 else "control"})
        study.save()
        out = self.path("study", "derivatives", "lnm", "nulls")
        record = nulls.run(self.bundle, out, study=self.path("study"), k=20, k_pool=2, rule=TOP10, emit=_quiet, cache=self.path("maps", "cache"))
        self.assertEqual(record["participants"], [sid for sid, _ in self.lesions[:4]])
        self.assertEqual(sorted(item["participant_id"] for item in record["excluded"]), [sid for sid, _ in self.lesions[4:6]])
        registered = runs.load(self.path("study"))
        own = registered[record["run_id"]]
        self.assertEqual(own["kind"], "nulls")
        self.assertTrue(any(registered[parent]["kind"] == "cohortmaps" for parent in own["parents"]))
        self.assertEqual(record["inputs"]["maps_tsv"], "maps.tsv")

    def test_mode_rules(self):
        with self.assertRaises(ValueError):
            nulls.run(self.bundle, self.path("bad"), maps_tsv=self.maps_tsv, mode="fast", rule={"kind": "fz_abs", "value": 0.05}, emit=_quiet, register=False)
        with self.assertRaises(ValueError):
            nulls.run(self.bundle, self.path("bad"), maps_tsv=self.maps_tsv, mode="fast", rule={"kind": "top_pct", "value": 10, "statistic": "T"}, emit=_quiet, register=False)
        with self.assertRaises(ValueError):
            nulls.run(self.bundle, self.path("bad"), maps_tsv=self.maps_tsv, kind="rl_overlap", mode="exact", emit=_quiet, register=False)
        with self.assertRaises(ValueError):
            nulls.run(self.bundle, self.path("bad"), maps_tsv=self.maps_tsv, kind="empirical_pool", emit=_quiet, register=False)
        failing = self.passing_parity()
        failing["gate"] = fastmap.gate({**failing, "lesions": [dict(item, partial_r=0.1) for item in failing["lesions"]]})
        with self.assertRaises(jobs.JobError) as context:
            nulls.run(self.bundle, self.path("bad"), maps_tsv=self.maps_tsv, mode="fast", rule=TOP10, parity=failing, emit=_quiet, register=False)
        self.assertEqual(context.exception.code, "parity_gate")

    def test_estimate_and_fast_index(self):
        small = nulls.estimate(10, self.bundle, 20, "exact")
        self.assertGreater(small, 0)
        self.assertGreater(nulls.estimate(40, self.bundle, 20, "exact"), small)
        self.assertGreater(nulls.estimate(10, self.bundle, 40, "exact"), small)
        details = nulls.estimate_details(10, self.bundle, 20, "exact", budget_bytes=1)
        self.assertEqual(details["passes"], 10)
        self.assertEqual(nulls.estimate_details(10, self.bundle, 20, "fast")["engine_s"], 0.0)
        self.assertEqual(nulls.find_fast_index(self.bundle), self.index.path)
        self.assertIsNone(nulls.find_fast_index(self.bundle, self.bundle.subject_ids[:3]))
        self.assertEqual(nulls.find_fast_index(self.bundle, verify=True), self.index.path)

    def test_caption_in_every_output_sidecar(self):
        parity_path = self.path("parity.json")
        with open(parity_path, "w", encoding="utf-8") as f:
            json.dump(self.passing_parity(), f)
        command = [sys.executable, "-m", "LesionNetworkMappingLib.nulls", "--maps", self.maps_tsv, "--bundle", self.bundle.path, "--k", "30"]
        for mode, extra in (("exact", ["--k-pool", "3"]), ("fast", ["--parity", parity_path])):
            out = self.path(f"cli-{mode}")
            result = subprocess.run([*command, "--out", out, "--mode", mode, "--rule", json.dumps(TOP10), *extra], check=False, capture_output=True, text=True, env=_env(), cwd=MODULE_DIR, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(events[0]["event"], "start")
            self.assertEqual(events[-1]["event"], "done")
            sidecars = glob.glob(os.path.join(out, "*_map.json"))
            self.assertEqual(len(sidecars), len(nulls.OUTPUT_DESCS))
            for path in [*sidecars, os.path.join(out, nulls.NULLS_JSON)]:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                record = json.loads(text)
                self.assertEqual(record["caption"], nulls.CAPTION, path)
                self.assertEqual(record["mode"], nulls.MODE_RECORDS[mode])
                self.assertIn("approximate" if mode == "fast" else "pooled exact null", record["mode_label"])
                self.assertAlmostEqual(record["min_attainable_p"], 1 / 31)
                self.assertAlmostEqual(record["min_attainable_q"], 1 / 31)
                self.assertNotIn(self.tmp, text, f"{os.path.basename(path)} holds an absolute local path")
                if mode == "fast":
                    self.assertEqual(record["parity"]["allowed_strata"], ["white-matter-supervoxel"])
            for name in os.listdir(out):
                if name.endswith(".nii.gz"):
                    self.assertTrue(os.path.isfile(os.path.join(out, name[: -len(".nii.gz")] + ".json")), name)
        # Usage errors and the estimate
        bad = subprocess.run([*command, "--out", self.path("cli-bad"), "--kind", "rl_overlap"], check=False, capture_output=True, text=True, env=_env(), cwd=MODULE_DIR, timeout=120)
        self.assertEqual(bad.returncode, jobs.EXIT_USAGE)
        estimate = subprocess.run([*command, "--out", self.path("cli-estimate"), "--estimate"], check=False, capture_output=True, text=True, env=_env(), cwd=MODULE_DIR, timeout=120)
        self.assertEqual(estimate.returncode, 0, estimate.stdout + estimate.stderr)
        result_event = json.loads(estimate.stdout.splitlines()[0])
        self.assertEqual(result_event["kind"], "estimate")
        self.assertGreater(result_event["seconds"], 0)


if __name__ == "__main__":
    unittest.main()

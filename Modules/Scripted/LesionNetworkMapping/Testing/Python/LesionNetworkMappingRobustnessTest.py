"""Tests of the robustness analyses (robustness.py, L-27): replication on another connectome with per-connectome rule
resolution, lesion split-half, leave-one-lesion-out downdates, seed variants, hemisphere consolidation and labels.

Two synthetic connectome bundles (20 and 30 subjects, same grid) and a small study whose lesions lie on a 1 mm
grid: three near blob A (left hemisphere), three near blob A2 (right hemisphere); two lesions also have a
single-slice ``desc-traced`` tracing and one a fractional ``desc-lesionProb`` file.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingRobustnessTest.py -v
"""

import hashlib
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

from LesionNetworkMappingLib import cohortmaps, connectome, glm, lesionops, nifti, robustness, runs, thresholds
from LesionNetworkMappingLib.study import Study, read_tsv

SPACE = "MNI152NLin6Asym"
SHAPE = (20, 24, 20)
FWE = {"kind": "fwe_bonferroni", "alpha": 0.05}
TOP10 = {"kind": "top_pct", "value": 10, "statistic": "T", "sign": "both"}


def _quiet(*_args, **_fields):
    pass


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))


def _fine_grid(grid):
    shape = tuple(2 * int(n) for n in grid.shape)
    affine = np.eye(4)
    affine[:3, 3] = grid.affine[:3, 3] - 0.5
    return shape, affine


def _sphere(shape, affine, centre, radius):
    world = np.indices(shape).reshape(3, -1).T @ affine[:3, :3].T + affine[:3, 3]
    return (np.linalg.norm(world - np.asarray(centre, dtype=np.float64), axis=1) <= radius).reshape(shape).astype(np.uint8)


def _tree_state(folder):
    """{relative path: (sha256, mtime_ns)} of every file under ``folder``."""
    state = {}
    for root, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(root, name)
            with open(path, "rb") as f:
                state[os.path.relpath(path, folder)] = (hashlib.sha256(f.read()).hexdigest(), os.stat(path).st_mtime_ns)
    return state


class RobustnessTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.mkdtemp(prefix="lnm-robustness-data-")
        cls.meta_a = connectome.make_synthetic(os.path.join(cls.data, "Synthetic A.lnmc"), shape=SHAPE, n_subjects=20, runs=(30, 30), seed=1, name="SynA")
        cls.meta_b = connectome.make_synthetic(os.path.join(cls.data, "Synthetic B.lnmc"), shape=SHAPE, n_subjects=30, runs=(30, 30), seed=2, name="SynB")
        cls.bundle_a = connectome.ConnectomeBundle.open(cls.meta_a["path"])
        cls.bundle_b = connectome.ConnectomeBundle.open(cls.meta_b["path"])
        cls.fine_shape, cls.fine_affine = _fine_grid(cls.bundle_a.grid)
        study_dir = os.path.join(cls.data, "Study")
        study = Study.create(study_dir, name="Robustness study")
        rng = np.random.default_rng(7)
        cls.sids, cls.sides = [], {}
        for index in range(6):
            blob = "A" if index < 3 else "A2"  # A is in the left hemisphere (x < 0), A2 in the right
            centre = np.array(cls.meta_a["blobs"][blob]["center_world"]) + rng.normal(0, 1.5, 3)
            mask = _sphere(cls.fine_shape, cls.fine_affine, centre, rng.uniform(3.5, 5.0))
            sid = f"sub-{index + 1:02d}"
            path = os.path.join(study_dir, "lesions", f"{sid}_space-{SPACE}_desc-lesion_mask.nii.gz")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            nifti.write_nifti(path, mask, cls.fine_affine)
            study.add_subject(sid, {"group": "case" if index % 2 == 0 else "control", "lesion": study.relpath(path), "status": "traced", "space": SPACE})
            cls.sids.append(sid)
            cls.sides[sid] = "L" if blob == "A" else "R"
            if index in (0, 3):  # single-slice tracings: the middle axial slice of the lesion
                traced = np.zeros_like(mask)
                k = int(np.round(np.argwhere(mask)[:, 2].mean()))
                traced[:, :, k] = mask[:, :, k]
                nifti.write_nifti(path.replace("desc-lesion", "desc-traced"), traced, cls.fine_affine)
            if index == 1:  # a fractional lesion next to the binary one
                nifti.write_nifti(path.replace("desc-lesion", "desc-lesionProb"), (mask * 0.6).astype(np.float32), cls.fine_affine)
        study.save()
        cls.study_dir = study_dir
        cls.lesions = [(sid, os.path.join(study_dir, "lesions", f"{sid}_space-{SPACE}_desc-lesion_mask.nii.gz")) for sid in cls.sids]
        cls.maps_a_dir = os.path.join(cls.data, "maps-A")
        cohortmaps.run(cls.lesions, cls.bundle_a, cls.maps_a_dir, cache_root=os.path.join(cls.data, "cache"), emit=_quiet, run_root=cls.data)
        cls.cohort_a = robustness.load_cohort(os.path.join(cls.maps_a_dir, cohortmaps.MAPS_TSV))

    @classmethod
    def tearDownClass(cls):
        cls.bundle_a.close()
        cls.bundle_b.close()
        shutil.rmtree(cls.data, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-robustness-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


class ReplicateTest(RobustnessTestBase):
    def test_rule_is_re_resolved_for_each_connectome(self):
        out = self.path("replicate")
        record = robustness.replicate_on(self.cohort_a, None, self.bundle_b, self.lesions, FWE, out, emit=_quiet, cohortmaps_options={"cache_root": self.path("cache")})
        a, b = record["connectome_a"], record["connectome_b"]
        self.assertEqual((a["n"], b["n"]), (20, 30))
        expected_a = thresholds.resolve(FWE, 20, self.bundle_a.n_voxels)
        expected_b = thresholds.resolve(FWE, 30, self.bundle_b.n_voxels)
        self.assertNotAlmostEqual(expected_a.value, expected_b.value, places=3)
        self.assertAlmostEqual(record["resolved_a"]["value"], expected_a.value, places=9)
        self.assertAlmostEqual(record["resolved_b"]["value"], expected_b.value, places=9)
        self.assertEqual((record["resolved_a"]["n"], record["resolved_b"]["n"]), (20, 30))
        _columns, rows = read_tsv(os.path.join(out, robustness.CONNECTOMES_TSV))
        lesion_rows = [row for row in rows if row["kind"] == "lesion"]
        self.assertEqual([row["participant_id"] for row in lesion_rows], self.sids)
        self.assertTrue(all(row["threshold_a"] == f"T {expected_a.value:.4g}" and row["threshold_b"] == f"T {expected_b.value:.4g}" for row in lesion_rows))
        self.assertTrue(all(-1.0 <= float(row["r"]) <= 1.0 for row in lesion_rows))
        self.assertTrue(all(row["peaks_persist"].endswith("/3") for row in lesion_rows))
        self.assertEqual({row["kind"] for row in rows}, {"lesion", "group_mean", "group_sensitivity"})
        # Registered with the summary that the reporting checklist reads
        record_run = runs.load(out)[record["run_id"]]
        summary = runs.read_summary(out, record_run)
        self.assertEqual(record_run["kind"], "robustness")
        self.assertEqual(len(summary["connectomes"]), 2)
        self.assertIn("cross_connectome_r", summary)
        # No absolute local path in the shared files
        for name in (robustness.CONNECTOMES_TSV, "replicate.json"):
            with open(os.path.join(out, name), encoding="utf-8") as f:
                self.assertNotIn(self.tmp, f.read())

    def test_top_pct_rule_is_resolved_on_each_map_and_identity_replicates(self):
        result = robustness.compare_cohorts(self.cohort_a, self.cohort_a, TOP10)
        lesions = [row for row in result["rows"] if row["kind"] == "lesion"]
        self.assertTrue(all(row["r"] == 1.0 and row["dice"] == 1.0 and row["peaks_persist"] == (3, 3) and not row["flag"] for row in lesions))
        self.assertIsInstance(result["resolved_a"], tuple)
        self.assertEqual(len({r.value for r in result["resolved_a"]}), len(self.sids))  # one threshold per lesion map

    def test_group_pca_compares_r_only_and_low_r_is_flagged(self):
        mean = self.cohort_a.maps.mean(axis=0)
        noise = np.random.default_rng(0).normal(0, 1, mean.size).astype(np.float32)
        group = {"PC1": {"a": -mean, "b": mean, "kind": "pc1"}, "noise": {"a": mean, "b": noise, "kind": "glm_t"}}
        result = robustness.compare_cohorts(self.cohort_a, self.cohort_a, FWE, group_maps=group)
        pc1 = next(row for row in result["rows"] if row["map"] == "PC1")
        self.assertAlmostEqual(pc1["r"], 1.0, places=6)  # sign of a component is arbitrary: |r|
        self.assertIsNone(pc1["dice"])
        self.assertIsNone(pc1["peaks_persist"])
        self.assertIn("|r| only", pc1["note"])
        low = next(row for row in result["rows"] if row["map"] == "noise")
        self.assertLess(low["r"], robustness.CROSS_CONNECTOME_R_FLAG)
        self.assertIn("Cotovio 2022: 0.86\u20130.90; deep lesions 0.5\u20130.7", low["flag"])


class SplitHalfInfluenceTest(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(11)
        self.L, self.V = 13, 400
        self.maps = rng.normal(0, 1, (self.L, self.V)).astype(np.float32)
        self.maps[:, :40] += 3.0  # a shared network
        self.groups = ["case" if index % 2 == 0 else "control" for index in range(self.L)]
        group = np.array([1.0 if label == "case" else 0.0 for label in self.groups])
        covariate = rng.normal(0, 1, self.L)
        self.X = np.column_stack([np.ones(self.L), group, covariate])
        self.C = np.array([[0.0, 1.0, 0.0]])

    def test_split_half_is_deterministic_and_stratified(self):
        first = robustness.lesion_split_half(self.maps, n_splits=50, seed=3, rule=TOP10, design=(self.X, self.C), strata=self.groups)
        again = robustness.lesion_split_half(self.maps, n_splits=50, seed=3, rule=TOP10, design=(self.X, self.C), strata=self.groups)
        other = robustness.lesion_split_half(self.maps, n_splits=50, seed=4, rule=TOP10, design=(self.X, self.C), strata=self.groups)
        for key in ("r_overlap", "dice_sensitivity", "r_glm_t"):
            np.testing.assert_array_equal(first[key], again[key])
        self.assertFalse(np.array_equal(first["r_overlap"], other["r_overlap"]))
        self.assertEqual((first["half_size"], first["left_out"]), (6, 1))  # 7 cases and 6 controls: 3 + 3 per half
        halves = robustness.split_indices(self.L, 50, 3, self.groups)
        for half in halves:
            self.assertTrue(all(sum(self.groups[i] == "case" for i in row) == 3 for row in half))
        self.assertTrue(all(not set(a) & set(b) for a, b in zip(*halves, strict=True)))
        self.assertEqual(first["label"], robustness.LESION_SPLIT_HALF_LABEL)
        self.assertEqual(set(first["summary"]), {"r_overlap", "dice_sensitivity", "r_glm_t"})

    def test_split_half_values_match_a_direct_computation(self):
        result = robustness.lesion_split_half(self.maps, n_splits=5, seed=1, rule=TOP10, overlap_pct=50, design=(self.X, self.C), batch=2)
        halves = robustness.split_indices(self.L, 5, 1)
        binary, _ = robustness._threshold_maps(self.maps, thresholds.normalize_rule(TOP10), None, self.V, "both", None)
        for split in range(5):
            a, b = halves[0][split], halves[1][split]
            counts_a, counts_b = binary[a].sum(axis=0), binary[b].sum(axis=0)
            self.assertAlmostEqual(result["r_overlap"][split], np.corrcoef(counts_a, counts_b)[0, 1], places=6)
            sens_a, sens_b = counts_a * 100 >= 50 * len(a), counts_b * 100 >= 50 * len(b)
            self.assertAlmostEqual(result["dice_sensitivity"][split], 2 * (sens_a & sens_b).sum() / (sens_a.sum() + sens_b.sum()), places=9)
            t_a = glm.fit(self.maps[a], self.X[a], self.C).stat
            t_b = glm.fit(self.maps[b], self.X[b], self.C).stat
            self.assertAlmostEqual(result["r_glm_t"][split], np.corrcoef(t_a, t_b)[0, 1], places=6)

    def test_leave_one_out_downdate_equals_refitting(self):
        stats = robustness.SufficientStatistics(self.maps, self.X, self.C[0])
        np.testing.assert_allclose(stats.t(), glm.fit(self.maps, self.X, self.C).stat, rtol=1e-8, atol=1e-8)
        for row in range(self.L):
            keep = np.arange(self.L) != row
            np.testing.assert_allclose(stats.t_without(row), glm.fit(self.maps[keep], self.X[keep], self.C).stat, rtol=1e-8, atol=1e-8)

    def test_leave_one_lesion_out_rows(self):
        ids = [f"sub-{index:02d}" for index in range(self.L)]
        result = robustness.leave_one_lesion_out(self.maps, design=(self.X, self.C), ids=ids, rule=TOP10, overlap_pct=50, top_k=3)
        full_t = glm.fit(self.maps, self.X, self.C).stat
        peak = int(np.argmax(np.abs(full_t)))
        self.assertEqual(result["peak"]["index"], peak)
        binary, _ = robustness._threshold_maps(self.maps, thresholds.normalize_rule(TOP10), None, self.V, "both", None)
        full = binary.sum(axis=0) * 100 >= 50 * self.L
        for row, item in enumerate(result["rows"]):
            keep = np.arange(self.L) != row
            refit = glm.fit(self.maps[keep], self.X[keep], self.C).stat
            self.assertAlmostEqual(item["t_at_peak"], refit[peak], places=8)
            self.assertAlmostEqual(item["delta_t_at_peak"], refit[peak] - full_t[peak], places=8)
            self.assertAlmostEqual(item["loo_peak_t"], refit[np.argmax(np.abs(refit))], places=8)
            loo = binary[keep].sum(axis=0) * 100 >= 50 * (self.L - 1)
            self.assertAlmostEqual(item["dice_vs_full"], 2 * (loo & full).sum() / (loo.sum() + full.sum()), places=9)
            self.assertEqual(item["voxels_changed"], int((loo ^ full).sum()))
        ranked = sorted(result["rows"], key=lambda item: -abs(item["delta_t_at_peak"]))
        self.assertEqual(result["top"], [item["participant_id"] for item in ranked[:3]])
        self.assertEqual(sum(item["top_k"] for item in result["rows"]), 3)
        path = robustness.write_influence_tsv(result, self.path_tsv())
        columns, rows = read_tsv(path)
        self.assertEqual(columns, list(robustness.INFLUENCE_COLUMNS))
        self.assertEqual(rows[0]["participant_id"], result["top"][0])
        # Without a design: ranked by 1 - Dice of the sensitivity map
        plain = robustness.leave_one_lesion_out(self.maps, ids=ids, rule=TOP10, overlap_pct=50)
        self.assertIsNone(plain["peak"])
        self.assertEqual(plain["influence"], "1 - Dice of the sensitivity map")

    def path_tsv(self):
        folder = tempfile.mkdtemp(prefix="lnm-robustness-influence-")
        self.addCleanup(shutil.rmtree, folder, True)
        return os.path.join(folder, robustness.INFLUENCE_TSV)


class SeedsTest(RobustnessTestBase):
    def test_variants_never_touch_lesions(self):
        lesions_dir = os.path.join(self.study_dir, "lesions")
        before = _tree_state(lesions_dir)
        out = os.path.join(self.study_dir, "derivatives", "lnm", "robustness-seeds")
        coordinates = {self.sids[0]: [tuple(self.meta_a["blobs"]["A"]["center_world"])], self.sids[4]: [tuple(self.meta_a["blobs"]["A2"]["center_world"])]}
        try:
            record = robustness.seed_variants(self.study_dir, robustness.DEFAULT_VARIANTS, out, bundle=self.bundle_a, coordinates=coordinates, rule=TOP10, overlap_pct=50,
                                              emit=_quiet, cohortmaps_options={"cache_root": self.path("cache")})
            self.assertEqual(_tree_state(lesions_dir), before)
            variants = record["variants"]
            self.assertFalse(variants["binarized"]["applicable"])  # binary lesions: nothing to binarize
            self.assertEqual(variants["probabilistic"]["changed"], 1)  # the desc-lesionProb file
            for name in ("extrude2", "extrude4"):
                derived = [entry["participant_id"] for entry in variants[name]["lesions"] if entry["action"] == "derived"]
                self.assertEqual(derived, [self.sids[0], self.sids[3]])
                self.assertTrue(variants[name]["probabilistic"])
            self.assertEqual(variants["sphere4"]["changed"], 2)
            self.assertEqual(variants["flipped"]["changed"], len(self.sids))
            real_out = os.path.realpath(out)
            for name, variant in variants.items():
                for entry in variant["lesions"]:
                    output = os.path.realpath(os.path.join(out, *entry["output"].split("/")))
                    if entry["action"] == "derived":
                        self.assertTrue(output.startswith(os.path.join(real_out, "variants", name) + os.sep), output)
                        self.assertTrue(os.path.isfile(output))
                    else:
                        self.assertFalse(output.startswith(real_out + os.sep) and "variants" in output)
            # Extrusion: exact total thickness of the slab (2 vs 4 mm of the 1 mm drawn slice)
            traced, _affine, _ = nifti.read_nifti(os.path.join(self.study_dir, "lesions", f"{self.sids[0]}_space-{SPACE}_desc-traced_mask.nii.gz"))
            for name, mm in (("extrude2", 2.0), ("extrude4", 4.0)):
                entry = next(item for item in variants[name]["lesions"] if item["participant_id"] == self.sids[0])
                data, _affine, _ = nifti.read_nifti(os.path.join(out, *entry["output"].split("/")))
                self.assertAlmostEqual(float(np.asarray(data).sum()), float(np.asarray(traced).sum()) * mm, places=3)
            # Comparison table
            _columns, rows = read_tsv(os.path.join(out, robustness.SEEDS_TSV))
            compared = {row["variant"] for row in rows if row["kind"] == "lesion"}
            self.assertEqual(compared, {"probabilistic", "extrude2", "extrude4", "sphere2", "sphere4", "sphere6", "flipped"})
            self.assertTrue(any(row["variant"] == "binarized" and row["note"].startswith("not applicable") for row in rows))
            unchanged = [row for row in rows if row["variant"] == "extrude2" and row["kind"] == "lesion" and row["changed"] == "false"]
            self.assertTrue(unchanged and all(float(row["r"]) > 0.999999 for row in unchanged))
            with open(os.path.join(out, robustness.VARIANTS_JSON), encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn(self.data, text)
            self.assertIn("approximate", json.loads(text)["mirror_note"])
        finally:
            shutil.rmtree(out, ignore_errors=True)
        with self.assertRaises(ValueError):
            robustness.seed_variants(self.study_dir, ("flipped",), os.path.join(lesions_dir, "variants"), compare=False)
        self.assertEqual(_tree_state(lesions_dir), before)

    def test_consolidation_manifest(self):
        lesions_dir = os.path.join(self.study_dir, "lesions")
        before = _tree_state(lesions_dir)
        out = self.path("consolidate")
        record = robustness.consolidate_hemisphere(self.study_dir, "left", out, run_root=out)
        self.assertEqual(_tree_state(lesions_dir), before)  # the traced masks are never overwritten
        with open(os.path.join(out, robustness.CONSOLIDATION_JSON), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["side"], "L")
        self.assertTrue(manifest["analysis_option"])
        self.assertFalse(manifest["traced_masks_modified"])
        self.assertIn("asymmetric", manifest["caveat"])
        self.assertIn("approximate", manifest["mirror_note"])
        self.assertEqual(manifest["counts"], {"mirrored": 3, "kept": 3, "bilateral_or_midline": 0, "empty": 0})
        self.assertNotIn(self.data, json.dumps(manifest))
        for entry in manifest["lesions"]:
            self.assertEqual(entry["traced_side"], self.sides[entry["participant_id"]])
            output = os.path.normpath(os.path.join(out, *entry["output"].split("/")))
            data, affine, _ = nifti.read_nifti(output)
            self.assertEqual(lesionops.side_check(np.asarray(data), affine, None)["traced_side"], "L")
            if entry["action"] == "mirrored":
                self.assertTrue(output.startswith(os.path.join(out, "consolidated") + os.sep))
            else:
                self.assertTrue(output.startswith(lesions_dir + os.sep))
        rows = cohortmaps.read_lesions_tsv(os.path.join(out, robustness.LESIONS_TSV))
        self.assertEqual([row[0] for row in rows], self.sids)
        run = runs.load(out)[record["run_id"]]
        self.assertEqual(runs.read_summary(out, run)["consolidation"]["mirrored"], 3)
        with self.assertRaises(ValueError):
            robustness.consolidate_hemisphere(self.study_dir, "bilateral", self.path("other"))


class LabelsAndCommandLineTest(RobustnessTestBase):
    def run_cli(self, *argv):
        process = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.robustness", *argv], cwd=MODULE_DIR, env=_env(), capture_output=True, text=True, timeout=600, check=False)
        events = [json.loads(line) for line in process.stdout.splitlines() if line.startswith("{")]
        return process.returncode, events, process.stderr

    def test_connectome_split_half_label(self):
        self.assertEqual(robustness.CONNECTOME_SPLIT_HALF_LABEL, "connectome sampling stability (≈0.99 expected for ≥ 500 per half; not evidence of robustness)")
        self.assertNotIn("0.98", robustness.LESION_SPLIT_HALF_LABEL)
        engine_out = self.path("engine")
        process = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.engine", "--bundle", self.meta_a["path"], "--lesion", self.lesions[0][1], "--out", engine_out, "--split-half"],
                                 cwd=MODULE_DIR, env=_env(), capture_output=True, text=True, timeout=600, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        summary = robustness.connectome_split_half(engine_out)
        self.assertEqual(summary["label"], robustness.CONNECTOME_SPLIT_HALF_LABEL)
        self.assertEqual((summary["n_odd"], summary["n_even"]), (10, 10))
        self.assertTrue(summary["small_halves"])
        self.assertTrue(all(robustness.CONNECTOME_SPLIT_HALF_LABEL in row["note"] for row in summary["rows"]))
        with self.assertRaises(ValueError):
            robustness.connectome_split_half({"bundle": "x"})
        out = self.path("connectome-split-half")
        code, events, stderr = self.run_cli("splithalf", "--connectome-run", engine_out, "--out", out)
        self.assertEqual(code, 0, stderr)
        with open(os.path.join(out, robustness.CONNECTOMES_TSV), encoding="utf-8") as f:
            self.assertIn(robustness.CONNECTOME_SPLIT_HALF_LABEL, f.read())
        self.assertFalse(os.path.exists(os.path.join(out, robustness.SPLITHALF_TSV)))  # never next to the lesion split-half
        self.assertEqual(events[-1]["summary"]["split_half"]["label"], robustness.CONNECTOME_SPLIT_HALF_LABEL)

    def test_command_line_sweep_splithalf_influence(self):
        maps = os.path.join(self.maps_a_dir, cohortmaps.MAPS_TSV)
        rule = json.dumps(TOP10)
        out = self.path("cli")
        code, events, stderr = self.run_cli("sweep", "--maps", maps, "--rule", rule, "--out", out, "--run-root", out)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(events[-1]["event"], "done")
        self.assertIn("conclusion_flips", events[-1]["summary"])
        code, events, stderr = self.run_cli("splithalf", "--maps", maps, "--rule", rule, "--n-splits", "20", "--overlap", "50", "--out", out, "--run-root", out)
        self.assertEqual(code, 0, stderr)
        _columns, rows = read_tsv(os.path.join(out, robustness.SPLITHALF_TSV))
        self.assertEqual({row["label"] for row in rows}, {robustness.LESION_SPLIT_HALF_LABEL})
        self.assertEqual({row["n_splits"] for row in rows}, {"20"})
        code, events, stderr = self.run_cli("influence", "--maps", maps, "--rule", rule, "--overlap", "50", "--top-k", "2", "--out", out, "--run-root", out)
        self.assertEqual(code, 0, stderr)
        _columns, rows = read_tsv(os.path.join(out, robustness.INFLUENCE_TSV))
        self.assertEqual(sorted(row["participant_id"] for row in rows), self.sids)
        analyses = sorted(runs.read_params(out, record)["analysis"] for record in runs.list_runs(out, kind="robustness"))
        self.assertEqual(analyses, ["influence", "splithalf", "sweep"])
        code, events, stderr = self.run_cli("consolidate", "--study", self.study_dir, "--side", "X", "--out", self.path("bad"))
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["event"], "error")

    def test_command_line_protocol(self):
        maps = os.path.join(self.maps_a_dir, cohortmaps.MAPS_TSV)
        rule = json.dumps(TOP10)
        cache = self.path("cache")
        # one terminal "done": the cohortmaps stage's start/done events become a result(kind="mapped")
        out = self.path("cli-replicate")
        code, events, stderr = self.run_cli("replicate", "--maps", maps, "--bundle-b", self.meta_b["path"], "--rule", rule, "--cache", cache, "--out", out, "--run-root", out)
        self.assertEqual(code, 0, stderr)
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds.count("done"), 1)
        self.assertEqual(kinds[-1], "done")
        self.assertIn("cross_connectome_r", events[-1]["summary"])
        self.assertTrue(any(event["event"] == "result" and event.get("kind") == "mapped" for event in events))
        out = self.path("cli-seeds")
        code, events, stderr = self.run_cli("seeds", "--study", self.study_dir, "--bundle", self.meta_a["path"], "--variants", "flipped,binarized", "--rule", rule,
                                            "--overlap", "50", "--cache", cache, "--out", out, "--run-root", out)
        self.assertEqual(code, 0, stderr)
        self.assertEqual([event["event"] for event in events].count("done"), 1)
        # a cancel file stops the job: exit code 3, a "cancelled" event and no run registered as done
        cancel_file = self.path("cancel")
        with open(cancel_file, "w", encoding="utf-8"):
            pass
        out = self.path("cli-cancelled")
        code, events, stderr = self.run_cli("replicate", "--maps", maps, "--bundle-b", self.meta_b["path"], "--rule", rule, "--cache", self.path("cache-cancel"),
                                            "--out", out, "--run-root", out, "--cancel-file", cancel_file)
        self.assertEqual(code, 3, stderr)
        self.assertEqual(events[-1]["event"], "cancelled")
        self.assertNotIn("done", [event["event"] for event in events])
        self.assertFalse(os.path.exists(os.path.join(out, robustness.CONNECTOMES_TSV)))
        self.assertEqual(list(runs.list_runs(out, kind="robustness")), [])
        code, events, stderr = self.run_cli("splithalf", "--maps", maps, "--rule", rule, "--n-splits", "20", "--out", out, "--cancel-file", cancel_file)
        self.assertEqual(code, 3, stderr)
        # an out folder inside lesions/ is refused before anything is created there
        lesions_dir = os.path.join(self.study_dir, "lesions")
        before = _tree_state(lesions_dir)
        derived = os.path.join(lesions_dir, "derived")
        for argv in (("seeds", "--no-maps", "--variants", "flipped"), ("consolidate", "--side", "L")):
            code, events, stderr = self.run_cli(*argv, "--study", self.study_dir, "--out", derived)
            self.assertEqual(code, 1, stderr)
            self.assertEqual(events[-1]["event"], "error")
            self.assertFalse(os.path.exists(derived))
        self.assertEqual(_tree_state(lesions_dir), before)


if __name__ == "__main__":
    unittest.main()

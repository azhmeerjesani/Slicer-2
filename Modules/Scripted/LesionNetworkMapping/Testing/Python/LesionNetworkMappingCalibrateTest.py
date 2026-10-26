"""Tests of the pipeline calibration (calibrate.py): empirical FWER of a correct two-sample design and of a one-sample
design, planted-network recovery with partialled metrics, the sensitivity route and the command line.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingCalibrateTest.py -v
"""

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

from LesionNetworkMappingLib import calibrate, nifti, runs
from LesionNetworkMappingLib import design as designlib
from LesionNetworkMappingLib.study import Study

SHAPE = (10, 10, 4)
N_VOXELS = int(np.prod(SHAPE))
AFFINE = np.diag([2.0, 2.0, 2.0, 1.0])


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))


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


def _patterns(rng):
    """LNM-like synthetic maps: a positive degree map shared by every lesion and four smooth network patterns."""
    i, j, k = np.meshgrid(*[np.arange(size, dtype=np.float64) for size in SHAPE], indexing="ij")
    degree = (0.25 + 0.08 * np.cos(i / 3.0) + 0.05 * np.sin(j / 2.5)).reshape(-1)
    networks = []
    for centre in ((2, 2, 1), (7, 7, 2), (2, 7, 1), (7, 2, 2)):
        distance2 = (i - centre[0]) ** 2 + (j - centre[1]) ** 2 + 2.0 * (k - centre[2]) ** 2
        networks.append(np.exp(-distance2 / 6.0).reshape(-1))
    return degree, np.array(networks)


def _maps(rng, n, degree, networks, noise=0.08):
    weights = rng.standard_normal((n, len(networks))) * 0.25
    return (degree[None, :] + weights @ networks + noise * rng.standard_normal((n, degree.size))).astype(np.float32)


class _CalibrateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-calibrate-test-")
        self.rng = np.random.default_rng(48)
        self.degree, self.networks = _patterns(self.rng)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def make_study(self, n_case=24, n_control=24, name="study"):
        study = Study.create(self.path(name), name=name)
        for index in range(n_case + n_control):
            group = "case" if index < n_case else "control"
            study.add_subject(f"sub-{group}{index:03d}", {"group": group, "volume_ml": f"{float(self.rng.uniform(2, 60)):.3f}"})
        study.save()
        return study


class EmpiricalFwerTest(_CalibrateTestCase):
    def test_two_sample_design_is_calibrated(self):
        study = self.make_study(24, 24)
        sids = study.subject_ids
        Y = _maps(self.rng, len(sids), self.degree, self.networks)
        spec = designlib.DesignSpec(test="two_group", levels=["case", "control"], contrast="two_sided", inference={"n_perm": 1000, "seed": 1})
        result = calibrate.empirical_fwer(study, spec, Y, sids, k=100, inner_perm=200, seed=3)
        self.assertEqual(result["null_source"], "label_permutation")
        self.assertEqual(result["k"], 100)
        self.assertEqual(result["inner_perm"], 200)
        low, high = result["fwe"]["ci"]
        self.assertLessEqual(low, 0.05, result["text"])
        self.assertGreaterEqual(high, 0.05, result["text"])
        self.assertIn(result["level"], ("consistent", "conservative"))
        self.assertEqual(result["chip"], "ok")
        self.assertEqual(result["fwe"]["ci_method"], calibrate.CI_METHOD)
        self.assertEqual(len(result["datasets"]), 100)
        self.assertEqual({item["n"] for item in result["datasets"]}, {48})
        # FDR is reported next to it, also with a CI
        self.assertEqual(len(result["fdr"]["ci"]), 2)
        json.dumps(result)  # JSON-compatible

    def test_one_sample_design_is_inflated(self):
        study = self.make_study(24, 24)
        sids = study.subject_ids
        Y = _maps(self.rng, len(sids), self.degree, self.networks)
        spec = designlib.DesignSpec(test="one_sample", group_column="group", levels=["case"], contrast="two_sided", inference={"n_perm": 1000})
        result = calibrate.empirical_fwer(study, spec, Y, sids, k=40, inner_perm=200, seed=5)
        self.assertEqual(result["null_source"], "random_subsets")
        self.assertEqual(result["n_candidates"], 48)
        self.assertGreater(result["fwe"]["rate"], 0.95)
        self.assertGreater(result["fwe"]["ci"][0], 0.05)
        self.assertEqual(result["level"], "inflated")
        self.assertEqual(result["chip"], "warn")
        self.assertIn("one-sample", result["text"].lower())
        self.assertEqual({item["n"] for item in result["datasets"]}, {24})

    def test_one_sample_without_other_lesions_is_explained(self):
        study = self.make_study(20, 0)
        sids = study.subject_ids
        Y = _maps(self.rng, len(sids), self.degree, self.networks)
        spec = designlib.DesignSpec(test="one_sample", group_column="group", levels=["case"], inference={"n_perm": 1000})
        with self.assertRaises(calibrate.NotCalibratable) as caught:
            calibrate.empirical_fwer(study, spec, Y, sids, k=10, inner_perm=200)
        self.assertIn("control set", str(caught.exception))
        # A pool of control lesions makes it calibratable (and inflated)
        pool = {f"sub-pool{index:03d}": 10.0 + index for index in range(20)}
        pooled, added = calibrate.add_pool(study, pool)
        self.assertEqual(len(added), 20)
        Y_pool = np.vstack([Y, _maps(self.rng, 20, self.degree, self.networks)])
        design, _ = calibrate.configured_design(study, spec)
        result = calibrate.empirical_fwer(pooled, spec, Y_pool, [*sids, *added], k=20, inner_perm=200, design=design)
        self.assertEqual(result["level"], "inflated")

    def test_score_design_and_resume(self):
        study = self.make_study(40, 0)
        for sid in study.subject_ids:
            study.set(sid, nihss=f"{float(self.rng.integers(0, 25))}")
        study.save()
        sids = study.subject_ids
        Y = _maps(self.rng, len(sids), self.degree, self.networks)
        spec = designlib.DesignSpec(test="continuous", score_column="nihss", group_column=None, inference={"n_perm": 1000}, extra={"slnm": {"method": "spearman", "flip": False}})
        seen = []
        full = calibrate.empirical_fwer(study, spec, Y, sids, k=12, inner_perm=150, seed=2, on_dataset=seen.append)
        self.assertEqual(full["null_source"], "score_permutation")
        self.assertEqual(full["design"]["method"], "spearman")
        self.assertEqual(len(seen), 12)
        # Resuming from the first 5 outcomes computes only the other 7 and gives the same result
        again = []
        resumed = calibrate.empirical_fwer(study, spec, Y, sids, k=12, inner_perm=150, seed=2, done=seen[:5], on_dataset=again.append)
        self.assertEqual(len(again), 7)
        self.assertEqual(resumed["datasets"], full["datasets"])
        self.assertEqual(resumed["fwe"], full["fwe"])

    def test_rates_and_verdicts(self):
        low, high = calibrate.clopper_pearson(5, 100)
        self.assertAlmostEqual(low, 0.01643, places=4)
        self.assertAlmostEqual(high, 0.11283, places=4)
        self.assertEqual(calibrate.clopper_pearson(0, 10)[0], 0.0)
        self.assertEqual(calibrate.clopper_pearson(10, 10)[1], 1.0)
        self.assertEqual(calibrate.fwer_verdict(calibrate.rate_summary(30, 100))[0], "inflated")
        self.assertEqual(calibrate.fwer_verdict(calibrate.rate_summary(4, 100))[0], "consistent")
        self.assertEqual(calibrate.fwer_verdict(calibrate.rate_summary(0, 100))[0], "conservative")
        self.assertEqual(calibrate.fwer_verdict(calibrate.rate_summary(0, 0))[0], "none")
        # The ETA follows the measured cost: K=100 x 500 at n=200, V=228,483 is about 7 min at p=4 and 11 min at p=6
        minutes = calibrate.estimate_seconds(200, 228_483, 4, 100, 500, planted=False) / 60.0
        self.assertTrue(6 <= minutes <= 8, minutes)
        self.assertAlmostEqual(calibrate.estimate_seconds(200, 228_483, 6, 100, 500, planted=False) / 60.0, 1.5 * minutes, places=6)


class PlantedNetworkTest(_CalibrateTestCase):
    def test_planted_network_is_recovered(self):
        study = self.make_study(60, 0)
        sids = study.subject_ids
        Y = _maps(self.rng, len(sids), self.degree, self.networks, noise=0.05)
        target = self.networks[0] > 0.5  # a network (not a parcel of the lesions)
        coords = calibrate.voxel_coordinates(np.arange(N_VOXELS), SHAPE, AFFINE)
        reference = np.vstack([self.degree, self.networks[2]])  # stand-ins for μC_r and PC1
        holder = {}
        result = calibrate.planted_network(study, Y, sids, target, effects=(0.2, 0.9), n_sims=6, inner_perm=200, seed=1, reference=reference, coords=coords, truth_out=holder)
        weak, strong = result["effects"]
        self.assertEqual(strong["beta"], 0.9)
        self.assertGreaterEqual(strong["power"]["rate"], 5 / 6)
        self.assertEqual(len(strong["power"]["ci"]), 2)
        self.assertGreaterEqual(strong["peak_in_target"]["rate"], 5 / 6)
        self.assertLess(strong["peak_distance_mm"]["median"], 1e-9)
        self.assertGreater(strong["spatial_r"]["mean"], 0.7)
        # Partialled (μC_r, PC1) recovery is reported
        self.assertTrue(result["partialled"])
        self.assertGreater(strong["partial"]["spatial_r"]["mean"], 0.6)
        self.assertIsNotNone(strong["partial"]["peak_distance_mm"])
        # Power grows with the effect
        self.assertLessEqual(weak["power"]["rate"], strong["power"]["rate"])
        # The truth is network-based: the voxelwise association of the maps with their target connectivity
        truth = holder["truth"]
        self.assertGreater(truth[target].mean(), truth[~target].mean())
        self.assertEqual(result["target"]["n_voxels"], int(target.sum()))
        self.assertIn("partial correlation", result["truth"])
        json.dumps(result)

    def test_target_from_map_and_errors(self):
        values = np.arange(100, dtype=np.float64) - 10
        target = calibrate.target_from_map(values, 5)
        self.assertEqual(int(target.sum()), 5)
        self.assertTrue(target[-1])
        study = self.make_study(10, 0)
        Y = _maps(self.rng, 10, self.degree, self.networks)
        with self.assertRaises(calibrate.NotCalibratable):
            calibrate.planted_network(study, Y, study.subject_ids, np.zeros(N_VOXELS, dtype=bool), n_sims=1, inner_perm=100)
        with self.assertRaises(ValueError):
            calibrate.planted_network(study, Y, study.subject_ids, np.ones(N_VOXELS, dtype=bool), effects=(1.5,), n_sims=1, inner_perm=100)


class SensitivityTest(unittest.TestCase):
    def test_routes(self):
        rng = np.random.default_rng(7)
        k, n_lesions, n_voxels = 60, 20, 500
        hub = np.linspace(0.2, 0.97, n_voxels)  # hub-like voxels pass in most maps of any lesion
        counts = rng.binomial(n_lesions, hub[None, :], size=(k, n_voxels))[:, None, :].astype(np.int16)
        result = calibrate.sensitivity_fpr(counts, n_lesions, overlap=90, sign="pos")
        self.assertEqual(result["k"], k)
        # The classic rule passes somewhere in nearly every random set (hub inflation); the null-referenced FWE does not
        self.assertGreater(result["classic"]["rate"], 0.9)
        self.assertLessEqual(result["null_fwe"]["rate"], 0.1)
        self.assertLessEqual(result["conjunction"]["rate"], result["null_fwe"]["rate"])
        self.assertEqual(len(result["classic"]["ci"]), 2)
        with self.assertRaises(ValueError):
            calibrate.sensitivity_fpr(counts[:1], n_lesions)


class CommandLineTest(_CalibrateTestCase):
    def write_maps(self, study, Y):
        folder = self.path("maps")
        os.makedirs(folder)
        mask = np.ones(SHAPE, dtype=np.uint8)
        nifti.write_nifti(self.path("mask.nii.gz"), mask, AFFINE)
        rows = ["participant_id\tAvgFz\tT\tvolume_ml"]
        for sid, values in zip(study.subject_ids, Y, strict=True):
            path = os.path.join(folder, f"{sid}_AvgFz.nii.gz")
            nifti.write_nifti(path, values.reshape(SHAPE).astype(np.float32), AFFINE)
            rows.append(f"{sid}\t{os.path.basename(path)}\t{os.path.basename(path)}\tn/a")
        with open(os.path.join(folder, "maps.tsv"), "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        return os.path.join(folder, "maps.tsv")

    def run_cli(self, *args):
        process = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.calibrate", *args], cwd=MODULE_DIR, env=_env(), capture_output=True, text=True, timeout=600, check=False)
        events = [json.loads(line) for line in process.stdout.splitlines() if line.startswith("{")]
        return process.returncode, events, process.stderr

    def test_command_line(self):
        study = self.make_study(16, 16)
        Y = _maps(self.rng, len(study.subject_ids), self.degree, self.networks)
        maps_tsv = self.write_maps(study, Y)
        spec = designlib.DesignSpec(test="two_group", levels=["case", "control"], inference={"n_perm": 1000})
        design_path = spec.save(self.path("design.json"))
        out = self.path("study", "derivatives", "lnm", "calibration", "caseVsControl")
        common = ["--maps", maps_tsv, "--participants", os.path.join(study.path, "participants.tsv"), "--design", design_path, "--mask", self.path("mask.nii.gz"), "--out", out]
        code, events, stderr = self.run_cli(*common, "--estimate")
        self.assertEqual(code, 0, stderr)
        self.assertTrue(any(event["event"] == "estimate" for event in events))
        code, events, stderr = self.run_cli(*common, "--k", "20", "--inner-perm", "100", "--sims", "3", "--effects", "0.3,0.8", "--target-top-pct", "10")
        self.assertEqual(code, 0, (stderr, events[-3:]))
        self.assertTrue(any(event["event"] == "progress" and event.get("eta") is not None for event in events))
        record_path = os.path.join(out, calibrate.CALIBRATION_JSON)
        with open(record_path, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(_absolute_strings(raw), [])
        self.assertEqual(raw["format"], calibrate.FORMAT_NAME)
        self.assertEqual(raw["fwer"]["k"], 20)
        self.assertEqual(len(raw["fwer"]["fwe"]["ci"]), 2)
        self.assertEqual([effect["beta"] for effect in raw["planted"]["effects"]], [0.3, 0.8])
        self.assertEqual(raw["planted"]["target"]["kind"], "lesion_network")
        self.assertIn("no connectome bundle", raw["planted"]["reference"]["reason"])
        for key in ("target", "truth"):
            self.assertTrue(os.path.isfile(os.path.join(out, raw["outputs"][key])))
        self.assertFalse(os.path.exists(os.path.join(out, calibrate.MAPS_MEMMAP)))
        record = calibrate.read_record(record_path)
        self.assertTrue(os.path.isabs(record["inputs"]["maps_tsv"]))
        # Registered as a calibration run, found by the checklist helper
        registered = runs.latest(study.path, "calibration", status="done")
        self.assertIsNotNone(registered)
        status, value, evidence = calibrate.checklist_evidence(study.path)
        self.assertIn(status, ("done", "flagged"))
        self.assertIn("empirical FWER", value)
        self.assertEqual(evidence, [registered["id"]])

    def test_blocked_and_usage(self):
        study = self.make_study(4, 0)
        Y = _maps(self.rng, 4, self.degree, self.networks)
        maps_tsv = self.write_maps(study, Y)
        common = ["--maps", maps_tsv, "--participants", os.path.join(study.path, "participants.tsv"), "--mask", self.path("mask.nii.gz"), "--out", self.path("out")]
        code, events, _ = self.run_cli(*common, "--inner-perm", "20")
        self.assertEqual(code, 2)
        self.assertEqual(events[-1]["event"], "error")
        code, events, _ = self.run_cli(*common)  # the default design needs two groups
        self.assertEqual(code, 2)
        self.assertEqual(events[-1]["code"], "design_blocked")


if __name__ == "__main__":
    unittest.main()

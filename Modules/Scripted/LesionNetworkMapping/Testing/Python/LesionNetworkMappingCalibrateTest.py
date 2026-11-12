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


class DesignMatchTest(_CalibrateTestCase):
    """A calibration counts only for the design it was run for (BB-1 calibration-result-not-out-of-date-after-design-change).

    The folder is named by the contrast label only, so a covariate change reuses it; the checklist evidence of the
    changed design must not be the old design's calibration.
    """

    def register(self, study, design, name):
        out = self.path("study", "derivatives", "lnm", "calibration", name)
        os.makedirs(out, exist_ok=True)
        run_id = runs.register(study.path, "calibration", out, params={"name": name})
        record = {"format": calibrate.FORMAT_NAME, "version": calibrate.FORMAT_VERSION, "run_id": run_id, "status": "done", "design": calibrate._design_info(design),
                  "fwer": {"level": "consistent", "fwe": calibrate.rate_summary(1, 20)}}
        calibrate.write_record(os.path.join(out, calibrate.CALIBRATION_JSON), record)
        runs.update(study.path, run_id, status="done")
        return run_id

    def test_evidence_follows_the_design(self):
        study = self.make_study(8, 8)
        with_volume = designlib.build_design(study, designlib.DesignSpec(test="two_group", levels=["case", "control"], covariates=["volume_ml"], inference={"n_perm": 1000}))
        without = designlib.build_design(study, designlib.DesignSpec(test="two_group", levels=["case", "control"], covariates=[], volume_off_reason="tested without volume",
                                                                      inference={"n_perm": 1000}))
        run_id = self.register(study, with_volume, "caseVsControl")
        record = calibrate.read_record(self.path("study", "derivatives", "lnm", "calibration", "caseVsControl", calibrate.CALIBRATION_JSON))
        self.assertTrue(calibrate.matches_design(record, with_volume))
        self.assertTrue(calibrate.matches_design(record, calibrate.design_identity(with_volume)))
        self.assertFalse(calibrate.matches_design(record, without))
        self.assertEqual(calibrate.checklist_evidence(study.path, design=with_volume)[2], [run_id])
        self.assertEqual(calibrate.checklist_evidence(study.path, design=without)[0], "not_run")
        self.assertEqual(calibrate.checklist_evidence(study.path)[2], [run_id])  # without a design: the newest, as before
        # The same folder calibrated again for the other design: the older run no longer holds its own record
        newer = self.register(study, without, "caseVsControl")
        self.assertEqual(calibrate.checklist_evidence(study.path, design=without)[2], [newer])
        self.assertEqual(calibrate.checklist_evidence(study.path, design=with_volume)[0], "not_run")

    def test_evidence_follows_transforms_and_statistic(self):
        """A t calibration of untransformed covariates is out of date for log(volume) or the Aspin-Welch v statistic
        (BB-1 calibration-stays-current-after-log-volume-or-welch-change), in the card's identity and in glm.json.
        """
        study = self.make_study(8, 8)

        def build(**kwargs):
            return designlib.build_design(study, designlib.DesignSpec(test="two_group", levels=["case", "control"], covariates=["volume_ml"], inference={"n_perm": 1000}, **kwargs))

        plain, logged, welch = build(), build(transforms={"volume_ml": "log"}), build(variance_groups=True)
        self.assertEqual((plain.stat, welch.stat), ("t", "v"))
        run_id = self.register(study, plain, "caseVsControl")
        record = calibrate.read_record(self.path("study", "derivatives", "lnm", "calibration", "caseVsControl", calibrate.CALIBRATION_JSON))
        self.assertTrue(calibrate.matches_design(record, plain))
        self.assertFalse(calibrate.matches_design(record, logged))
        self.assertFalse(calibrate.matches_design(record, welch))
        self.assertNotEqual(calibrate.design_identity(record["design"]), calibrate.design_identity(calibrate._design_info(logged, logged.spec)))
        self.assertEqual(calibrate.checklist_evidence(study.path, design=plain)[2], [run_id])
        self.assertEqual(calibrate.checklist_evidence(study.path, design=logged)[0], "not_run")
        self.assertEqual(calibrate.checklist_evidence(study.path, design=welch)[0], "not_run")
        # glm.json of the group analysis (checklist F8): the transforms and the statistic are compared too
        glm_record = {"test": "two_group", "design_type": plain.design_type, "n": plain.n, "covariates": ["volume_ml"], "transforms": {}, "stat": "t",
                      "contrast": {"spec": plain.spec.contrast}, "permutation": {"scheme": plain.permute, "exchange": plain.exchange, "block_column": plain.block_column}}
        self.assertTrue(calibrate.matches_group(record, glm_record, "glm"))
        self.assertFalse(calibrate.matches_group(record, dict(glm_record, transforms={"volume_ml": "log"}), "glm"))
        self.assertFalse(calibrate.matches_group(record, dict(glm_record, stat="v"), "glm"))
        # A record written before the transforms were stored is out of date rather than assumed untransformed
        old = dict(record, design={key: value for key, value in record["design"].items() if key != "transforms"})
        self.assertFalse(calibrate.matches_design(old, plain))


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

    def test_stopped_recalibration_keeps_the_finished_result(self):
        """A new calibration of the same folder that is stopped keeps the previous finished record (bug bash BB-1)."""
        study = self.make_study(16, 16)
        Y = _maps(self.rng, len(study.subject_ids), self.degree, self.networks)
        maps_tsv = self.write_maps(study, Y)
        design_path = designlib.DesignSpec(test="two_group", levels=["case", "control"], inference={"n_perm": 1000}).save(self.path("design.json"))
        out = self.path("study", "derivatives", "lnm", "calibration", "caseVsControl")
        common = ["--maps", maps_tsv, "--participants", os.path.join(study.path, "participants.tsv"), "--design", design_path, "--mask", self.path("mask.nii.gz"), "--out", out,
                  "--inner-perm", "100", "--no-planted"]
        code, events, stderr = self.run_cli(*common, "--k", "10")
        self.assertEqual(code, 0, (stderr, events[-3:]))
        first = runs.latest(study.path, "calibration", status="done")["id"]
        before = calibrate.checklist_evidence(study.path)
        self.assertIn("empirical FWER", before[1])
        self.assertNotIn("not computed", before[1])
        # Other settings, stopped at once
        cancel = self.path("cancel.flag")
        open(cancel, "w").close()
        code, events, _ = self.run_cli(*common, "--k", "15", "--cancel-file", cancel)
        self.assertEqual(code, 3)
        self.assertEqual(runs.list_runs(study.path, kind="calibration")[-1]["status"], "cancelled")
        record = calibrate.read_record(os.path.join(out, calibrate.CALIBRATION_JSON))
        self.assertEqual((record["status"], record["run_id"], record["fwer"]["k"]), ("done", first, 10))
        self.assertEqual(calibrate.checklist_evidence(study.path), before)
        self.assertEqual(calibrate.latest_record(study.path)[0], first)
        # A record that is not the run's own (an older version's stopped stub over the folder) is not cited
        stub = dict(record, status="cancelled", run_id="calibration-other", fwer=None)
        calibrate.write_record(os.path.join(out, calibrate.CALIBRATION_JSON), stub)
        self.assertEqual(calibrate.checklist_evidence(study.path)[0], "not_run")

    def test_checklist_battery_and_methods_carry_the_calibration(self):
        """Checklist item F8, the Specificity battery dot, checklist.md and the methods include the calibration of the
        group analysis (bug bash BB-1 calibration-evidence-missing-from-checklist-battery-report-methods, L-48).
        """
        from LesionNetworkMappingLib import checklist, methods

        study = self.make_study(16, 16)
        Y = _maps(self.rng, len(study.subject_ids), self.degree, self.networks)
        maps_tsv = self.write_maps(study, Y)
        design_path = designlib.DesignSpec(test="two_group", levels=["case", "control"], inference={"n_perm": 1000}).save(self.path("design.json"))
        common = ["--maps", maps_tsv, "--participants", os.path.join(study.path, "participants.tsv"), "--design", design_path, "--mask", self.path("mask.nii.gz")]
        glm_out = self.path("study", "derivatives", "lnm", "glm", "caseVsControl")
        process = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.glm", *common, "--out", glm_out], cwd=MODULE_DIR, env=_env(), capture_output=True, text=True, timeout=600, check=False)
        self.assertEqual(process.returncode, 0, process.stderr[-2000:])
        out = self.path("study", "derivatives", "lnm", "calibration", "caseVsControl")
        code, events, stderr = self.run_cli(*common, "--out", out, "--k", "10", "--inner-perm", "100", "--no-planted")
        self.assertEqual(code, 0, (stderr, events[-3:]))
        calibration_id = runs.latest(study.path, "calibration", status="done")["id"]
        glm_id = runs.latest(study.path, "glm", status="done")["id"]
        record_path = os.path.join(out, calibrate.CALIBRATION_JSON)
        record = calibrate.read_record(record_path)
        with open(os.path.join(glm_out, "glm.json"), encoding="utf-8") as f:
            glm_record = json.load(f)
        self.assertTrue(calibrate.matches_group(record, glm_record, "glm"))  # the same design, read from both records

        def f8():
            items = checklist.evaluate(study.path)
            dot = next(dot for dot in checklist.battery(study.path, items=items) if dot["id"] == "specificity")
            return next(item for item in items if item["id"] == "F8"), dot, checklist.checklist_markdown(items, checklist.battery(study.path, items=items))

        item, dot, markdown = f8()
        self.assertIn("calibration: empirical FWER", item["value"])
        self.assertEqual(item["evidence"], [glm_id, calibration_id])
        self.assertIn("empirical FWER", dot["text"])
        self.assertIn("calibration: empirical FWER", markdown)
        text = methods.render(methods.methods_sections(study.path), fmt="txt")
        self.assertIn(f"Pipeline calibration (run {calibration_id})", text)
        self.assertIn("Empirical family-wise error rate: 10 null datasets", text)
        # An inflated FWER (the one-sample error class) flags the item and turns the battery dot amber
        raw = calibrate.read_record(record_path)
        raw["fwer"]["level"] = "inflated"
        calibrate.write_record(record_path, raw)
        item, dot, markdown = f8()
        self.assertEqual(item["status"], checklist.FLAGGED)
        self.assertIn("(inflated)", item["value"])
        self.assertEqual(dot["colour"], checklist.AMBER)
        # A calibration of another design (another covariate set) is out of date for the group analysis
        raw["fwer"]["level"] = "consistent"
        raw["design"]["covariates"] = []
        calibrate.write_record(record_path, raw)
        self.assertFalse(calibrate.matches_group(calibrate.read_record(record_path), glm_record, "glm"))
        item, dot, _ = f8()
        self.assertEqual(item["status"], checklist.FLAGGED)
        self.assertIn("calibration: out of date", item["value"])
        self.assertIn(calibration_id, item["evidence"])
        self.assertEqual(dot["colour"], checklist.AMBER)
        # The methods do not cite it as a calibration of the configured analysis (BB-1
        # methods-cite-out-of-date-calibration-as-configured-analysis): they say it was run for another design
        text = methods.render(methods.methods_sections(study.path), fmt="txt")
        self.assertIn(f"Pipeline calibration (run {calibration_id}): out of date", text)
        self.assertIn("covariates: none", text)
        self.assertNotIn("the configured analysis", text)
        self.assertNotIn("Empirical family-wise error rate", text)
        raw["design"]["covariates"], raw["design"]["transforms"] = ["volume_ml"], {"volume_ml": "log"}
        calibrate.write_record(record_path, raw)
        self.assertIn("covariates: log(volume_ml)", methods.render(methods.methods_sections(study.path), fmt="txt"))

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

    def test_missing_input_file_is_a_usage_error_naming_the_option(self):
        # A missing --maps/--participants/--mask/--target (calibrate, glm, slnm) or a --out without pipeline.json (pipeline
        # report) is a usage error (exit 2) that names the option, not an "exception" event with a traceback (exit 1).
        missing = self.path("nonexistent", "x.tsv")
        here = {"maps": self.path("maps.tsv"), "participants": self.path("participants.tsv"), "mask": self.path("mask.nii.gz")}
        for path in here.values():
            open(path, "w", encoding="utf-8").close()
        extra = {"glm": [], "slnm": ["--score", "s"], "calibrate": []}

        def run(module, args):
            process = subprocess.run([sys.executable, "-m", f"LesionNetworkMappingLib.{module}", *args], cwd=MODULE_DIR, env=_env(), capture_output=True, text=True, timeout=600, check=False)
            events = [json.loads(line) for line in process.stdout.splitlines() if line.startswith("{")]
            return process.returncode, events[-1]

        cases = []
        for module, tail in extra.items():
            for option in here:
                given = {**here, option: missing}
                cases.append((module, option, [arg for key, value in given.items() for arg in (f"--{key}", value)] + tail))
        cases.append(("calibrate", "target", [arg for key, value in here.items() for arg in (f"--{key}", value)] + ["--target", missing]))
        for module, option, args in cases:
            label = f"{module} --{option}"
            code, event = run(module, [*args, "--out", self.path("out")])
            self.assertEqual(code, 2, label)
            self.assertEqual(event["code"], "usage", label)
            self.assertNotIn("traceback", event, label)
            self.assertIn(f"--{option} does not exist", event["message"], label)
        code, event = run("pipeline", ["report", "--out", self.path("nowhere")])
        self.assertEqual(code, 2)
        self.assertEqual(event["code"], "usage")
        self.assertNotIn("traceback", event)
        self.assertIn("--out", event["message"])

    def test_sensitivity_overlap_out_of_range_is_refused(self):
        # An overlap outside 50-100 % (or nan) is a usage error: a rule of at least -1 % passes every voxel and would be
        # written as a calibrated 0 % false-positive rate.
        common = ["--maps", self.path("maps.tsv"), "--participants", self.path("participants.tsv"), "--mask", self.path("mask.nii.gz"), "--out", self.path("out"), "--no-fwer", "--no-planted", "--sensitivity"]
        for value in ("-1", "0", "49", "150", "nan"):
            code, events, _ = self.run_cli(*common, "--overlap", value)
            self.assertEqual(code, 2, value)
            self.assertEqual(events[-1]["event"], "error", value)
            self.assertEqual(events[-1]["code"], "usage", value)
            self.assertIn("--overlap", events[-1]["message"], value)

    def test_sensitivity_rule_is_validated_before_any_work(self):
        # A mistyped --rule is a usage error at the start, not after the empirical FWER stage has run.
        common = ["--maps", self.path("maps.tsv"), "--participants", self.path("participants.tsv"), "--mask", self.path("mask.nii.gz"), "--out", self.path("out"), "--sensitivity"]
        code, events, _ = self.run_cli(*common, "--rule", '{"kind": "fwe_bonferroni", "alpha": 0.05')
        self.assertEqual(code, 2)
        self.assertEqual(events[-1]["event"], "error")
        self.assertEqual(events[-1]["code"], "usage")
        self.assertIn("--rule", events[-1]["message"])
        self.assertEqual([e for e in events if e.get("event") in ("stage", "progress", "result")], [])

    def test_target_is_validated_before_any_work(self):
        # A --target on another grid, or not a NIfTI, is a usage error at the start, not after the empirical FWER stage.
        study = self.make_study(4, 4)
        Y = _maps(self.rng, 8, self.degree, self.networks)
        maps_tsv = self.write_maps(study, Y)
        common = ["--maps", maps_tsv, "--participants", os.path.join(study.path, "participants.tsv"), "--mask", self.path("mask.nii.gz"), "--out", self.path("out")]
        nifti.write_nifti(self.path("wrong.nii.gz"), np.ones((SHAPE[0] + 1, SHAPE[1], SHAPE[2]), np.uint8), AFFINE)
        for target in (self.path("wrong.nii.gz"), maps_tsv):
            code, events, _ = self.run_cli(*common, "--target", target)
            self.assertEqual(code, 2, target)
            self.assertEqual(events[-1]["event"], "error", target)
            self.assertEqual(events[-1]["code"], "usage", target)
            self.assertIn("target", events[-1]["message"], target)
            self.assertEqual([e for e in events if e.get("event") in ("stage", "progress", "result")], [], target)


if __name__ == "__main__":
    unittest.main()

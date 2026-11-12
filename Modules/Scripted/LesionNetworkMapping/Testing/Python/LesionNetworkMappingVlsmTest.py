"""Tests of vlsm.py: the minimum-overlap mask (exact counts, voxels lesioned in every lesion dropped), a planted voxel
effect detected with FWE (and nothing else), the classic VLSM t (score ~ lesion + volume) reproduced, lesion volume a
covariate by default (filled from the masks when participants.tsv has none; off only with a recorded reason), the output
names and portable records, clusters, re-seeding a cluster as a circuit (linked to the VLSM run and its lesions, so that
the same lesions' damage scores are same-cohort), and the command line.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingVlsmTest.py -v
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

from LesionNetworkMappingLib import circuits, connectome, jobs, lesionset, nifti, runs, vlsm
from LesionNetworkMappingLib import design as designlib
from LesionNetworkMappingLib.geometry import Grid
from LesionNetworkMappingLib.study import Study

SPACE = "MNI152NLin2009cAsym"
SHAPE = (16, 16, 16)
AFFINE = np.diag([2.0, 2.0, 2.0, 1.0])
AFFINE[:3, 3] = -15.0
PLANTED = (slice(11, 14), slice(11, 14), slice(11, 14))
FAR = (3, 3, 3)


def _quiet(*args, **kwargs):
    return None


def _absolute_strings(value):
    """The strings of a JSON value that are absolute local paths (shared records hold none, SPEC 9.7 rule 10)."""
    if isinstance(value, dict):
        return [item for child in value.values() for item in _absolute_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _absolute_strings(child)]
    return [value] if isinstance(value, str) and os.path.isabs(value) else []


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))


def _run_cli(*args):
    result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.vlsm", *args], check=False, capture_output=True, text=True, env=_env(), timeout=300)
    return result.returncode, [json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith("{")], result.stderr


def make_study(folder, n=40, seed=1, grid_shape=SHAPE, affine=AFFINE, planted=PLANTED, volume_column=False, space=SPACE):
    """A study whose lesions are random boxes of random size; subjects with a positive score also have a lesion in the
    planted box with probability 0.85 (else 0.1). Groups alternate case/control. Returns (study, scores).
    """
    rng = np.random.default_rng(seed)
    study = Study.create(folder, name="study", space=space)
    os.makedirs(os.path.join(folder, "lesions"), exist_ok=True)
    scores = []
    for index in range(n):
        score = float(rng.normal())
        volume = np.zeros(grid_shape, dtype=np.uint8)
        a, b, c = rng.integers(2, 9, 3)
        w = rng.integers(3, 7, 3)
        volume[a:a + w[0], b:b + w[1], c:c + w[2]] = 1
        if rng.random() < (0.85 if score > 0 else 0.1):
            volume[planted] = 1
        path = f"lesions/sub-{index:02d}_mask.nii.gz"
        nifti.write_nifti(os.path.join(folder, path), volume, affine)
        values = {"group": "case" if index % 2 else "control", "lesion": path, "score": score, "space": space}
        if volume_column:
            values["volume_ml"] = round(float(volume.sum()) * 8.0 / 1000.0, 4)
        study.add_subject(f"{index:02d}", values)
        scores.append(score)
    study.save()
    return study, scores


def score_spec(contrast="positive", **kwargs):
    return designlib.DesignSpec(test="continuous", score_column="score", contrast=contrast, group_column=None, **kwargs)


class MinimumOverlapTest(unittest.TestCase):
    def test_minimum_count_rule(self):
        self.assertEqual(vlsm.min_lesion_count(20), 5)  # max(5, 2)
        self.assertEqual(vlsm.min_lesion_count(100), 10)  # max(5, 10)
        self.assertEqual(vlsm.min_lesion_count(101), 11)  # 10% rounded up
        self.assertEqual(vlsm.min_lesion_count(100, min_lesions=3, min_percent=0), 3)
        self.assertEqual(vlsm.min_lesion_count(4, min_lesions=0, min_percent=0), 1)
        self.assertEqual(vlsm.min_lesion_rule(), "max(5, 10%)")
        self.assertEqual(vlsm.min_lesion_rule(3, 0), "3 lesions")
        self.assertEqual(vlsm.min_lesion_rule(1, 0), "1 lesion")

    def test_mask_is_exact(self):
        grid = Grid((10, 4, 4), np.diag([2.0, 2.0, 2.0, 1.0]))
        volumes = []
        for k in range(8):  # column i is covered by min(i, 8) lesions (box i >= 8 - k ... ), and i = 9 by all
            volume = np.zeros(grid.shape)
            volume[8 - k:10, 1:3, 1:3] = 1.0
            volume[9, :, :] = 1.0
            volumes.append(volume)
        occ = lesionset.LesionSet.from_volumes(volumes, grid, SPACE)
        expected = np.zeros(grid.shape, dtype=np.int64)
        for volume in volumes:
            expected += volume.astype(np.int64)
        matrix = vlsm.lesion_matrix(occ, min_count=5)
        np.testing.assert_array_equal(matrix.coverage, expected)
        tested = (expected >= 5) & (expected < 8)
        np.testing.assert_array_equal(matrix.tested_mask(), tested)
        self.assertEqual(matrix.n_voxels, int(tested.sum()))
        self.assertEqual(matrix.n_all_lesioned, int((expected == 8).sum()))  # no variance: not tested
        binary = np.stack([volume.reshape(-1) for volume in volumes])
        np.testing.assert_array_equal(matrix.Y, binary[:, matrix.indices].astype(np.float32))
        # Rows follow the given order; a fractional occupancy below 0.5 is not lesion
        reordered = vlsm.lesion_matrix(occ, sids=list(reversed(occ.sids)), min_count=5)
        np.testing.assert_array_equal(reordered.Y, matrix.Y[::-1])
        volumes[0][9, 3, 3] = 0.4
        occ = lesionset.LesionSet.from_volumes(volumes, grid, SPACE)
        self.assertEqual(int(vlsm.lesion_matrix(occ, min_count=1).coverage[9, 3, 3]), 7)


class VlsmRunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-vlsm-")
        cls.grid = Grid(SHAPE, AFFINE)
        cls.study, cls.scores = make_study(os.path.join(cls.tmp, "study"))
        cls.record = vlsm.vlsm(cls.study.path, score_spec(), grid=cls.grid, space=SPACE, n_perm=1000, keep_matrix=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _map(self, key):
        folder = os.path.dirname(self.record["record"])
        return nifti.read_nifti(os.path.join(folder, self.record["outputs"][key]))[0]

    def test_planted_effect_is_detected(self):
        p_fwe = self._map("p_fwe")
        t = self._map("stat")
        self.assertTrue(np.all(p_fwe[PLANTED] <= 0.05), p_fwe[PLANTED].max())
        self.assertTrue(np.all(t[PLANTED] > 0))
        outside = np.ones(SHAPE, dtype=bool)
        outside[PLANTED] = False
        self.assertFalse(np.any(p_fwe[outside] <= 0.05))  # nothing else survives FWE
        self.assertEqual(self.record["fwe"]["n_significant"], 27)
        self.assertEqual(float(p_fwe[FAR]), 1.0)  # untested voxels have p = 1
        table = vlsm.clusters(self.record)
        self.assertEqual(len(table), 1)
        self.assertEqual(table[0]["voxels"], 27)
        self.assertEqual(table[0]["sign"], "pos")
        self.assertEqual(self.record["clusters"]["table"][0]["voxels"], 27)

    def test_t_equals_classic_vlsm_regression(self):
        """With lesion presence as the outcome, the t of the score equals the t of the lesion in score ~ lesion + volume."""
        prepared = self.record["_prepared"]
        result = self.record["_result"].result
        design = prepared.design
        score = design.X[:, design.columns.index("score")]
        volume = design.X[:, design.columns.index("volume_ml")]
        for column in (0, prepared.matrix.n_voxels // 2, prepared.matrix.n_voxels - 1):
            lesion = prepared.matrix.Y[:, column].astype(np.float64)
            X = np.column_stack([np.ones(design.n), lesion, volume])
            beta, _residual, _rank, _sv = np.linalg.lstsq(X, score, rcond=None)
            df = design.n - X.shape[1]
            sigma2 = float(np.sum((score - X @ beta) ** 2)) / df
            t = beta[1] / np.sqrt(sigma2 * np.linalg.inv(X.T @ X)[1, 1])
            self.assertAlmostEqual(float(result.stat[column]), float(t), places=3)

    def test_outputs_are_named(self):
        folder = os.path.dirname(self.record["record"])
        self.assertEqual(os.path.basename(folder), "scorePos")
        self.assertEqual(os.path.dirname(folder), vlsm.vlsm_root(self.study.path))
        stem = "group_space-MNI152NLin2009cAsym_desc-vlsmScorePos"
        self.assertEqual(self.record["outputs"]["stat"], f"{stem}Tstat_map.nii.gz")
        self.assertEqual(self.record["outputs"]["p_fwe"], f"{stem}PFwe_map.nii.gz")
        self.assertEqual(self.record["outputs"]["p_fdr"], f"{stem}PFdr_map.nii.gz")
        self.assertEqual(self.record["outputs"]["effect"], f"{stem}PartialR_map.nii.gz")
        self.assertEqual(self.record["outputs"]["coverage"], "group_space-MNI152NLin2009cAsym_desc-vlsmCoverage_map.nii.gz")
        self.assertEqual(self.record["outputs"]["tested"], "group_space-MNI152NLin2009cAsym_desc-vlsmTested_mask.nii.gz")
        for name in self.record["outputs"].values():
            data, affine, _ = nifti.read_nifti(os.path.join(folder, name))
            self.assertEqual(data.shape, SHAPE)
            np.testing.assert_allclose(affine, AFFINE, atol=1e-5)
        coverage = self._map("coverage")
        self.assertEqual(int((coverage >= 5).sum() - (coverage == 40).sum()), self.record["n_voxels"])
        with open(self.record["record"], encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(_absolute_strings({key: value for key, value in stored.items() if key not in ("software", "versions")}), [])
        self.assertEqual(stored["format"], vlsm.FORMAT_NAME)
        self.assertEqual(vlsm.find_results(self.study.path), [folder])
        # lesions.tsv: the lesions behind the result by sha256, no paths
        with open(os.path.join(folder, vlsm.LESIONS_TSV), encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("lesions/", text)
        self.assertEqual(len(circuits.lesions_of_maps(os.path.join(folder, vlsm.LESIONS_TSV))["sha256"]), 40)
        registered = runs.load(self.study.path)[self.record["run_id"]]
        self.assertEqual((registered["kind"], registered["status"]), ("vlsm", "done"))

    def test_volume_covariate_on_by_default(self):
        prepared = self.record["_prepared"]
        self.assertIn(designlib.VOLUME_COLUMN, prepared.design.spec.covariates)
        self.assertIn(designlib.VOLUME_COLUMN, prepared.design.columns)
        self.assertTrue(self.record["volume_covariate"])
        self.assertEqual(self.record["volume_from_masks"], 40)  # participants.tsv has no volume_ml: filled from the masks
        volumes = prepared.design.X[:, prepared.design.columns.index(designlib.VOLUME_COLUMN)]
        expected = prepared.occ.volumes_ml[[prepared.occ.sids.index(sid) for sid in prepared.design.sids]]
        np.testing.assert_allclose(volumes, expected - expected.mean(), atol=1e-5)  # mean-centred
        self.assertIn(designlib.VOLUME_COLUMN, vlsm.prepare(self.study.path, None, grid=self.grid, space=SPACE).design.columns)  # the default spec too
        # Off without a reason is a warning; with a reason, an info check (both recorded in the design)
        off = vlsm.prepare(self.study.path, score_spec(covariates=[]), grid=self.grid, space=SPACE)
        self.assertIn("volume_off", [check.code for check in off.design.warnings])
        reason = vlsm.prepare(self.study.path, score_spec(covariates=[], volume_off_reason="matched volumes"), grid=self.grid, space=SPACE)
        self.assertNotIn("volume_off", [check.code for check in reason.design.warnings])
        self.assertEqual(reason.design.spec.volume_off_reason, "matched volumes")

    def test_methods_paragraph_describes_the_run(self):
        """The Statistics methods text of a VLSM run names the rule actually used (overlap rule, contrast, covariates,
        permutation method, corrections), the smallest observed FWE p rather than the attainable floor 1/n_perm, and
        describes VLSM as location, not as a network (bug bash: it was a dump of the registry summary keys).
        """
        from LesionNetworkMappingLib import methods

        study, _scores = make_study(os.path.join(self.tmp, "methods-study"), n=24, seed=5)
        spec = designlib.DesignSpec(test="continuous", score_column="score", contrast="negative", group_column=None, covariates=["volume_ml"])
        record = vlsm.vlsm(study.path, spec, grid=self.grid, space=SPACE, min_lesions=6, min_percent=0, n_perm=1000, seed=3)
        text = methods.render([methods.find_section(methods.methods_sections(study.path), "statistics")], "txt")
        self.assertIn("at least 6 lesions were tested", text)
        self.assertIn(f"{record['min_overlap']['n_voxels_tested']} voxels", text)
        self.assertIn(f"contrast {record['contrast']['label']} (negative)", text)
        self.assertIn("covariates: volume_ml (total lesion volume).", text)
        self.assertIn("Freedman-Lane", text)
        self.assertIn("1000 permutations, seed 3", text)
        self.assertIn("voxelwise max-statistic FWE (primary, alpha 0.05) and Benjamini-Hochberg FDR", text)
        observed = methods._number(record["fwe"]["min_p"])
        self.assertIn(f"the smallest observed FWE p was {observed} (the smallest attainable p is 0.001)", text)
        self.assertNotIn("min_p", text)
        self.assertNotIn("network associated with", text)
        self.assertIn("VLSM tests location, not the network", text)
        self.assertEqual(methods.lint_wording(text), [])
        # The registry summary names the attainable floor as such and keeps the observed FWE minimum
        summary = runs.load(study.path)[record["run_id"]]["summary"]
        self.assertNotIn("min_p", summary)
        self.assertEqual((summary["min_attainable_p"], summary["fwe_min_p"]), (record["min_attainable_p"], record["fwe"]["min_p"]))
        # Without vlsm.json (or a registry written before), the old summary "min_p" is called the attainable p
        legacy = methods.vlsm_summary_text({"n": 24, "n_voxels": 73, "n_perm": 1000, "fwe_significant": 0, "min_p": 0.001}, "vlsm-old")
        self.assertIn("smallest attainable p 0.001", legacy)
        self.assertNotIn("min_p", legacy)

    def test_share_export_describes_vlsm_maps(self):
        """The share-safe export and the NeuroVault metadata describe VLSM maps as lesion-location maps (not network
        maps), the coverage map as a lesion count, and record the sign coding (the "negative" score contrast flips the
        sign) and the df; vlsm.json records the sign coding too (bug bash: generic "Group lesion network map" text,
        SignCoding "not recorded", modality fMRI-BOLD).
        """
        from LesionNetworkMappingLib import export, share

        study, _scores = make_study(os.path.join(self.tmp, "share-study"), n=24, seed=5)
        spec = designlib.DesignSpec(test="continuous", score_column="score", contrast="negative", group_column=None, covariates=["volume_ml"])
        record = vlsm.vlsm(study.path, spec, grid=self.grid, space=SPACE, min_lesions=6, min_percent=0, n_perm=200, seed=3)
        self.assertIn("lesion at the voxel goes with a lower score (after the covariates)", record["sign_coding"])
        self.assertIn("flipped", record["sign_coding"])
        out = os.path.join(self.tmp, "share-out")
        share.export_dataset(study.path, out, {"licence": "CC0-1.0", "maps": True})
        desc = record["contrast"]["desc"]

        def sidecar(suffix):
            with open(os.path.join(out, "group", f"group_space-{SPACE}_desc-{suffix}_map.json"), encoding="utf-8") as stream:
                return json.load(stream)

        tstat = sidecar(desc + "Tstat")
        self.assertTrue(tstat["Description"].startswith("Voxel-based lesion-symptom map"), tstat)
        self.assertNotIn("network", tstat["Description"])
        self.assertEqual(tstat["Statistic"], "T")
        self.assertEqual(tstat["SignCoding"], record["sign_coding"])
        self.assertEqual(tstat["DegreesOfFreedom"], record["df"])
        self.assertEqual(tstat["NumberOfSubjects"], record["n"])
        self.assertIn("lesion goes with a higher score", sidecar(desc + "PartialR")["SignCoding"])
        coverage = sidecar("vlsmCoverage")
        self.assertIn("lesion count", coverage["Description"])
        self.assertNotIn("statistic;", coverage["Description"])
        self.assertNotEqual(coverage["SignCoding"], export.NOT_RECORDED)
        folder = os.path.dirname(record["record"])
        images = {image["name"]: image for image in export.neurovault_metadata([os.path.join(folder, name) for name in sorted(os.listdir(folder)) if name.endswith(".nii.gz")])["images"]}
        for name, image in images.items():
            self.assertEqual(image["modality"], "Other", name)
            self.assertNotIn("lesion network map", image["description"] or "", name)
            self.assertEqual(image["target_template_image"], "GenericMNI", name)
        self.assertIn("p-value", images[f"group_space-{SPACE}_desc-{desc}PFwe_map"]["description"])
        # A vlsm.json written before it recorded the sign coding: derived from its contrast; group contrasts name the levels
        legacy = {key: value for key, value in record.items() if key != "sign_coding"}
        self.assertEqual(vlsm.sign_coding(legacy), record["sign_coding"])
        groups = {"test": "two_group", "stat": "t", "contrast": {"spec": "B>A"}, "n_per_level": {"case": 20, "control": 20}}
        self.assertIn("lesioned more often in control than in case", vlsm.sign_coding(groups))

    def test_group_contrast_and_refusals(self):
        out = os.path.join(self.tmp, "groups")
        record = vlsm.vlsm(self.study.path, designlib.DesignSpec(levels=["case", "control"], contrast="A>B"), grid=self.grid, space=SPACE, n_perm=200, out_dir=out, register=False)
        self.assertEqual(record["outputs"]["stat"], "group_space-MNI152NLin2009cAsym_desc-vlsmCaseGtControlTstat_map.nii.gz")
        self.assertEqual(record["outputs"]["effect"], "group_space-MNI152NLin2009cAsym_desc-vlsmCaseGtControlHedgesG_map.nii.gz")
        self.assertTrue(record["permutation"]["preview"])
        self.assertEqual(record["n_per_level"], {"case": 20, "control": 20})
        with self.assertRaises(designlib.DesignError):
            vlsm.prepare(self.study.path, designlib.DesignSpec(test="one_sample", group_column=None), grid=self.grid, space=SPACE)
        with self.assertRaisesRegex(jobs.JobError, "No voxel is lesioned in at least 40"):
            vlsm.vlsm(self.study.path, score_spec(), grid=self.grid, space=SPACE, min_lesions=40, n_perm=200, out_dir=os.path.join(self.tmp, "none"), register=False)

    def test_stopped_or_failed_rerun_keeps_finished_result(self):
        """A rerun into the folder of a finished result that is stopped (or fails) during the permutations leaves that
        result whole: its maps, design.json, lesions.tsv and its vlsm.json (status done), and find_results still lists it.
        """
        study, _scores = make_study(os.path.join(self.tmp, "rerun-study"), seed=2)  # its own study: find_results of the class study stays one result
        out = vlsm.default_out_dir(study.path, vlsm.prepare(study.path, score_spec(), grid=self.grid, space=SPACE).design)  # the folder the VLSM tab reruns into
        first = vlsm.vlsm(study.path, score_spec(), grid=self.grid, space=SPACE, n_perm=200, out_dir=out, register=False)

        def snapshot():
            found = {}
            for name in sorted(os.listdir(out)):
                with open(os.path.join(out, name), "rb") as handle:
                    found[name] = handle.read()
            return found

        before = snapshot()
        self.assertTrue(all(name in before for name in first["outputs"].values()))
        token = jobs.CancelToken(signals=False)

        def emit(event, **fields):
            if event == "progress" and fields.get("stage") == "permutations":
                token.cancel("stopped")

        with self.assertRaises(jobs.Cancelled):
            vlsm.vlsm(study.path, score_spec(), grid=self.grid, space=SPACE, min_lesions=3, n_perm=5000, seed=4, out_dir=out, register=False, cancel=token, emit=emit,
                      batch=50)
        self.assertEqual(snapshot(), before)

        def failing_emit(event, **fields):
            if event == "progress" and fields.get("stage") == "permutations":
                raise RuntimeError("disk full")

        with self.assertRaises(RuntimeError):
            vlsm.vlsm(study.path, score_spec(), grid=self.grid, space=SPACE, min_lesions=3, n_perm=5000, seed=4, out_dir=out, register=False, emit=failing_emit, batch=50)
        self.assertEqual(snapshot(), before)
        self.assertEqual(vlsm.read_record(out)["status"], "done")
        self.assertIn(out, vlsm.find_results(study.path))
        # A rerun that finishes replaces the earlier result (no map of it is left next to the new one)
        again = vlsm.vlsm(study.path, score_spec(), grid=self.grid, space=SPACE, min_lesions=3, n_perm=200, seed=4, out_dir=out, register=False)
        self.assertEqual(again["min_overlap"]["min_lesions"], 3)
        self.assertEqual(sorted(os.listdir(out)), sorted([*again["outputs"].values(), vlsm.VLSM_JSON, again["design_json"], again["design_matrix"], again["lesions_tsv"]]))

    def test_command_line(self):
        out = os.path.join(self.tmp, "cli")
        design = score_spec().save(os.path.join(self.tmp, "design.json"))
        grid = os.path.join(self.tmp, "grid.nii.gz")
        nifti.write_nifti(grid, np.zeros(SHAPE, dtype=np.uint8), AFFINE)
        code, events, stderr = _run_cli("run", "--study", self.study.path, "--design", design, "--grid", grid, "--space", SPACE, "--out", out, "--perm", "200", "--threads", "1")
        self.assertEqual(code, 0, stderr + json.dumps(events[-1:]))
        self.assertEqual(events[0]["event"], "start")
        self.assertEqual(events[-1]["event"], "done")
        self.assertTrue(any(event["event"] == "progress" and event.get("stage") == "permutations" for event in events))
        with open(os.path.join(out, vlsm.VLSM_JSON), encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored["inputs"]["design"], "../design.json")
        self.assertIn("binary lesion masks", stored["inputs"]["outcome"])  # design.json's "input" (Fz/T) is not the outcome
        self.assertEqual(_absolute_strings(stored["argv"]), [])
        bad = designlib.DesignSpec(test="one_sample", group_column=None).save(os.path.join(self.tmp, "one.json"))
        code, events, _stderr = _run_cli("run", "--study", self.study.path, "--design", bad, "--grid", grid, "--space", SPACE, "--out", os.path.join(self.tmp, "bad"))
        self.assertEqual(code, jobs.EXIT_USAGE)
        self.assertEqual(events[-1]["code"], "design_blocked")

    def test_command_line_usage_errors_name_the_option(self):
        grid = os.path.join(self.tmp, "usage-grid.nii.gz")
        nifti.write_nifti(grid, np.zeros(SHAPE, dtype=np.uint8), AFFINE)
        out = os.path.join(self.tmp, "usage")
        cases = (
            (("--study", self.study.path, "--grid", grid, "--space", "Foo"), "Unknown --space Foo \\(known: MNI152NLin2009cAsym.*: no lesion of the study is in its family \\(space MNI152NLin2009cAsym is not in the unknown family of Foo: 40\\)"),
            (("--study", self.study.path, "--grid", grid, "--space", "MNI152NLin6Asym"), "No lesion of the study is in the family of --space MNI152NLin6Asym \\(space MNI152NLin2009cAsym is not in the fsl6 family"),
            (("--study", os.path.join(self.tmp, "no-such-study"), "--grid", grid, "--space", SPACE), "The --study folder does not exist"),
            (("--study", self.tmp, "--grid", grid, "--space", SPACE), "The --study folder is not a study folder"),
            (("--study", self.study.path, "--grid", grid), "Pass --space for this grid: The grid \\(16 x 16 x 16 voxels, 2x2x2 mm\\)"),
            (("--study", self.study.path, "--grid", os.path.join(self.tmp, "missing.nii.gz")), "The --grid .*missing.nii.gz does not exist \\(accepted: template2mm"),
            (("--study", self.study.path, "--grid", "bogus"), "The --grid bogus cannot be used: .*\\(accepted: template2mm, a grid id"),
            (("--study", self.study.path, "--grid", "user:no-such-template"), "The --grid user:no-such-template cannot be used"),
        )
        for arguments, pattern in cases:
            with self.subTest(arguments=arguments):
                code, events, stderr = _run_cli("run", *arguments, "--out", out, "--perm", "200")
                self.assertEqual(code, jobs.EXIT_USAGE, stderr)
                self.assertEqual(events[-1]["code"], "usage")
                self.assertRegex(events[-1]["message"], pattern)
                self.assertNotIn("Error:", events[-1]["message"])
                self.assertNotIn("Grid(", events[-1]["message"])

    def test_command_line_accepts_the_study_own_space_label(self):
        # A user's own template label (outside the known families) is a valid --space when the lesions carry it: the
        # label is not rejected up front, only one no lesion of the study can use is
        custom = "MyTemplate"
        study, _scores = make_study(os.path.join(self.tmp, "custom-study"), n=12, space=custom)
        grid = os.path.join(self.tmp, "custom-grid.nii.gz")
        nifti.write_nifti(grid, np.zeros(SHAPE, dtype=np.uint8), AFFINE)
        out = os.path.join(self.tmp, "custom")
        code, events, stderr = _run_cli("run", "--study", study.path, "--grid", grid, "--space", custom, "--out", out, "--perm", "50", "--min-lesions", "2", "--min-percent", "0", "--threads", "1")
        self.assertEqual(code, 0, stderr + json.dumps(events[-1:]))
        self.assertEqual(events[-1]["event"], "done")
        with open(os.path.join(out, vlsm.VLSM_JSON), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["space"], custom)
        # The same label on lesions that carry another one is the usage error, not an unexplained empty design
        code, events, _stderr = _run_cli("run", "--study", self.study.path, "--grid", grid, "--space", custom, "--out", os.path.join(self.tmp, "custom-bad"), "--perm", "50")
        self.assertEqual(code, jobs.EXIT_USAGE)
        self.assertEqual(events[-1]["code"], "usage")
        self.assertIn("Unknown --space MyTemplate", events[-1]["message"])


class SeedClusterTest(unittest.TestCase):
    """A VLSM on the grid of a synthetic connectome whose planted lesion region is blob A: its cluster seeds a circuit."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-vlsm-seed-")
        cls.meta = connectome.make_synthetic(os.path.join(cls.tmp, "syn.lnmc"), shape=(16, 18, 16), n_subjects=4, runs=(30,), seed=2, name="Syn")
        connectome.set_metadata(cls.meta["path"], space=SPACE)
        cls.bundle = connectome.ConnectomeBundle.open(cls.meta["path"])
        i, j, k = cls.meta["blobs"]["A"]["center_ijk"]
        planted = (slice(i - 1, i + 2), slice(j - 1, j + 2), slice(k - 1, k + 2))
        cls.study, _scores = make_study(os.path.join(cls.tmp, "study"), grid_shape=cls.bundle.shape, affine=cls.bundle.affine, planted=planted)
        cls.record = vlsm.vlsm(cls.study.path, score_spec(), grid=cls.meta["path"], n_perm=1000)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_seed_from_cluster(self):
        table = vlsm.clusters(self.record)
        self.assertGreaterEqual(len(table), 1)
        out = os.path.join(self.study.path, "derivatives", "lnm", "circuits", "circuit-VlsmA")
        circuit = vlsm.seed_cluster(self.record["record"], self.meta["path"], out, name="VlsmA", cluster=1, emit=_quiet)
        self.assertEqual(circuit["name"], "VlsmA")
        self.assertIn(self.record["run_id"], circuit["parents"])
        self.assertEqual(circuit["seed"]["voxels_source"], table[0]["voxels"])
        self.assertTrue(circuit["source"]["known"])
        self.assertEqual(len(circuit["source"]["lesion_sha256"]), 40)
        for key in ("T", "AvgFz", "AvgR"):
            self.assertTrue(os.path.isfile(circuit["maps"][key]), key)
        # The seeded network is blob A's: high T in its partner A2
        t = nifti.read_nifti(circuit["maps"]["T"])[0].reshape(-1)[self.bundle.mask_indices]
        self.assertGreater(t[self.meta["blobs"]["A2"]["mask"]].mean(), 3.0 * abs(np.median(t)))
        registered = runs.load(self.study.path)[circuit["run_id"]]
        self.assertEqual(registered["kind"], "circuits")
        self.assertIn(self.record["run_id"], registered["parents"])
        with open(os.path.join(out, vlsm.CLUSTER_JSON), encoding="utf-8") as handle:
            described = json.load(handle)
        self.assertEqual(described["cluster"]["id"], 1)
        self.assertEqual(_absolute_strings(described), [])
        # Damage scores of the same lesions against this circuit are same-cohort (double dipping is refused)
        scored = circuits.lesions_of_maps(os.path.join(os.path.dirname(self.record["record"]), vlsm.LESIONS_TSV))
        provenance = circuits.provenance_of(circuit["maps"]["T"], circuits._sha256(circuit["maps"]["T"]), scored, scored_study=self.study.path)
        self.assertEqual(provenance["provenance"], "same_cohort")

    def test_all_significant_voxels_and_errors(self):
        roi, _affine, info = vlsm.cluster_roi(self.record, "all", sign="pos")
        folder = os.path.dirname(self.record["record"])
        t = nifti.read_nifti(os.path.join(folder, self.record["outputs"]["stat"]))[0]
        p = nifti.read_nifti(os.path.join(folder, self.record["outputs"]["p_fwe"]))[0]
        np.testing.assert_array_equal(roi.astype(bool), (p <= 0.05) & (t > 0))
        self.assertGreater(int(roi.sum()), 0)
        self.assertEqual(info["id"], "all")
        with self.assertRaisesRegex(ValueError, "No cluster 99"):
            vlsm.cluster_roi(self.record, 99)
        with self.assertRaisesRegex(ValueError, "No voxel"):
            vlsm.cluster_roi(self.record, "all", sign="neg", alpha=1e-9)

    def test_seed_command_line(self):
        out = os.path.join(self.tmp, "cli-circuit")
        folder = os.path.dirname(self.record["record"])
        code, events, stderr = _run_cli("seed", "--vlsm", folder, "--bundle", self.meta["path"], "--out", out, "--cluster", "1", "--threads", "1")
        self.assertEqual(code, 0, stderr + json.dumps(events[-1:]))
        self.assertTrue(os.path.isfile(os.path.join(out, circuits.CIRCUIT_JSON)))
        self.assertEqual(events[-1]["event"], "done")
        code, events, _stderr = _run_cli("seed", "--vlsm", folder, "--bundle", self.meta["path"], "--out", out, "--cluster", "7")
        self.assertNotEqual(code, 0)
        self.assertIn("No cluster 7", events[-1].get("message", ""))


if __name__ == "__main__":
    unittest.main()

"""Tests of slicer-lnm, the headless twin of the lesion network mapping pipeline (app.py, L-47).

A synthetic BIDS dataset (the lesions of the A-04 pipeline fixture, named *_label-lesion_roi,
*_desc-lesion_mask and *_label-lesion_mask in the raw data and in derivatives) is analysed with the
BIDS-App levels (participant, then group), and the same lesions with the application's path (the
pipeline command line on the fixture's study, which Library ▸ Analysis and Map run). The checks:

- the participant level writes the per-lesion part of the application's file set, and its maps equal the
  in-process cohort maps (``cohortmaps.run``) within 1e-5;
- the group level reuses those maps and writes the application's file set; every map equals the
  application's within 1e-5; ``dataset_description.json`` has the application's fields;
- ``--participant-label``, a study folder as input, usage errors, ``--figures``;
- reproduce.sh runs with either entry point (``slicer-lnm engine`` on PATH, else PythonSlicer);
- the DMG wrapper of build_dmg.sh in folders with spaces and through a symbolic link;
- pyproject.toml and the container recipes (no data inside, pinned numpy and scipy).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingAppTest.py -v
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(MODULE_DIR)))
for folder in (MODULE_DIR, TEST_DIR):
    if folder not in sys.path:
        sys.path.insert(0, folder)

import numpy as np

import lnm_pipeline_fixture as fixture
from LesionNetworkMappingLib import app, cohortmaps, connectome, export, nifti, pipeline
from LesionNetworkMappingLib.study import Study, read_tsv

TOLERANCE = 1e-5
COMMON = ["--accept-terms", "--perm", "1000", "--events", "json", "--threads", "2"]
BIDS_OPTIONS = ["--group-map", "patient=case,HC=control", "--score-columns", "aphasia_score"]
SPACE = fixture.SPACE
#: folders of a run that hold job state, not results (compared separately or not at all)
PRIVATE_PREFIXES = ("stages/", ".pipeline/", "figures/")
DMG_SCRIPT = os.path.join(REPO_DIR, "Utilities", "Scripts", "SlicerStudioMacOS", "build_dmg.sh")
RECIPES_DIR = os.path.join(REPO_DIR, "Utilities", "SlicerStudioLNM")


def _env(**extra):
    env = dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""), SLICER_LNM_OFFLINE="1")
    env.pop(pipeline.PROVIDERS_ENV, None)
    for key, value in extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _events(text):
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def cli(*args, module="app", env=None, timeout=600):
    """Run ``python -m LesionNetworkMappingLib.<module>``; return (exit code, events, stdout, stderr)."""
    result = subprocess.run([sys.executable, "-m", f"LesionNetworkMappingLib.{module}", *args], cwd=MODULE_DIR, env=env or _env(), capture_output=True, text=True, encoding="utf-8",
                            timeout=timeout, check=False)
    return result.returncode, _events(result.stdout), result.stdout, result.stderr


def last_event(events):
    return next((event for event in reversed(events) if event["event"] in ("done", "error", "cancelled")), None)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def file_set(folder):
    """Relative paths ("/" separators) of the files under ``folder``."""
    out = set()
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            out.add(os.path.relpath(os.path.join(root, name), folder).replace(os.sep, "/"))
    return out


def tree_digest(folder):
    return {path: export.file_sha256(os.path.join(folder, path)) for path in file_set(folder)}


def volume(path):
    return np.asarray(nifti.read_nifti(path)[0], dtype=np.float64)


def make_bids(root, study_path):
    """A BIDS dataset with the lesions of ``study_path`` (three naming patterns) and decoys; return its folder."""
    study = Study.open(study_path)
    bids = os.path.join(root, "bids input")
    os.makedirs(bids)
    with open(os.path.join(bids, "dataset_description.json"), "w", encoding="utf-8") as f:
        json.dump({"Name": "synthetic lesions", "BIDSVersion": "1.9.0", "DatasetType": "raw"}, f)
    lines = ["participant_id\tgroup\taphasia_score\tage"]
    for index, row in enumerate(study.subjects):
        sid = row["participant_id"]
        source = study.abspath(row["lesion"])
        if index % 3 == 0:
            target = os.path.join(bids, "derivatives", "lesions", sid, "anat", f"{sid}_space-{SPACE}_label-lesion_roi.nii.gz")
        elif index % 3 == 1:
            target = os.path.join(bids, "derivatives", "lesions", sid, "ses-1", "anat", f"{sid}_ses-1_space-{SPACE}_desc-lesion_mask.nii.gz")
        else:
            target = os.path.join(bids, sid, "anat", f"{sid}_space-{SPACE}_label-lesion_mask.nii.gz")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(source, target)
        lines.append(f"{sid}\t{'patient' if row['group'] == 'case' else 'HC'}\t{row['aphasia_score']}\t{50 + index}")
    with open(os.path.join(bids, "participants.tsv"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    # Decoys: a brain mask (not a lesion) and a second, native-space lesion of sub-02 (the template one is used)
    decoy = os.path.join(bids, "derivatives", "fmriprep", "sub-01", "anat", "sub-01_desc-brain_mask.nii.gz")
    os.makedirs(os.path.dirname(decoy), exist_ok=True)
    shutil.copyfile(study.abspath(study.subject("sub-01")["lesion"]), decoy)
    native = os.path.join(bids, "sub-02", "anat", "sub-02_label-lesion_mask.nii.gz")
    os.makedirs(os.path.dirname(native), exist_ok=True)
    shutil.copyfile(study.abspath(study.subject("sub-02")["lesion"]), native)
    return bids


class AppHelpersTest(unittest.TestCase):
    """The pure helpers of app.py and the command-line usage errors (no pipeline run)."""

    def test_lesion_file_patterns(self):
        yes = ("sub-01_label-lesion_mask.nii.gz", "sub-01_ses-2_space-MNI152NLin2009cAsym_label-lesion_roi.nii", "sub-x1_desc-lesion_mask.nii.gz", "sub-01_space-MNI152NLin6Asym_desc-lesion_mask.nii")
        no = ("sub-01_desc-brain_mask.nii.gz", "sub-01_T1w.nii.gz", "sub-01_label-lesion_dseg.nii.gz", "sub-01_desc-lesion_roi.nii.gz", "lesion_mask.nii.gz", "sub-01_label-lesion_mask.json",
              "patient 7 lesion.nii.gz", "sub-01_label-CSF_mask.nii.gz")
        for name in yes:
            self.assertTrue(app.is_lesion_file(os.path.join("x", name)), name)
        for name in no:
            self.assertFalse(app.is_lesion_file(os.path.join("x", name)), name)

    def test_find_bids_lesions_prefers_template_space_and_skips_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = ["sub-01/anat/sub-01_label-lesion_mask.nii.gz", "derivatives/l/sub-01/anat/sub-01_space-MNI152NLin2009cAsym_label-lesion_mask.nii.gz",
                     "sub-02/anat/sub-02_desc-lesion_mask.nii", "out/conn-X/sub-03_label-lesion_mask.nii.gz", ".hidden/sub-04_label-lesion_mask.nii.gz"]
            for name in files:
                path = os.path.join(tmp, *name.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "wb").close()
            found = app.find_bids_lesions(tmp, skip=[os.path.join(tmp, "out")])
            self.assertEqual(sorted(found), ["sub-01", "sub-02"])
            self.assertIn("space-MNI152NLin2009cAsym", found["sub-01"][0])
            self.assertEqual(len(found["sub-01"]), 2)

    def test_labels_and_version(self):
        self.assertEqual(app.normalize_labels(["01", "sub-02", "03,04"]), ["sub-01", "sub-02", "sub-03", "sub-04"])
        self.assertIsNone(app.normalize_labels(None))
        self.assertEqual(app.build_version("abc/12_X"), f"{export.LIBRARY_VERSION}+fork.abc.12.x")
        with mock.patch.dict(os.environ, {"SLICER_FORK_REV": "1e1d8ad"}):
            self.assertEqual(app.build_version(), f"{export.LIBRARY_VERSION}+fork.1e1d8ad")
            self.assertEqual(app.__version__, f"{export.LIBRARY_VERSION}+fork.1e1d8ad")
        self.assertRegex(app.version(), r"^\d+\.\d+\+fork\.[a-z0-9.]+$")
        commands = app.library_commands()
        self.assertTrue({"engine", "pipeline", "cohortmaps", "glm", "slnm"} <= set(commands), commands)
        self.assertNotIn("app", commands)

    def test_design_options(self):
        from LesionNetworkMappingLib import design

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "design.json")
            design.DesignSpec(test="two_group", covariates=["volume_ml", "age"], inference={"n_perm": 2000, "seed": 7, "tfce": False, "fdr": True}).save(path)
            options = app.design_options(path)
            self.assertEqual((options["specificity"], options["perm"], options["perm_seed"], options["covariates"], options["input"]), ("glm", 2000, 7, ["volume_ml", "age"], "AvgFz"))
            design.DesignSpec(test="continuous", score_column="aphasia_score", input="T").save(path)
            options = app.design_options(path)
            self.assertEqual((options["scores"], options["input"]), (["aphasia_score"], "T"))
            design.DesignSpec(test="two_group", block_column="dataset", permute="within").save(path)
            with self.assertRaisesRegex(ValueError, "block_column"):
                app.design_options(path)

    def test_design_without_volume_names_a_slicer_lnm_remedy(self):
        """A design that leaves out volume_ml must not send slicer-lnm users to 'pipeline run --no-volume-covariate'."""
        from LesionNetworkMappingLib import design

        parser = app._parser()
        with tempfile.TemporaryDirectory() as tmp:
            base = ["in", "out", "group", "--connectome", "c", "--accept-terms"]
            path = os.path.join(tmp, "noage.json")
            design.DesignSpec(test="two_group", covariates=["age"]).save(path)
            with self.assertRaises(ValueError) as caught:
                app.build_config(parser.parse_args(base + ["--design", path]), tmp)
            self.assertIn("volume_off_reason", str(caught.exception))
            self.assertNotIn("--no-volume-covariate", str(caught.exception))
            design.DesignSpec(test="two_group", covariates=["age"], volume_off_reason="age-matched cohort").save(path)
            config = app.build_config(parser.parse_args(base + ["--design", path]), tmp)
            self.assertEqual((config.covariates, config.volume_off_reason), (["age"], "age-matched cohort"))

    def test_usage_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, events, _out, _err = cli(tmp, os.path.join(tmp, "out"), "group", "--connectome", tmp, "--subject-list", "a,b", "--events", "json")
            self.assertEqual(code, 2)
            self.assertIn("--subject-list", events[-1]["message"])
            code, events, _out, _err = cli(tmp, os.path.join(tmp, "out"), "group", "--connectome", tmp, "--figures", "glass,poster", "--events", "json")
            self.assertEqual(code, 2)
            self.assertIn("poster", events[-1]["message"])
            code, events, _out, _err = cli(tmp, os.path.join(tmp, "out"), "group", "--connectome", os.path.join(tmp, "missing.lnmc"), "--events", "json")
            self.assertEqual(code, 2)
            # An explicit file option that names nothing is a usage error, not a silent fallback (black report background)
            for option in ("--template", "--atlas", "--atlas-labels"):
                code, events, _out, _err = cli(tmp, os.path.join(tmp, "out"), "group", "--connectome", tmp, option, os.path.join(tmp, "nosuch.nii.gz"), "--events", "json")
                self.assertEqual(code, 2, option)
                self.assertIn(option, events[-1]["message"])
                self.assertIn("nosuch.nii.gz", events[-1]["message"])
            code, _events, _out, err = cli(tmp, os.path.join(tmp, "out"), "sideways", "--connectome", tmp)
            self.assertEqual(code, 2)
            code, _events, out, _err = cli("--version")
            self.assertEqual(code, 0)
            self.assertTrue(out.startswith("slicer-lnm "), out)
            code, _events, _out, err = cli("engine", "--help")
            self.assertEqual(code, 0)
            self.assertIn("--bundle", err)


class AppLevelsTest(unittest.TestCase):
    """The BIDS-App levels against the application's path on the same lesions and connectome."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-app-")
        cls.fx = fixture.make_fixture(os.path.join(cls.tmp, "f"), extra_rows=False)
        cls.bundle = connectome.ConnectomeBundle.open(cls.fx["bundle"])
        cls.label = cls.bundle.name
        cls.run_name = export.run_dir_name(cls.bundle.name)
        cls.bids = make_bids(cls.tmp, cls.fx["study"])
        cls.bids_before = tree_digest(cls.bids)
        cls.output = os.path.join(cls.tmp, "slicer lnm out")
        cls.out = os.path.join(cls.output, cls.run_name)
        cls.sids = cls.fx["sids"]
        level_args = [cls.bids, cls.output]
        # participant level, then a snapshot of its results
        cls.participant = cli(*level_args, "participant", "--connectome", cls.fx["bundle"], *COMMON, *BIDS_OPTIONS)
        cls.participant_files = file_set(cls.out) if os.path.isdir(cls.out) else set()
        cls.participant_json = read_json(os.path.join(cls.out, pipeline.PIPELINE_JSON)) if os.path.isfile(os.path.join(cls.out, pipeline.PIPELINE_JSON)) else {}
        cls.participant_maps = {}
        for sid in cls.sids:
            for key, path in export.map_paths(cls.out, sid, cls.bundle.name).items():
                if os.path.isfile(path):
                    cls.participant_maps[(sid, key)] = volume(path)
        # group level
        cls.group = cli(*level_args, "group", "--connectome", cls.fx["bundle"], *COMMON, *BIDS_OPTIONS, "--figures", "glass")
        cls.group_json = read_json(os.path.join(cls.out, pipeline.PIPELINE_JSON)) if os.path.isfile(os.path.join(cls.out, pipeline.PIPELINE_JSON)) else {}
        # the application's path: the pipeline command line on the study (Library ▸ Analysis, Map ▸ Run everything)
        cls.gui = cli("run", "--study", cls.fx["study"], "--bundle", cls.fx["bundle"], "--accept-terms", "--perm", "1000", "--events", "json", "--threads", "2", module="pipeline")
        cls.gui_out = cls.fx["out"]
        # the in-process cohort maps of the same lesion files
        study = Study.open(cls.fx["study"])
        lesions = [(sid, study.abspath(study.subject(sid)["lesion"])) for sid in cls.sids]
        cls.inprocess_out = os.path.join(cls.tmp, "inprocess")
        cohortmaps.run(lesions, cls.bundle, cls.inprocess_out, cache_root=os.path.join(cls.tmp, "inprocess-cache"), threads=2, register=False, emit=lambda *args, **kwargs: None)
        cls.inprocess = {row["participant_id"]: row for row in cohortmaps.read_maps_tsv(os.path.join(cls.inprocess_out, cohortmaps.MAPS_TSV))}
        # a study folder as the input: the application's study (its map cache is shared)
        cls.study_output = os.path.join(cls.tmp, "study input out")
        cls.study_run = cli(cls.fx["study"], cls.study_output, "group", "--connectome", cls.fx["bundle"], *COMMON)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def assertDone(self, result):
        code, events, _stdout, stderr = result
        self.assertEqual(code, 0, stderr[-2000:] + str(last_event(events)))

    def group_files(self, folder):
        return {path for path in file_set(folder) if not path.startswith(PRIVATE_PREFIXES)}

    def per_lesion_files(self):
        names = set()
        for sid in self.sids:
            names |= {os.path.basename(path) for path in export.map_paths(self.out, sid, self.bundle.name).values()}
            names.add(os.path.basename(export.sidecar_path(self.out, sid, self.bundle.name)))
            names.add(f"inputs/lesions/{sid}_desc-lesion_mask.nii.gz")
        return names

    # Participant level

    def test_participant_level_outputs(self):
        self.assertDone(self.participant)
        files = {path for path in self.participant_files if not path.startswith(PRIVATE_PREFIXES)}
        self.assertTrue(self.per_lesion_files() <= files, sorted(self.per_lesion_files() - files))
        for name in ("provenance.json", "methods.md", "reproduce.sh", "reproduce_pipeline.sh", "pipeline.json", "qa.json", "checklist.md"):
            self.assertIn(name, files)
        self.assertFalse([path for path in files if path.startswith("group_conn-")], "the participant level writes no group map")
        self.assertNotIn("report.html", files)
        stages = {stage["name"]: stage for stage in self.participant_json["stages"]}
        for name, _label in pipeline.STAGES:
            if name in app.PARTICIPANT_STAGES:
                self.assertIn(stages[name]["status"], ("done", "reused", "skipped"), stages[name])
                self.assertNotEqual(stages[name].get("reason"), app.DEFERRED)
            else:
                self.assertEqual((stages[name]["status"], stages[name]["reason"]), ("skipped", app.DEFERRED))
        self.assertEqual(stages["maps"]["status"], "done")
        self.assertEqual(self.participant_json["software"]["name"], "slicer-lnm")
        qa = read_json(os.path.join(self.out, "qa.json"))
        self.assertEqual(sorted(qa["lesions"]), self.sids)

    def test_participant_maps_equal_in_process_cohortmaps(self):
        keys = {"AvgR": "AvgR", "AvgFz": "AvgFz", "T": "T"}
        self.assertEqual(len(self.participant_maps), 3 * len(self.sids))
        for sid in self.sids:
            for key, column in keys.items():
                expected = volume(self.inprocess[sid][column])
                np.testing.assert_allclose(self.participant_maps[(sid, key)], expected, rtol=0, atol=TOLERANCE, err_msg=f"{sid} {key}")

    # Group level

    def test_group_level_reuses_participant_maps(self):
        self.assertDone(self.group)
        done = last_event(self.group[1])
        self.assertEqual((done["computed"], done["reused"]), (0, len(self.sids)))
        stages = {stage["name"]: stage for stage in self.group_json["stages"]}
        self.assertTrue(stages["maps"]["reused"], stages["maps"])
        for name, _label in pipeline.STAGES:
            self.assertNotEqual(stages[name].get("reason"), app.DEFERRED)
        for (sid, key), before in self.participant_maps.items():
            np.testing.assert_array_equal(volume(export.map_paths(self.out, sid, self.bundle.name)[key]), before)

    def test_group_file_set_equals_application(self):
        self.assertDone(self.group)
        self.assertDone(self.gui)
        app_files, gui_files = self.group_files(self.out), self.group_files(self.gui_out)
        self.assertEqual(app_files, gui_files)
        self.assertTrue(self.per_lesion_files() <= app_files)
        for name in ("report.html", "lesionset.json", "peaks.tsv", f"group_conn-{export.connectome_label(self.label)}_desc-meanT_map.nii.gz"):
            self.assertIn(name, app_files)
        # the same stages ran
        gui_json = read_json(os.path.join(self.gui_out, pipeline.PIPELINE_JSON))
        self.assertEqual({stage["name"]: stage["status"] for stage in self.group_json["stages"] if stage["name"] != "maps"},
                         {stage["name"]: stage["status"] for stage in gui_json["stages"] if stage["name"] != "maps"})

    def test_group_maps_equal_application_within_tolerance(self):
        self.assertDone(self.group)
        self.assertDone(self.gui)
        compared = 0
        for path in sorted(self.group_files(self.out)):
            if not path.endswith(".nii.gz") or path.startswith("inputs/"):
                continue
            ours, theirs = volume(os.path.join(self.out, path)), volume(os.path.join(self.gui_out, path))
            self.assertEqual(ours.shape, theirs.shape, path)
            np.testing.assert_allclose(ours, theirs, rtol=0, atol=TOLERANCE, err_msg=path)
            ours_header, theirs_header = nifti.read_nifti_header(os.path.join(self.out, path)), nifti.read_nifti_header(os.path.join(self.gui_out, path))
            np.testing.assert_array_equal(ours_header["affine"], theirs_header["affine"], err_msg=path)
            compared += 1
        self.assertGreater(compared, 3 * len(self.sids) + 10)

    def test_sidecars_and_provenance_match_application(self):
        for sid in self.sids[:3]:
            ours = read_json(export.sidecar_path(self.out, sid, self.bundle.name))
            theirs = read_json(export.sidecar_path(self.gui_out, sid, self.bundle.name))
            self.assertEqual(sorted(ours), sorted(theirs))
        ours, theirs = read_json(os.path.join(self.out, "provenance.json")), read_json(os.path.join(self.gui_out, "provenance.json"))
        self.assertEqual(sorted(ours), sorted(theirs))
        group_sidecars = sorted(path for path in self.group_files(self.out) if path.startswith("group_conn-") and path.endswith(".json"))
        self.assertTrue(group_sidecars)
        for path in group_sidecars:
            self.assertEqual(sorted(read_json(os.path.join(self.out, path))), sorted(read_json(os.path.join(self.gui_out, path))), path)

    def test_dataset_description(self):
        self.assertDone(self.group)
        ours = read_json(os.path.join(self.output, "dataset_description.json"))
        theirs = read_json(os.path.join(self.fx["study"], "derivatives", "lnm", "dataset_description.json"))
        self.assertEqual(ours, theirs)
        self.assertEqual(ours["DatasetType"], "derivative")
        self.assertEqual(ours["BIDSVersion"], "1.9.0")
        self.assertEqual(ours["GeneratedBy"][0]["Name"], pipeline.GENERATED_BY)
        self.assertEqual(ours["GeneratedBy"][0]["Version"], export.LIBRARY_VERSION)

    def test_bids_study_and_input_untouched(self):
        self.assertEqual(tree_digest(self.bids), self.bids_before)
        study_path = os.path.join(self.output, *app.STUDY_RELPATH)
        study = Study.open(study_path)
        self.assertEqual(study.subject_ids, self.sids)
        groups = {row["participant_id"]: row["group"] for row in study.subjects}
        gui = Study.open(self.fx["study"])
        self.assertEqual(groups, {row["participant_id"]: row["group"] for row in gui.subjects})
        self.assertIn(("aphasia_score", True), study.score_columns())
        self.assertEqual({row["space"] for row in study.subjects}, {SPACE})
        _columns, rows = read_tsv(os.path.join(study_path, "participants.tsv"))
        for row in rows:
            self.assertFalse(os.path.isabs(row["lesion"]), row["lesion"])
            self.assertNotIn("brain_mask", row["lesion"])
        self.assertIn("space-", {row["participant_id"]: row["lesion"] for row in rows}["sub-02"])
        skipped = [event for event in self.participant[1] if event["event"] == "warning" and event.get("code") == "bids_lesion_skipped"]
        self.assertEqual([event["participant_id"] for event in skipped], ["sub-02"])

    def test_no_absolute_paths_in_shared_files(self):
        script = os.path.join(REPO_DIR, "Utilities", "Scripts", "StudioPrivacyCheck.py")
        if not os.path.isfile(script):
            self.skipTest("StudioPrivacyCheck.py not found")
        result = subprocess.run([sys.executable, script, self.output], capture_output=True, text=True, check=False)
        failing = [line for line in result.stdout.splitlines() if line.startswith("FAIL") and "reproduce_pipeline.sh" not in line]
        self.assertEqual(failing, [], result.stdout[-3000:])

    def test_figures(self):
        self.assertDone(self.group)
        folder = os.path.join(self.out, "figures", "slicer-lnm-glass")
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.assertFalse(os.path.isdir(folder))
            return
        self.assertTrue(os.path.isfile(os.path.join(folder, "slicer-lnm-glass.png")), os.listdir(os.path.join(self.out, "figures")) if os.path.isdir(os.path.join(self.out, "figures")) else None)

    def test_study_folder_input_shares_the_application_cache(self):
        self.assertDone(self.study_run)
        done = last_event(self.study_run[1])
        self.assertEqual(done["computed"], 0)  # every map is in the study's cache (computed by the application's run)
        out = os.path.join(self.study_output, self.run_name)
        self.assertEqual(self.group_files(out), self.group_files(self.gui_out))
        self.assertTrue(os.path.isfile(os.path.join(self.study_output, "dataset_description.json")))
        self.assertFalse(os.path.isdir(os.path.join(self.study_output, "sourcedata")))

    def test_participant_label(self):
        output = os.path.join(self.tmp, "labels out")
        code, events, _stdout, stderr = cli(self.bids, output, "participant", "--connectome", self.fx["bundle"], "--participant-label", "02", "sub-05", *COMMON, *BIDS_OPTIONS)
        self.assertEqual(code, 0, stderr[-2000:] + str(last_event(events)))
        out = os.path.join(output, self.run_name)
        mapped = sorted(name.split("_")[0] for name in os.listdir(out) if name.endswith("_desc-T_funcmap.nii.gz"))
        self.assertEqual(mapped, ["sub-02", "sub-05"])
        rows = {row["participant_id"]: row for row in read_json(os.path.join(out, pipeline.PIPELINE_JSON))["inputs"]["rows"]}
        self.assertEqual({sid for sid, row in rows.items() if row["reason"] == app.NOT_SELECTED}, set(self.sids) - {"sub-02", "sub-05"})
        self.assertEqual(last_event(events)["computed"], 2)  # another output folder: another study and map cache
        code, events, _stdout, _stderr = cli(self.bids, output, "participant", "--connectome", self.fx["bundle"], "--participant-label", "99", *COMMON, *BIDS_OPTIONS)
        self.assertEqual(code, 2)
        self.assertIn("sub-99", events[-1]["message"])


class CorruptLesionMessagesTest(unittest.TestCase):
    """An unreadable lesion file is named and called unreadable (not "missing") by the engine and the BIDS-App."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-corrupt-")
        cls.fx = fixture.make_fixture(os.path.join(cls.tmp, "f"), n_cases=3, n_controls=3, extra_rows=False)
        cls.bundle = connectome.ConnectomeBundle.open(cls.fx["bundle"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def corrupt_file(self, name):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(b"hello\n")
        return path

    def test_prepare_lesion_names_an_unreadable_file(self):
        from LesionNetworkMappingLib import engine

        path = self.corrupt_file("not a nifti.nii.gz")
        with self.assertRaises(ValueError) as context:
            engine.prepare_lesion(path, self.bundle)
        message = str(context.exception)
        self.assertIn(path, message)
        self.assertIn("cannot be read", message)
        # a truncated gzip stream is an EOFError, not a bad header
        truncated = os.path.join(self.tmp, "truncated.nii.gz")
        study = Study.open(self.fx["study"])
        with open(study.abspath(study.subject("sub-01")["lesion"]), "rb") as f:
            blob = f.read()
        with open(truncated, "wb") as f:
            f.write(blob[: len(blob) // 2])
        with self.assertRaises(ValueError) as context:
            engine.prepare_lesion(truncated, self.bundle)
        self.assertIn(truncated, str(context.exception))

    def test_engine_error_event_names_the_file(self):
        path = self.corrupt_file("odd lesion.nii.gz")
        code, events, _stdout, _stderr = cli("--bundle", self.fx["bundle"], "--lesion", path, "--out", os.path.join(self.tmp, "engine out"), module="engine")
        self.assertEqual(code, 1)
        self.assertIn(path, last_event(events)["message"])

    def test_bids_app_reports_an_unreadable_lesion_as_unreadable(self):
        bids = make_bids(self.tmp, self.fx["study"])
        target = os.path.join(bids, "sub-03", "anat", f"sub-03_space-{SPACE}_label-lesion_mask.nii.gz")
        if not os.path.isfile(target):
            target = next(path for path in app.find_bids_lesions(bids)["sub-03"])
        with open(target, "wb") as f:
            f.write(b"hello\n")
        events = []
        emit = lambda kind, **fields: events.append({"event": kind, **fields})  # noqa: E731
        study_path, _summary = app.prepare_bids_study(bids, os.path.join(self.tmp, "bids out"), group_map="patient=case,HC=control", emit=emit)
        warnings = [event for event in events if event["event"] == "warning" and event.get("code") == "bids_lesion_unreadable"]
        self.assertEqual([event["participant_id"] for event in warnings], ["sub-03"])
        self.assertIn(os.path.basename(target), warnings[0]["message"])
        self.assertNotIn(self.tmp, warnings[0]["message"])
        study = Study.open(study_path)
        self.assertTrue(study.subject("sub-03")["lesion"].endswith(os.path.basename(target)))
        config = pipeline.PipelineConfig(study=study.path, bundle=self.fx["bundle"], accept_terms=True, nulls="off")
        excluded = pipeline._builtin_row_checks(study, config)
        self.assertEqual(excluded["sub-03"], "lesion file unreadable")
        self.assertNotIn("sub-01", excluded)


class ReproduceEntryPointTest(unittest.TestCase):
    """reproduce.sh runs ``slicer-lnm engine`` when slicer-lnm is on PATH, else PythonSlicer (export.reproduce_script)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-app-reproduce ")
        syn = connectome.make_synthetic(os.path.join(cls.tmp, "Synthetic connectome.lnmc"), shape=(12, 14, 12), n_subjects=6, runs=(20,), seed=2)
        bundle = connectome.ConnectomeBundle.open(syn["path"])
        mask = np.zeros(bundle.grid.shape, np.uint8)
        mask[5:8, 6:9, 5:8] = 1
        cls.lesion = os.path.join(cls.tmp, "lesions", "sub-01_desc-lesion_mask.nii.gz")
        os.makedirs(os.path.dirname(cls.lesion))
        nifti.write_nifti(cls.lesion, mask, bundle.grid.affine)
        run_dir = os.path.join(cls.tmp, "run")
        code, _events, _out, err = cli("--bundle", syn["path"], "--lesion", cls.lesion, "--out", run_dir, "--run-json", os.path.join(run_dir, "run.json"), "--threads", "1", module="engine")
        assert code == 0, err
        cls.run_dir = run_dir
        cls.files = export.export_run(run_dir, os.path.join(cls.tmp, "export"))
        cls.script = cls.files["reproduce"]
        cls.original = {os.path.basename(path): volume(path) for path in cls.files["maps"]}
        # stand-ins that record their arguments and run the real entry points
        cls.bin_lnm, cls.bin_python = os.path.join(cls.tmp, "bin lnm"), os.path.join(cls.tmp, "bin python")
        for folder, name, command in ((cls.bin_lnm, "slicer-lnm", f'"{sys.executable}" -m LesionNetworkMappingLib.app'), (cls.bin_python, "PythonSlicer", f'"{sys.executable}"')):
            os.makedirs(folder)
            path = os.path.join(folder, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f'#!/bin/sh\necho "$@" > "{path}.log"\nPYTHONPATH="{MODULE_DIR}${{PYTHONPATH:+:$PYTHONPATH}}" exec {command} "$@"\n')
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if sys.platform == "win32" or shutil.which("sh") is None:
            self.skipTest("needs a POSIX shell")
        for path in (os.path.join(self.bin_lnm, "slicer-lnm.log"), os.path.join(self.bin_python, "PythonSlicer.log")):
            if os.path.exists(path):
                os.remove(path)

    def reproduce(self, name, **env):
        out = os.path.join(self.tmp, name)
        environment = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONSLICER", "SLICER_LNM")}
        environment.update({"OUT": out, "SLICER_LNM_OFFLINE": "1", **env})
        result = subprocess.run([shutil.which("sh"), self.script], env=environment, capture_output=True, text=True, timeout=300, check=False)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        for name_, original in self.original.items():
            np.testing.assert_allclose(volume(os.path.join(out, name_)), original, rtol=0, atol=TOLERANCE, err_msg=name_)
        return result

    def logged(self):
        out = {}
        for folder, name in ((self.bin_lnm, "slicer-lnm"), (self.bin_python, "PythonSlicer")):
            path = os.path.join(folder, f"{name}.log")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    out[name] = f.read().split()
        return out

    def test_script_documents_both_entry_points(self):
        with open(self.script, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("command -v slicer-lnm", text)
        self.assertIn("-m LesionNetworkMappingLib.engine", text)
        self.assertIn("PYTHONSLICER", text)
        self.assertIn(export.DISCLAIMER, text)

    def test_slicer_lnm_on_path(self):
        result = self.reproduce("with slicer-lnm", PATH=os.pathsep.join((self.bin_lnm, self.bin_python)))
        used = self.logged()
        self.assertEqual(list(used), ["slicer-lnm"])
        self.assertEqual(used["slicer-lnm"][0], "engine")
        self.assertIn("slicer-lnm engine", result.stderr)

    def test_pythonslicer_without_slicer_lnm(self):
        self.reproduce("with pythonslicer", PATH=self.bin_python)
        used = self.logged()
        self.assertEqual(list(used), ["PythonSlicer"])
        self.assertEqual(used["PythonSlicer"][:2], ["-m", "LesionNetworkMappingLib.engine"])

    def test_pythonslicer_variable_wins(self):
        self.reproduce("forced pythonslicer", PATH=os.pathsep.join((self.bin_lnm, self.bin_python)), PYTHONSLICER=os.path.join(self.bin_python, "PythonSlicer"))
        self.assertEqual(list(self.logged()), ["PythonSlicer"])

    def test_slicer_lnm_variable(self):
        self.reproduce("named slicer-lnm", PATH=self.bin_python, SLICER_LNM=os.path.join(self.bin_lnm, "slicer-lnm"))
        self.assertEqual(list(self.logged()), ["slicer-lnm"])

    def test_writer_commands_without_path(self):
        """Neither command on PATH (a venv that is not activated, a Studio export): the commands that wrote the script run."""
        system_path = os.pathsep.join(path for path in ("/usr/bin", "/bin") if os.path.isdir(path))
        cases = (("baked slicer-lnm", os.path.join(self.bin_lnm, "slicer-lnm"), "slicer-lnm"), ("baked pythonslicer", "", "PythonSlicer"))
        for name, slicer_lnm, expected in cases:
            with self.subTest(name):
                for path in (os.path.join(self.bin_lnm, "slicer-lnm.log"), os.path.join(self.bin_python, "PythonSlicer.log")):
                    if os.path.exists(path):
                        os.remove(path)
                with (
                    mock.patch.object(pipeline, "_slicer_lnm_executable", return_value=slicer_lnm),
                    mock.patch.object(pipeline, "_python_executable", return_value=os.path.join(self.bin_python, "PythonSlicer")),
                    mock.patch.object(export.shutil, "which", return_value=None),
                ):
                    script = export.export_run(self.run_dir, os.path.join(self.tmp, f"export {name}"))["reproduce"]
                environment = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONSLICER", "SLICER_LNM")}
                environment.update({"OUT": os.path.join(self.tmp, name), "SLICER_LNM_OFFLINE": "1", "PATH": system_path})
                result = subprocess.run([shutil.which("sh"), script], env=environment, capture_output=True, text=True, timeout=300, check=False)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertEqual(list(self.logged()), [expected])
                for name_, original in self.original.items():
                    np.testing.assert_allclose(volume(os.path.join(self.tmp, name, name_)), original, rtol=0, atol=TOLERANCE, err_msg=name_)


class PackagingTest(unittest.TestCase):
    """pyproject.toml, the container recipes and the DMG wrapper of build_dmg.sh."""

    def test_pyproject(self):
        import tomllib

        with open(os.path.join(MODULE_DIR, "pyproject.toml"), "rb") as f:
            data = tomllib.load(f)
        project = data["project"]
        self.assertEqual(project["name"], "slicer-lnm")
        self.assertEqual(project["scripts"], {"slicer-lnm": "LesionNetworkMappingLib.app:main"})
        self.assertEqual(sorted(re.split(r"[<>=]", item)[0] for item in project["dependencies"]), ["numpy", "scipy"])
        self.assertEqual(sorted(project["optional-dependencies"]), ["all", "pillow", "simpleitk"])
        self.assertEqual(data["tool"]["setuptools"]["packages"], ["LesionNetworkMappingLib"])
        self.assertEqual(sorted(data["tool"]["setuptools"]["package-data"]["LesionNetworkMappingLib"]), ["catalog/*.json", "recipes/*.json"])
        self.assertEqual(data["tool"]["setuptools"]["dynamic"]["version"], {"attr": "LesionNetworkMappingLib.app.__version__"})

    def test_container_recipes_hold_no_data(self):
        with open(os.path.join(RECIPES_DIR, "Dockerfile"), encoding="utf-8") as f:
            docker = f.read()
        with open(os.path.join(RECIPES_DIR, "lnm.def"), encoding="utf-8") as f:
            apptainer = f.read()
        copies = [line.split()[1:] for line in docker.splitlines() if line.startswith("COPY ")]
        self.assertEqual(copies, [["pyproject.toml", "./"], ["LesionNetworkMappingLib", "./LesionNetworkMappingLib"], ["--from=wheel", "/dist/", "/tmp/dist/"]])
        files = apptainer.split("\n%files\n", 1)[1].split("\n%post\n", 1)[0]
        sources = [line.split()[0] for line in files.splitlines() if line.strip() and not line.strip().startswith("#")]
        self.assertEqual(sources, ["pyproject.toml", "LesionNetworkMappingLib"])
        for text in (docker, apptainer):
            self.assertNotRegex(text, r"(?i)(curl|wget|\.lnmc[^ :]|\.nii)")  # nothing is downloaded or copied besides the library
            self.assertIn("2.4.6", text)
            self.assertIn("1.17.1", text)
            self.assertIn("slicer-lnm", text)
        self.assertIn('ENTRYPOINT ["slicer-lnm"]', docker)
        if os.path.basename(sys.executable).lower().startswith("python") and "python-install" in sys.executable:
            import scipy

            # the pinned versions are those of the PythonSlicer that runs this test
            self.assertEqual((np.__version__, scipy.__version__), ("2.4.6", "1.17.1"))

    def test_dmg_wrapper_with_spaces_and_symlink(self):
        bash = shutil.which("bash")
        if bash is None or sys.platform == "win32":
            self.skipTest("needs bash")
        result = subprocess.run([bash, "-n", DMG_SCRIPT], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tempfile.TemporaryDirectory(prefix="lnm app ") as tmp:
            app_dir = os.path.join(tmp, "Applications folder", "Slicer Studio.app")
            contents = os.path.join(app_dir, "Contents")
            library = os.path.join(contents, "lib", "Slicer-5.13", "qt-scripted-modules")
            shutil.copytree(os.path.join(MODULE_DIR, "LesionNetworkMappingLib"), os.path.join(library, "LesionNetworkMappingLib"), ignore=shutil.ignore_patterns("__pycache__"))
            # a marker version: the wrapper must run this copy of the library, not the source tree
            marker = os.path.join(library, "LesionNetworkMappingLib", "export.py")
            with open(marker, encoding="utf-8") as f:
                text = f.read()
            with open(marker, "w", encoding="utf-8") as f:
                f.write(text.replace(f'LIBRARY_VERSION = "{export.LIBRARY_VERSION}"', 'LIBRARY_VERSION = "9.9"'))
            os.makedirs(os.path.join(contents, "bin"))
            python = os.path.join(contents, "bin", "PythonSlicer")
            with open(python, "w", encoding="utf-8") as f:
                f.write(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
            os.chmod(python, 0o755)
            command = f'source "{DMG_SCRIPT}"; DRY_RUN=0; SOURCE_DIR="{REPO_DIR}"; install_lnm_wrapper "$1"'
            result = subprocess.run([bash, "-c", command, "test", app_dir], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            wrapper = os.path.join(contents, "bin", "slicer-lnm")
            self.assertTrue(os.access(wrapper, os.X_OK))
            with open(wrapper, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("lib/Slicer-5.13/qt-scripted-modules", text)
            self.assertNotIn(tmp, text)  # the wrapper finds the application from its own location
            links = os.path.join(tmp, "my bin")
            os.makedirs(links)
            os.symlink(wrapper, os.path.join(links, "slicer-lnm"))
            env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            for entry in (wrapper, os.path.join(links, "slicer-lnm")):
                result = subprocess.run([entry, "--version"], capture_output=True, text=True, env=env, cwd=tmp, check=False, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertRegex(result.stdout, r"^slicer-lnm 9\.9\+fork\.[a-z0-9.]+")
            # library jobs through the wrapper
            result = subprocess.run([os.path.join(links, "slicer-lnm"), "engine", "--help"], capture_output=True, text=True, env=env, cwd=tmp, check=False, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--bundle", result.stderr)


if __name__ == "__main__":
    unittest.main()

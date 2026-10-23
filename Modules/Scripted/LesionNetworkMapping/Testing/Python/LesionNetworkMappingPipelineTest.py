"""Tests of the automated cohort pipeline (pipeline.py, A-04) on a synthetic study and connectome.

Every acceptance item of A-04 is checked: the outputs of a full run (per-lesion maps copied from the
cache, group maps in the connectome's exact header, sidecars, methods, reproduce scripts, the run
registry), the numerical equivalence with direct calls of the stage jobs, resume (an unchanged rerun
starts no process; one modified lesion recomputes one map), batching, cancellation (STOP during the maps
and during the GLM, the --parent-pid watchdog, a terminated PythonSlicer launcher), the data use terms,
the output lock, native-space rows, the study files (never written by a run), ``init``, the events and
the code rules. ``BaselinePipelineTest`` runs the core checks again with LNM_PIPELINE_PROVIDERS=baseline
(the wave-1 fallbacks).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingPipelineTest.py -v
"""

import glob
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
for folder in (MODULE_DIR, TEST_DIR):
    if folder not in sys.path:
        sys.path.insert(0, folder)

import numpy as np

import lnm_pipeline_fixture as fixture
from LesionNetworkMappingLib import cohort, cohortmaps, connectome, export, jobs, lesionset, nifti, pipeline, preflight, runs, thresholds
from LesionNetworkMappingLib.study import Study

FAST = ["--nulls", "off"]  # the null models take most of a synthetic run; scenario runs leave them out
HEADER_FIELDS = ("qform_code", "sform_code", "quatern_b", "quatern_c", "quatern_d", "qoffset_x", "qoffset_y", "qoffset_z", "srow_x", "srow_y", "srow_z", "pixdim")


def _env(**extra):
    env = dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""), SLICER_LNM_OFFLINE="1")
    env.update({key: value for key, value in extra.items() if value is not None})
    for key, value in extra.items():
        if value is None:
            env.pop(key, None)
    return env


def _events(text):
    out = []
    for line in text.splitlines():
        if line.startswith("{"):
            out.append(json.loads(line))
    return out


def cli(*args, env=None, timeout=600, on_event=None, launcher=None):
    """Run the pipeline command line; return (exit code, events, stdout, stderr, wall seconds)."""
    command = [launcher or sys.executable, "-m", "LesionNetworkMappingLib.pipeline", *args]
    started = time.monotonic()
    if on_event is None:
        result = subprocess.run(command, cwd=MODULE_DIR, env=env or _env(), capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        return result.returncode, _events(result.stdout), result.stdout, result.stderr, time.monotonic() - started
    process = subprocess.Popen(command, cwd=MODULE_DIR, env=env or _env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    events, lines = [], []
    stderr = []
    reader = threading.Thread(target=lambda: stderr.append(process.stderr.read()), daemon=True)
    reader.start()
    for line in process.stdout:
        lines.append(line)
        if line.startswith("{"):
            event = json.loads(line)
            event["_wall"] = time.monotonic() - started
            events.append(event)
            on_event(event, process)
    code = process.wait(timeout=timeout)
    reader.join(5)
    return code, events, "".join(lines), "".join(stderr), time.monotonic() - started


def run_args(fx, *extra):
    return ["run", "--study", fx["study"], "--bundle", fx["bundle"], "--accept-terms", "--perm", "1000", "--events", "json", *extra]


def done_event(events):
    return next((event for event in reversed(events) if event["event"] in ("done", "error", "cancelled")), None)


def stage_status(events):
    return {event["name"]: event["status"] for event in events if event["event"] == "stage"}


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def masked(path, bundle):
    data, _affine, _header = nifti.read_nifti(path)
    return np.asarray(data, dtype=np.float64).reshape(-1)[bundle.mask_indices]


def sha256(path):
    return export.file_sha256(path)


def tree_digest(folder):
    out = {}
    for root, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(root, name)
            out[os.path.relpath(path, folder)] = sha256(path)
    return out


def run_in_process(fx, *extra, baseline=False):
    """Run the pipeline in this process (with a Popen counter); return (code, events, Popen calls, seconds)."""
    parser = pipeline._parser()
    args = parser.parse_args(run_args(fx, *extra))
    config = pipeline.config_from_args(args)
    events = []
    calls = []
    real = subprocess.Popen

    def counting(*a, **k):
        calls.append(a[0] if a else k.get("args"))
        return real(*a, **k)

    started = time.monotonic()
    environment = {pipeline.PROVIDERS_ENV: "baseline"} if baseline else {}
    with mock.patch("subprocess.Popen", side_effect=counting), mock.patch.dict(os.environ, environment):
        if not baseline:
            os.environ.pop(pipeline.PROVIDERS_ENV, None)
        code = pipeline.run(config, emit=pipeline.Emitter("json", sink=lambda event, record: events.append(record)))
    return code, events, calls, time.monotonic() - started


ENV_BASELINE = os.environ.get(pipeline.PROVIDERS_ENV, "").strip().lower() == "baseline"


class PipelineChecks:
    """Checks of one full run (``setUpClass``), shared by the preferred and the baseline suites."""

    baseline = ENV_BASELINE

    @classmethod
    def env(cls, **extra):
        if cls.baseline:
            extra.setdefault(pipeline.PROVIDERS_ENV, "baseline")
        return _env(**extra)

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-pipeline-")
        cls.fx = fixture.make_fixture(os.path.join(cls.tmp, "f1"))
        cls.bundle = connectome.ConnectomeBundle.open(cls.fx["bundle"])
        cls.before_tsv = sha256(os.path.join(cls.fx["study"], "participants.tsv"))
        cls.before_lesions = tree_digest(os.path.join(cls.fx["study"], "lesions"))
        cls.progress_samples = []
        stop = threading.Event()
        progress_path = os.path.join(cls.fx["out"], pipeline.PRIVATE_DIR, pipeline.PROGRESS_JSON)

        def poll():
            while not stop.is_set():
                if os.path.exists(progress_path):
                    try:
                        with open(progress_path, encoding="utf-8") as f:
                            cls.progress_samples.append(json.load(f))
                    except ValueError:
                        cls.progress_samples.append(None)
                    except OSError:
                        pass
                time.sleep(0.03)

        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        cls.code, cls.events, cls.stdout, cls.stderr, cls.wall = cli(*run_args(cls.fx), env=cls.env())
        stop.set()
        poller.join(2)
        cls.out = cls.fx["out"]
        cls.pipeline_json = read_json(os.path.join(cls.out, pipeline.PIPELINE_JSON)) if os.path.isfile(os.path.join(cls.out, pipeline.PIPELINE_JSON)) else None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def mapped_sids(self):
        return [f"sub-{index:02d}" for index in range(1, 13)]

    def group_path(self, desc, out=None):
        return export.group_map_path(out or self.out, self.bundle.name, desc)

    # Synthetic fixture run

    def test_full_run(self):
        self.assertEqual(self.code, 0, self.stderr[-2000:] + str(done_event(self.events)))
        self.assertLessEqual(self.wall, 30 if self.baseline else 60)
        data = self.pipeline_json
        self.assertEqual(data["status"], "done")
        names = [stage["name"] for stage in data["stages"]]
        self.assertEqual(names, [name for name, _ in pipeline.STAGES])
        for stage in data["stages"]:
            self.assertIn("provider", stage)
            self.assertIn(stage["status"], ("done", "reused", "skipped", "unavailable"), stage)
        self.assertEqual(data["disclaimer"], export.DISCLAIMER)

    # Per-lesion outputs

    def test_per_lesion_outputs(self):
        maps = sorted(glob.glob(os.path.join(self.out, "*_funcmap.nii.gz")))
        sids = sorted({os.path.basename(path).split("_conn-")[0] for path in maps})
        self.assertEqual(sids, self.mapped_sids())
        tsv = cohortmaps.read_maps_tsv(os.path.join(self.out, "stages", "cohortmaps" if not self.baseline else "maps", "maps.tsv"))
        cache = {row["participant_id"]: row for row in tsv}
        for sid in sids:
            paths = export.map_paths(self.out, sid, self.bundle.name)
            for key, path in paths.items():
                self.assertTrue(os.path.isfile(path), path)
                self.assertEqual(os.stat(path).st_nlink, 1, "D-A21: independent copies, never hard links")
                self.assertEqual(sha256(path), sha256(cache[sid][key]))
            self.assertTrue(os.path.isfile(export.sidecar_path(self.out, sid, self.bundle.name)))
        # The case with the foreign file name is exported under its participant id
        sidecar = read_json(export.sidecar_path(self.out, fixture.FOREIGN, self.bundle.name))
        self.assertEqual(sidecar["pipeline"]["source"], "raw scans/patient 7 lesion.nii.gz")
        self.assertEqual(sidecar["pipeline"]["export_link_mode"], "copy")
        # The 5 excluded rows are listed with their reasons, in pipeline.json and in warning events
        rows = {row["participant_id"]: row for row in self.pipeline_json["inputs"]["rows"]}
        excluded = {sid: row["reason"] for sid, row in rows.items() if row["status"] == "excluded"}
        self.assertEqual(sorted(excluded), ["sub-13", "sub-14", "sub-15", "sub-16", "sub-17"])
        self.assertEqual(excluded["sub-14"], "qc=fail")
        warned = {event.get("participant_id") for event in self.events if event["event"] == "warning" and event.get("code") == "excluded"}
        self.assertTrue(set(excluded) <= warned, warned)

    # Run-level files

    def test_run_level_files(self):
        out = self.out
        for name in ("provenance.json", "methods.md", "reproduce.sh", "reproduce_pipeline.sh", "pipeline.json", "peaks.tsv", "qa.json"):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
        with open(os.path.join(out, "methods.md"), encoding="utf-8") as f:
            methods_text = f.read()
        self.assertIn(export.DISCLAIMER, methods_text)
        from LesionNetworkMappingLib import methods

        self.assertEqual(methods.lint_wording(methods_text), [])
        if not self.baseline:
            self.assertTrue(os.path.isfile(os.path.join(out, "checklist.md")))
            self.assertTrue(glob.glob(os.path.join(out, "clusters_*.tsv")))
        for name in ("reproduce.sh", "reproduce_pipeline.sh"):
            self.assertEqual(os.stat(os.path.join(out, name)).st_mode & 0o777, 0o755)
        with open(os.path.join(out, "reproduce_pipeline.sh"), "rb") as f:
            script = f.read()
        self.assertNotIn(b"\r\n", script)
        self.assertIn(b"${ACCEPT_TERMS:+--accept-terms}", script)
        description = read_json(os.path.join(self.fx["study"], "derivatives", "lnm", "dataset_description.json"))
        self.assertEqual(description["DatasetType"], "derivative")
        self.assertEqual(description["GeneratedBy"][0]["Name"], pipeline.GENERATED_BY)
        # Snapshots equal the planned sha256
        plan = read_json(os.path.join(out, pipeline.PRIVATE_DIR, pipeline.PLAN_JSON))
        for row in plan["rows"]:
            if row["status"] in ("compute", "reuse"):
                snapshot = os.path.join(out, "inputs", "lesions", os.path.basename(row["snapshot"]))
                self.assertEqual(sha256(snapshot), row["sha256"])
                self.assertEqual(sha256(snapshot), sha256(Study.open(self.fx["study"]).abspath(row["lesion"])))
        # Every group map has its sidecar
        groups = glob.glob(os.path.join(out, "group_conn-*_map.nii.gz"))
        self.assertGreaterEqual(len(groups), 7)
        for path in groups:
            side = path[: -len(".nii.gz")] + ".json"
            content = read_json(side)
            self.assertEqual(content["format"], "slicer-lnm-group-map")
            self.assertEqual(content["disclaimer"], export.DISCLAIMER)
            self.assertEqual(content["connectome"]["N"], 20)
        # runs.jsonl: the pipeline run and its children, with parent links
        registry = runs.load(self.fx["study"])
        run_id = self.pipeline_json["run_id"]
        self.assertEqual(registry[run_id]["kind"], "pipeline")
        self.assertEqual(registry[run_id]["status"], "done")
        if not self.baseline:
            children = runs.children(self.fx["study"], run_id)
            kinds = {child["kind"] for child in children}
            self.assertTrue({"cohortmaps", "glm", "slnm"} <= kinds, kinds)

    def test_no_absolute_paths_in_shared_files(self):
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(MODULE_DIR))), "Utilities", "Scripts", "StudioPrivacyCheck.py")
        if not os.path.isfile(script):
            self.skipTest("StudioPrivacyCheck.py not found")
        result = subprocess.run([sys.executable, script, self.out], capture_output=True, text=True)
        failing = [line for line in result.stdout.splitlines() if line.startswith("FAIL") and "reproduce_pipeline.sh" not in line]
        self.assertEqual(failing, [], result.stdout[-3000:])

    # File compatibility (SPEC 9.7)

    def test_group_map_headers(self):
        reference = nifti.read_raw_header(os.path.join(self.fx["bundle"], "mask.nii.gz"))
        for path in glob.glob(os.path.join(self.out, "group_conn-*_map.nii.gz")):
            header = nifti.read_raw_header(path)
            for field in HEADER_FIELDS:
                self.assertEqual(header[field].tobytes(), reference[field].tobytes(), f"{os.path.basename(path)}: {field}")
            desc = re.search(r"_desc-([A-Za-z0-9]+)_map", path).group(1)
            if re.fullmatch(r"sens\d+(Pos|Neg)|conj(Pos|Neg)", desc):
                self.assertEqual(int(header["datatype"]), 2, f"{desc} must be uint8")

    # Numerical equivalence

    def _case_maps(self, column):
        rows = [row for row in self.pipeline_json["inputs"]["rows"] if row["status"] in ("computed", "reused")]
        groups = [row["group"] for row in rows]
        key = {"T": "T", "AvgFz": "AvgFz"}[column]
        Y = np.stack([masked(export.map_paths(self.out, row["participant_id"], self.bundle.name)[key], self.bundle) for row in rows]).astype(np.float32)
        return Y, groups

    def test_overlap_equals_direct_call(self):
        Y, groups = self._case_maps("T")
        for sign, suffix in (("pos", "Pos"), ("neg", "Neg")):
            result = cohort.group_maps(Y, thresholds.DEFAULT_RULE, groups=groups, sensitivity_pct=90, sign=sign, n=20, legacy=(), conjunction_map=False)
            np.testing.assert_allclose(masked(self.group_path(f"overlapCount{suffix}"), self.bundle), result.maps["overlapCount"], atol=1e-6)
            np.testing.assert_allclose(masked(self.group_path(f"overlapPct{suffix}"), self.bundle), result.maps["overlapPct"], atol=1e-5)
            np.testing.assert_array_equal(masked(self.group_path(f"sens90{suffix}"), self.bundle), result.maps["sens90"])
        cases = [index for index, group in enumerate(groups) if group == "case"]
        np.testing.assert_allclose(masked(self.group_path("meanT"), self.bundle), Y[cases].mean(axis=0), atol=1e-5)

    def test_glm_slnm_lesionset_conjunction_equal_direct_calls(self):
        if self.baseline:
            self.skipTest("GLM, sLNM and lesion-set diagnostics are not available with the wave-1 providers")
        out = self.out
        maps_tsv = os.path.join(out, "stages", "cohortmaps", "maps.tsv")
        mask = os.path.join(out, pipeline.PRIVATE_DIR, pipeline.MASK_NAME)
        participants = os.path.join(self.fx["study"], "participants.tsv")
        direct = os.path.join(self.tmp, "direct-glm")
        subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.glm", "--maps", maps_tsv, "--participants", participants, "--design", os.path.join(out, "stages", "glm", "design.json"), "--mask", mask,
                        "--out", direct, "--perm", "1000", "--seed", "0", "--conn-label", self.bundle.name], cwd=MODULE_DIR, env=_env(), check=True, capture_output=True)
        for stat in ("Tstat", "PFwe", "PUnc"):
            np.testing.assert_allclose(masked(self.group_path(f"caseVsControl{stat}"), self.bundle), masked(self.group_path(f"caseVsControl{stat}", direct), self.bundle), atol=1e-6)
        direct_slnm = os.path.join(self.tmp, "direct-slnm")
        subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.slnm", "--maps", maps_tsv, "--participants", participants, "--score", "aphasia_score", "--covariates", "volume_ml", "--perm", "1000",
                        "--seed", "0", "--bundle", self.fx["bundle"], "--mask", mask, "--out", direct_slnm, "--conn-label", self.bundle.name], cwd=MODULE_DIR, env=_env(), check=True, capture_output=True)
        np.testing.assert_allclose(masked(self.group_path("slnmAphasiaScoreR"), self.bundle), masked(self.group_path("slnmAphasiaScoreR", direct_slnm), self.bundle), atol=1e-6)
        # Lesion overlap: lesionset on the snapshots
        study = Study.open(self.fx["study"])
        rows = []
        for sid in self.mapped_sids():
            row = study.subject(sid)
            row["lesion"] = glob.glob(os.path.join(out, "inputs", "lesions", f"{sid}_desc-lesion_mask.nii*"))[0]
            rows.append(row)
        subset = Study(study.path, dict(study.metadata), study.columns, rows, study.dictionary)
        occ = lesionset.load_occupancies(subset, lesionset.resolve_grid(subset, self.bundle, space=fixture.SPACE), space=fixture.SPACE)
        expected = lesionset.overlap_maps(occ).count.reshape(-1)[self.bundle.mask_indices]
        np.testing.assert_array_equal(masked(self.group_path("lesionCount"), self.bundle), expected)
        # Conjunction = sens AND sign(t) AND PFwe < alpha
        t = masked(self.group_path("caseVsControlTstat"), self.bundle)
        p = masked(self.group_path("caseVsControlPFwe"), self.bundle)
        for suffix, sign in (("Pos", t > 0), ("Neg", t < 0)):
            sens = masked(self.group_path(f"sens90{suffix}"), self.bundle) != 0
            np.testing.assert_array_equal(masked(self.group_path(f"conj{suffix}"), self.bundle) != 0, sens & sign & (p < 0.05))

    # Study files

    def test_study_files_untouched_and_inbox(self):
        self.assertEqual(sha256(os.path.join(self.fx["study"], "participants.tsv")), self.before_tsv)
        self.assertEqual(tree_digest(os.path.join(self.fx["study"], "lesions")), self.before_lesions)
        inbox = [path for path in glob.glob(os.path.join(self.fx["study"], ".studio", "inbox", "*.json")) if path.endswith("-pipeline.json")]
        self.assertEqual(len(inbox), 1)
        content = read_json(inbox[0])
        self.assertEqual(sorted(content["rows"]), self.mapped_sids())
        self.assertTrue(all(values == {"status": "mapped"} for values in content["rows"].values()))

    # Events

    def test_events(self):
        for line in self.stdout.splitlines():
            self.assertTrue(line.isascii(), line[:200])
            event = json.loads(line)
            self.assertIn("event", event)
            self.assertIn("t", event)
        start = next(event for event in self.events if event["event"] == "start")
        for key in ("job", "version", "pid", "argv", "threads", "n_rows", "n_compute", "n_reuse", "n_normalize", "n_excluded", "stages", "estimate_s"):
            self.assertIn(key, start)
        times = [event["t"] for event in self.events if event["event"] == "progress"]
        for index in range(len(times)):
            window = [value for value in times[index:] if value - times[index] < 1.0]
            self.assertLessEqual(len(window), 5, "at most 5 progress events per second")
        self.assertTrue(self.progress_samples, "progress.json was written during the run")
        self.assertNotIn(None, self.progress_samples, "progress.json is valid JSON at any moment")
        done = done_event(self.events)
        self.assertEqual(done["event"], "done")
        for key in ("out", "report", "pipeline_json", "computed", "reused", "excluded", "failed_stages", "elapsed", "stages", "warnings"):
            self.assertIn(key, done)

    def test_status_command(self):
        code, _events, stdout, _stderr, _wall = cli("status", "--out", self.out, "--json", env=self.env())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["status"], "done")
        code, _events, stdout, _stderr, _wall = cli("status", "--out", os.path.join(self.tmp, "nowhere"), "--json", env=self.env())
        self.assertEqual(json.loads(stdout)["status"], "never")

    def test_unchanged_rerun_starts_no_process(self):
        code, events, calls, seconds = run_in_process(self.fx, baseline=self.baseline)
        self.assertEqual(code, 0, done_event(events))
        self.assertEqual(calls, [])
        done = done_event(events)
        self.assertEqual((done["computed"], done["reused"]), (0, 12))
        self.assertLessEqual(seconds, 5.0)


@unittest.skipIf(ENV_BASELINE, "the full suite runs without LNM_PIPELINE_PROVIDERS=baseline")
class PipelineTest(PipelineChecks, unittest.TestCase):
    """The full suite (preferred providers), plus the scenarios that need their own runs."""

    def test_resume_scenarios(self):
        fx = fixture.make_fixture(os.path.join(self.tmp, "f2"))
        code, events, _stdout, stderr, _wall = cli(*run_args(fx, *FAST))
        self.assertEqual(code, 0, stderr[-2000:])
        # Re-saving a lesion with identical bytes changes nothing
        path = os.path.join(fx["study"], "lesions", f"sub-02_space-{fixture.SPACE}_desc-lesion_mask.nii.gz")
        with open(path, "rb") as f:
            data = f.read()
        with open(path, "wb") as f:
            f.write(data)
        code, events, calls, _seconds = run_in_process(fx, *FAST)
        self.assertEqual((code, calls), (0, []))
        # Modifying one lesion recomputes exactly that lesion and the stages that depend on it
        path = os.path.join(fx["study"], "lesions", f"sub-03_space-{fixture.SPACE}_desc-lesion_mask.nii.gz")
        volume, affine, _header = nifti.read_nifti(path)
        volume = np.array(volume)
        index = tuple(int(v) for v in np.argwhere(volume)[0])
        volume[index] = 0
        nifti.write_nifti(path, volume, affine)
        code, events, _stdout, stderr, _wall = cli(*run_args(fx, *FAST))
        self.assertEqual(code, 0, stderr[-2000:])
        done = done_event(events)
        self.assertEqual((done["computed"], done["reused"]), (1, 11))
        record = cohortmaps.read_record(os.path.join(fx["out"], "stages", "cohortmaps", "cohortmaps.json"))
        self.assertEqual((record["lesions"]["computed"], record["lesions"]["cache_hits"]), (1, 11))
        status = stage_status(events)
        self.assertEqual(status["normalize"], "reused")
        self.assertEqual(status["maps"], "done")
        for name in ("overlap", "specificity", "slnm", "export"):
            self.assertEqual(status[name], "done", name)
        # Changing --null-relocations recomputes the maps
        code, events, _stdout, stderr, _wall = cli(*run_args(fx, *FAST, "--null-relocations", "3"))
        self.assertEqual(code, 0, stderr[-2000:])
        self.assertEqual(stage_status(events)["maps"], "done")
        # A preview run writes conn-Synthetic_n-10 with PreviewN10 descs and never reuses the full run
        code, events, _stdout, stderr, _wall = cli(*run_args(fx, *FAST, "--preview", "10"))
        self.assertEqual(code, 0, stderr[-2000:])
        preview = os.path.join(fx["study"], "derivatives", "lnm", "conn-Synthetic_n-10")
        self.assertTrue(os.path.isfile(os.path.join(preview, "sub-01_conn-Synthetic_desc-TPreviewN10_funcmap.nii.gz")))
        self.assertEqual(done_event(events)["computed"], 12)
        self.assertEqual(read_json(os.path.join(preview, "pipeline.json"))["subjects"]["n"], 10)

    def test_batching_and_reproduction(self):
        fx = self.fx
        bundle = self.bundle
        per_lesion = cohortmaps.lesion_pass_bytes(bundle.n_voxels, 0)
        budget_gb = (4 * per_lesion + per_lesion // 2) / 1024**3
        out = os.path.join(self.tmp, "batched")
        code, events, _stdout, stderr, _wall = cli(*run_args(fx, "--recompute", "--null-relocations", "0", "--memory-budget-gb", repr(budget_gb), "--out", out, "--specificity", "off", "--scores", "none", *FAST))
        self.assertEqual(code, 0, stderr[-2000:])
        with open(os.path.join(out, "stages", "cohortmaps", "job.log"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn("--budget-gb", log)
        record = cohortmaps.read_record(os.path.join(out, "stages", "cohortmaps", "cohortmaps.json"))
        self.assertEqual(record["lesions"]["computed"], 12)
        progress = [event for event in events if event["event"] == "progress" and event.get("stage") == "maps" and event.get("batches")]
        self.assertTrue(progress and max(event["batches"] for event in progress) == 3, progress[-3:])
        for sid in self.mapped_sids():
            for key in ("AvgR", "AvgFz", "T"):
                # float32 accumulation in other batch sizes: equal within 1e-5 (relative, T values reach 50)
                np.testing.assert_allclose(masked(export.map_paths(out, sid, bundle.name)[key], bundle), masked(export.map_paths(self.out, sid, bundle.name)[key], bundle), rtol=1e-5, atol=1e-5)
        # reproduce.sh recomputes every map in one engine pass
        pythonslicer = shutil.which("PythonSlicer") or os.path.join(os.path.dirname(sys.executable), "PythonSlicer")
        target = os.path.join(self.tmp, "reproduced")
        result = subprocess.run(["sh", os.path.join(self.out, "reproduce.sh")], env=_env(OUT=target, PYTHONSLICER=pythonslicer if os.path.isfile(pythonslicer) else sys.executable), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(sum(1 for line in result.stdout.splitlines() if '"event": "start"' in line), 1)
        for sid in self.mapped_sids():
            np.testing.assert_allclose(masked(os.path.join(target, f"{sid}_conn-Synthetic_desc-T_funcmap.nii.gz"), bundle), masked(export.map_paths(self.out, sid, bundle.name)["T"], bundle), rtol=1e-5, atol=1e-5)
        # reproduce_pipeline.sh reproduces the cohort numbers
        target2 = os.path.join(self.tmp, "reproduced-pipeline")
        result = subprocess.run(["sh", os.path.join(self.out, "reproduce_pipeline.sh")], env=_env(OUT=target2, ACCEPT_TERMS="1", PYTHONSLICER=pythonslicer if os.path.isfile(pythonslicer) else sys.executable),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:] + result.stdout[-2000:])
        again = read_json(os.path.join(target2, "pipeline.json"))
        for key in ("n_mapped", "n_cases", "n_controls", "per_lesion_counts", "conjunction"):
            self.assertEqual(again["cohort"][key], self.pipeline_json["cohort"][key], key)
        self.assertEqual(again["cohort"]["specificity"]["fwe"], self.pipeline_json["cohort"]["specificity"]["fwe"])
        for label, value in self.pipeline_json["cohort"]["slnm"].items():
            self.assertAlmostEqual(again["cohort"]["slnm"][label]["max_abs_r"], value["max_abs_r"], places=6)
        self.assertEqual(again["cohort"]["nulls"]["classic_rule"], self.pipeline_json["cohort"]["nulls"]["classic_rule"])

    def test_cancel_during_maps_and_resume(self):
        fx = self.fx
        out = os.path.join(self.tmp, "cancel-maps")
        per_lesion = cohortmaps.lesion_pass_bytes(self.bundle.n_voxels, 50, engine_null_voxels(self.bundle))
        args = run_args(fx, "--recompute", "--null-relocations", "50", "--memory-budget-gb", repr(1.2 * per_lesion / 1024**3), "--out", out, *FAST, "--specificity", "off", "--scores", "none")
        stopped = {}

        def on_event(event, process):
            if "at" not in stopped and event["event"] == "progress" and event.get("stage") == "maps" and (event.get("batch") or 0) >= 2:
                open(os.path.join(out, pipeline.PRIVATE_DIR, pipeline.STOP_NAME), "w").close()
                stopped["at"] = event["_wall"]

        code, events, _stdout, stderr, wall = cli(*args, on_event=on_event)
        self.assertIn("at", stopped, "the maps stage ran long enough to be stopped")
        self.assertEqual(code, 3, stderr[-2000:])
        self.assertEqual(done_event(events)["event"], "cancelled")
        self.assertLessEqual(wall - stopped["at"], 3.0)
        self.assertEqual(glob.glob(os.path.join(out, "*_funcmap.nii.gz")), [], "no partial map in the output folder")
        self.assertFalse(os.path.exists(os.path.join(out, pipeline.PRIVATE_DIR, pipeline.STOP_NAME)))
        code, events, _stdout, stderr, _wall = cli(*args)
        self.assertEqual(code, 0, stderr[-2000:])
        record = cohortmaps.read_record(os.path.join(out, "stages", "cohortmaps", "cohortmaps.json"))
        self.assertGreaterEqual(record["lesions"]["cache_hits"], 1, "the completed batches are reused")

    def test_cancel_during_glm_and_resume(self):
        fx = self.fx
        out = os.path.join(self.tmp, "cancel-glm")
        args = ["run", "--study", fx["study"], "--bundle", fx["bundle"], "--accept-terms", "--perm", "60000", "--events", "json", "--out", out, *FAST, "--scores", "none"]
        stopped = {}

        def on_event(event, process):
            if "at" not in stopped and event["event"] == "progress" and event.get("stage") == "specificity" and event.get("child_stage") == "permutations" and (event.get("done") or 0) > 0:
                open(os.path.join(out, pipeline.PRIVATE_DIR, pipeline.STOP_NAME), "w").close()
                stopped["at"] = event["_wall"]

        code, events, _stdout, stderr, wall = cli(*args, on_event=on_event)
        self.assertIn("at", stopped)
        self.assertEqual(code, 3, stderr[-2000:])
        self.assertLessEqual(wall - stopped["at"], 3.0)
        state = read_json(os.path.join(out, "stages", "glm", ".glm-resume.json"))
        cursor = state["data"]["cursor"]
        self.assertGreater(cursor, 1)
        resumed = {}

        def on_event_again(event, process):
            if "first" not in resumed and event["event"] == "progress" and event.get("stage") == "specificity" and event.get("child_stage") == "permutations":
                resumed["first"] = event.get("done")
                open(os.path.join(out, pipeline.PRIVATE_DIR, pipeline.STOP_NAME), "w").close()

        code, events, _stdout, stderr, _wall = cli(*args, on_event=on_event_again)
        self.assertEqual(code, 3, stderr[-2000:])
        self.assertGreaterEqual(resumed["first"], cursor - 1, "the GLM resumes from its cursor")

    def test_parent_pid_watchdog(self):
        out = os.path.join(self.tmp, "parent")
        per_lesion = cohortmaps.lesion_pass_bytes(self.bundle.n_voxels, 50, engine_null_voxels(self.bundle))
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        killed = {}

        def on_event(event, process):
            if "at" not in killed and event["event"] == "progress" and event.get("stage") == "maps":
                parent.kill()
                parent.wait()
                killed["at"] = event["_wall"]

        code, events, _stdout, stderr, wall = cli(*run_args(self.fx, "--recompute", "--null-relocations", "50", "--memory-budget-gb", repr(1.2 * per_lesion / 1024**3), "--out", out, *FAST,
                                                           "--specificity", "off", "--scores", "none", "--parent-pid", str(parent.pid)), on_event=on_event)
        self.assertIn("at", killed)
        self.assertEqual(code, 3, stderr[-2000:])
        self.assertLessEqual(wall - killed["at"], 5.0)

    def test_launcher_terminated(self):
        launcher = shutil.which("PythonSlicer") or os.path.join(os.path.dirname(sys.executable), "PythonSlicer")
        if not os.path.isfile(launcher):
            self.skipTest("no PythonSlicer launcher")
        out = os.path.join(self.tmp, "launcher")
        per_lesion = cohortmaps.lesion_pass_bytes(self.bundle.n_voxels, 50, engine_null_voxels(self.bundle))
        process = subprocess.Popen([launcher, "-m", "LesionNetworkMappingLib.pipeline", *run_args(self.fx, "--recompute", "--null-relocations", "50", "--memory-budget-gb", repr(1.2 * per_lesion / 1024**3),
                                                                                                       "--out", out, *FAST, "--specificity", "off", "--scores", "none")],
                                   cwd=MODULE_DIR, env=_env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        worker = None
        for line in process.stdout:
            event = json.loads(line) if line.startswith("{") else {}
            if event.get("event") == "start":
                worker = event["pid"]
            if event.get("event") == "progress" and event.get("stage") == "maps":
                break
        self.assertIsNotNone(worker)
        if worker == process.pid:
            process.kill()
            self.skipTest("the launcher runs Python in its own process")
        started = time.monotonic()
        process.terminate()
        process.wait(10)
        while jobs.pid_alive(worker) and time.monotonic() - started < 5.0:
            time.sleep(0.1)
        self.assertFalse(jobs.pid_alive(worker), "the worker stops when its launcher is terminated")

    # Terms and locking

    def test_terms_not_accepted(self):
        out = os.path.join(self.tmp, "no-terms")
        args = [arg for arg in run_args(self.fx, "--out", out) if arg != "--accept-terms"]
        code, events, _stdout, _stderr, _wall = cli(*args)
        self.assertEqual(code, 4)
        error = done_event(events)
        self.assertEqual(error["code"], "terms_not_accepted")
        self.assertIn("data_use_terms", error)
        self.assertFalse(os.path.exists(out), "nothing is written")

    def test_lock(self):
        out = os.path.join(self.tmp, "locked")
        os.makedirs(os.path.join(out, pipeline.PRIVATE_DIR))
        lock = jobs.OutputLock(os.path.join(out, pipeline.PRIVATE_DIR, pipeline.LOCK_NAME)).acquire()
        try:
            code, events, _stdout, _stderr, _wall = cli(*run_args(self.fx, "--out", out, *FAST))
        finally:
            lock.release()
        self.assertEqual(code, 75)
        self.assertEqual(done_event(events)["code"], "locked")
        # A lock older than 60 s is reclaimed
        path = os.path.join(out, pipeline.PRIVATE_DIR, pipeline.LOCK_NAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "host": "elsewhere", "token": "x", "started": "2026-01-01T00:00:00Z"}, f)
        os.utime(path, (time.time() - 120, time.time() - 120))
        code, events, _stdout, stderr, _wall = cli(*run_args(self.fx, "--out", out, *FAST, "--specificity", "off", "--scores", "none"))
        self.assertEqual(code, 0, stderr[-2000:])
        self.assertTrue(any(event.get("code") == "stale_lock" for event in events))

    # Native-space row

    def test_native_row(self):
        fx = fixture.make_fixture(os.path.join(self.tmp, "f4"))
        native = "sub-15"
        cheap = ["--specificity", "off", "--scores", "none", *FAST]
        code, events, _stdout, stderr, _wall = cli(*run_args(fx, *cheap, "--out", os.path.join(self.tmp, "native-base")), env=_env(**{pipeline.PROVIDERS_ENV: "baseline"}))
        self.assertEqual(code, 0, stderr[-2000:])
        rows = {row["participant_id"]: row for row in read_json(os.path.join(self.tmp, "native-base", "pipeline.json"))["inputs"]["rows"]}
        self.assertEqual(rows[native]["reason"], pipeline.NATIVE_UNAVAILABLE)
        stub = fixture.make_normalizer_stub(self.tmp, fx["bundle"])
        env = _env(**{pipeline.NORMALIZER_ENV: stub})
        code, events, _stdout, stderr, _wall = cli(*run_args(fx, *cheap, "--out", os.path.join(self.tmp, "native-stub")), env=env)
        self.assertEqual(code, 0, stderr[-2000:])
        rows = {row["participant_id"]: row for row in read_json(os.path.join(self.tmp, "native-stub", "pipeline.json"))["inputs"]["rows"]}
        self.assertEqual(rows[native]["reason"], pipeline.NORM_NEEDS_REVIEW)
        code, events, _stdout, stderr, _wall = cli(*run_args(fx, *cheap, "--native", "include-unreviewed", "--out", os.path.join(self.tmp, "native-include")), env=env)
        self.assertEqual(code, 0, stderr[-2000:])
        data = read_json(os.path.join(self.tmp, "native-include", "pipeline.json"))
        rows = {row["participant_id"]: row for row in data["inputs"]["rows"]}
        self.assertEqual(rows[native]["status"], "computed")
        self.assertTrue(any(item.get("what") == "norm_qc" and item.get("participant_id") == native for item in data["overrides"]))
        self.assertTrue(os.path.isfile(export.map_paths(os.path.join(self.tmp, "native-include"), native, self.bundle.name)["T"]))

    # init

    def test_init(self):
        source = os.path.join(self.tmp, "folder of lesions")
        os.makedirs(source)
        study = Study.open(self.fx["study"])
        lines = ["participant_id,diagnosis,aphasia_score"]
        for sid in self.mapped_sids()[:6]:
            path = study.abspath(study.subject(sid)["lesion"])
            shutil.copyfile(path, os.path.join(source, f"{sid}_lesion.nii.gz"))
            lines.append(f"{sid},{'patient' if int(sid[-2:]) <= 4 else 'HC'},{study.subject(sid)['aphasia_score']}")
        csv = os.path.join(self.tmp, "table.csv")
        with open(csv, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        before = tree_digest(source)
        target = os.path.join(self.tmp, "new study")
        code, events, _stdout, stderr, _wall = cli("init", "--from", source, "--participants", csv, "--study", target, "--score-columns", "aphasia_score", "--group-column", "diagnosis",
                                                   "--group-map", "patient=case,HC=control")
        self.assertEqual(code, 0, stderr[-2000:] + str(events))
        summary = next(event for event in events if event["event"] == "result")
        self.assertEqual(summary["subjects"], 6)
        self.assertEqual(summary["lesions_found"], 6)
        created = Study.open(target)
        self.assertEqual(created.subject("sub-01")["group"], "case")
        self.assertEqual(created.subject("sub-06")["group"], "control")
        self.assertIn(("aphasia_score", True), created.score_columns())
        self.assertEqual(tree_digest(source), before, "the source files are never modified")
        # A live GUI session refuses init
        session = os.path.join(target, ".studio", "session.json")
        with open(session, "w", encoding="utf-8") as f:
            json.dump({"format": "slicer-studio-session", "pid": os.getpid(), "host": socket.gethostname(), "heartbeat_epoch": time.time()}, f)
        code, events, _stdout, _stderr, _wall = cli("init", "--from", source, "--study", target)
        self.assertEqual(code, 75)
        self.assertIn("study open in Slicer", done_event(events)["message"])

    # Events and speed

    def test_text_events_and_help(self):
        out = os.path.join(self.tmp, "text")
        args = [arg if arg != "json" else "text" for arg in run_args(self.fx, "--out", out, *FAST, "--specificity", "off", "--scores", "none")]
        code, _events, stdout, stderr, _wall = cli(*args)
        self.assertEqual(code, 0, stderr[-2000:])
        self.assertIn("[5/16 Network maps]", stdout)
        self.assertIn("done:", stdout)
        self.assertFalse(any(line.startswith("{") for line in stdout.splitlines()))
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.pipeline", "--help"], cwd=MODULE_DIR, env=_env(), capture_output=True, text=True)
        wall = time.monotonic() - started
        self.assertEqual(result.returncode, 0)
        self.assertIn("init", result.stderr + result.stdout)
        self.assertLessEqual(wall, 0.3 if not os.environ.get("LNM_SLOW_HOST") else 1.0)
        probe = subprocess.run([sys.executable, "-c", "import sys; from LesionNetworkMappingLib import pipeline; pipeline._parser().format_help(); print('numpy' in sys.modules)"], cwd=MODULE_DIR, env=_env(),
                               capture_output=True, text=True)
        self.assertEqual(probe.stdout.strip(), "False")

    def test_plan_speed_300_lesions(self):
        root = os.path.join(self.tmp, "big")
        os.makedirs(root)
        study = Study.create(os.path.join(root, "study"), name="big")
        grid = self.bundle.grid
        shape = (46, 46, 46)  # about 97 KB uncompressed
        affine = np.eye(4)
        affine[:3, 3] = grid.affine[:3, 3]
        for index in range(300):
            sid = f"sub-{index + 1:03d}"
            path = os.path.join(study.path, "lesions", f"{sid}_space-{fixture.SPACE}_desc-lesion_mask.nii")
            volume = np.zeros(shape, np.uint8)
            volume[10 + index % 20:14 + index % 20, 10:14, 10 + index // 20:14 + index // 20] = 1
            nifti.write_nifti(path, volume, affine)
            stat = os.stat(path)
            study.add_subject(sid, {"group": "case" if index % 2 else "control", "lesion": study.relpath(path), "status": "traced", "space": fixture.SPACE, "volume_ml": 0.064, "fractional_sum": 8.0,
                                    "lesion_mtime": round(stat.st_mtime, 3), "lesion_size": stat.st_size, "n_parts": 1})
        study.save()
        before = sha256(os.path.join(study.path, "participants.tsv"))
        config = pipeline.PipelineConfig(study=study.path, bundle=self.fx["bundle"], accept_terms=True, nulls="off")
        pipeline.plan(pipeline.PipelineConfig(study=self.fx["study"], bundle=self.fx["bundle"], accept_terms=True, nulls="off"))  # imports
        started = time.perf_counter()
        result = pipeline.plan(config)
        seconds = time.perf_counter() - started
        self.assertEqual(result.count("compute"), 300)
        self.assertLessEqual(seconds, 1.0, f"plan of 300 lesions took {seconds:.2f} s")
        self.assertFalse(os.path.exists(result.out), "plan writes nothing")
        self.assertEqual(sha256(os.path.join(study.path, "participants.tsv")), before)

    # Code rules

    def test_code_rules(self):
        path = os.path.join(MODULE_DIR, "LesionNetworkMappingLib", "pipeline.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertIsNone(re.search(r"^(import|from) (slicer|qt|vtk|ctk)\b", source, re.MULTILINE))
        for banned in ("lstsq", "permutation_maxstat", "welch_t", "liebermeister(", "np.random"):
            self.assertNotIn(banned, source)
        self.assertNotIn("class OutputLock", source)
        self.assertIn("jobs.OutputLock", source)
        self.assertIn("jobs.start_watchdogs", source)


class BaselinePipelineTest(PipelineChecks, unittest.TestCase):
    """The core checks with the wave-1 fallback providers (LNM_PIPELINE_PROVIDERS=baseline)."""

    baseline = True


def engine_null_voxels(bundle):
    from LesionNetworkMappingLib import engine

    return min(bundle.n_voxels, engine.NULL_MAX_VOXELS)


if __name__ == "__main__":
    unittest.main()

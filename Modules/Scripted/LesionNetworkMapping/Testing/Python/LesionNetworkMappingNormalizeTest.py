"""Tests of the lesion-aware normalization (LesionNetworkMappingLib.normalize, A-03).

They run the real command line (BRAINSFit and BRAINSResample child processes) on synthetic native
scans made from the bundled MNI152NLin2009cAsym 2 mm brain (lnm_normalize_fixture.py), so the
template-space truth is known. No network. Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingNormalizeTest.py -v

The tilt test needs the whole-head template (tpl-MNI152NLin2009cAsym_res-02_T1w.nii.gz, not
bundled): it runs when the file is found in a folder named by $LNM_TEST_TEMPLATE_DIR (or the
default template folders) and is skipped otherwise.
"""

import ast
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_DIR = os.path.dirname(os.path.dirname(TEST_DIR))
for folder in (MODULE_DIR, TEST_DIR):
    if folder not in sys.path:
        sys.path.insert(0, folder)

import numpy as np  # noqa: E402

import lnm_normalize_fixture as fixture  # noqa: E402
from LesionNetworkMappingLib import jobs, nifti, normalize  # noqa: E402
from LesionNetworkMappingLib.study import Study  # noqa: E402

TEMPLATE = "MNI152NLin2009cAsym"
LESIONS = ("large", "small")
EXTRA_TEMPLATE_DIRS = [os.environ["LNM_TEST_TEMPLATE_DIR"]] if os.environ.get("LNM_TEST_TEMPLATE_DIR") else []
TEMPLATE_DIR_ARGS = [arg for folder in EXTRA_TEMPLATE_DIRS for arg in ("--template-dir", folder)]


def _env(**extra):
    env = dict(os.environ)
    env["PYTHONPATH"] = MODULE_DIR + os.pathsep + env.get("PYTHONPATH", "")
    for name in jobs.THREAD_ENV_VARS:
        env.pop(name, None)
    env.update(extra)
    return env


def _command(*args):
    return [sys.executable, "-m", "LesionNetworkMappingLib.normalize", *args]


def _events(stdout):
    """Every line must be an ASCII JSON event with "event" and "t"."""
    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        line.encode("ascii")  # raises for non-ASCII characters
        event = json.loads(line)
        assert isinstance(event, dict) and "event" in event and isinstance(event.get("t"), (int, float)), line
        events.append(event)
    return events


def _run(*args, env=None, timeout=600):
    started = time.monotonic()
    process = subprocess.run(_command(*args, "--events", "json"), check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env or _env(), cwd=MODULE_DIR, timeout=timeout)
    return process.returncode, _events(process.stdout), process.stderr, time.monotonic() - started


def _first(events, name, **match):
    for event in events:
        if event["event"] == name and all(event.get(key) == value for key, value in match.items()):
            return event
    return None


def _available():
    try:
        normalize.find_executable("BRAINSFit")
        normalize.find_executable("BRAINSResample")
    except jobs.JobError:
        return False
    found = normalize.find_template(TEMPLATE)
    return not any(role in found["missing"] for role in normalize.REQUIRED_TEMPLATE_FILES)


def _read(path):
    data, affine, _ = nifti.read_nifti(path)
    return np.asarray(data), affine


def _wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _process_gone(pid):
    """True when ``pid`` exited (a zombie that no one reaps counts as exited)."""
    if not jobs.pid_alive(pid):
        return True
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        return False
    return not state or state.startswith("Z")


@unittest.skipUnless(_available(), "BRAINSFit/BRAINSResample or the bundled template are not available (run with PythonSlicer)")
class NormalizeTest(unittest.TestCase):
    """Shared fixture and runs: built once, reused by the tests (the class is ordered by name)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-normalize-test-")
        cls.data = os.path.join(cls.tmp, "Étude native")  # non-ASCII folder with a space
        cls.subject = fixture.make_subject(cls.data, "sub-01", LESIONS)
        cls.runs = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def run_cached(self, key, *args):
        """Run the CLI once per key (later calls reuse the result)."""
        if key not in self.runs:
            out = self.path(key)
            code, events, stderr, seconds = _run(*args, "--out", out)
            self.runs[key] = {"code": code, "events": events, "stderr": stderr, "seconds": seconds, "out": out}
        run = self.runs[key]
        self.assertEqual(run["code"], 0, f"{key}: {run['stderr'][-2000:]} {[e for e in run['events'] if e['event'] == 'error']}")
        return run

    def affine_run(self):
        # "run" implied by a first argument starting with "--" (A-04 contract)
        return self.run_cached("affine", "--scan", self.subject["scan"], "--lesion", self.subject["lesion"], "--quality", "affine", *TEMPLATE_DIR_ARGS)

    def standard_run(self):
        return self.run_cached("standard", "run", "--scan", self.subject["scan"], "--lesion", self.subject["lesion"])

    def record(self, run):
        with open(os.path.join(run["out"], normalize.PROVENANCE_JSON), encoding="utf-8") as f:
            return json.load(f)

    def output(self, run, key):
        record = self.record(run)
        return os.path.join(run["out"], *record["outputs"][key]["path"].split("/"))

    def lesion_dice(self, run):
        lesion, _ = _read(self.output(run, "lesion"))
        return fixture.dice(lesion, self.subject["truth"]), float((lesion > 0).sum()) / 1000.0

    # Static checks

    def test_00_static(self):
        source_path = normalize.__file__
        with open(source_path, encoding="utf-8") as f:
            source = f.read()
        for text in ("/opt", "/proc", "xvfb"):
            self.assertNotIn(text, source)
        tree = ast.parse(source)
        for node in tree.body:  # module level
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                self.assertNotIn(name.split(".")[0], ("slicer", "qt", "vtk", "ctk", "numpy", "scipy", "SimpleITK", "PIL"), name)
        code = "import sys, LesionNetworkMappingLib.normalize; print([m for m in ('numpy', 'scipy', 'SimpleITK', 'PIL', 'slicer', 'qt', 'vtk') if m in sys.modules])"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=_env(), cwd=MODULE_DIR, check=True)
        self.assertEqual(result.stdout.strip(), "[]")
        self.assertFalse(normalize.REQUIRES_SLICER)
        self.assertEqual(normalize.FORMAT, "slicer-lnm-normalization")
        self.assertEqual(normalize.QUALITY["standard"]["grid"], (8, 8, 8))
        self.assertEqual(normalize.QUALITY["fine"]["grid"], (14, 10, 12))
        # The output grids come from geometry.GRID_REGISTRY and equal the TemplateFlow res-01 grids
        grid = normalize.template_grid("MNI152NLin2009cAsym")
        self.assertEqual(tuple(grid.shape), (193, 229, 193))
        np.testing.assert_allclose(grid.affine, [[1, 0, 0, -96], [0, 1, 0, -132], [0, 0, 1, -78], [0, 0, 0, 1]])
        grid = normalize.template_grid("MNI152NLin6Asym")
        self.assertEqual(tuple(grid.shape), (182, 218, 182))
        np.testing.assert_allclose(grid.affine, [[-1, 0, 0, 90], [0, 1, 0, -126], [0, 0, 1, -72], [0, 0, 0, 1]])

    def test_01_help_and_check(self):
        started = time.monotonic()
        result = subprocess.run(_command("--help"), capture_output=True, text=True, env=_env(), cwd=MODULE_DIR, check=False)
        seconds = time.monotonic() - started
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")  # help goes to standard error; standard output only carries events
        self.assertLess(seconds, 2.0)  # 0.15-0.25 s measured on an idle host
        code, events, _, _ = _run("check")
        self.assertEqual(code, 0)
        report = _first(events, "result", kind="check")
        self.assertTrue(report["ok"])
        self.assertTrue(report["executables"]["BRAINSFit"]["found"])
        self.assertTrue(report["templates"][TEMPLATE]["usable"])
        self.assertIn("head_template", report["templates"][TEMPLATE])
        self.assertEqual(_first(events, "start")["pid"] > 0, True)
        code, events, _, _ = _run("bogus")
        self.assertEqual(code, jobs.EXIT_USAGE)

    def test_02_plan(self):
        started = time.monotonic()
        plan = normalize.plan(self.subject["scan"], self.subject["lesion"])
        self.assertLess(time.monotonic() - started, 2.0)  # header-only (<= 0.2 s idle; the lesion voxels are read)
        self.assertEqual(plan.modality, "T1w")
        self.assertEqual(plan.subject, "sub-01")
        self.assertFalse(plan.two_step)
        self.assertAlmostEqual(plan.native_ml, self.subject["native_ml"], places=3)
        self.assertEqual(normalize.guess_modality("sub-01_FLAIR.nii.gz"), ("FLAIR", False))
        self.assertEqual(normalize.guess_modality("x_T2-FLAIR.nii.gz"), ("FLAIR", False))
        self.assertEqual(normalize.guess_modality("scan.nii.gz"), ("T1w", True))
        self.assertEqual(normalize.subject_id(lesion="a/sub-17_desc-lesion_mask.nii.gz"), "sub-17")

    def test_03_errors(self):
        scan, lesion = self.subject["scan"], self.subject["lesion"]
        _, affine = _read(lesion)
        empty = self.path("errors", "sub-01_empty_mask.nii.gz")
        os.makedirs(os.path.dirname(empty), exist_ok=True)
        nifti.write_nifti(empty, np.zeros(self.subject["shape"], dtype=np.uint8), affine, sform_code=1, qform_code=1)
        code, events, _, _ = _run("run", "--scan", scan, "--lesion", empty, "--out", self.path("errors", "out"))
        self.assertEqual(code, normalize.EXIT_NOTHING_TO_DO)
        self.assertEqual(_first(events, "error")["code"], "empty_lesion")
        # a lesion far from the scan
        far = affine.copy()
        far[:3, 3] += 500.0
        outside = self.path("errors", "sub-01_far_mask.nii.gz")
        data, _ = _read(lesion)
        nifti.write_nifti(outside, data, far, sform_code=1, qform_code=1)
        code, events, _, _ = _run("run", "--scan", scan, "--lesion", outside, "--out", self.path("errors", "out"))
        self.assertEqual(code, jobs.EXIT_ERROR)
        self.assertEqual(_first(events, "error")["code"], "lesion_outside_scan")
        # a scan already on the template grid
        template = normalize.find_template(TEMPLATE)["files"]["fixed"]
        code, events, _, _ = _run("run", "--scan", template, "--out", self.path("errors", "out"))
        self.assertEqual(code, normalize.EXIT_NOTHING_TO_DO)
        self.assertEqual(_first(events, "error")["code"], "already_template_space")
        # no BRAINSFit on an emptied PATH
        code, events, _, _ = _run("run", "--scan", scan, "--lesion", lesion, "--out", self.path("errors", "out"), env=_env(PATH=""))
        self.assertEqual(code, jobs.EXIT_ERROR)
        self.assertEqual(_first(events, "error")["code"], "executable_missing")
        # a missing template (NLin6 files are never bundled)
        code, events, _, _ = _run("run", "--scan", scan, "--lesion", lesion, "--out", self.path("errors", "out"), "--template", "MNI152NLin2009cAsym", "--template-image", self.path("missing.nii.gz"))
        self.assertEqual(code, jobs.EXIT_ERROR)
        self.assertEqual(_first(events, "error")["code"], "template_missing")
        if normalize.find_template("MNI152NLin6Asym", dirs=EXTRA_TEMPLATE_DIRS)["files"]["fixed"] is None:
            code, events, _, _ = _run("run", "--scan", scan, "--lesion", lesion, "--out", self.path("errors", "out"), "--template", "MNI152NLin6Asym")
            self.assertEqual(code, jobs.EXIT_ERROR)
            error = _first(events, "error")
            self.assertEqual(error["code"], "template_missing")
            self.assertTrue(error["files"][0]["url"].startswith("https://templateflow.s3.amazonaws.com/"))

    # Accuracy

    def test_10_affine_accuracy(self):
        run = self.affine_run()
        dice, ml = self.lesion_dice(run)
        self.assertGreaterEqual(dice, 0.85)
        self.assertLess(abs(ml - self.subject["truth_ml"]) / self.subject["truth_ml"], 0.15)
        record = self.record(run)
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["qc"]["suggestion"], "looks-ok", record["qc"]["reasons"])
        self.assertEqual(record["method"]["quality"], "affine")
        self.assertGreaterEqual(len(record["method"]["attempts"]), 1)

    def test_11_standard_accuracy(self):
        dice_affine, _ = self.lesion_dice(self.affine_run())
        run = self.standard_run()
        dice, _ = self.lesion_dice(run)
        self.assertGreaterEqual(dice, 0.85)
        self.assertGreaterEqual(dice, dice_affine - 0.03)
        record = self.record(run)
        self.assertEqual(record["method"]["label"], "brainsfit-cfm:standard")
        metrics = record["qc"]["metrics"]
        for key in ("ncc_xl", "nmi_xl", "normalized_ratio", "logJ_lesion_minus_brain", "folding_pct", "outside_brain_pct", "centroid_mni", "side", "native_ml", "mni_ml", "mni_ml_frac", "affine_scale"):
            self.assertIn(key, metrics)
        self.assertEqual(metrics["side"], "L")
        self.assertAlmostEqual(metrics["normalized_ratio"], 1.0, delta=0.15)
        self.assertAlmostEqual(metrics["affine_scale"], fixture.SCALE**3, delta=0.05)

    def test_12_unmasked(self):
        masked = self.standard_run()
        run = self.run_cached("unmasked", "run", "--scan", self.subject["scan"], "--lesion", self.subject["lesion"], "--dilate-mm", "-1")
        _, masked_ml = self.lesion_dice(masked)
        _, ml = self.lesion_dice(run)
        self.assertLess(ml, masked_ml)  # the lesion is pulled towards the lesion-free template without the mask
        record = self.record(run)
        self.assertEqual(record["qc"]["suggestion"], "review")
        self.assertIn("cost-function masking was disabled", record["qc"]["reasons"])
        self.assertEqual(record["method"]["label"], "brainsfit-nomask:standard")

    # Outputs

    def test_20_outputs(self):
        run = self.affine_run()
        out = run["out"]
        names = normalize.output_names("sub-01", "T1w", TEMPLATE)
        for key in ("transform", "affine", "affine_inverse", "lesion", "lesion_sidecar", "lesion_prob", "scan", "costmask", "qc_png", "qc_json"):
            self.assertTrue(os.path.isfile(os.path.join(out, names[key])), names[key])
        self.assertTrue(os.path.isfile(os.path.join(out, "normalization.json")))
        self.assertFalse(any(name.startswith(".work-") or name == normalize.LOCK_NAME for name in os.listdir(out)))
        grid = normalize.template_grid(TEMPLATE)
        found = normalize.find_template(TEMPLATE, dirs=EXTRA_TEMPLATE_DIRS)
        for key, dtype in (("lesion", np.uint8), ("lesion_prob", np.float32), ("scan", np.int16)):
            header = nifti.read_nifti_header(os.path.join(out, names[key]))
            self.assertEqual(tuple(header["shape"]), tuple(grid.shape))
            self.assertEqual(header["dtype"], np.dtype(dtype).newbyteorder(header["byteorder"]) if np.dtype(dtype).itemsize > 1 else np.dtype(dtype))
            self.assertEqual((int(header["sform_code"]), int(header["qform_code"])), (4, 4))
            np.testing.assert_allclose(header["sform"], grid.affine, atol=1e-5)
            np.testing.assert_allclose(header["qform"], grid.affine, atol=1e-5)
            if found["files"]["reference"] and self.record(run)["template"]["header_reference"]:
                reference = nifti.read_raw_header(found["files"]["reference"])
                raw = nifti.read_raw_header(os.path.join(out, names[key]))
                for field in nifti.GEOMETRY_FIELDS:
                    self.assertEqual(raw[field].tobytes(), reference[field].tobytes(), field)
        prob, _ = _read(os.path.join(out, names["lesion_prob"]))
        self.assertGreaterEqual(float(prob.min()), 0.0)
        self.assertLessEqual(float(prob.max()), 1.0)
        with open(os.path.join(out, names["lesion_sidecar"]), encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(sidecar["Type"], "Lesion")
        self.assertEqual(sidecar["SpatialReference"], TEMPLATE)
        self.assertEqual(sidecar["Resolution"], "1 mm")
        self.assertEqual(len(sidecar["Sources"]), 2)
        self.assertFalse(os.path.isabs(sidecar["Sources"][0]))
        studio = sidecar["SlicerStudio"]
        self.assertEqual(studio["norm_qc"], "unreviewed")
        self.assertEqual(studio["norm_method"], "brainsfit-cfm:affine")
        self.assertIn(studio["norm_suggestion"], ("review", "looks-ok"))
        record = self.record(run)
        for key in ("format", "version", "status", "fingerprint", "created", "subject", "inputs", "template", "method", "outputs", "qc", "review", "warps", "timings", "versions", "host", "disclaimer"):
            self.assertIn(key, record)
        self.assertEqual(record["review"], {"verdict": None, "by": None, "at": None})
        self.assertIn("BRAINSFit", record["versions"])
        self.assertTrue(record["method"]["argv"])
        self.assertEqual(record["method"]["tool"], "BRAINSFit")
        self.assertIn("reruns may differ slightly", record["method"]["note"])
        for item in record["outputs"].values():
            self.assertFalse(os.path.isabs(item["path"]))
            self.assertEqual(os.path.getsize(os.path.join(out, item["path"])), item["bytes"])
        with open(os.path.join(out, names["qc_json"]), encoding="utf-8") as f:
            qc = json.load(f)
        self.assertEqual(qc["format"], "slicer-lnm-normqc")
        self.assertEqual(qc["suggestion"], record["qc"]["suggestion"])
        from PIL import Image

        png = os.path.join(out, names["qc_png"])
        self.assertLessEqual(os.path.getsize(png), 300 * 1024)
        with Image.open(png) as image:
            image.load()
            self.assertGreater(image.size[0], 300)
        # events: start with pid, stages, results, done
        events = run["events"]
        start = _first(events, "start")
        self.assertEqual(start["job"], "normalize")
        self.assertGreater(start["pid"], 0)
        self.assertEqual([e["name"] for e in events if e["event"] == "stage" and e.get("attempt") in (None, 1)][:1], ["costmask"])
        self.assertTrue(any(e["event"] == "progress" and "eta" in e and "fraction" in e for e in events))
        self.assertEqual(_first(events, "result", kind="normalization")["status"], "done")
        self.assertEqual(events[-1]["event"], "done")

    # Reuse, cancel, watchdogs, lock

    def test_30_reuse(self):
        run = self.affine_run()
        code, events, _, _ = _run("run", "--scan", self.subject["scan"], "--lesion", self.subject["lesion"], "--quality", "affine", "--out", run["out"], *TEMPLATE_DIR_ARGS)
        self.assertEqual(code, 0)
        self.assertEqual(_first(events, "result", kind="normalization")["status"], "reused")
        self.assertLess(events[-1]["t"], 1.0)  # job time (process start-up excluded)
        self.assertIsNone(_first(events, "stage"))

    def test_31_cancel_and_changed_lesion(self):
        previous = self.standard_run()
        out = self.path("cancel")
        shutil.copytree(previous["out"], out)
        before = {name: os.path.getmtime(os.path.join(out, name)) for name in os.listdir(out) if os.path.isfile(os.path.join(out, name))}
        # one changed lesion byte gives another fingerprint: the job computes again
        lesion = self.path("cancel-input", "sub-01_space-T1w_desc-lesion_mask.nii")
        os.makedirs(os.path.dirname(lesion), exist_ok=True)
        data, affine = _read(self.subject["lesion"])
        data = data.astype(np.float32)
        index = tuple(int(v) for v in np.argwhere(data > 0)[0])
        data[index] = 0.5 if data[index] != 0.5 else 0.25
        nifti.write_nifti(lesion, data, affine, sform_code=1, qform_code=1)
        cancel_file = self.path("cancel.flag")
        process = subprocess.Popen(
            _command("run", "--scan", self.subject["scan"], "--lesion", lesion, "--subject", "sub-01", "--out", out, "--cancel-file", cancel_file, "--events", "json"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=_env(), cwd=MODULE_DIR,
        )
        events, cancelled_at = [], None
        for line in process.stdout:
            event = json.loads(line)
            events.append(event)
            if event["event"] == "stage" and event["name"] == "bspline" and cancelled_at is None:
                open(cancel_file, "w").close()
                cancelled_at = time.monotonic()
        process.wait(timeout=60)
        process.stdout.close()
        self.assertIsNotNone(cancelled_at, "the job never reached the bspline stage")
        self.assertLess(time.monotonic() - cancelled_at, 3.0)
        self.assertEqual(process.returncode, jobs.EXIT_CANCELLED)
        self.assertEqual(events[-1]["event"], "cancelled")
        self.assertEqual(_first(events, "stage")["name"], "costmask")  # computed, not reused
        after = {name: os.path.getmtime(os.path.join(out, name)) for name in os.listdir(out) if os.path.isfile(os.path.join(out, name))}
        self.assertEqual(after, before)  # no new final files; the previous result is intact
        self.assertEqual(self.record({"out": out})["status"], "done")
        self.assertFalse(any(name.startswith(".work-") for name in os.listdir(out)))

    def _start_job(self, out, *extra, stdout=subprocess.PIPE):
        return subprocess.Popen(
            _command("run", "--scan", self.subject["scan"], "--lesion", self.subject["lesion"], "--quality", "affine", "--no-reuse", "--out", out, "--events", "json", *extra),
            stdout=stdout, stderr=subprocess.DEVNULL, text=True, env=_env(), cwd=MODULE_DIR,
        )

    def test_32_parent_watchdog(self):
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            job = self._start_job(self.path("parent"), "--parent-pid", str(parent.pid))
            for line in job.stdout:
                if json.loads(line)["event"] == "stage":
                    break
            parent.kill()
            parent.wait()
            killed = time.monotonic()
            job.wait(timeout=30)
            job.stdout.close()
            self.assertLess(time.monotonic() - killed, 3.0)
            self.assertEqual(job.returncode, jobs.EXIT_CANCELLED)
        finally:
            if parent.poll() is None:
                parent.kill()

    @unittest.skipIf(sys.platform == "win32", "POSIX re-parenting")
    def test_33_orphan_watchdog(self):
        log = self.path("orphan.jsonl")
        wrapper_code = (
            "import subprocess, sys, time\n"
            f"subprocess.Popen({_command('run', '--scan', self.subject['scan'], '--lesion', self.subject['lesion'], '--quality', 'affine', '--no-reuse', '--out', self.path('orphan'), '--events', 'json')!r}, stdout=open({log!r}, 'w'), stderr=subprocess.DEVNULL)\n"
            "time.sleep(120)\n"
        )
        wrapper = subprocess.Popen([sys.executable, "-c", wrapper_code], env=_env(), cwd=MODULE_DIR)
        try:

            def started():
                try:
                    with open(log, encoding="utf-8") as f:
                        return [json.loads(line) for line in f if line.strip()]
                except (OSError, ValueError):
                    return []

            self.assertTrue(_wait_for(lambda: any(e["event"] == "stage" for e in started()), 60))
            pid = _first(started(), "start")["pid"]
            wrapper.kill()  # the job's direct parent (like a terminated PythonSlicer launcher)
            wrapper.wait()
            killed = time.monotonic()
            self.assertTrue(_wait_for(lambda: _process_gone(pid), 10))
            self.assertLess(time.monotonic() - killed, 3.0)
            self.assertEqual(started()[-1]["event"], "cancelled")
        finally:
            if wrapper.poll() is None:
                wrapper.kill()

    def test_34_lock(self):
        run = self.affine_run()
        lock = os.path.join(run["out"], normalize.LOCK_NAME)
        holder = {"pid": os.getpid(), "host": socket.gethostname(), "token": "test", "started": "2026-01-01T00:00:00Z"}
        with open(lock, "w", encoding="utf-8") as f:
            json.dump(holder, f)
        try:
            code, events, _, _ = _run("run", "--scan", self.subject["scan"], "--lesion", self.subject["lesion"], "--quality", "affine", "--out", run["out"], *TEMPLATE_DIR_ARGS)
            self.assertEqual(code, jobs.EXIT_LOCKED)
            self.assertEqual(_first(events, "error")["code"], "locked")
            # a lock whose heartbeat stopped more than 60 s ago is reclaimed (the run is then reused),
            # together with the work folder its dead job left behind
            stale_work = os.path.join(run["out"], ".work-999999")
            os.makedirs(os.path.join(stale_work, "final"))
            old = time.time() - 120
            os.utime(lock, (old, old))
            code, events, _, _ = _run("run", "--scan", self.subject["scan"], "--lesion", self.subject["lesion"], "--quality", "affine", "--out", run["out"], *TEMPLATE_DIR_ARGS)
            self.assertEqual(code, 0)
            self.assertIsNotNone(_first(events, "warning", code="stale_lock"))
            self.assertFalse(os.path.exists(lock))
            self.assertFalse(os.path.exists(stale_work))
        finally:
            if os.path.exists(lock):
                os.remove(lock)

    def test_36_modality_guessed(self):
        """A scan name without a modality gives the modality_guessed warning, when computing and when reusing."""
        folder = self.path("guessed-input")
        os.makedirs(folder, exist_ok=True)
        scan = os.path.join(folder, "scan.nii.gz")
        shutil.copyfile(self.subject["scan"], scan)
        cancel_file = self.path("guessed.flag")
        open(cancel_file, "w").close()  # cancelled before the first stage: the plan warnings are already out
        code, events, _, _ = _run("run", "--scan", scan, "--lesion", self.subject["lesion"], "--subject", "sub-01", "--out", self.path("guessed"), "--cancel-file", cancel_file, *TEMPLATE_DIR_ARGS)
        self.assertEqual(code, jobs.EXIT_CANCELLED)
        warning = _first(events, "warning", code="modality_guessed")
        self.assertIsNotNone(warning, events)
        self.assertIn("T1w", warning["message"])
        # the same content and subject as the affine run: reused, and the guess is still reported
        run = self.affine_run()
        code, events, _, _ = _run("run", "--scan", scan, "--lesion", self.subject["lesion"], "--subject", "sub-01", "--quality", "affine", "--out", run["out"], *TEMPLATE_DIR_ARGS)
        self.assertEqual(code, 0)
        self.assertEqual(_first(events, "result", kind="normalization")["status"], "reused")
        self.assertIsNotNone(_first(events, "warning", code="modality_guessed"))

    def wrong_initial_transform(self):
        """A rigid transform rotated 90 degrees about x: an alignment started from it fails."""
        import SimpleITK as sitk

        path = self.path("wrong_initial_xfm.h5")
        if not os.path.exists(path):
            transform = sitk.VersorRigid3DTransform()
            transform.SetRotation((1.0, 0.0, 0.0), np.pi / 2)
            sitk.WriteTransform(transform, path)
        return path

    def test_35_alignment_failed(self):
        if normalize.find_template(TEMPLATE)["files"]["head"] is not None:
            self.skipTest("the whole-head template is installed: the tilt recovery applies (test_50)")
        code, events, _, _ = _run("run", "--scan", self.subject["scan"], "--lesion", self.subject["lesion"], "--quality", "affine", "--initial-transform", self.wrong_initial_transform(), "--out", self.path("misaligned"))
        self.assertEqual(code, jobs.EXIT_ERROR)
        error = _first(events, "error")
        self.assertEqual(error["code"], "alignment_failed")
        self.assertIn("head_template_missing", error["codes"])
        self.assertTrue(error["head_template_missing"])
        self.assertEqual([attempt["name"] for attempt in error["attempts"]], ["A4"])
        self.assertIn("--initial-transform", error["message"])
        self.assertFalse(os.path.exists(os.path.join(self.path("misaligned"), "sub-01_space-MNI152NLin2009cAsym_res-1_desc-lesion_mask.nii.gz")))
        self.assertEqual(self.record({"out": self.path("misaligned")})["status"], "error")

    # Two-step, warp, batch, tilt

    def test_40_two_step(self):
        t1_lesion, _ = _read(self.output(self.affine_run(), "lesion"))
        flair = fixture.make_native(self.path("flair"), "sub-01", LESIONS, contrast="FLAIR", motion=((8.0, 0.0, 0.0), (5.0, 0.0, 0.0)))
        run = self.run_cached("two-step", "run", "--scan", flair["scan"], "--lesion", flair["lesion"], "--anat", self.subject["scan"], "--quality", "affine")
        record = self.record(run)
        self.assertTrue(record["method"]["two_step"])
        self.assertEqual(record["method"]["modality"], "FLAIR")
        self.assertTrue(os.path.isfile(self.output(run, "anat_transform")))
        self.assertIn("_from-FLAIR_to-MNI152NLin2009cAsym_mode-image_xfm.h5", self.output(run, "transform"))
        lesion, _ = _read(self.output(run, "lesion"))
        self.assertGreaterEqual(fixture.dice(lesion, t1_lesion), 0.95)
        # without --anat: affine only (even when Standard is asked), a warning and "review"
        run = self.run_cached("flair-only", "run", "--scan", flair["scan"], "--lesion", flair["lesion"], "--quality", "standard")
        record = self.record(run)
        self.assertEqual(record["method"]["quality"], "affine")
        self.assertTrue(record["method"]["experimental"])
        self.assertEqual(record["qc"]["suggestion"], "review")
        self.assertIsNotNone(_first(run["events"], "warning", code="cross_modality"))

    def test_41_warp(self):
        run = self.affine_run()
        target = self.path("warped", "sub-01_rewarped_mask.nii.gz")
        code, _, _, _ = _run("warp", "--run", run["out"], "--image", self.subject["lesion"], "--out", target)
        self.assertEqual(code, 0)
        rewarped, _ = _read(target)
        original, _ = _read(self.output(run, "lesion"))
        np.testing.assert_array_equal(rewarped, original)
        self.assertEqual(len(self.record(run)["warps"]), 1)
        written, expected = nifti.read_raw_header(target), nifti.read_raw_header(self.output(run, "lesion"))
        for field in ("dim", "datatype", *nifti.GEOMETRY_FIELDS):
            self.assertEqual(written[field].tobytes(), expected[field].tobytes(), field)
        self.assertTrue(os.path.isfile(self.path("warped", "sub-01_rewarped_mask_prob.nii.gz")))
        # the probability file is named from the file name only, not from folder names
        target = self.path("x.nii-folder desc-lesion_mask", "rewarp.nii.gz")
        code, _, _, _ = _run("warp", "--run", run["out"], "--image", self.subject["lesion"], "--kind", "lesion", "--out", target)
        self.assertEqual(code, 0)
        np.testing.assert_array_equal(_read(target)[0], original)
        self.assertEqual(sorted(os.listdir(os.path.dirname(target))), ["rewarp.nii.gz", "rewarp_prob.nii.gz"])
        self.assertEqual(len(self.record(run)["warps"]), 2)

    def test_42_batch(self):
        study_path = self.path("study")
        study = Study.create(study_path, name="Normalize test", space=TEMPLATE)
        for sid, name in (("sub-01", "a"), ("sub-02", "b")):
            folder = os.path.join(study_path, "native", name)
            os.makedirs(folder)
            scan = os.path.join(folder, f"{sid}_T1w.nii.gz")
            lesion = os.path.join(folder, f"{sid}_desc-lesion_mask.nii.gz")
            shutil.copyfile(self.subject["scan"], scan)
            shutil.copyfile(self.subject["lesion"], lesion)
            study.add_subject(sid, scan=study.relpath(scan), lesion=study.relpath(lesion), space="native")
        template_lesion = os.path.join(study_path, "lesions", f"sub-03_space-{TEMPLATE}_desc-lesion_mask.nii.gz")
        truth, truth_affine = fixture.truth(("small",))
        nifti.write_nifti(template_lesion, (truth >= 0.5).astype(np.uint8), truth_affine)
        study.add_subject("sub-03", lesion=study.relpath(template_lesion), space=TEMPLATE)
        policy = study.trace_policy
        policy["space"] = TEMPLATE
        study.trace_policy = policy
        study.save()

        def snapshot():
            files = {}
            for root, _, names in os.walk(os.path.join(study_path, "lesions")):
                for name in names:
                    with open(os.path.join(root, name), "rb") as f:
                        files[name] = f.read()
            with open(os.path.join(study_path, "participants.tsv"), "rb") as f:
                files["participants.tsv"] = f.read()
            return files

        before = snapshot()
        code, events, stderr, _ = _run("batch", "--study", study_path, "--quality", "affine", "--template", "auto")
        self.assertEqual(code, 0, stderr[-2000:])
        self.assertEqual(snapshot(), before)
        subjects = [e for e in events if e["event"] == "result" and e.get("kind") == "subject"]
        self.assertEqual(sorted(e["subject"] for e in subjects), ["sub-01", "sub-02"])
        self.assertEqual({e["status"] for e in subjects}, {"done"})
        inbox = os.path.join(study_path, ".studio", "inbox")
        files = sorted(os.listdir(inbox))
        self.assertEqual(len(files), 2)
        for name in files:
            with open(os.path.join(inbox, name), encoding="utf-8") as f:
                content = json.load(f)
            self.assertEqual(content["source"], "normalize")
            ((sid, values),) = content["rows"].items()
            self.assertTrue(os.path.isdir(os.path.join(study_path, "derivatives", "normalization", sid)))
            for key in ("scan_native", "lesion_native", "scan", "lesion", "space", "norm_method", "norm_qc", "norm_suggestion", "norm_fingerprint", "volume_ml", "volume_native_ml"):
                self.assertIn(key, values)
            self.assertEqual(values["space"], TEMPLATE)
            self.assertEqual(values["norm_qc"], "unreviewed")
            self.assertEqual(values["norm_method"], "brainsfit-cfm:affine")
            self.assertTrue(values["lesion"].startswith(f"derivatives/normalization/{sid}/"))
            self.assertEqual(len(values["norm_fingerprint"]), 12)
            self.assertEqual(content["audit"][0]["event"], "normalize")
            self.assertEqual(content["audit"][0]["sid"], sid)
        # the session merges the inbox (single writer): the rows then point at the template-space files
        study = Study.open(study_path)
        study.apply_inbox()
        self.assertEqual(study.value("sub-01", "space"), TEMPLATE)
        self.assertEqual(study.value("sub-01", "norm_qc"), "unreviewed")
        with open(os.path.join(study_path, ".studio", "audit.jsonl"), encoding="utf-8") as f:
            self.assertEqual(sum(1 for line in f if '"normalize"' in line), 2)
        # an unsupported study space falls back to 2009c with a warning; the rows are reused
        study.trace_policy = {**study.trace_policy, "space": "MNIColin27"}
        study.save()
        code, events, _, _ = _run("batch", "--study", study_path, "--subjects", "sub-01", "--quality", "affine")
        self.assertEqual(code, 0)
        self.assertIsNotNone(_first(events, "warning", code="template_family_mismatch"))
        self.assertEqual(_first(events, "result", kind="subject")["status"], "reused")
        # nothing eligible
        code, events, _, _ = _run("batch", "--study", study_path, "--subjects", "sub-03", "--quality", "affine")
        self.assertEqual(code, normalize.EXIT_NOTHING_TO_DO)

    def test_50_tilt(self):
        dirs = EXTRA_TEMPLATE_DIRS
        if normalize.find_template(TEMPLATE, dirs=dirs)["files"]["head"] is None:
            self.skipTest("the whole-head template is not available (set LNM_TEST_TEMPLATE_DIR)")
        straight = fixture.make_native(self.path("head"), "sub-01", ("large",), head=True, template_dirs=dirs)
        tilted = fixture.make_native(self.path("head-tilted"), "sub-01", ("large",), head=True, header_rotation_deg=(20.0, 12.0), template_dirs=dirs)
        extra = [arg for folder in dirs for arg in ("--template-dir", folder)]
        reference = self.run_cached("head", "run", "--scan", straight["scan"], "--lesion", straight["lesion"], "--quality", "affine", *extra)
        straight_lesion, _ = _read(self.output(reference, "lesion"))
        # a head tilted 20 and 12 degrees (header rotation) is aligned
        run = self.run_cached("head-tilted", "run", "--scan", tilted["scan"], "--lesion", tilted["lesion"], "--quality", "affine", *extra)
        lesion, _ = _read(self.output(run, "lesion"))
        self.assertGreaterEqual(fixture.dice(lesion, straight_lesion), 0.85)
        self.assertGreaterEqual(len(self.record(run)["method"]["attempts"]), 1)
        # a failed start (A4 from a wrong initial transform) is recovered by the head-to-head rigid (A2)
        run = self.run_cached("head-recovered", "run", "--scan", straight["scan"], "--lesion", straight["lesion"], "--quality", "affine", "--initial-transform", self.wrong_initial_transform(), *extra)
        record = self.record(run)
        self.assertGreaterEqual(len(record["method"]["attempts"]), 2)
        self.assertEqual(record["method"]["attempts"][0]["name"], "A4")
        self.assertFalse(record["method"]["attempts"][0]["accepted"])
        lesion, _ = _read(self.output(run, "lesion"))
        self.assertGreaterEqual(fixture.dice(lesion, straight_lesion), 0.85)
        self.assertEqual(record["qc"]["suggestion"], "review")  # more than one attempt


class CheckTextTest(unittest.TestCase):
    """'normalize check' with the text output (a terminal) lists the tools, the templates and why a user template is unusable."""

    def test_text_output_lists_tools_templates_and_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "Missing mask")
            os.makedirs(folder)
            image = "tpl-MNI152NLin2009cAsym_res-02_desc-brain_T1w.nii.gz"
            source = normalize.find_template(TEMPLATE)["files"]["fixed"]
            self.assertTrue(source, "the bundled template image")
            shutil.copy(source, os.path.join(folder, image))
            grid = {"shape": [97, 115, 97], "affine": [[2.0, 0.0, 0.0, -96.5], [0.0, 2.0, 0.0, -132.5], [0.0, 0.0, 2.0, -78.5], [0.0, 0.0, 0.0, 1.0]]}
            record = {"schema": "slicer-studio-template", "version": 1, "id": "missing-mask", "name": "Missing mask", "space": "MNI", "resolution_mm": 2, "shape": grid["shape"], "contrast": "T1w",
                      "files": {"surfaces": [], "image": image, "mask": "my_brain_mask.nii.gz"}, "sha256": {}, "grid": grid, "notes": "", "added": "2026-10-29", "default": False}
            with open(os.path.join(folder, "template.json"), "w", encoding="utf-8") as handle:
                json.dump(record, handle)
            process = subprocess.run(_command("check", "--template", "user:missing-mask", "--template-dir", tmp, "--events", "text"), check=False, capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", env=_env(), cwd=MODULE_DIR, timeout=120)
        out = process.stdout
        for name in ("BRAINSFit", "BRAINSResample", "BRAINSROIAuto"):
            self.assertIn(f"  {name}: ", out)
        self.assertIn("  template user:missing-mask: not usable: Template user:missing-mask", out)
        self.assertIn("my_brain_mask.nii.gz", out)
        self.assertIn("is missing from", out)
        self.assertEqual(process.returncode, jobs.EXIT_ERROR)
        error = [line for line in out.splitlines() if "[template_missing]" in line]
        if error:  # the tools are installed: the error names the template problem, not a generic one
            self.assertIn("my_brain_mask.nii.gz", error[0])

    def test_text_format_of_a_check_result(self):
        import io

        sink = io.StringIO()
        stream = normalize._EventStream(sink, text=True)
        stream.write(json.dumps({"event": "result", "t": 0.1, "kind": "check", "ok": True,
                                 "executables": {"BRAINSFit": {"found": True, "path": "/x/BRAINSFit", "version": "5.8.0"}, "BRAINSROIAuto": {"found": False, "message": "BRAINSROIAuto not found"}},
                                 "templates": {"A": {"usable": True}, "B": {"usable": False, "files": {"fixed": {"found": False}}}}}) + "\n")
        text = sink.getvalue()
        self.assertIn("BRAINSFit: found, version 5.8.0 (/x/BRAINSFit)", text)
        self.assertIn("BRAINSROIAuto: not found (BRAINSROIAuto not found)", text)
        self.assertIn("template A: usable", text)
        self.assertIn("template B: not usable: fixed file missing", text)


if __name__ == "__main__":
    unittest.main()

"""Unit tests of the LNM job protocol (LesionNetworkMappingLib.jobs) and run registry (LesionNetworkMappingLib.runs).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingJobsTest.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import warnings

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from LesionNetworkMappingLib import jobs, runs

SOFTWARE = {"python": "3.12", "LesionNetworkMappingLib": "1.0"}  # avoids importing numpy and scipy in tests that do not need them


def _env(**extra):
    env = dict(os.environ)
    env["PYTHONPATH"] = MODULE_DIR + os.pathsep + env.get("PYTHONPATH", "")
    for name in jobs.THREAD_ENV_VARS:
        env.pop(name, None)
    env.update(extra)
    return env


def _events(stdout):
    """Parse every line of a job's standard output; each one must be a JSON event with "event" and "t"."""
    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        assert isinstance(event, dict) and "event" in event and isinstance(event.get("t"), (int, float)), line
        events.append(event)
    return events


class _TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-jobs-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


class SelfTestJobTest(_TempDirTestCase):
    def _command(self, *args):
        return [sys.executable, "-m", "LesionNetworkMappingLib.jobs", "selftest", *args]

    def _run(self, *args, timeout=120, **env):
        result = subprocess.run(self._command(*args), check=False, capture_output=True, text=True, env=_env(**env), cwd=self.tmp, timeout=timeout)
        return result.returncode, _events(result.stdout), result.stderr

    def test_success_events(self):
        code, events, stderr = self._run("--seconds", "0.2", "--units", "4", "--out", self.path("out"))
        self.assertEqual(code, 0, stderr)
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds[0], "start")
        self.assertEqual(kinds[-1], "done")
        self.assertEqual(kinds.count("progress"), 4)
        self.assertIn("result", kinds)
        start = events[0]
        self.assertEqual((start["job"], start["threads"]), ("selftest", 2))
        self.assertIsInstance(start["pid"], int)
        self.assertIn("version", start)
        last = [event for event in events if event["event"] == "progress"][-1]
        self.assertEqual((last["done"], last["total"], last["eta"]), (4, 4, 0.0))
        self.assertEqual(events[-1]["outputs"], [self.path("out", "selftest.json")])
        times = [event["t"] for event in events]
        self.assertEqual(times, sorted(times))

    def test_cancel_file_stops_with_exit_3(self):
        cancel_file = self.path("STOP")
        process = subprocess.Popen(self._command("--seconds", "60", "--units", "600", "--cancel-file", cancel_file), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env(), cwd=self.tmp)
        try:
            first = process.stdout.readline()  # the start event: the job is running
            self.assertEqual(json.loads(first)["event"], "start")
            time.sleep(0.3)
            with open(cancel_file, "w", encoding="utf-8"):
                pass
            started = time.monotonic()
            stdout, stderr = process.communicate(timeout=30)
            stopped_in = time.monotonic() - started
        finally:
            if process.poll() is None:
                process.kill()
        self.assertEqual(process.returncode, jobs.EXIT_CANCELLED, stderr)
        events = _events(first + stdout)
        self.assertEqual(events[-1]["event"], "cancelled")
        self.assertIsInstance(events[-1]["done"], int)
        self.assertGreater(events[-1]["done"], 0)
        self.assertNotIn("done", [event["event"] for event in events])
        self.assertLess(stopped_in, 5.0)

    def test_sigterm_stops_with_exit_3(self):
        if sys.platform == "win32":
            self.skipTest("POSIX signal")
        process = subprocess.Popen(self._command("--seconds", "60", "--units", "600"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env(), cwd=self.tmp)
        try:
            first = process.stdout.readline()
            process.terminate()
            stdout, stderr = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
        self.assertEqual(process.returncode, jobs.EXIT_CANCELLED, stderr)
        self.assertEqual(_events(first + stdout)[-1]["event"], "cancelled")

    def test_resume_skips_done_units(self):
        out = self.path("out")
        cancel_file = self.path("STOP")
        process = subprocess.Popen(self._command("--seconds", "30", "--units", "300", "--out", out, "--cancel-file", cancel_file), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env(), cwd=self.tmp)
        try:
            process.stdout.readline()
            time.sleep(0.5)
            with open(cancel_file, "w", encoding="utf-8"):
                pass
            process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
        self.assertEqual(process.returncode, 3)
        done_before = len(jobs.ResumeState(os.path.join(out, ".selftest-resume.json")).done)
        self.assertGreater(done_before, 0)
        os.remove(cancel_file)
        code, events, stderr = self._run("--seconds", "3", "--units", "300", "--out", out, "--resume")
        self.assertEqual(code, 0, stderr)
        progress = [event for event in events if event["event"] == "progress"]
        self.assertEqual(len(progress), 300 - done_before)
        self.assertEqual(events[-1]["units"], 300)

    def test_fail_gives_exit_1_and_error_event(self):
        code, events, stderr = self._run("--seconds", "0.1", "--units", "3", "--fail")
        self.assertEqual(code, jobs.EXIT_ERROR, stderr)
        error = events[-1]
        self.assertEqual((error["event"], error["code"], error["type"]), ("error", "exception", "RuntimeError"))
        self.assertIn("failure requested", error["message"])
        self.assertTrue(1 <= len(error["traceback"]) <= 5)
        self.assertIn("RuntimeError", error["traceback"][-1])

    def test_usage_error_gives_exit_2_with_json(self):
        for args in (["--units", "abc"], ["--unknown"], ["--sec", "1"]):  # abbreviations are refused
            code, events, _ = self._run(*args)
            self.assertEqual(code, jobs.EXIT_USAGE, args)
            self.assertEqual((events[-1]["event"], events[-1]["code"]), ("error", "usage"), args)
        result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.jobs", "nonsense"], check=False, capture_output=True, text=True, env=_env(), cwd=self.tmp, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(_events(result.stdout)[-1]["code"], "usage")
        code, events, _ = self._run("--threads", "0")
        self.assertEqual((code, events[-1]["code"]), (jobs.EXIT_USAGE, "usage"))

    def test_help_keeps_stdout_json_only(self):
        result = subprocess.run(self._command("--help"), check=False, capture_output=True, text=True, env=_env(), cwd=self.tmp, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("--threads", result.stderr)

    def test_blas_threads_set_before_numpy(self):
        code, events, stderr = self._run("--seconds", "0", "--units", "1", "--threads", "3", "--numpy")
        self.assertEqual(code, 0, stderr)
        start = events[0]
        self.assertEqual(start["blas"]["OPENBLAS_NUM_THREADS"], "3")
        self.assertEqual(set(start["blas"].values()), {"3"})
        self.assertIs(start["numpy_before_blas"], False)
        self.assertIn("numpy", next(event for event in events if event["event"] == "snapshot"))
        code, events, stderr = self._run("--seconds", "0", "--units", "1", "--threads=1")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(events[0]["blas"]["OMP_NUM_THREADS"], "1")
        code, events, stderr = self._run("--seconds", "0", "--units", "1")
        self.assertEqual(events[0]["blas"]["MKL_NUM_THREADS"], "2")  # default

    def test_blas_threads_in_child_process(self):
        script = textwrap.dedent(
            """
            import os, sys
            from LesionNetworkMappingLib.jobs import set_blas_threads
            set_blas_threads(sys.argv[1:])
            print("numpy" in sys.modules, os.environ["OPENBLAS_NUM_THREADS"])
            """,
        )
        result = subprocess.run([sys.executable, "-c", script, "--threads", "5"], check=False, capture_output=True, text=True, env=_env(), cwd=self.tmp, timeout=60)
        self.assertEqual(result.stdout.split(), ["False", "5"], result.stderr)

    def test_parent_watchdog(self):
        if sys.platform == "win32":
            self.skipTest("uses a POSIX sleep process")
        parent = subprocess.Popen(["sleep", "30"])
        try:
            process = subprocess.Popen(self._command("--seconds", "60", "--units", "600", "--parent-pid", str(parent.pid)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env(), cwd=self.tmp)
            first = process.stdout.readline()
            parent.kill()
            parent.wait()
            stdout, stderr = process.communicate(timeout=30)
        finally:
            for proc in (parent, process):
                if proc.poll() is None:
                    proc.kill()
        self.assertEqual(process.returncode, 3, stderr)
        cancelled = _events(first + stdout)[-1]
        self.assertEqual(cancelled["event"], "cancelled")
        self.assertIn("is gone", cancelled["reason"])

    def test_orphan_watchdog(self):
        if sys.platform == "win32":
            self.skipTest("POSIX re-parenting")
        # An intermediate shell plays the PythonSlicer launcher: killing it re-parents the job.
        marker = self.path("events.jsonl")
        launcher = subprocess.Popen(["sh", "-c", f'"{sys.executable}" -m LesionNetworkMappingLib.jobs selftest --seconds 60 --units 600 > "{marker}" 2>/dev/null; true'], env=_env(), cwd=self.tmp)
        try:
            deadline = time.monotonic() + 30
            start = None
            while time.monotonic() < deadline and start is None:
                time.sleep(0.1)
                if os.path.exists(marker):
                    with open(marker, encoding="utf-8") as f:
                        line = f.readline()
                    if line.endswith("\n"):
                        start = json.loads(line)
            self.assertIsNotNone(start)
            launcher.kill()
            launcher.wait()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and jobs.pid_alive(start["pid"]):
                time.sleep(0.1)
            self.assertFalse(jobs.pid_alive(start["pid"]), "the orphaned job kept running")
            with open(marker, encoding="utf-8") as f:
                events = _events(f.read())
            self.assertEqual(events[-1]["event"], "cancelled")
            self.assertIn("launcher", events[-1]["reason"])
        finally:
            if launcher.poll() is None:
                launcher.kill()
            if start and jobs.pid_alive(start["pid"]):
                os.kill(start["pid"], 9)


class JobPrimitivesTest(_TempDirTestCase):
    def test_budget_threads(self):
        old = os.environ.pop(jobs.MAX_THREADS_ENV, None)
        try:
            self.assertEqual(jobs.budget_threads(), max(1, (len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()) - 1))
            self.assertEqual(jobs.budget_threads(7), 7)
            os.environ[jobs.MAX_THREADS_ENV] = "1"
            self.assertEqual(jobs.budget_threads(), 1)
            self.assertEqual(jobs.budget_threads(7), 1)
            os.environ[jobs.MAX_THREADS_ENV] = "junk"
            self.assertEqual(jobs.budget_threads(3), 3)
        finally:
            os.environ.pop(jobs.MAX_THREADS_ENV, None)
            if old is not None:
                os.environ[jobs.MAX_THREADS_ENV] = old

    def test_resume_state(self):
        path = self.path("sub", "state.json")
        state = jobs.ResumeState(path)
        self.assertFalse(state.is_done("a"))
        state.mark_done("a")
        state.mark_done(2)
        state.data["params"] = {"x": 1}
        state.save()
        again = jobs.ResumeState(path)
        self.assertTrue(again.is_done("a") and again.is_done("2") and again.is_done(2))
        self.assertEqual(again.data, {"params": {"x": 1}})
        self.assertEqual(os.listdir(self.path("sub")), ["state.json"])  # no temporary file left
        with open(path, "w", encoding="utf-8") as f:
            f.write("{garbage")
        self.assertEqual(jobs.ResumeState(path).done, [])
        again.clear()
        self.assertFalse(os.path.exists(path))

    def test_cancel_token(self):
        cancel_file = self.path("STOP")
        token = jobs.CancelToken(cancel_file, signals=False)
        self.assertFalse(token.cancelled() or token.is_set() or token())
        with open(cancel_file, "w", encoding="utf-8"):
            pass
        self.assertTrue(token.cancelled() and token.is_set() and token())
        with self.assertRaises(jobs.Cancelled) as raised:
            token.raise_if_cancelled(done=4)
        self.assertEqual(raised.exception.done, 4)
        other = jobs.CancelToken(None, signals=False)
        other.cancel("test")
        self.assertTrue(other.cancelled())
        self.assertEqual(other.reason, "test")

    def test_run_job_exit_codes(self):
        buffer = []

        def capture(main):
            import contextlib
            import io

            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = jobs.run_job(main, [], job="t", orphan=False, exit=False)
            buffer[:] = _events(stream.getvalue())
            return code

        def cancelled(argv):
            raise jobs.Cancelled(done=2)

        def locked(argv):
            raise jobs.Locked("busy")

        def custom(argv):
            return 5

        self.assertEqual(capture(lambda argv: None), 0)
        self.assertEqual(capture(cancelled), 3)
        self.assertEqual((buffer[-1]["event"], buffer[-1]["done"]), ("cancelled", 2))
        self.assertEqual(capture(locked), 75)
        self.assertEqual((buffer[-1]["event"], buffer[-1]["code"]), ("error", "locked"))
        self.assertEqual(capture(custom), 5)

    def test_emit_is_strict_json(self):
        import contextlib
        import io

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            jobs.emit("warning", message="x", value=float("nan"), path=jobs.__file__ and os.path.abspath(self.tmp), items={1, 2})
        event = json.loads(stream.getvalue())
        self.assertEqual((event["event"], event["value"], sorted(event["items"])), ("warning", None, [1, 2]))
        self.assertEqual(list(event)[:2], ["event", "t"])

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            jobs.emit("warning", t=-1, values={(1, 2): float("inf")})
        event = json.loads(stream.getvalue())
        self.assertGreaterEqual(event["t"], 0)
        self.assertEqual(event["values"], {"(1, 2)": None})

    def test_emit_t_is_monotonic_across_threads(self):
        import contextlib
        import io
        import threading

        stream = io.StringIO()

        def worker():
            for index in range(200):
                jobs.emit("progress", done=index)

        with contextlib.redirect_stdout(stream):
            threads = [threading.Thread(target=worker) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        times = [event["t"] for event in _events(stream.getvalue())]
        self.assertEqual(len(times), 1200)
        self.assertEqual(times, sorted(times))

    def test_run_job_keeps_slicer_signal_handlers(self):
        import signal
        import types

        saved = {name: signal.getsignal(getattr(signal, name)) for name in ("SIGINT", "SIGTERM")}
        installed = jobs._signals_installed["done"]
        fake = "slicer" not in sys.modules
        try:
            if fake:
                sys.modules["slicer"] = types.ModuleType("slicer")
            jobs._signals_installed["done"] = False
            marker = signal.getsignal(signal.SIGTERM)
            self.assertEqual(jobs.run_job(lambda argv: None, [], orphan=False, exit=False), 0)
            self.assertIs(signal.getsignal(signal.SIGTERM), marker)
            self.assertFalse(jobs._signals_installed["done"])
        finally:
            if fake:
                del sys.modules["slicer"]
            jobs._signals_installed["done"] = installed
            for name, handler in saved.items():
                signal.signal(getattr(signal, name), handler)

    def test_reset_cancel(self):
        jobs.request_cancel("test")
        try:
            self.assertTrue(jobs.CancelToken(None, signals=False).cancelled())
        finally:
            jobs.reset_cancel()
        self.assertFalse(jobs.CancelToken(None, signals=False).cancelled())

    def test_output_lock(self):
        path = self.path("out", ".lock")
        lock = jobs.OutputLock(path, heartbeat_s=0.1, stale_s=60).acquire()
        try:
            with open(path, encoding="utf-8") as f:
                holder = json.load(f)
            self.assertEqual(set(holder), {"pid", "host", "token", "started"})
            self.assertEqual(holder["pid"], os.getpid())
            with self.assertRaises(jobs.Locked) as raised:
                jobs.OutputLock(path).acquire()
            self.assertEqual(raised.exception.exit_code, 75)
            os.utime(path, (time.time() - 30, time.time() - 30))
            time.sleep(0.4)
            self.assertGreater(os.stat(path).st_mtime, time.time() - 5)  # heartbeat
        finally:
            lock.release()
        self.assertFalse(os.path.exists(path))
        # Stale by age
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "host": "elsewhere", "token": "x", "started": "then"}, f)
        os.utime(path, (time.time() - 120, time.time() - 120))
        with jobs.OutputLock(path) as reclaimed:
            self.assertEqual(reclaimed.reclaimed["host"], "elsewhere")
        # Stale because its process is gone on this host
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": dead.pid, "host": jobs._host(), "token": "y", "started": "now"}, f)
        with jobs.OutputLock(path) as reclaimed:
            self.assertEqual(reclaimed.reclaimed["token"], "y")
        # A lock on another host that is fresh is live
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": 1, "host": "elsewhere", "token": "z", "started": "now"}, f)
        with self.assertRaises(jobs.Locked):
            jobs.OutputLock(path).acquire()

    def test_priority_and_keep_awake(self):
        script = "import os; from LesionNetworkMappingLib import jobs; changed = jobs.set_priority(5); print(changed, os.nice(0) if hasattr(os, 'nice') else 5)"
        result = subprocess.run([sys.executable, "-c", script], check=False, capture_output=True, text=True, env=_env(), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        changed, nice = result.stdout.split()
        self.assertEqual(changed, "True")
        self.assertGreaterEqual(int(nice), 5)
        self.assertFalse(jobs.set_priority(0))
        with jobs.KeepAwake() as awake:
            self.assertEqual(awake.active, sys.platform in ("darwin", "win32"))
        self.assertFalse(awake.active)


class RunRegistryTest(_TempDirTestCase):
    def test_new_run_id(self):
        run_id = runs.new_run_id("glm")
        self.assertRegex(run_id, r"^glm-\d{8}T\d{6}-[0-9a-f]{4}$")
        with self.assertRaises(ValueError):
            runs.new_run_id("nonsense")
        self.assertIn("pipeline", runs.KINDS)

    def test_register_update_fold(self):
        root = self.tmp
        self.assertEqual(runs.load(root), {})
        map_id = runs.register(root, "map", self.path("derivatives", "lnm", "map1"), params={"b": 1, "a": [1, 2]}, seeds={"subjects": 0}, software_versions=SOFTWARE)
        record = runs.load(root)[map_id]
        self.assertEqual(record["dir"], "derivatives/lnm/map1")
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["params_sha256"], runs.params_sha256({"a": [1, 2], "b": 1}))
        self.assertEqual(record["software"], SOFTWARE)
        self.assertTrue(record["created"].endswith("Z"))
        runs.update(root, map_id, status="done", summary={"n": 10})
        runs.update(root, map_id, summary={"n": 12})
        record = runs.load(root)[map_id]
        self.assertEqual((record["status"], record["summary"]), ("done", {"n": 12}))
        self.assertIn("updated", record)
        outside = tempfile.mkdtemp(prefix="lnm-outside-")
        try:
            other = runs.register(root, "export", outside, software_versions=SOFTWARE)
            self.assertEqual(runs.load(root)[other]["dir"], os.path.realpath(outside))
        finally:
            shutil.rmtree(outside)
        with self.assertRaises(ValueError):
            runs.update(root, map_id, id="x")
        with open(runs.registry_path(root), encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 4)

    def test_software_default_uses_export_versions(self):
        run_id = runs.register(self.tmp, "map", self.tmp)
        software = runs.load(self.tmp)[run_id]["software"]
        self.assertEqual(software["LesionNetworkMappingLib"], runs._software()["LesionNetworkMappingLib"])
        self.assertIn("python", software)

    def test_queries_and_lineage(self):
        root = self.tmp
        maps = runs.register(root, "cohortmaps", self.path("maps"), software_versions=SOFTWARE)
        glm_a = runs.register(root, "glm", self.path("glm-a"), parents=[maps], params={"alpha": 0.05}, software_versions=SOFTWARE)
        glm_b = runs.register(root, "glm", self.path("glm-b"), parents=[maps], params={"alpha": 0.01}, software_versions=SOFTWARE)
        figure = runs.register(root, "figure", self.path("fig"), parents=[glm_b, "nulls-missing"], software_versions=SOFTWARE)
        runs.update(root, glm_a, status="done")
        self.assertEqual([r["id"] for r in runs.list_runs(root, kind="glm")], [glm_a, glm_b])
        self.assertEqual([r["id"] for r in runs.list_runs(root, kind="glm", status="done")], [glm_a])
        self.assertEqual([r["id"] for r in runs.list_runs(root, kind=("glm", "figure"))], [glm_a, glm_b, figure])
        self.assertEqual(runs.latest(root, "glm")["id"], glm_b)
        self.assertEqual(runs.latest(root, "glm", where={"status": "done"})["id"], glm_a)
        self.assertEqual(runs.latest(root, "glm", where=lambda r: r["params"]["alpha"] > 0.02)["id"], glm_a)
        self.assertIsNone(runs.latest(root, "vlsm"))
        ancestors = runs.lineage(root, figure)
        self.assertEqual([r["id"] for r in ancestors], [glm_b, "nulls-missing", maps])
        self.assertTrue(ancestors[1]["missing"])
        self.assertEqual([r["id"] for r in runs.children(root, maps)], [glm_a, glm_b])
        self.assertEqual(runs.lineage(root, "unknown"), [])
        # A cycle (only possible through a hand-edited registry) terminates
        loop_a = runs.register(root, "report", self.path("r"), parents=["report-b"], run_id="report-a", software_versions=SOFTWARE)
        runs.register(root, "report", self.path("r"), parents=[loop_a], run_id="report-b", software_versions=SOFTWARE)
        self.assertEqual([r["id"] for r in runs.lineage(root, "report-a")], ["report-b"])

    def test_malformed_lines_are_skipped(self):
        root = self.tmp
        first = runs.register(root, "map", self.path("a"), software_versions=SOFTWARE)
        with open(runs.registry_path(root), "ab") as f:
            f.write(b'{"op":"register","id":"tru')  # a writer crashed mid-line
        second = runs.register(root, "map", self.path("b"), software_versions=SOFTWARE)
        with open(runs.registry_path(root), "ab") as f:
            f.write(b"not json\n\xff\xfe\n[1,2]\n")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            loaded = runs.load(root)
        self.assertEqual(list(loaded), [first, second])
        self.assertTrue(any(issubclass(w.category, runs.RegistryWarning) for w in caught))

    def test_large_summary_goes_to_a_file(self):
        root = self.tmp
        run_id = runs.register(root, "nulls", self.path("nulls"), software_versions=SOFTWARE)
        big = {"values": list(range(30000))}
        runs.update(root, run_id, status="done", summary=big)
        record = runs.load(root)[run_id]
        self.assertEqual(set(record["summary"]), {"$file"})
        self.assertTrue(record["summary"]["$file"].startswith("nulls/"))
        self.assertEqual(runs.read_summary(root, record), big)
        with open(runs.registry_path(root), "rb") as f:
            self.assertTrue(all(len(line) <= runs.MAX_RECORD_BYTES for line in f))
        with self.assertRaises(ValueError):
            runs.register(root, "map", None, summary=big, software_versions=SOFTWARE)

    def test_non_finite_values_are_stored_as_null(self):
        root = self.tmp
        params = {"alpha": float("inf"), "weights": [1.0, float("nan")]}
        run_id = runs.register(root, "glm", self.path("glm"), params=params, summary={"auc": float("nan")}, software_versions=SOFTWARE)
        runs.update(root, run_id, status="done", summary={"auc": float("nan"), "p": [0.5, float("-inf")]})
        record = runs.load(root)[run_id]
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["summary"], {"auc": None, "p": [0.5, None]})
        self.assertEqual(record["params"], {"alpha": None, "weights": [1.0, None]})
        self.assertEqual(record["params_sha256"], runs.params_sha256(record["params"]))
        with open(runs.registry_path(root), encoding="utf-8") as f:
            for line in f:
                json.loads(line, parse_constant=lambda name: self.fail(f"non-standard JSON constant {name}"))
        try:
            import numpy as np
        except ImportError:
            return
        runs.update(root, run_id, summary={"auc": np.float64("nan"), "n": np.int64(3), "v": np.array([1.0, np.inf])})
        self.assertEqual(runs.load(root)[run_id]["summary"], {"auc": None, "n": 3, "v": [1.0, None]})
        big = {"values": [float("nan")] * 20000}
        runs.update(root, run_id, summary=big)
        record = runs.load(root)[run_id]
        self.assertEqual(set(record["summary"]), {"$file"})
        self.assertEqual(runs.read_summary(root, record), {"values": [None] * 20000})

    def test_large_params_go_to_a_file(self):
        root = self.tmp
        params = {"subjects": [f"sub-{index:05d}" for index in range(8000)]}
        run_id = runs.register(root, "cohortmaps", self.path("maps"), params=params, software_versions=SOFTWARE)
        record = runs.load(root)[run_id]
        self.assertEqual(set(record["params"]), {"$file"})
        self.assertEqual(runs.read_params(root, record), params)
        self.assertEqual(record["params_sha256"], runs.params_sha256(params))

    def test_concurrent_appends(self):
        root = self.path("study")
        script = textwrap.dedent(
            """
            import sys
            from LesionNetworkMappingLib import runs
            root, worker = sys.argv[1], sys.argv[2]
            for index in range(200):
                run_id = runs.register(root, "map", root + "/maps/" + worker, params={"worker": worker, "index": index, "pad": "x" * (index * 37 % 3000)}, run_id=f"map-{worker}-{index}", software_versions={"python": "x"})
            """,
        )
        processes = [subprocess.Popen([sys.executable, "-c", script, root, str(worker)], env=_env(), stderr=subprocess.PIPE, text=True) for worker in range(4)]
        for process in processes:
            _, stderr = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, stderr)
        with open(runs.registry_path(root), "rb") as f:
            lines = f.read().split(b"\n")
        self.assertEqual(lines[-1], b"")
        records = [json.loads(line) for line in lines[:-1]]  # every line is one whole record
        self.assertEqual(len(records), 800)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            loaded = runs.load(root)
        self.assertEqual(len(loaded), 800)
        for worker in range(4):
            indices = [record["params"]["index"] for record in records if record["params"]["worker"] == str(worker)]
            self.assertEqual(indices, list(range(200)))
            self.assertEqual(loaded[f"map-{worker}-7"]["dir"], f"maps/{worker}")


class ImportCostTest(unittest.TestCase):
    def test_no_numpy_import(self):
        script = "import sys; import LesionNetworkMappingLib.jobs, LesionNetworkMappingLib.runs; print('numpy' in sys.modules, 'scipy' in sys.modules)"
        result = subprocess.run([sys.executable, "-c", script], check=False, capture_output=True, text=True, env=_env(), timeout=60)
        self.assertEqual(result.stdout.split(), ["False", "False"], result.stderr)


if __name__ == "__main__":
    unittest.main()

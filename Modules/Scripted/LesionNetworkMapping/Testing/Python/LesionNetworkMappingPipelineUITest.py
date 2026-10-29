"""Tests of the Run everything card (A-04c): the Map card of the cohort pipeline and the Library Analysis card.

They need the Slicer application and run the real pipeline (A-04) on the synthetic study of ``lnm_pipeline_fixture.py``
(12 mapped lesions, 2 groups, 1 score; no network), in Classic under ``--testing``::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingPipelineUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
import signal
import sys
import tempfile
import time
import unittest

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

#: Largest gap of the 10 ms probe timer while the pipeline runs on an idle host (ms); a busy host gets a looser bound
MAX_GAP_IDLE_MS = 100.0
MAX_GAP_BUSY_MS = 400.0
#: Longest a run of the fixture may take
RUN_TIMEOUT_S = 120.0
#: Options of the stop / resume test: one lesion per engine pass (a tiny memory budget) and relocation nulls that miss the cache
ONE_PER_PASS = {"memory_budget_gb": 1e-6, "null_relocations": 30}


def _wait(condition, timeoutS, step=0.01):
    deadline = time.monotonic() + timeoutS
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        if condition():
            return True
        time.sleep(step)
    slicer.app.processEvents()
    return bool(condition())


def _pump(ms):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


def _hostBusy():
    try:
        return os.getloadavg()[0] > (os.cpu_count() or 1) * 0.75
    except (AttributeError, OSError):
        return False


class _GapProbe:
    """A 10 ms QTimer that records the gaps between its ticks (GUI responsiveness)."""

    def __init__(self):
        self.timer = qt.QTimer()
        self.timer.setInterval(10)
        self.timer.connect("timeout()", self._tick)
        self.last = None
        self.gaps = []

    def _tick(self):
        now = time.perf_counter()
        if self.last is not None:
            self.gaps.append((now, (now - self.last) * 1000.0))
        self.last = now

    def maxGapMs(self):
        return max((gap for _when, gap in self.gaps), default=0.0)

    def start(self):
        self.last = None
        self.gaps = []
        self.timer.start()

    def stop(self):
        self.timer.stop()


def _processesWith(text):
    """Pids of the processes whose command line contains ``text`` (Linux /proc; [] elsewhere)."""
    if not os.path.isdir("/proc"):
        return []
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                command = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as f:
                state = f.read().rsplit(")", 1)[-1].split()[0]
        except OSError:
            continue
        if text in command and state != "Z":
            found.append(int(entry))
    return found


class LesionNetworkMappingPipelineUITest(unittest.TestCase):
    """The card in Classic, on one fixture study; the tests run in name order and build on each other."""

    @classmethod
    def setUpClass(cls):
        import lnm_pipeline_fixture as fixture

        from LesionNetworkMapping import LesionNetworkMappingLogic
        from LesionNetworkMappingUI import card_pipeline, jobrunner

        cls.cp = card_pipeline
        cls.jobrunner = jobrunner
        cls.tmp = tempfile.mkdtemp(prefix="lnm pipeline ui ")
        cls.fx = fixture.make_fixture(os.path.join(cls.tmp, "f1"), extra_rows=False)
        cls.settingsBefore = set(qt.QSettings().allKeys())
        slicer.util.selectModule("LesionNetworkMapping")
        cls.widget = slicer.modules.LesionNetworkMappingWidget
        cls.bundle = LesionNetworkMappingLogic.registerConnectome(cls.fx["bundle"])
        LesionNetworkMappingLogic.setTermsAccepted(cls.bundle, True)  # the settings key, as "I accept" does
        cls.widget.refreshConnectomes(select=cls.bundle.path)
        cls.widget.openCohortStudy(cls.fx["study"])
        card_pipeline.setCohortSeed(cls.widget)
        cls.card = cls.widget.ctx.card("pipeline")
        cls.participants = os.path.join(cls.fx["study"], "participants.tsv")
        with open(cls.participants, "rb") as f:
            cls.participantsBytes = f.read()
        qt.QSettings().remove(card_pipeline.OPTIONS_SETTING)

    @classmethod
    def tearDownClass(cls):
        for job in cls.cp.pipelineJobs():
            job.stop()
        _wait(lambda: not cls.cp.pipelineJobs(), 20)
        try:
            from StudioLib import study_ui

            study_ui.closeStudy()
        except Exception:
            pass
        qt.QSettings().remove(cls.cp.OPTIONS_SETTING)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # Helpers

    def _settle(self):
        """Let the coalesced updates and the staleness plan finish."""
        self.card.scheduleRecheck()
        _pump(self.cp.RECHECK_MS + 100)
        self.card.worker.waitForIdle(30)
        _pump(self.cp.APPLY_MS + 50)

    def _runToEnd(self, job, timeoutS=RUN_TIMEOUT_S):
        self.assertIsNotNone(job, self.card.stateLabel.text)
        self.assertTrue(job.waitForFinished(timeoutS), f"the pipeline did not end within {timeoutS} s")
        _pump(self.cp.APPLY_MS + 50)
        return job

    def _setOptions(self, options):
        self.cp.saveOptions(dict(self.cp.DEFAULT_OPTIONS, **options))

    def _out(self):
        return self.card.outDir()

    # Tests

    def test_1_run(self):
        """Run everything: one process within 50 ms of the click, Done within 120 s, GUI gaps < 100 ms; Map network blocked meanwhile."""
        card, widget = self.card, self.widget
        self._settle()
        self.assertEqual(card.stateLabel.text, "Never run")
        self.assertIn("12 lesions", card.summaryLabel.text)
        self.assertIn("8 cases / 4 controls", card.summaryLabel.text)
        self.assertIn("aphasia_score", card.summaryLabel.text)
        self.assertTrue(card.estimateLabel.text)
        override = widget.ctx.primaryOverride("cohort")
        self.assertIsNotNone(override, "the card registers the primary action of Seed = Cohort")
        self.assertEqual(override[0], "Run everything · 12 lesions")
        self.assertEqual(widget.computeButton.text, "Run everything · 12 lesions")
        self.assertTrue(widget.mapsOnlyButton.visible and widget.mapsOnlyButton.enabled)
        self.assertTrue(card.runButton.enabled, card.runButton.toolTip)
        probe = _GapProbe()
        probe.start()
        card.applyMaxSeconds = 0.0
        started = time.perf_counter()
        card.runButton.click()
        clickMs = (time.perf_counter() - started) * 1000.0
        job = card.job
        self.assertIsNotNone(job)
        self.assertIsNotNone(job.proc, "one PythonSlicer process is started by the click")
        self.assertLessEqual(clickMs, 50.0 if not _hostBusy() else 150.0, f"the click took {clickMs:.1f} ms")
        self.assertIn("--accept-terms", job.args)
        self.assertIn("--parent-pid", job.args)
        self.assertEqual(job.args[job.args.index("--nice") + 1], "5")
        self.assertIn("--cancel-file", job.args)
        self.assertEqual(self.cp.pipelineJobs(), [job])
        self.assertTrue(_wait(lambda: job.startEvent is not None, 30))
        _pump(200)
        self.assertTrue(card.stopButton.isVisible() and not card.runButton.isVisible())
        self.assertFalse(widget.ctx.computeEnabled(), "Map network is blocked while the pipeline runs")
        self.assertFalse(widget.mapsOnlyButton.enabled)
        self.assertTrue(card.stateLabel.text.startswith("Running ·"), card.stateLabel.text)
        self.assertTrue(card.progressBar.isVisible())
        self._runToEnd(job)
        probe.stop()
        applyMaxMs = card.applyMaxSeconds * 1000.0
        self.assertEqual(job.state, "done", job.failureReason)
        gap = probe.maxGapMs()
        limit = MAX_GAP_BUSY_MS if _hostBusy() else MAX_GAP_IDLE_MS
        print(f"run: click {clickMs:.1f} ms, run {job.finishedAt - job.startedAt:.1f} s, largest GUI gap {gap:.1f} ms, longest card update {applyMaxMs:.2f} ms")
        self.assertLess(gap, limit, f"the GUI froze for {gap:.0f} ms during the run")
        self._settle()
        self.assertTrue(widget.ctx.computeEnabled())
        self.assertTrue(widget.mapsOnlyButton.enabled)
        self.assertTrue(card.stateLabel.text.startswith("Up to date"), card.stateLabel.text)
        glyphs = {rowId: parts[0].text for rowId, parts in card.rowWidgets.items()}
        for rowId in ("prepare", "maps", "nulls", "statistics", "qa", "export", "report"):
            self.assertEqual(glyphs[rowId], "✓", (rowId, glyphs))
        self.assertTrue(card.rowWidgets["normalize"][0].isHidden(), "Normalize is shown only when native lesions are normalized")
        self.assertTrue(card.resultFrame.isVisible())
        self.assertTrue(card.openReportButton.enabled)
        # Statuses: the inbox is posted, participants.tsv is never written by the card (no study session in Classic here)
        inbox = (self.cp.modelFor(job).doneEvent or {}).get("inbox")
        self.assertTrue(inbox and os.path.isfile(inbox), inbox)
        with open(self.participants, "rb") as f:
            self.assertEqual(f.read(), self.participantsBytes, "participants.tsv was changed")
        self.assertEqual(card.noteLabel.text, "Statuses will update when the study is opened in Library.")
        self.assertEqual(self.cp.modelFor(job).statusText(), "Cohort · Report 100%")

    def test_2_open_report_fallbacks(self):
        """Open report: the URL opener with report.html; when it refuses (or SLICER_STUDIO_WEB=1), the in-app viewer, else the path."""
        card = self.card
        calls = []
        original = self.cp._openUrl
        self.cp._openUrl = lambda url: calls.append(url.toLocalFile()) or True
        try:
            self.assertEqual(card.openReport(), "desktop")
            self.assertEqual(calls[-1], os.path.join(self._out(), "report.html"))
            self.cp._openUrl = lambda url: calls.append(url.toLocalFile()) or False
            self.assertEqual(card.openReport(), "viewer" if hasattr(slicer, "qSlicerWebWidget") else "dialog")
            viewer = card.viewers[-1]
            self.assertTrue(viewer.dialog.isVisible() and not viewer.dialog.isModal())
            viewer.dialog.close()
            os.environ["SLICER_STUDIO_WEB"] = "1"
            count = len(calls)
            try:
                card.openReport()
            finally:
                del os.environ["SLICER_STUDIO_WEB"]
            self.assertEqual(len(calls), count, "the web build never hands the report to the desktop")
            card.viewers[-1].dialog.close()
            webWidget = getattr(slicer, "qSlicerWebWidget", None)
            if webWidget is not None:
                delattr(slicer, "qSlicerWebWidget")
            try:
                self.assertEqual(card.showFolder(), "dialog")
            finally:
                if webWidget is not None:
                    slicer.qSlicerWebWidget = webWidget
            dialog = card.viewers[-1].dialog
            field = dialog.findChild(qt.QLineEdit, "LNMPipelinePathField")
            self.assertEqual(field.text, self._out())
            dialog.close()
        finally:
            self.cp._openUrl = original

    def test_3_load_maps(self):
        """Load maps: at most 6 volumes, one per tick, on the bundle grid, LNM.Result=1, T-like maps on 'LNM Diverging'."""
        import numpy as np
        import vtk

        classesBefore = {slicer.mrmlScene.GetNthNode(i).GetClassName() for i in range(slicer.mrmlScene.GetNumberOfNodes())}
        probe = _GapProbe()
        probe.start()
        queued = self.card.loadMaps()
        self.assertTrue(1 <= len(queued) <= 6, queued)
        self.assertTrue(_wait(lambda: not self.card.mapQueue and self.card._loadStack is None, 20))
        probe.stop()
        volumes = self.card.loadedVolumes
        self.assertEqual(len(volumes), len(queued))
        self.assertEqual([volume.GetName() for volume in volumes], [f"LNM {desc}" for desc in queued])
        affine = np.asarray(self.bundle.affine, dtype=float)
        for volume, desc in zip(volumes, queued, strict=True):
            self.assertEqual(volume.GetAttribute("LNM.Result"), "1")
            ijkToRas = vtk.vtkMatrix4x4()
            volume.GetIJKToRASMatrix(ijkToRas)
            values = np.array([[ijkToRas.GetElement(r, c) for c in range(4)] for r in range(4)])
            self.assertTrue(np.allclose(values, affine, atol=1e-5), (desc, values, affine))
            if desc.endswith(("Tstat", "R")) or desc == "meanT":
                self.assertEqual(volume.GetDisplayNode().GetColorNode().GetName(), "LNM Diverging", desc)
        classesAfter = {slicer.mrmlScene.GetNthNode(i).GetClassName() for i in range(slicer.mrmlScene.GetNumberOfNodes())}
        allowed = {"vtkMRMLScalarVolumeNode", "vtkMRMLScalarVolumeDisplayNode", "vtkMRMLColorTableNode", "vtkMRMLVolumeArchetypeStorageNode"}
        self.assertTrue((classesAfter - classesBefore) <= allowed, classesAfter - classesBefore)
        gap = probe.maxGapMs()
        print(f"load maps: {len(volumes)} maps, largest GUI gap {gap:.1f} ms")
        self.assertLess(gap, MAX_GAP_BUSY_MS if _hostBusy() else MAX_GAP_IDLE_MS)
        # Loading again reuses the volumes
        self.card.loadMaps()
        self.assertTrue(_wait(lambda: not self.card.mapQueue and self.card._loadStack is None, 20))
        self.assertEqual(len([n for n in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode") if n.GetAttribute("LNM.PipelineMap")]), len(queued))

    def test_4_stale_then_one_lesion(self):
        """One rewritten lesion: '1 new or changed lesion' within 2 s of the card being shown; the run computes exactly that lesion."""
        from LesionNetworkMappingLib import nifti

        card = self.card
        study = self.widget.cohortStudy
        path = study.abspath(study.subject("sub-03")["lesion"])
        data, affine, _header = nifti.read_nifti(path)
        changed = data.copy()
        first = tuple(int(axis[0]) for axis in (data > 0).nonzero())
        changed[first] = 0  # one voxel less: another lesion file, same grid
        nifti.write_nifti(path, changed, affine)
        card.widget.hide()
        _pump(50)
        shown = time.monotonic()
        card.widget.show()
        self.assertTrue(_wait(lambda: card.stateLabel.text.startswith("1 new or changed lesion"), 10), card.stateLabel.text)
        elapsed = time.monotonic() - shown
        print(f"stale: '{card.stateLabel.text}' after {elapsed:.2f} s")
        self.assertLessEqual(elapsed, 2.0 if not _hostBusy() else 5.0)
        self.assertIn("11 up to date", card.stateLabel.text)
        job = self._runToEnd(card.run())
        self.assertEqual(job.state, "done", job.failureReason)
        start = job.startEvent
        self.assertEqual(start["n_compute"], 1, start)
        self.assertEqual(start["n_reuse"], 11, start)

    def test_4b_design_change_is_not_up_to_date(self):
        """A group or include change after a run: the card and the Library card say the design changed, never 'Up to date'."""
        from LesionNetworkMappingLib.study import read_tsv, write_tsv

        card = self.card
        self._settle()
        self.assertTrue(card.stateLabel.text.startswith("Up to date"), card.stateLabel.text)
        with open(self.participants, "rb") as f:
            before = f.read()
        columns, rows = read_tsv(self.participants)
        columns = columns if "include" in columns else [*columns, "include"]
        cases = [row for row in rows if row["group"] == "case"]
        cases[0]["group"] = "control"
        cases[1]["include"] = "0"
        try:
            write_tsv(self.participants, columns, rows)
            self._settle()
            self.assertIn("11 lesions", card.summaryLabel.text)
            self.assertIn("6 cases / 5 controls", card.summaryLabel.text)
            self.assertEqual(card.stateLabel.text, "Design changed since the last run")
            try:
                from StudioLib import libcard_analysis
            except ImportError:
                libcard_analysis = None
            if libcard_analysis is not None:
                text, level = libcard_analysis.stateLine(libcard_analysis.latestAnalysis(self.fx["study"]), card.plan)
                self.assertEqual((text, level), ("Cohort analysis: design changed since the last run", "warning"))
        finally:
            with open(self.participants, "wb") as f:
                f.write(before)
        self._settle()
        self.assertTrue(card.stateLabel.text.startswith("Up to date"), card.stateLabel.text)

    def test_5_stop_and_resume(self):
        """With one lesion per pass: Stop shows 'Stopped · k of 12 maps kept' within 3 s; the next run reuses at least k and completes."""
        card = self.card
        self._setOptions(ONE_PER_PASS)
        self._settle()
        self.assertTrue(card.stateLabel.text.startswith("12 new or changed lesions"), card.stateLabel.text)
        job = card.run()
        self.assertIsNotNone(job)
        model = self.cp.modelFor(job)
        self.assertTrue(_wait(lambda: model.current == "maps" and int(model.progress.get("batch") or 0) >= 3, 90), (model.current, model.progress))
        self.assertEqual(int(model.progress.get("batches") or 0), 12, model.progress)
        stopped = time.monotonic()
        card.stopButton.click()
        self.assertTrue(_wait(lambda: card.stateLabel.text.startswith("Stopped ·"), 10), card.stateLabel.text)
        elapsed = time.monotonic() - stopped
        text = card.stateLabel.text
        print(f"stop: '{text}' after {elapsed:.2f} s (exit code {job.exitCode})")
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(job.exitCode, 3)
        self.assertLessEqual(elapsed, 3.0 if not _hostBusy() else 6.0)
        kept = int(text.split("·")[1].split("of")[0])
        self.assertTrue(text.endswith("of 12 maps kept"), text)
        self.assertGreaterEqual(kept, 1)
        job = self._runToEnd(card.run())
        self.assertEqual(job.state, "done", job.failureReason)
        self.assertGreaterEqual(job.startEvent["n_reuse"], kept, job.startEvent)

    def test_6_launcher_terminated(self):
        """Terminating only the PythonSlicer launcher leaves no pipeline or engine process after 5 s."""
        card = self.card
        self._setOptions(dict(ONE_PER_PASS, null_relocations=20))
        self._settle()
        job = card.run()
        self.assertIsNotNone(job)
        self.assertTrue(_wait(lambda: job.pid is not None, 30))
        worker = job.pid
        self.assertNotEqual(worker, job.proc.pid, "the launcher is not the worker")
        job.proc.terminate()  # only the launcher
        from LesionNetworkMappingLib import jobs

        terminated = time.monotonic()
        gone = _wait(lambda: not jobs.pid_alive(worker), 5.0, step=0.05)
        print(f"launcher terminated: worker {worker} gone after {time.monotonic() - terminated:.2f} s")
        self.assertTrue(gone, f"the pipeline worker {worker} survived its launcher")
        self.assertTrue(_wait(lambda: not _processesWith(self.fx["study"]), 5.0, step=0.05), _processesWith(self.fx["study"]))
        self.assertTrue(_wait(job.isFinished, 20))
        _pump(200)
        self.assertNotEqual(job.state, "done")

    def test_7_reattach(self):
        """A card created while a run goes on follows it; a run of another process shows 'Running in another process' and stops with STOP."""
        card = self.card
        self._setOptions(dict(ONE_PER_PASS, null_relocations=10))
        self._settle()
        job = card.run()
        self.assertTrue(_wait(lambda: job.startEvent is not None, 30))
        other = self.cp.PipelineCard(self.widget.ctx)
        try:
            self.assertIs(other.job, job, "a re-created card re-attaches to the running job")
            _pump(300)
            self.assertTrue(other.stateLabel.text.startswith("Running ·"), other.stateLabel.text)
            before = other.progressBar.value
            self.assertTrue(_wait(lambda: other.progressBar.value > before or job.isFinished(), 30))
        finally:
            other.cleanup()
            other.widget.deleteLater()
            self.widget.ctx.registerCard("pipeline", card)
            card._publishedPrimary = None
            card.apply()
        job.stop()
        self.assertTrue(job.waitForFinished(30))
        # A command-line run of the same output folder (another process)
        import subprocess

        config = card.config(dict(self.cp.DEFAULT_OPTIONS, **dict(ONE_PER_PASS, null_relocations=7)), forRun=True)
        config.pop("parent_pid", None)
        args = [self.jobrunner.pythonExecutable(), "-m", self.cp.PIPELINE_MODULE, *self.cp.commandArguments(config)]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([self.jobrunner.moduleDir(), os.environ.get("PYTHONPATH", "")]), LC_ALL="C.UTF-8")
        process = subprocess.Popen(args, cwd=self.jobrunner.moduleDir(), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            out = self._out()
            lock = os.path.join(out, ".pipeline", "lock.json")
            self.assertTrue(_wait(lambda: os.path.isfile(lock) and os.path.isfile(os.path.join(out, ".pipeline", "progress.json")), 30))
            _pump(300)
            card.recheck()
            self.assertTrue(_wait(lambda: card.stateLabel.text.startswith("Running in another process"), 5), card.stateLabel.text)
            self.assertFalse(card.runButton.isVisible())
            self.assertFalse(card.canRun())
            card.stopButton.click()
            self.assertTrue(_wait(lambda: process.poll() is not None, 15), "the STOP file stops the other process")
            self.assertEqual(process.returncode, 3)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(10)
        _pump(200)
        card.recheck()
        self.assertTrue(_wait(lambda: not card.stateLabel.text.startswith("Running"), 5), card.stateLabel.text)

    def test_8_session_merges_inbox(self):
        """Statuses become 'mapped' only when the study session merges the inbox (on the main thread); the card never writes them."""
        from LesionNetworkMappingLib.study import Study

        self.assertNotIn("mapped", {row.get("status") for row in Study.open(self.fx["study"]).subjects})
        try:
            from StudioLib import study_ui
        except ImportError:
            self.skipTest("the Studio module is not part of this build")
        session = study_ui.openStudy(self.fx["study"], showLibrary=False)
        try:
            self.assertTrue(_wait(lambda: {row.get("status") for row in Study.open(self.fx["study"]).subjects} == {"mapped"}, 10),
                            {row.get("status") for row in Study.open(self.fx["study"]).subjects})
            # A run with the session open: the card flushes it first, and the session merges the inbox when the run is done
            self._setOptions({})
            self._settle()
            job = self._runToEnd(self.card.run())
            self.assertEqual(job.state, "done", job.failureReason)
            self.assertTrue(getattr(self.cp.modelFor(job), "inboxApplied", False))
            self.assertEqual(self.card.noteLabel.text, "")
        finally:
            study_ui.closeStudy()
        self.assertIsNotNone(session)

    def test_9_options_dialog_and_settings(self):
        """The options dialog is non-modal, at most 420 px wide, and stores its values in Studio/LNM/Pipeline/Options only."""
        card = self.card
        self.cp.saveOptions(dict(self.cp.DEFAULT_OPTIONS, contrast="A>B"))  # a recipe's one-sided contrast (no control in the dialog)
        options = card.showOptions()
        dialog = options.dialog
        self.assertEqual(dialog.objectName, "LNMPipelineOptionsDialog")
        self.assertTrue(dialog.isVisible() and not dialog.isModal())
        self.assertLessEqual(dialog.width, self.cp.DIALOG_MAX_WIDTH)
        options.volumeCheck.setChecked(False)
        self.assertFalse(options.startButton.enabled, "turning the volume covariate off needs a reason")
        options.volumeReason.setText("volume is matched by design")
        self.assertTrue(options.startButton.enabled)
        options.sensitivitySpin.setValue(80)
        values = options.values()
        self.assertEqual(values["volume_off_reason"], "volume is matched by design")
        self.assertEqual(values["sensitivity"], 80.0)
        self.assertEqual(values["contrast"], "A>B", "the dialog keeps the recipe's contrast")
        self.assertFalse(options.ruleChecks["t7"].enabled, "|T| >= 7 needs N close to 1000")
        self.cp.saveOptions(values)
        config = card.config(values, forRun=True)
        args = self.cp.commandArguments(config)
        self.assertIn("--no-volume-covariate", args)
        self.assertEqual(args[args.index("--sensitivity") + 1], "80")
        self.assertEqual(args[args.index("--contrast") + 1], "A>B")
        self.assertNotIn("--contrast", self.cp.commandArguments(card.config(dict(values, contrast="two_sided"), forRun=True)))
        dialog.close()
        qt.QSettings().remove(self.cp.OPTIONS_SETTING)
        added = set(qt.QSettings().allKeys()) - self.settingsBefore
        pipelineKeys = sorted(key for key in added if "Pipeline" in key or "AutoRerun" in key)
        self.assertEqual(pipelineKeys, [], pipelineKeys)
        self.assertFalse([key for key in qt.QSettings().allKeys() if key.endswith("AutoRerun")])

    def test_a_palette_entries_and_library_card(self):
        """The Search entries (dict contract) and the Library Analysis card's line."""
        entries = {entry["id"]: entry for entry in self.cp.paletteEntries()}
        self.assertEqual(entries["lnm.run-everything"]["title"], "Run cohort analysis (lesion network mapping)…")
        self.assertIn("map all lesions", entries["lnm.run-everything"]["synonyms"])
        self.assertIn("lnm.open-report", entries)
        self.assertIn("lnm.show-analysis-folder", entries)
        try:
            from StudioLib import libcard_analysis
        except ImportError:
            self.skipTest("the Studio module is not part of this build")
        analysis = libcard_analysis.latestAnalysis(self.fx["study"])
        self.assertIsNotNone(analysis)
        text, level = libcard_analysis.stateLine(analysis)
        self.assertTrue(text.startswith("Cohort analysis: up to date"), text)
        self.assertEqual(level, "success")
        self.assertEqual(libcard_analysis.allTraced(self.widget.cohortStudy)[0], 12)

    def test_b_foreign_compute_block(self):
        """A Compute block of another Map plug-in (e.g. Preflight) refuses Run everything: card button, primary override and Ctrl+Enter."""
        card, widget = self.card, self.widget
        self._settle()
        self.assertTrue(card.canRun(), card.runButton.toolTip)
        reason = "Preflight: sub-05 lesion file is missing"
        widget.ctx.setComputeEnabled(False, reason, key="test-block")
        try:
            _pump(self.cp.APPLY_MS + 50)
            widget.updateComputeState()
            self.assertEqual(card.runState(), (False, reason))
            self.assertFalse(card.runButton.enabled)
            self.assertFalse(widget.computeButton.enabled, "the primary override follows the block")
            self.assertIsNone(card.run())
            widget.onPrimary()  # the Ctrl+Enter path
            card.runButton.click()
            _pump(200)
            self.assertEqual(self.cp.pipelineJobs(), [], "Run everything started while Compute was blocked")
        finally:
            widget.ctx.setComputeEnabled(True, key="test-block")
        _pump(self.cp.APPLY_MS + 50)
        self.assertTrue(card.canRun(), card.runButton.toolTip)

    def test_c_other_study_never_run(self):
        """After a run on study A, a never-run study B shows 'Never run' (no glyph, warning or result of A's run)."""
        import lnm_pipeline_fixture as fixture

        card, widget = self.card, self.widget
        self.cp.saveOptions(dict(self.cp.DEFAULT_OPTIONS))
        self._settle()
        if card.job is None or card.job.state != "done":
            self.assertEqual(self._runToEnd(card.run()).state, "done")
        self._settle()
        self.assertTrue(card.stateLabel.text.startswith("Up to date"), card.stateLabel.text)
        other = fixture.make_fixture(os.path.join(self.tmp, "f2"), extra_rows=False, study_name="Study B")
        try:
            widget.openCohortStudy(other["study"])
            self.cp.setCohortSeed(widget)
            _pump(self.cp.APPLY_MS + 50)
            immediate = card.stateLabel.text
            self.assertFalse(immediate.startswith("Up to date"), immediate)
            self._settle()
            self.assertIn("Study B", card.outDir())
            self.assertEqual(card.stateLabel.text, "Never run")
            glyphs = "".join(parts[0].text for parts in card.rowWidgets.values())
            self.assertNotIn("✓", glyphs)
            self.assertNotIn("↺", glyphs)
            self.assertFalse(card.resultFrame.isVisible())
        finally:
            widget.openCohortStudy(self.fx["study"])
            self.cp.setCohortSeed(widget)
            qt.QSettings().remove(self.cp.OPTIONS_SETTING)
        self._settle()
        self.assertTrue(card.stateLabel.text.startswith("Up to date"), card.stateLabel.text)


if __name__ == "__main__":
    unittest.main()

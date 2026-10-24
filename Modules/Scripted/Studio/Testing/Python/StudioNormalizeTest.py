"""Tests of the study automation in Library (A-03c): normalize the native subjects of a study in the background, review the
results, revert, redo, stale lesions, the automatic mode, Stop and resume, and the Classic panel.

They need the main window and BRAINSFit, and run the real normalization job (``normalize batch``) on the synthetic native
subject of ``lnm_normalize_fixture.py`` (no network). Studio never runs by itself under ``--testing``: the tests apply it
with ``Shell(force=True)``::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest Studio StudioNormalizeTest

The tests share the study of the class and run in the order of their numbers (the batch first). Under a plain PythonSlicer
(the library test glob) the module is skipped.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

LNM_TEST_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "LesionNetworkMapping", "Testing", "Python"))
if LNM_TEST_DIR not in sys.path:
    sys.path.insert(0, LNM_TEST_DIR)

TEMPLATE = "MNI152NLin2009cAsym"
#: Largest gap of the 10 ms probe timer during a batch on an idle host (ms); a busy host gets a looser bound
MAX_GAP_IDLE_MS = 100.0
MAX_GAP_BUSY_MS = 400.0
#: The Done hook returns at once (ms)
MAX_HOOK_MS = 5.0
BATCH_TIMEOUT_S = 150


def _wait(condition, timeoutS, step=0.01):
    deadline = time.monotonic() + timeoutS
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        if condition():
            return True
        time.sleep(step)
    slicer.app.processEvents()
    return bool(condition())


def _pump(ms=50):
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        time.sleep(0.005)


def _load():
    return os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class _GapProbe:
    """A 10 ms QTimer that records the gaps between its ticks (responsiveness of the main thread)."""

    def __init__(self):
        self.timer = qt.QTimer()
        self.timer.setInterval(10)
        self.timer.connect("timeout()", self._tick)
        self.last = None
        self.gaps = []
        self.times = []

    def _tick(self):
        now = time.perf_counter()
        if self.last is not None:
            self.gaps.append((now - self.last) * 1000.0)
            self.times.append(now)
        self.last = now

    def start(self):
        self.last = None
        self.gaps = []
        self.times = []
        self.began = time.perf_counter()
        self.timer.start()

    def stop(self):
        self.timer.stop()

    def maxGapMs(self):
        return max(self.gaps) if self.gaps else 0.0

    def longGaps(self, limit=100.0):
        """[(seconds after start, gap ms)] of the gaps over ``limit`` (for the failure message)."""
        return [(round(when - self.began, 2), round(gap)) for when, gap in zip(self.times, self.gaps, strict=False) if gap > limit]


def _settingsKeys():
    return {str(key) for key in qt.QSettings().allKeys()}


class StudioNormalizeTest(unittest.TestCase):
    """The Norm column, the filters, the Normalization card and section, the batch job, review, revert, redo, stale, auto, Stop."""

    @classmethod
    def setUpClass(cls):
        import numpy as np

        import lnm_normalize_fixture as fixture
        from LesionNetworkMappingLib import nifti
        from LesionNetworkMappingLib.study import Study

        cls.np, cls.fixture, cls.nifti, cls.Study = np, fixture, nifti, Study
        cls.tmp = tempfile.mkdtemp(prefix="StudioNormalizeTest")
        slicer.mrmlScene.Clear(0)
        subject = fixture.make_native(os.path.join(cls.tmp, "source"), "sub-src", ("large",))
        data, affine, _header = nifti.read_nifti(subject["lesion"])
        cls.scanFile = subject["scan"]
        cls.lesionFile = os.path.join(cls.tmp, "source", "lesion_binary.nii.gz")
        nifti.write_nifti(cls.lesionFile, (np.asarray(data) >= 0.5).astype(np.uint8), affine, sform_code=nifti.XFORM_SCANNER, qform_code=nifti.XFORM_SCANNER)
        # participants.tsv is written on the main thread only (SPEC single-writer rule)
        cls.saveThreads = []
        cls._originalSave = Study.save

        def guardedSave(study, *args, **kwargs):
            cls.saveThreads.append(threading.current_thread() is threading.main_thread())
            return cls._originalSave(study, *args, **kwargs)

        Study.save = guardedSave
        from StudioLib import shell

        cls.shell = shell.Shell(force=True)
        cls.shell.apply()
        _pump(100)
        # Studio builds the Map panel once, shortly after it starts (a one-time pause that is not part of what is measured)
        _wait(lambda: getattr(slicer.modules, "LesionNetworkMappingWidget", None) is not None, 20)
        _pump(300)
        cls.studies = {}
        cls.keysBefore = _settingsKeys()

    @classmethod
    def tearDownClass(cls):
        from StudioLib import libcard_normalize, shell, study_ui

        cls.Study.save = cls._originalSave
        manager = libcard_normalize._manager
        if manager is not None:
            manager.stop()
            _wait(lambda: not manager.isBusy(), 30)
        try:
            study_ui.closeStudy()
        except Exception:
            pass
        if shell._shell is not None:
            shell._shell.dispose()
        slicer.mrmlScene.Clear(0)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    #
    # Helpers
    #

    @classmethod
    def makeStudy(cls, name, native=3, templateRows=1, statuses=None):
        """A study of ``native`` native rows (scan + lesion, traced) and ``templateRows`` template-space rows. Return its path."""
        np, Study, nifti = cls.np, cls.Study, cls.nifti
        path = os.path.join(cls.tmp, name)
        study = Study.create(path, name=name, space=TEMPLATE)
        for index in range(1, native + 1):
            sid = f"sub-{index:02d}"
            folder = os.path.join(path, "native", sid)
            os.makedirs(folder)
            scan = os.path.join(folder, f"{sid}_T1w.nii.gz")
            lesion = os.path.join(folder, f"{sid}_desc-lesion_mask.nii.gz")
            shutil.copyfile(cls.scanFile, scan)
            shutil.copyfile(cls.lesionFile, lesion)
            status = (statuses or {}).get(sid, "traced")
            study.add_subject(sid, scan=study.relpath(scan), lesion=study.relpath(lesion), space="native", status=status, group="case")
        for index in range(templateRows):
            sid = f"sub-{native + index + 1:02d}"
            lesion = os.path.join(path, "lesions", f"{sid}_space-{TEMPLATE}_desc-lesion_mask.nii.gz")
            truth, affine = cls.fixture.truth(("small",))
            nifti.write_nifti(lesion, (truth >= 0.5).astype(np.uint8), affine)
            study.add_subject(sid, lesion=study.relpath(lesion), space=TEMPLATE, status="traced", group="control")
        policy = study.trace_policy
        policy["space"] = TEMPLATE
        study.trace_policy = policy
        study.metadata["normalization"] = {"auto_after_done": False, "template": "auto", "quality": "affine", "dilate_mm": 3.0}
        study.save()
        return path

    def openStudy(self, path, withShell=True):
        from StudioLib import study_ui

        session = study_ui.openStudy(path, self.shell if withShell else None)
        _pump(100)
        self.assertIsNotNone(session)
        return session

    def card(self):
        from StudioLib import libcard_normalize

        from StudioLib import study_ui

        slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
        session = study_ui.session()
        cards = [libcard_normalize._cards[w] for w in slicer.util.mainWindow().findChildren("QWidget", "StudioNormalizeLibraryCard")
                 if libcard_normalize._cards.get(w) is not None and libcard_normalize._cards[w].session is session]
        self.assertTrue(cards, "the Normalization card was not created")
        return cards[-1]

    def lists(self):
        from StudioLib import library

        self.shell.switchTo("library")
        _pump(100)
        library.canvas().refreshData()
        _pump(50)
        return library.canvas().studyPage.list

    def chips(self, sids=None):
        from StudioLib import libcard_normalize as ln
        from StudioLib import study_ui

        session = study_ui.session()
        return {row["participant_id"]: ln.normCell(row)[0] for row in session.study.subjects if sids is None or row["participant_id"] in sids}

    def disk(self, path, sid):
        return self.Study.open(path).subject(sid)

    def waitIdle(self, timeoutS=BATCH_TIMEOUT_S):
        from StudioLib import libcard_normalize as ln

        manager = ln._manager
        self.assertIsNotNone(manager)
        self.assertTrue(_wait(lambda: not manager.isBusy(), timeoutS), "the normalization job did not finish")
        _pump(150)

    def auditEvents(self, path, event=None, sid=None):
        events = []
        try:
            with open(os.path.join(path, ".studio", "audit.jsonl"), encoding="utf-8") as f:
                for line in f:
                    record = json.loads(line)
                    if (event is None or record.get("event") == event) and (sid is None or record.get("sid") == sid):
                        events.append(record)
        except FileNotFoundError:
            pass
        return events

    #
    # The columns, the filters and the card
    #

    def test_01_chips_filters_card(self):
        from LesionNetworkMappingLib import schema
        from StudioLib import library, palette

        path = self.makeStudy("study")
        self.__class__.studies["main"] = path
        session = self.openStudy(path)
        listWidget = self.lists()
        self.assertEqual(library.libcardModules() and [entry["key"] for entry in library.libcardColumns() if entry["key"] == "norm"], ["norm"])
        headers = [str(listWidget.model.headerData(i, qt.Qt.Horizontal)) for i in range(listWidget.model.columnCount())]
        self.assertIn("Norm", headers)
        self.assertEqual(self.chips(), {"sub-01": "Native", "sub-02": "Native", "sub-03": "Native", "sub-04": "—"})
        # Needs scan, No lesion
        study = session.study
        study.set("sub-03", scan=None)
        self.assertEqual(self.chips(["sub-03"]), {"sub-03": "Needs scan"})
        study.set("sub-03", scan=study.subject("sub-01")["scan"], lesion=None)
        self.assertEqual(self.chips(["sub-03"]), {"sub-03": "No lesion"})
        study.set("sub-03", lesion=study.subject("sub-01")["lesion"])
        # Filters and queue
        # The normalization filters, after the lesion QC filters of L-28 (libcard_flags: 2D, Has flags)
        self.assertEqual([entry["key"] for entry in library.libcardFilters()], ["two_d", "has_flags", "needs_norm", "norm_review"])
        review = next(entry for entry in library.libcardFilters() if entry["key"] == "norm_review")
        self.assertEqual(review["qcColumn"], "norm_qc")
        listWidget.setFilter("needs_norm")
        self.assertEqual(sorted(row["participant_id"] for row in listWidget.model.rows), ["sub-01", "sub-02", "sub-03"])
        listWidget.setFilter("norm_review")
        self.assertEqual(listWidget.model.rowCount(), 0)
        self.assertEqual(listWidget.qcColumn(), "norm_qc")
        listWidget.setFilter("all")
        self.assertEqual(listWidget.qcColumn(), "qc")
        self.assertEqual([entry["key"] for entry in library.libcardQueues()], ["norm_review"])
        # The card
        card = self.card()
        card.refresh()
        self.assertTrue(card.widget.isVisible() or card.widget.isVisibleTo(card.widget.window()))
        self.assertEqual(str(card.summary.text), "3 native")
        self.assertEqual(str(card.primary.text), "Normalize 3 native subjects")
        self.assertTrue(card.primary.isEnabled())
        for name in ("StudioNormalizeAllButton", "StudioNormalizeAutoCheck", "StudioNormalizeOptionsButton", "StudioNormalizeStopButton"):
            self.assertIsNotNone(card.widget.findChild("QWidget", name), name)
        self.assertFalse(card.autoCheck.isChecked())
        # Search entries (dict contract) and the columns of the registry
        entries = {entry.id: entry for entry in palette.providerEntries(self.shell)}
        self.assertEqual(entries["lnm.normalize-all"].title, "Normalize all native lesions")
        self.assertEqual(entries["lnm.normalize-review"].title, "Review normalizations")
        self.assertTrue(entries["lnm.normalize-all"].enabled())
        self.assertEqual(entries["lnm.normalize-all"].category, "Library")
        for column in ("scan_native", "lesion_native", "norm_method", "norm_qc", "norm_suggestion", "norm_fingerprint", "volume_native_ml"):
            self.assertIsNotNone(schema.registered(column), column)

    #
    # The batch
    #

    def test_02_batch(self):
        from StudioLib import libcard_normalize as ln

        path = self.studies["main"]
        session = self.openStudy(path) if self._session() is None else self._session()
        card = self.card()
        lesionsBefore = {name: _sha(os.path.join(path, "lesions", name)) for name in os.listdir(os.path.join(path, "lesions")) if name.endswith(".nii.gz")}
        self.saveThreads.clear()
        probe = _GapProbe()
        load = _load()
        runner = ln._jobrunner().instance()
        concurrent = []
        runner.jobEvent.append(lambda job, event: concurrent.append(len([j for j in runner.runningJobs() if j.heavy])))
        probe.start()
        seen = []
        started = time.perf_counter()
        card.primary.click()
        manager = ln._manager
        self.assertTrue(manager.isBusy())
        deadline = time.monotonic() + BATCH_TIMEOUT_S
        while manager.isBusy() and time.monotonic() < deadline:
            slicer.app.processEvents()
            time.sleep(0.01)
            state = tuple(self.chips(["sub-01", "sub-02", "sub-03"]).values())
            if not seen or seen[-1] != state:
                seen.append(state)
        _pump(200)
        probe.stop()
        elapsed = time.perf_counter() - started
        runner.jobEvent.pop()
        self.assertFalse(manager.isBusy(), "the batch did not finish")
        self.assertTrue(any(state[0].startswith("Running") and state[1] == "Queued" and state[2] == "Queued" for state in seen), seen)
        self.assertTrue(any(state[0] in ("Unreviewed", "Review") and state[1].startswith("Running") and state[2] == "Queued" for state in seen), seen)
        self.assertTrue(max(concurrent or [1]) <= 1, "more than one heavy job ran at a time")
        chips = self.chips()
        for sid in ("sub-01", "sub-02", "sub-03"):
            self.assertIn(chips[sid], ("Review", "Unreviewed"), chips)
        self.assertEqual(chips["sub-04"], "—")
        # The batch itself keeps CPUs - 1 cores busy (BRAINSFit): the machine is idle only if it was before and after it
        load = max(load, _load())
        print(f"StudioNormalizeTest batch: max gap {probe.maxGapMs():.1f} ms (load {load:.1f}, {elapsed:.0f} s)")
        self.assertLessEqual(probe.maxGapMs(), MAX_GAP_IDLE_MS if load < 1.0 else MAX_GAP_BUSY_MS, f"max gap {probe.maxGapMs():.0f} ms (load {load:.1f}, {elapsed:.0f} s); long gaps {probe.longGaps()}")
        # participants.tsv: written by the main thread only, with the values of A-03's inbox contract
        self.assertTrue(self.saveThreads and all(self.saveThreads), self.saveThreads)
        for sid in ("sub-01", "sub-02", "sub-03"):
            row = self.disk(path, sid)
            self.assertEqual(row["space"], TEMPLATE)
            self.assertEqual(row["norm_qc"], "unreviewed")
            self.assertTrue(row["norm_method"].startswith("brainsfit-cfm:affine"), row["norm_method"])
            self.assertIn(row["norm_suggestion"], ("looks-ok", "review"))
            self.assertEqual(len(row["norm_fingerprint"]), 12)
            self.assertTrue(row["scan"].startswith(f"derivatives/normalization/{sid}/"))
            self.assertTrue(row["lesion"].startswith(f"derivatives/normalization/{sid}/"))
            self.assertEqual(row["scan_native"], f"native/{sid}/{sid}_T1w.nii.gz")
            self.assertEqual(row["lesion_native"], f"native/{sid}/{sid}_desc-lesion_mask.nii.gz")
            self.assertGreater(float(row["volume_native_ml"]), 0)
            self.assertGreater(float(row["volume_ml"]), 0)
        # lesions/ was never written
        lesionsAfter = {name: _sha(os.path.join(path, "lesions", name)) for name in os.listdir(os.path.join(path, "lesions")) if name.endswith(".nii.gz")}
        self.assertEqual(lesionsAfter, lesionsBefore)
        self.assertEqual(len(self.auditEvents(path, "normalize")), 3)
        self.assertIn("Normalized 3 subjects", manager.lastSummary)
        # The card: counts, Review button, nothing left to normalize
        card.refresh()
        self.assertEqual(str(card.summary.text), "0 native · 3 normalized (3 to review, 0 pass, 0 fail)")
        self.assertEqual(str(card.reviewButton.text), "Review 3 →")
        self.assertFalse(card.primary.isEnabled())
        self.assertFalse(os.path.exists(os.path.join(path, ".studio", ln.QUEUE_FILE)))

    def _session(self):
        from StudioLib import study_ui

        return study_ui.session()

    def test_03_review_queue_and_verdicts(self):
        from StudioLib import library
        from StudioLib import libcard_normalize as ln

        path = self.studies["main"]
        session = self._session()
        listWidget = self.lists()
        study = session.study
        # The filter and the queue list the unreviewed results
        card = self.card()
        card.reviewButton.click()
        _pump(100)
        self.assertEqual(listWidget.filterKey(), "norm_review")
        self.assertEqual(sorted(row["participant_id"] for row in listWidget.model.rows), ["sub-01", "sub-02", "sub-03"])
        self.assertEqual(library.studySelection(), ["sub-01"])
        # Pass on row 1: norm_qc in participants.tsv and one audit line
        self.assertTrue(ln.setNormQc("sub-01", "pass"))
        _pump(100)
        self.assertEqual(self.disk(path, "sub-01")["norm_qc"], "pass")
        events = self.auditEvents(path, "norm_qc", "sub-01")
        self.assertEqual(len(events), 1, events)
        self.assertEqual((events[0]["from"], events[0]["to"]), ("unreviewed", "pass"))
        self.assertEqual(self.chips(["sub-01"]), {"sub-01": "Pass"})
        # analysis_rows excludes the rows that are normalized and not passed
        rows, excluded = self.Study.open(path).analysis_rows()
        self.assertEqual(sorted(row["participant_id"] for row in rows), ["sub-01", "sub-04"])
        self.assertEqual(sorted(excluded.get("norm_qc != pass", [])), ["sub-02", "sub-03"])
        # Next to review (SUBJECT section, after a verdict) opens the next unreviewed result in table order
        listWidget.selectIds(["sub-01"])
        section = self.section()
        self.assertTrue(_wait(lambda: not section.nextRow.isHidden(), 10), "Next to review was not offered after the verdict")
        self.assertIn("2 left", str(section.nextButton.text))
        section.nextButton.click()
        _pump(300)
        self.assertEqual(session.current(), "sub-02")
        # Review in Draw: the subject opens in Draw with the template behind and the warped scan at 50 % (A-03b's layers)
        controller = ln._reviewController()
        self.assertTrue(ln.reviewInDraw("sub-02"))
        _pump(100)
        self.assertEqual(self.shell.currentWorkspace(), "draw")
        self.assertTrue(controller.reviewing)
        composites = slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode")
        self.assertTrue(composites and all(abs(composite.GetForegroundOpacity() - 0.5) < 1e-6 and composite.GetForegroundVolumeID() for composite in composites))
        # Opening another subject ends the review without touching the new subject's layers
        self.assertTrue(session.open("sub-03", switchToDraw=False))
        self.assertFalse(controller.reviewing)
        self.assertTrue(session.open("sub-02", switchToDraw=False))
        listWidget = self.lists()
        # The QC panel of A-03b in the SUBJECT section: Doesn't look right on row 2
        listWidget.selectIds(["sub-02"])
        section = self.section()
        self.assertTrue(_wait(lambda: section.qcPanel is not None, 20), "the QC panel was not built")
        panel = section.qcPanel
        self.assertIsNotNone(panel.findChild("QPushButton", "LNMNormalizeMosaic"))
        panel.findChild("QPushButton", "LNMNormalizeFailButton").click()
        _pump(100)
        self.assertEqual(self.disk(path, "sub-02")["norm_qc"], "fail")
        self.assertEqual(len(self.auditEvents(path, "norm_qc", "sub-02")), 1)
        review = ln._qcResultFor("sub-02").review()
        self.assertEqual(review.get("verdict"), "fail")
        self.assertEqual(self.chips(["sub-02"]), {"sub-02": "Fail"})
        # Next to review is offered after a verdict
        self.assertTrue(_wait(lambda: not section.nextRow.isHidden(), 5))
        # P in the list writes norm_qc while the review filter is active
        listWidget.setFilter("norm_review")
        listWidget.selectIds(["sub-03"])
        shortcuts = {str(shortcut.objectName): shortcut for shortcut in listWidget.shortcuts}
        shortcuts["StudioLibraryQcPassShortcut"].activated()
        _pump(150)
        self.assertEqual(self.disk(path, "sub-03")["norm_qc"], "pass")
        self.assertEqual(study.subject("sub-03")["qc"], "unreviewed")  # the general QC column is untouched
        self.assertTrue(_wait(lambda: len(self.auditEvents(path, "norm_qc", "sub-03")) == 1, 5), self.auditEvents(path, "norm_qc", "sub-03"))
        listWidget.setFilter("all")

    def section(self):
        from StudioLib import libcard_normalize as ln

        from StudioLib import study_ui

        session = study_ui.session()
        sections = [ln._sections[w] for w in slicer.util.mainWindow().findChildren("QWidget", "StudioNormalizeSubjectSection")
                    if ln._sections.get(w) is not None and ln._sections[w].session is session]
        self.assertTrue(sections)
        return sections[-1]

    def test_04_revert_and_undo(self):
        from StudioLib import libcard_normalize as ln

        path = self.studies["main"]
        session = self._session()
        before = dict(self.disk(path, "sub-03"))
        # sub-03 is open: its scene is reloaded with the native scan and lesion
        self.assertTrue(session.open("sub-03", switchToDraw=False))
        self.assertIn("derivatives/normalization/sub-03", session.volumeNode().GetStorageNode().GetFileName())
        self.assertTrue(ln.revertToNative("sub-03", announce=False))
        self.assertEqual(session.current(), "sub-03")
        self.assertTrue(session.volumeNode().GetStorageNode().GetFileName().endswith("native/sub-03/sub-03_T1w.nii.gz"))
        self.assertFalse(session.isDirty())
        row = self.disk(path, "sub-03")
        self.assertEqual(row["scan"], before["scan_native"])
        self.assertEqual(row["lesion"], before["lesion_native"])
        self.assertEqual(row["space"], "native")
        for column in ("norm_method", "norm_qc", "norm_suggestion", "norm_fingerprint", "scan_native", "lesion_native", "volume_native_ml"):
            self.assertEqual(row[column], "n/a", column)
        self.assertEqual(row["volume_ml"], before["volume_native_ml"])
        self.assertTrue(os.path.isdir(os.path.join(path, "derivatives", "normalization", "sub-03")))
        self.assertEqual(len(self.auditEvents(path, "norm_revert", "sub-03")), 1)
        self.assertEqual(self.chips(["sub-03"]), {"sub-03": "Native"})
        # The row can be normalized again; Undo puts the normalized values back
        previous = {key: before.get(key) for key in ln._REVERT_KEYS}
        self.assertTrue(ln.undoRevert("sub-03", previous))
        self.assertIn("derivatives/normalization/sub-03", session.volumeNode().GetStorageNode().GetFileName())
        row = self.disk(path, "sub-03")
        self.assertEqual((row["space"], row["norm_qc"], row["scan"]), (TEMPLATE, "pass", before["scan"]))
        self.assertEqual(self.chips(["sub-03"]), {"sub-03": "Pass"})

    def test_05_stale_native_lesion_and_rewarp(self):
        from StudioLib import libcard_normalize as ln

        np = self.np
        path = self.studies["main"]
        session = self._session()
        native = session.study.abspath(session.study.subject("sub-02")["lesion_native"])
        ln.refreshStale(wait=True)
        self.assertEqual(self.chips(["sub-02"]), {"sub-02": "Fail"})
        # An edit of the native lesion after its normalization: the chip says so (a worker thread checks the file)
        data, affine, _header = self.nifti.read_nifti(native)
        edited = np.asarray(data).copy()
        edited[edited > 0] = 0
        index = np.argwhere(np.asarray(data) > 0)
        center = index.mean(axis=0).astype(int)
        edited[tuple(slice(max(0, c - 4), c + 4) for c in center)] = 1
        self.nifti.write_nifti(native, edited.astype(np.uint8), affine, sform_code=self.nifti.XFORM_SCANNER, qform_code=self.nifti.XFORM_SCANNER)
        ln.refreshStale(wait=True)
        self.assertEqual(self.chips(["sub-02"]), {"sub-02": "Stale"})
        self.assertEqual(self.chips(["sub-01", "sub-03"]), {"sub-01": "Pass", "sub-03": "Pass"})
        self.assertTrue(ln.needsNormalization(session.study.subject("sub-02")))
        card = self.card()
        card.refresh()
        self.assertIn("1 stale", str(card.summary.text))
        self.assertTrue(card.primary.isEnabled())
        self.assertEqual(str(card.primary.text), "Normalize 1 native subject")
        # Re-warp the edited lesion with the same transform (a light job): the row points at the new warped lesion
        job = ln.rewarpSubject("sub-02")
        self.assertIsNotNone(job)
        self.assertTrue(_wait(job.isFinished, 60), "the re-warp did not finish")
        self.assertTrue(job.succeeded(), job.failureReason)
        _pump(200)
        row = self.disk(path, "sub-02")
        self.assertIn("/warps/", row["lesion"])
        self.assertEqual(row["norm_qc"], "fail")
        self.assertEqual(row["lesion_native"], f"native/sub-02/sub-02_desc-lesion_mask.nii.gz")
        ln.refreshStale(wait=True)
        self.assertEqual(self.chips(["sub-02"]), {"sub-02": "Fail"})
        self.assertEqual(len(self.auditEvents(path, "norm_rewarp", "sub-02")), 1)

    def test_06_redo(self):
        from StudioLib import libcard_normalize as ln

        path = self.studies["main"]
        before = self.disk(path, "sub-01")
        job = ln.normalizeSubjects(["sub-01"], options={"dilate_mm": 6.0}, redo=True)
        self.assertIsNotNone(job)
        self.assertTrue(self.chips(["sub-01"])["sub-01"] in ("Queued",) or self.chips(["sub-01"])["sub-01"].startswith("Running"))
        self.assertIn("--force", job.args)
        self.assertIn("--no-reuse", job.args)
        self.assertEqual(job.args[job.args.index("--dilate-mm") + 1], "6")
        self.waitIdle()
        row = self.disk(path, "sub-01")
        self.assertNotEqual(row["norm_fingerprint"], before["norm_fingerprint"])
        self.assertEqual(row["norm_qc"], "unreviewed")  # a new result needs a new review
        self.assertEqual(row["scan_native"], before["scan_native"])
        self.assertEqual(self.chips(["sub-01"])["sub-01"] in ("Unreviewed", "Review"), True)
        # The native lesion stays the native one (the newest run of the row is compared, not the one before it)
        self.assertEqual(row["lesion_native"], before["lesion_native"])
        self.assertEqual(len(self.auditEvents(path, "norm_lesion_edited")), 0)
        ln.refreshStale(wait=True)
        self.assertNotEqual(self.chips(["sub-01"])["sub-01"], "Stale")

    def test_06b_normalize_stale_row(self):
        from StudioLib import libcard_normalize as ln

        np = self.np
        path = self.makeStudy("stale", native=1, templateRows=0)
        session = self.openStudy(path)
        card = self.card()
        card.primary.click()
        self.waitIdle()
        first = dict(self.disk(path, "sub-01"))
        native = first["lesion_native"]
        self.assertNotIn("derivatives", native)
        # Edit the native lesion: the row is Stale
        nativePath = session.study.abspath(native)
        data, affine, _header = self.nifti.read_nifti(nativePath)
        edited = np.asarray(data).copy()
        edited[edited > 0] = 0
        center = np.argwhere(np.asarray(data) > 0).mean(axis=0).astype(int)
        edited[tuple(slice(max(0, c - 4), c + 4) for c in center)] = 1
        self.nifti.write_nifti(nativePath, edited.astype(np.uint8), affine, sform_code=self.nifti.XFORM_SCANNER, qform_code=self.nifti.XFORM_SCANNER)
        ln.refreshStale(wait=True)
        self.assertEqual(self.chips(["sub-01"]), {"sub-01": "Stale"})
        card.refresh()
        self.assertEqual(str(card.primary.text), "Normalize 1 native subject")
        # Normalize on the Stale row (the earlier run is finished; the new one is live when its first result is merged)
        card.primary.click()
        self.waitIdle()
        row = self.disk(path, "sub-01")
        self.assertNotEqual(row["norm_fingerprint"], first["norm_fingerprint"])
        self.assertEqual(row["lesion_native"], native)  # not the MNI lesion of the first normalization
        self.assertNotIn("derivatives", row["lesion_native"])
        self.assertEqual(row["norm_qc"], "unreviewed")
        self.assertEqual(len(self.auditEvents(path, "norm_lesion_edited")), 0)
        ln.refreshStale(wait=True)
        self.assertNotEqual(self.chips(["sub-01"])["sub-01"], "Stale")
        self.assertFalse(ln.needsNormalization(session.study.subject("sub-01")))
        # Revert gives the native scan and lesion again
        self.assertTrue(ln.revertToNative("sub-01", announce=False))
        row = self.disk(path, "sub-01")
        self.assertEqual((row["space"], row["lesion"]), ("native", native))

    #
    # Automatic mode
    #

    def test_07_auto_after_done(self):
        from StudioLib import libcard_normalize as ln

        path = self.makeStudy("auto", native=3, templateRows=0, statuses={"sub-03": "untraced"})
        session = self.openStudy(path)
        card = self.card()
        # Off: Done enqueues nothing
        runner = ln._jobrunner().instance()
        submitted = []
        originalSubmit = runner.submit

        def counting(spec, context=None):
            submitted.append(spec)
            return originalSubmit(spec, context)

        runner.submit = counting
        try:
            self.assertFalse(card.autoCheck.isChecked())
            self.assertTrue(session.open("sub-01", switchToDraw=False))
            session.done()
            _pump(AUTO_WAIT_MS)
            self.assertEqual([spec for spec in submitted if spec.kind == "normalization"], [])
            # On: the checkbox stores it in study.json (main thread), and Done on a native subject enqueues exactly one job
            card.autoCheck.setChecked(True)
            _pump(100)
            self.assertTrue(session.study.metadata["normalization"]["auto_after_done"])
            session.flush()
            self.assertTrue(json.load(open(os.path.join(path, "study.json")))["normalization"]["auto_after_done"])
            self.assertTrue(session.open("sub-03", switchToDraw=False))
            durations = []
            hooks = []
            for order, sequence, fn in session._doneHooks:
                def timed(sid, array, ijkToRas, ctx, fn=fn):
                    started = time.perf_counter()
                    result = fn(sid, array, ijkToRas, ctx)
                    durations.append((time.perf_counter() - started) * 1000.0)
                    return result

                hooks.append((order, sequence, timed))
            session._doneHooks = hooks
            started = time.perf_counter()
            result = session.done()
            doneMs = (time.perf_counter() - started) * 1000.0
            self.assertIsNotNone(result)
            self.assertTrue(durations, "the Done hook of the normalization did not run")
            self.assertLessEqual(max(durations), MAX_HOOK_MS, durations)
            self.assertLess(doneMs, 3000.0)
            self.assertEqual([spec for spec in submitted if spec.kind == "normalization"], [])  # not at once
            self.assertTrue(_wait(lambda: any(spec.kind == "normalization" for spec in submitted), 5))
            _pump(AUTO_WAIT_MS)
            jobs = [spec for spec in submitted if spec.kind == "normalization"]
            self.assertEqual(len(jobs), 1, "Done must enqueue exactly one normalization")
            args = jobs[0].args
            self.assertEqual(args[args.index("--subjects") + 1], "sub-03")
            self.assertEqual(args[args.index("--nice") + 1], "10")
            self.assertEqual(args[args.index("--template") + 1], TEMPLATE)
            self.assertIn("--events", args)
            self.assertIn("--parent-pid", args)
            self.assertNotIn("sub-01", args[args.index("--subjects") + 1])
        finally:
            runner.submit = originalSubmit
        self.waitIdle()
        row = self.disk(path, "sub-03")
        self.assertTrue(row["norm_method"].startswith("brainsfit-cfm"))
        # sub-03 stayed open (nothing follows it): its scene now shows the normalized scan, so nothing native can be written as MNI
        self.assertEqual(session.current(), "sub-03")
        self.assertIn("derivatives/normalization/sub-03", session.volumeNode().GetStorageNode().GetFileName())
        self.assertEqual(self.disk(path, "sub-01")["space"], "native")  # the other natives are not touched
        self.assertEqual(self.disk(path, "sub-02")["space"], "native")
        self.assertEqual(self.chips(["sub-03"])["sub-03"] in ("Unreviewed", "Review"), True)

    def test_07b_edits_while_normalizing(self):
        from StudioLib import libcard_normalize as ln

        path = self.makeStudy("edits", native=2, templateRows=0)
        session = self.openStudy(path)
        self.assertTrue(session.open("sub-01", switchToDraw=False))
        # A subject with edits that are not written is not queued
        session.markDirty()
        self.assertIsNone(ln.normalizeSubjects(["sub-01"]))
        session._dirty = session._markedDirty = False
        job = ln.normalizeSubjects(["sub-01"])
        self.assertIsNotNone(job)
        session.markDirty()  # edited while it is normalized
        manager = ln._manager
        self.assertTrue(_wait(job.isFinished, BATCH_TIMEOUT_S))
        _pump(300)
        # The result waits for the edits of the open subject (its row would otherwise point to other files under them)
        self.assertIn("sub-01", manager.unmerged)
        self.assertEqual(session.study.subject("sub-01")["space"], "native")
        self.assertTrue(session.open("sub-02", switchToDraw=False))  # leaving writes the edits
        self.assertEqual(session.study.subject("sub-01")["lesion"], "lesions/sub-01_space-native_desc-lesion_mask.nii.gz")
        self.assertTrue(_wait(lambda: not manager.unmerged, 15), "the result was not merged after the edits were written")
        row = self.disk(path, "sub-01")
        self.assertEqual(row["space"], TEMPLATE)
        self.assertEqual(row["lesion_native"], "lesions/sub-01_space-native_desc-lesion_mask.nii.gz")  # the edit is not lost
        ln.refreshStale(wait=True)
        self.assertEqual(self.chips(["sub-01"]), {"sub-01": "Stale"})
        self.assertEqual(len(self.auditEvents(path, "norm_lesion_edited", "sub-01")), 1)

    #
    # Stop and resume
    #

    def test_08_stop_and_resume(self):
        from StudioLib import libcard_normalize as ln
        from StudioLib import study_ui

        study_ui.closeStudy()
        _pump(100)
        path = self.makeStudy("stop", native=3, templateRows=0)
        self.openStudy(path)
        card = self.card()
        card.primary.click()
        manager = ln._manager
        self.assertTrue(manager.isBusy())
        self.assertTrue(_wait(lambda: self.chips(["sub-01"])["sub-01"] in ("Unreviewed", "Review"), BATCH_TIMEOUT_S), "the first subject did not finish")
        fingerprint = self.disk(path, "sub-01")["norm_fingerprint"]
        outputs = os.path.join(path, "derivatives", "normalization", "sub-01")
        marks = {name: os.path.getmtime(os.path.join(outputs, name)) for name in os.listdir(outputs) if name.endswith(".nii.gz")}
        self.assertTrue(_wait(lambda: self.chips(["sub-02"])["sub-02"].startswith("Running") or not manager.isBusy(), 30))
        self.assertTrue(card.progress.stopButton.isEnabled())
        self.assertTrue(os.path.exists(os.path.join(path, ".studio", ln.QUEUE_FILE)))
        card.progress.stopButton.click()
        self.waitIdle(60)
        chips = self.chips()
        self.assertIn(chips["sub-01"], ("Unreviewed", "Review"))
        self.assertEqual(chips["sub-03"], "Native")
        self.assertEqual(self.disk(path, "sub-01")["norm_fingerprint"], fingerprint)  # completed subjects keep their results
        self.assertIn("Stopped", manager.lastSummary)
        card.refresh()
        self.assertFalse(card.resumeRow.isHidden())
        self.assertIn("Stopped", str(card.resumeLabel.text))
        self.assertIn("2 subjects left", str(card.resumeLabel.text))
        # A study opened again (an interrupted session) offers to resume as well
        study_ui.closeStudy()
        _pump(100)
        self.openStudy(path)
        card = self.card()
        card.refresh()
        self.assertFalse(card.resumeRow.isHidden())
        self.assertIn("Interrupted", str(card.resumeLabel.text))
        manager = ln._manager
        # Resume runs what is left; the completed subject is not computed again
        card.resumeButton.click()
        self.assertTrue(manager.isBusy())
        self.waitIdle()
        chips = self.chips()
        for sid in ("sub-01", "sub-02", "sub-03"):
            self.assertIn(chips[sid], ("Unreviewed", "Review"), chips)
        self.assertEqual(self.disk(path, "sub-01")["norm_fingerprint"], fingerprint)
        self.assertEqual({name: os.path.getmtime(os.path.join(outputs, name)) for name in marks}, marks)
        self.assertFalse(os.path.exists(os.path.join(path, ".studio", ln.QUEUE_FILE)))
        # Redo of the whole selection is offered for normalized rows
        from StudioLib import library

        library._setStudySelection(["sub-01", "sub-02"])
        _pump(100)
        card.refresh()
        self.assertFalse(card.redoButton.isHidden())
        self.assertEqual(str(card.redoButton.text), "Redo 2 selected")
        self.assertTrue(card.selectedButton.isHidden())
        library._setStudySelection([])

    #
    # Classic and settings
    #

    def test_09_classic_panel(self):
        from StudioLib import library, study_ui

        path = self.studies["main"]
        study_ui.closeStudy()
        self.shell.dispose()
        _pump(100)
        slicer.mrmlScene.Clear(0)
        try:
            session = study_ui.openStudy(path, None)
            self.assertIsNotNone(session)
            _pump(100)
            panel = library.LibraryPanel()
            panel.build()
            panel.refresh()
            _pump(100)
            self.assertIsNotNone(panel.studyCards)
            self.assertIsNotNone(panel.studyCards.list, "Classic shows the subject list in the module panel")
            cards = panel.widget.findChildren("QWidget", "StudioNormalizeLibraryCard")
            self.assertEqual(len(cards), 1)
            headers = [str(panel.studyCards.list.model.headerData(i, qt.Qt.Horizontal)) for i in range(panel.studyCards.list.model.columnCount())]
            self.assertIn("Norm", headers)
            from StudioLib import libcard_normalize as ln

            card = ln._cards[cards[0]]
            self.assertIn("normalized", str(card.summary.text))
            self.assertEqual(card.widget.findChild("QWidget", "StudioNormalizeAllButton") is not None, True)
            panel.widget.hide()
        finally:
            study_ui.closeStudy()

    def test_10_options_and_settings(self):
        from StudioLib import libcard_normalize as ln
        from StudioLib import shell, study_ui

        path = self.makeStudy("options", native=2, templateRows=0)
        self.shell = shell.Shell(force=True)
        self.shell.apply()
        _pump(100)
        session = self.openStudy(path)
        card = self.card()
        keys = _settingsKeys()
        popover = card.showOptions()
        _pump(50)
        self.assertEqual(str(popover.objectName), "StudioNormalizeOptions")
        options = card.options
        self.assertEqual(options.qualityCombo.itemData(options.qualityCombo.currentIndex), "affine")
        options.qualityCombo.setCurrentIndex(options.qualityCombo.findData("fine"))
        options.marginSpin.setValue(5.0)
        _pump(50)
        session.flush()
        stored = json.load(open(os.path.join(path, "study.json")))["normalization"]
        self.assertEqual((stored["quality"], stored["dilate_mm"], stored["template"], stored["auto_after_done"]), ("fine", 5.0, "auto", False))
        self.assertEqual(ln.resolveTemplate(session.study)[0], TEMPLATE)
        popover.hide()
        # Fork-only keys only: nothing of the normalization is written to Slicer.ini
        _pump(100)
        new = _settingsKeys() - keys
        self.assertFalse([key for key in new if "ormaliz" in key or key.startswith("Studio/LNM")], new)
        self.assertFalse([key for key in _settingsKeys() - self.keysBefore if "ormaliz" in key], _settingsKeys() - self.keysBefore)
        study_ui.closeStudy()


#: How long to let events run after Done so that the deferred automatic start has happened (ms)
AUTO_WAIT_MS = 600


if __name__ == "__main__":
    unittest.main()

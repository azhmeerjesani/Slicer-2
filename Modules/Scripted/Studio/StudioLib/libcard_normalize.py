"""Library extension: normalize the native-space subjects of a study to MNI space (A-03c, LNM addendum 3.10, 3.13, 7.2).

A study (W3-07) keeps native-space lesions next to the scans they were traced on. This module runs A-03's batch
normalization (``python -m LesionNetworkMappingLib.normalize batch``) in the background while the researcher keeps
tracing, and makes every result pass a quick review before it can enter a cohort:

- **Norm column** in the subject list: ``Native`` (not normalized yet), ``Queued`` / ``Running 40%`` (live job),
  ``Review`` / ``Unreviewed`` (normalized, not yet judged; ``Review`` when the automatic QC suggests a look), ``Pass``,
  ``Fail``, ``Stale`` (the native lesion changed after the normalization: checked in a worker thread by the file's
  SHA-256 against ``normalization.json``, cached by file size and time) and ``Needs scan``.
- **Filters** "Needs normalization" and "Normalization to review" (with ``qcColumn`` norm_qc: P / X in the list set
  ``norm_qc`` while it is active) and the queue ``norm_review`` of Done → next.
- **Normalization card** in the STUDY card: counts, "Normalize 12 native subjects" (one queued background job, one subject at
  a time, live progress with an estimate, Stop, "Interrupted · Resume"), Options (template, quality, margin), the
  automatic mode (``study.json`` ``normalization.auto_after_done``) and "Review N →". Rows selected in the list offer
  "Normalize N selected" / "Redo N selected" in the same card (a library module has one card).
- **Normalization section** of the SUBJECT card: Normalize to MNI, the live progress, the QC panel of A-03b
  (``NormalizeController.buildQcWidget``: mosaic, suggestion, metrics, Looks right / Doesn't look right) and a ⋯ menu with
  Review in Draw, Redo, Re-warp edited native lesion and Revert to native.

Jobs go through the process-wide L-11 ``JobRunner`` (one heavy job at a time across Map, Draw and Library), never through a
launcher of this module. The job posts each subject's values to ``.studio/inbox``; the session merges them on the main
thread (``StudySession.applyInbox``), the single writer of ``participants.tsv``. This module never writes
``participants.tsv`` itself and never writes ``lesions/``. Review verdicts, reverts and re-warps are posted through the
same inbox with an audit record (``.studio/audit.jsonl``).

``normalizeSubjects(sids, options=None, auto=False, redo=False)`` is the entry point (the Draw card calls it). Plain
callback lists and no Qt signals; works in Classic (the Studio module panel hosts the same cards) and without a
shell. Settings: none; the options of a study live in its ``study.json`` (``normalization``).
"""

import contextlib
import datetime
import json
import logging
import os
import shlex
import threading
import time
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import library, ui

#: The job module (A-03's command line)
JOB_MODULE = "LesionNetworkMappingLib.normalize"
#: Additive key of ``study.json``: {"auto_after_done", "template", "quality", "dilate_mm"}
STUDY_KEY = "normalization"
#: Pending subjects of an interrupted batch (in the study's ``.studio`` folder)
QUEUE_FILE = "normalize-queue.json"
QUEUE_FORMAT = "slicer-studio-normalize-queue"
DEFAULT_TEMPLATE = "MNI152NLin2009cAsym"
TEMPLATE_IDS = (DEFAULT_TEMPLATE, "MNI152NLin6Asym")
QUALITIES = ("standard", "fine", "affine")
DEFAULT_OPTIONS = {"auto_after_done": False, "template": "auto", "quality": "standard", "dilate_mm": 3.0}
MARGIN_RANGE_MM = (1.0, 10.0)
#: Done → next: the automatic normalization starts this long after the lesion was written (the subject switch is not delayed)
AUTO_DELAY_MS = 300
#: Progress labels and bars are updated at most this often; the lists (chips) at most every LIST_INTERVAL_MS
UI_INTERVAL_MS = 150
LIST_INTERVAL_MS = 2000
#: Delay of the stale check after a change of the study (worker thread)
STALE_DELAY_MS = 600
#: A merge that has to wait for the open subject's edits is retried this often
MERGE_RETRY_MS = 1500
#: Thumbnail width of the QC panel in the SUBJECT card
QC_WIDTH_PX = 420
QC_MAX_HEIGHT_PX = 200

STAGE_LABELS = {"costmask": "preparing", "anat": "T1w alignment", "affine": "affine", "bspline": "nonlinear", "warp": "warping", "qc": "QC"}
FINISHED = ("done", "reused", "failed", "skipped", "locked", "cancelled")


#
# Lazy access to the other packages
#


def _lnm(name):
    import importlib

    return importlib.import_module("LesionNetworkMappingLib." + name)


def _nc():
    """``LesionNetworkMappingUI.normalize_controller`` (A-03b), or None when it cannot be imported."""
    import importlib

    try:
        return importlib.import_module("LesionNetworkMappingUI.normalize_controller")
    except Exception:
        logging.error(f"Studio Library: the normalization controller is not available:\n{traceback.format_exc()}")
        return None


def _jobrunner():
    import importlib

    return importlib.import_module("LesionNetworkMappingUI.jobrunner")


def _session():
    studyUi = library._studyUi()
    return studyUi.session() if studyUi is not None else None


def _toast(text, ms=3000, **kwargs):
    shell = library._shell()
    if shell is not None:
        shell.toast(text, ms, **kwargs)
    else:
        logging.info(f"Studio normalization: {text}")


def _now():
    return _lnm("study").now_iso()


#
# Rows
#


def _missing(value):
    return value is None or value == "" or value == "n/a"


def isNormalized(row):
    """The row has a normalization (``norm_method``)."""
    return not _missing(row.get("norm_method"))


def isNative(row):
    """A native-space row that is not normalized."""
    return not isNormalized(row) and row.get("space") == "native"


def isStale(row):
    """A normalized row whose native lesion changed after the normalization (checked in a worker thread)."""
    return isNormalized(row) and _stale.get(row.get("participant_id")) is True


def _included(row):
    return str(row.get("include", "1")).strip() == "1"


def needsNormalization(row):
    """The 'Needs normalization' filter: an included native row with a lesion, or a stale one."""
    return _included(row) and ((isNative(row) and not _missing(row.get("lesion"))) or isStale(row))


def canNormalizeRow(row):
    """A row the batch can normalize now: it needs normalization and has its scan and its lesion."""
    if not needsNormalization(row):
        return False
    if isNormalized(row):
        return not _missing(row.get("scan_native")) and not _missing(row.get("lesion_native"))
    return not _missing(row.get("scan"))


def canRedoRow(row):
    """A normalized, included row whose native scan and lesion are known."""
    return _included(row) and isNormalized(row) and not _missing(row.get("scan_native")) and not _missing(row.get("lesion_native"))


def toReview(row):
    """The 'Normalization to review' filter and queue: normalized and not judged yet."""
    return isNormalized(row) and row.get("norm_qc") not in ("pass", "fail")


def counts(studyObject):
    """Numbers of the card: native, normalized, review, passed, failed, stale, needScan and the rows the batch can normalize."""
    result = {"native": 0, "normalized": 0, "review": 0, "pass": 0, "fail": 0, "stale": 0, "needScan": 0, "eligible": 0}
    for row in studyObject.subjects:
        if isNormalized(row):
            result["normalized"] += 1
            qc = row.get("norm_qc")
            if qc == "pass":
                result["pass"] += 1
            elif qc == "fail":
                result["fail"] += 1
            else:
                result["review"] += 1
            if isStale(row):
                result["stale"] += 1
        elif isNative(row) and not _missing(row.get("lesion")):
            result["native"] += 1
            if _missing(row.get("scan")):
                result["needScan"] += 1
        if canNormalizeRow(row):
            result["eligible"] += 1
    return result


def normCell(row):
    """``(text, level, tooltip)`` of the Norm column."""
    sid = row.get("participant_id")
    live = _manager.liveState(sid) if _manager is not None else None
    if live is not None:
        state, fraction = live
        if state == "queued":
            return _("Queued"), "neutral", _("Waiting for the normalization job")
        return _("Running {pct}%").format(pct=round(100 * (fraction or 0.0))), "neutral", _("Normalizing in the background")
    if isNormalized(row):
        if isStale(row):
            return _("Stale"), "warn", _("The native lesion changed after the normalization: Re-warp it or Redo")
        qc = row.get("norm_qc")
        if qc == "pass":
            return _("Pass"), "ok", _("Normalization reviewed: looks right")
        if qc == "fail":
            return _("Fail"), "danger", _("Normalization reviewed: does not look right")
        if row.get("norm_suggestion") == "review":
            return _("Review"), "warn", _("The automatic checks suggest a look at the alignment")
        return _("Unreviewed"), "neutral", _("Normalized, not reviewed yet (excluded from analyses until it passes)")
    if row.get("space") == "native":
        if _missing(row.get("scan")):
            return _("Needs scan"), "warn", _("The scan this lesion was traced on is needed to normalize it")
        if _missing(row.get("lesion")):
            return _("No lesion"), "muted", _("Trace the lesion first")
        return _("Native"), "warn", _("Native space: normalize it to use it for mapping")
    return "—", "muted", ""


#
# Options of a study (study.json "normalization")
#


def _validOptions(stored):
    """``stored`` (a dict from study.json or a caller) with defaults for what is missing or invalid."""
    options = dict(DEFAULT_OPTIONS)
    if isinstance(stored, dict):
        if isinstance(stored.get("auto_after_done"), bool):
            options["auto_after_done"] = stored["auto_after_done"]
        if stored.get("template") in ("auto", *TEMPLATE_IDS):
            options["template"] = stored["template"]
        if stored.get("quality") in QUALITIES:
            options["quality"] = stored["quality"]
        try:
            margin = float(stored.get("dilate_mm"))
        except (TypeError, ValueError):
            margin = None
        if margin is not None and MARGIN_RANGE_MM[0] <= margin <= MARGIN_RANGE_MM[1]:
            options["dilate_mm"] = margin
    return options


def readOptions(studyObject):
    """The stored options with defaults: {"auto_after_done", "template", "quality", "dilate_mm"} (invalid values are ignored)."""
    return _validOptions(studyObject.metadata.get(STUDY_KEY) if studyObject is not None else None)


def setOptions(session, **changes):
    """Store options in ``study.json`` (saved soon through the session, on the main thread). Return the options."""
    options = readOptions(session.study)
    options.update({key: value for key, value in changes.items() if key in DEFAULT_OPTIONS})
    options = _validOptions(options)
    if session.study.metadata.get(STUDY_KEY) != options:
        session.study.metadata[STUDY_KEY] = options
        session.scheduleSave()
    return options


def resolveTemplate(studyObject, options=None):
    """``(template id, note)``: the chosen template, else the study's (``trace_policy`` space or connectome family; the CLI's rule)."""
    options = options or readOptions(studyObject)
    if options.get("template") in TEMPLATE_IDS:
        return options["template"], ""
    nc = _nc()
    try:
        templateId, note = _lnm("normalize")._study_template(studyObject, "auto", nc.templateDirectories() if nc is not None else ())
    except Exception:
        logging.error(f"Studio Library: the normalization template could not be chosen:\n{traceback.format_exc()}")
        return DEFAULT_TEMPLATE, ""
    return templateId, note or ""


#
# Worker threads (stat and JSON only; they never touch Qt, VTK or MRML)
#


class _Background:
    """Run ``fn()`` in a Python thread and call ``onDone(result, error)`` on the main thread (polled by a timer)."""

    _running = set()

    def __init__(self, fn, onDone):
        self.fn = fn
        self.onDone = onDone
        self.result = None
        self.error = None
        self.cancelled = False
        self._finished = threading.Event()
        self._thread = threading.Thread(target=self._run, name="StudioNormalizeWorker", daemon=True)
        self._timer = qt.QTimer()
        self._timer.setInterval(20)
        self._timer.connect("timeout()", self._poll)

    def start(self):
        _Background._running.add(self)
        self._thread.start()
        self._timer.start()
        return self

    def _run(self):
        try:
            self.result = self.fn()
        except BaseException as exc:  # handed to the main thread
            self.error = exc
            self.errorText = traceback.format_exc()
        finally:
            self._finished.set()

    def _poll(self):
        if not self._finished.is_set():
            return
        self._timer.stop()
        _Background._running.discard(self)
        if self.error is not None:
            logging.error(f"Studio Library: a normalization worker failed:\n{getattr(self, 'errorText', self.error)}")
        if not self.cancelled and self.onDone is not None:
            try:
                self.onDone(self.result, self.error)
            except Exception:
                logging.error(f"Studio Library: a normalization worker callback failed:\n{traceback.format_exc()}")

    def wait(self, timeoutS=None):
        return self._finished.wait(timeoutS)


#
# Stale native lesions
#

#: {participant id: True when its native lesion changed after the normalization}
_stale = {}
_recordCache = {}


def _outputFolder(studyObject, sid):
    return os.path.join(studyObject.path, "derivatives", "normalization", sid)


def _staleOf(sid, lesionPath, outDir):
    """Worker: whether the native lesion file differs from what the normalization (or a later re-warp) used.

    ``normalization.json`` records the SHA-256 of the lesion file it used (``inputs.lesion``) and of every re-warped
    image (``warps``), not its size or time: the file is hashed once per size and modification time (cached). A file
    with another content, wherever it is, is stale; nothing recorded or a file that is missing counts as not stale.
    """
    normalize = _lnm("normalize")
    path = os.path.join(outDir, normalize.PROVENANCE_JSON)
    try:
        stat = os.stat(path)
        key = (path, stat.st_size, stat.st_mtime_ns)
        if _recordCache.get(path, (None,))[0] != key:
            record = normalize.read_result(outDir)
            known = set()
            if record:
                lesion = (record.get("inputs") or {}).get("lesion") or {}
                if lesion.get("sha256"):
                    known.add(lesion["sha256"])
                for warp in record.get("warps") or []:
                    if warp.get("kind") == "lesion" and (warp.get("input") or {}).get("sha256"):
                        known.add(warp["input"]["sha256"])
            _recordCache[path] = (key, known)
        known = _recordCache[path][1]
        return bool(known) and normalize._sha256(lesionPath) not in known
    except (OSError, ValueError):
        return False


class _StaleChecker:
    """Checks the normalized rows for stale native lesions in a worker thread, soon after a change of the study."""

    def __init__(self):
        self.worker = None
        self.again = False
        self.timer = qt.QTimer()
        self.timer.setSingleShot(True)
        self.timer.connect("timeout()", self._start)

    def schedule(self, delayMs=STALE_DELAY_MS):
        if not self.timer.isActive():
            self.timer.start(delayMs)

    def _start(self):
        session = _session()
        if session is None:
            _stale.clear()
            return
        if self.worker is not None:
            self.again = True
            return
        studyObject = session.study
        _lnm("normalize")  # imported on the main thread: a worker never holds the import lock
        jobs = []
        for row in studyObject.subjects:
            if isNormalized(row) and not _missing(row.get("lesion_native")):
                sid = row["participant_id"]
                jobs.append((sid, studyObject.abspath(row["lesion_native"]), _outputFolder(studyObject, sid)))
        known = {job[0] for job in jobs}

        def work():
            return {sid: _staleOf(sid, lesion, out) for sid, lesion, out in jobs}

        def finished(result, error):
            self.worker = None
            if error is None and _session() is session:
                changed = [sid for sid in set(result) | set(_stale) if bool(result.get(sid)) != bool(_stale.get(sid))]
                _stale.clear()
                _stale.update({sid: True for sid, value in result.items() if value})
                if changed:
                    _refreshLists()
            if self.again:
                self.again = False
                self.schedule()

        for sid in [sid for sid in _stale if sid not in known]:
            del _stale[sid]
        if jobs:
            self.worker = _Background(work, finished).start()
        else:
            _stale.clear()


_staleChecker = None


def _checker():
    global _staleChecker
    if _staleChecker is None:
        _staleChecker = _StaleChecker()
    return _staleChecker


def refreshStale(wait=False):
    """Check the stale native lesions now (a worker thread). ``wait`` blocks until it ended (tests)."""
    checker = _checker()
    checker.timer.stop()
    checker._start()
    if wait:
        deadline = time.monotonic() + 30
        while checker.worker is not None and time.monotonic() < deadline:
            checker.worker.wait(0.05)
            slicer.app.processEvents()


#
# Refreshing the lists
#

_listTimer = None


def _refreshLists():
    """Refresh the subject lists and cards (chips changed): coalesced, at most every LIST_INTERVAL_MS."""
    global _listTimer
    if _listTimer is None:
        _listTimer = qt.QTimer()
        _listTimer.setSingleShot(True)
        _listTimer.connect("timeout()", _doRefreshLists)
    if not _listTimer.isActive():
        waited = (time.monotonic() - _lastListRefresh[0]) * 1000.0
        _listTimer.start(max(0, int(LIST_INTERVAL_MS - waited)))


_lastListRefresh = [0.0]


def _doRefreshLists():
    _lastListRefresh[0] = time.monotonic()
    try:
        library.refreshStudy()
    except Exception:
        logging.error(f"Studio Library: refreshing the study failed:\n{traceback.format_exc()}")


#
# Batch jobs
#


def _template_arguments():
    """``--template-dir`` and the executables, like A-03b's controller."""
    nc = _nc()
    args = []
    for folder in nc.templateDirectories():
        args += ["--template-dir", folder]
    for option, moduleName in (("--brainsfit", "brainsfit"), ("--brainsresample", "brainsresample"), ("--brainsroiauto", "brainsroiauto")):
        module = getattr(slicer.modules, moduleName, None)
        if module is not None and module.path:
            args += [option, module.path]
    return args


class _Run:
    """One batch job of the session: its subjects, their states and the progress of the running one."""

    def __init__(self, manager, sids, options, auto, redo, args):
        self.manager = manager
        self.sids = list(sids)
        self.options = dict(options)
        self.auto = auto
        self.redo = redo
        self.args = list(args)
        self.job = None
        self.status = dict.fromkeys(self.sids, "queued")
        #: The lesion path of each row when the job was queued: a path that changed meanwhile (an edit that was written to
        #: lesions/) is kept as lesion_native after the merge, so that the edit is not lost (the row then shows Stale)
        self.lesionAtStart = {sid: manager.session.study.subject(sid).get("lesion") for sid in self.sids}
        self.reasons = {}
        self.current = None
        self.stage = None
        self.fraction = 0.0
        self.eta = None
        self.total = len(self.sids)
        self.durations = []
        self._subjectStarted = None
        self.waitingFor = None
        self.stopRequested = False
        self.error = None
        self.warnings = []
        self.finishedAt = None

    # Reading

    def finished(self):
        return sum(1 for state in self.status.values() if state in ("done", "reused", "failed", "locked"))

    def isLive(self):
        return self.job is not None and not self.job.isFinished()

    def etaSeconds(self):
        """Time left of the batch: the running subject's estimate plus the median duration of the finished ones for the rest."""
        if self.eta is None:
            return None
        remaining = max(0, self.total - self.finished() - 1)
        if self.durations:
            ordered = sorted(self.durations)
            per = ordered[len(ordered) // 2]
        else:
            elapsed = time.monotonic() - self._subjectStarted if self._subjectStarted is not None else 0.0
            per = elapsed + float(self.eta)
        return float(self.eta) + remaining * per

    def text(self):
        """"Normalizing 3/12 · sub-007 · nonlinear 40% · ~9 min left" (the job's own line while it waits)."""
        if self.stopRequested and self.isLive():
            return _("Stopping…")
        if self.job is not None and self.job.isQueued():
            return _("Waiting for {title} to finish").format(title=self.waitingFor or _("the running job"))
        parts = [_("Normalizing {n}/{total}").format(n=min(self.finished() + 1, max(self.total, 1)), total=max(self.total, 1))]
        if self.current:
            parts.append(self.current)
        if self.stage:
            parts.append(f"{_(STAGE_LABELS.get(self.stage, self.stage))} {round(100 * self.fraction)}%")
        left = _jobrunner().formatEta(self.etaSeconds())
        if left:
            parts.append("~" + left if not left.startswith("<") else left)
        return " · ".join(parts)

    def chipText(self, job=None):
        """The text of the JobRunner's status chip: "Normalizing 3/12 · 40%"."""
        text = _("Normalizing {n}/{total}").format(n=min(self.finished() + 1, max(self.total, 1)), total=max(self.total, 1))
        if self.stage:
            text += f" · {round(100 * self.fraction)}%"
        left = _jobrunner().formatEta(self.etaSeconds())
        return f"{text} · {left}" if left else text

    # Events of the job (main thread)

    def onEvent(self, event):
        kind = event.get("event")
        if kind == "queued":
            self.waitingFor = event.get("waitingFor")
        elif kind in ("stage", "progress"):
            subject = event.get("subject")
            if subject and subject != self.current:
                self._begin(subject)
            if kind == "stage":
                self.stage = event.get("name") or self.stage
                self.fraction = 0.0
            else:
                self.stage = event.get("stage") or self.stage
                try:
                    self.fraction = min(max(float(event.get("fraction") or 0.0), 0.0), 1.0)
                except (TypeError, ValueError):
                    pass
                self.eta = event.get("eta")
                if isinstance(event.get("total"), int) and event["total"] > 0:
                    self.total = event["total"]
        elif kind == "warning":
            self.warnings.append({"code": event.get("code"), "message": event.get("message")})
        elif kind == "result" and event.get("kind") == "subject":
            self._subjectResult(event)
        self.manager.scheduleUi()

    def _begin(self, subject):
        self.current = subject
        self.fraction = 0.0
        self.stage = None
        self.eta = None
        self._subjectStarted = time.monotonic()
        if self.status.get(subject) == "queued":
            self.status[subject] = "running"

    def _subjectResult(self, event):
        sid = event.get("subject")
        state = event.get("status")
        if sid is None:
            return
        if state in ("done", "reused"):
            self.status[sid] = state
            if self._subjectStarted is not None and self.current == sid:
                self.durations.append(time.monotonic() - self._subjectStarted)
            self.manager.unmerged.add(sid)
            self.manager.failures.pop(sid, None)
            self.manager.requestMerge()
        elif state in ("failed", "locked", "skipped"):
            self.status[sid] = state
            self.reasons[sid] = str(event.get("reason") or event.get("code") or state)
        if self.current == sid:
            self.current, self.stage, self.eta, self.fraction = None, None, None, 0.0

    def onDone(self, job):
        for sid, state in list(self.status.items()):
            if state in ("queued", "running"):
                if job.state == "cancelled":
                    self.status[sid] = "cancelled"
                elif job.state == "done":
                    self.status[sid] = "skipped"  # the batch left it out (not eligible any more)
                    self.reasons.setdefault(sid, _("not eligible any more"))
                else:
                    self.status[sid] = "failed"
                    self.reasons.setdefault(sid, job.failureReason or _("the normalization job ended"))
        if job.state == "failed" and not self.reasons:
            self.error = job.failureReason
        self.finishedAt = time.time()
        self.manager.runFinished(self, job)


class _Manager:
    """The normalization state of one study session: live jobs, the merge of their results and the audit of verdicts."""

    def __init__(self, session):
        self.session = session
        self.runs = []
        self.history = []
        self.unmerged = set()
        self.stopped = False
        self.failures = {}
        self.lastSummary = ""
        self.changed = []
        self.closed = False
        self._qcSeen = {}
        #: Verdicts this module is about to merge itself ({sid: value}): followQc() does not audit them a second time
        self._expectQc = {}
        self._hooked = False
        self._uiTimer = qt.QTimer()
        self._uiTimer.setSingleShot(True)
        self._uiTimer.setInterval(UI_INTERVAL_MS)
        self._uiTimer.connect("timeout()", self._notifyUi)
        self._mergeTimer = qt.QTimer()
        self._mergeTimer.setSingleShot(True)
        self._mergeTimer.connect("timeout()", self.merge)
        self._snapshotQc()

    # Hooks of the session

    def attach(self):
        if not self._hooked:
            self.session.addDoneHook(_doneHook, order=90)
            self.session.addOpenHook(_openHook)
            self._hooked = True

    def close(self):
        """The study was closed: stop the running jobs (the queue file lets the next session resume), drop the hooks."""
        self.closed = True
        if self._hooked:
            with contextlib.suppress(Exception):
                self.session.removeDoneHook(_doneHook)
                self.session.removeOpenHook(_openHook)
            self._hooked = False
        _dropController()
        for run in list(self.runs):
            if run.isLive():
                run.stopRequested = True
                run.job.stop()
        self._uiTimer.stop()
        self._mergeTimer.stop()
        self.changed.clear()

    # Live state

    def liveRun(self, sid):
        for run in self.runs:
            if run.status.get(sid) in ("queued", "running") and run.isLive():
                return run
        return None

    def liveState(self, sid):
        """``(state, fraction)`` of a subject in a live job ("queued" or "running"), else None."""
        run = self.liveRun(sid)
        if run is None:
            return None
        return ("running", run.fraction) if run.current == sid else ("queued", 0.0)

    def isBusy(self):
        return any(run.isLive() for run in self.runs)

    def liveSids(self):
        return [sid for run in self.runs if run.isLive() for sid, state in run.status.items() if state in ("queued", "running")]

    # Starting

    def submit(self, sids, options=None, auto=False, redo=False):
        """Queue a batch normalization of ``sids`` (the rows that qualify). Return the JobRunner job, or None."""
        session = self.session
        studyObject = session.study
        nc = _nc()
        if nc is None:
            return None
        stored = readOptions(studyObject)
        merged = {**stored, **{key: value for key, value in (options or {}).items() if key in DEFAULT_OPTIONS}}
        chosen, notes = [], []
        for sid in dict.fromkeys(sids or []):
            if sid not in studyObject:
                continue
            row = studyObject.subject(sid)
            if self.liveRun(sid) is not None:
                continue
            qualifies = canRedoRow(row) if redo else canNormalizeRow(row)
            if not qualifies:
                continue
            if sid == session.current() and session.isDirty():
                notes.append(_("{sid} has edits that are not saved yet: finish it first (Done or Alt+→)").format(sid=sid))
                continue
            chosen.append(sid)
        if not chosen:
            _toast(notes[0] if notes else _("Nothing to normalize: no native subject with a scan and a lesion"), 4000, level="warning")
            return None
        available = nc.normalizationAvailable()
        if not available:
            _toast(available[1], 5000, level="warning")
            return None
        template, note = resolveTemplate(studyObject, merged)
        if not nc.templateAvailable(template):
            _toast(_("The {template} template is not on this computer (Options → download it)").format(template=template), 6000, level="warning")
            return None
        args = ["batch", "--study", studyObject.path, "--subjects", ",".join(chosen), "--template", template, "--quality", merged["quality"],
                "--dilate-mm", f"{float(merged['dilate_mm']):g}"]
        if redo:
            args += ["--force", "--no-reuse"]
        args += _template_arguments()
        args += ["--events", "json", "--parent-pid", str(os.getpid()), "--nice", "10" if auto else "5"]
        run = _Run(self, chosen, merged, auto, redo, args)
        jobrunner = _jobrunner()
        threads = max(1, (os.cpu_count() or 2) - 2) if auto else None
        spec = jobrunner.JobSpec(JOB_MODULE, args, title=_("Normalizing"), heavy=True, kind="normalization",
                                 runRoot=os.path.join(studyObject.path, ".studio", "jobs"), onEvent=run.onEvent, onDone=run.onDone,
                                 statusText=run.chipText, threads=threads, name="normalize-batch")
        self.stopped = False
        for sid in chosen:
            self.failures.pop(sid, None)
        self.runs.append(run)
        if not auto and not redo:
            self._writeQueue(run)
        run.job = jobrunner.instance().submit(spec)
        if note:
            _toast(note, 6000, level="warning")
        elif not auto:
            _toast(_("Normalizing {n} subjects in the background").format(n=len(chosen)) if len(chosen) > 1
                   else _("Normalizing {sid} in the background").format(sid=chosen[0]), 3000)
        _refreshLists()
        self.scheduleUi()
        return run.job

    # Stop

    def stop(self):
        """Stop the live jobs (the subjects that are done keep their results). Never bound to Esc."""
        for run in list(self.runs):
            if run.isLive():
                run.stopRequested = True
                run.job.stop()
        self.stopped = True
        self.scheduleUi()

    # Merging

    def requestMerge(self):
        """Merge in the next event-loop turn: the merge (a locked save of participants.tsv) then does not add to the turn
        that handles the job's events.
        """
        if not self._mergeTimer.isActive():
            self._mergeTimer.start(0)

    def _newestFirst(self):
        """The runs, newest first: a live run (``runs``) is always newer than a finished one (``history``)."""
        return self.runs[::-1] + self.history[::-1]

    def merge(self):
        """Merge the posted results on the main thread (the session writes participants.tsv).

        The open subject is left first when its own result is merged (its row then points at other files), so that
        nothing of the native scene is written to the normalized lesion; edits that are not written yet wait.
        """
        session = self.session
        if session is None or self.closed or _session() is not session:
            return []
        current = session.current()
        affected = current is not None and current in self.unmerged
        if affected and session.isDirty():
            self._mergeTimer.start(MERGE_RETRY_MS)
            return []
        edited = {}
        for sid in self.unmerged:
            run = next((run for run in self._newestFirst() if sid in run.lesionAtStart), None)
            row = session.study.subject(sid) if sid in session.study else None
            if run is not None and row is not None and not _missing(row.get("lesion")) and row.get("lesion") != run.lesionAtStart.get(sid):
                edited[sid] = row["lesion"]
        with _subjectClosed(session, current if affected else None, deferReopen=True):
            applied = session.applyInbox()
        merged = {sid for _path, sids in applied for sid in sids}
        self.unmerged -= merged
        kept = {sid: path for sid, path in edited.items() if sid in merged}
        if kept:
            self._keepEditedLesions(kept)
        self._snapshotQc()
        _checker().schedule()
        return applied

    def _keepEditedLesions(self, kept):
        """A lesion edited while it was normalized was written to another file: it becomes the native lesion of the row."""
        session = self.session
        audits = [{"event": "norm_lesion_edited", "sid": sid, "lesion_native": path, "by": "normalize", "at": _now()} for sid, path in kept.items()]
        rows = {sid: {"lesion_native": path} for sid, path in kept.items()}
        _lnm("study").Study.post_results(session.study.path, rows, "normalize-edited", audit=audits)
        session.applyInbox()
        _toast(_("The lesion of {sids} was edited while it was normalized: it is marked Stale (Redo or Re-warp)").format(sids=", ".join(sorted(kept))), 8000,
               level="consequence")

    # Finishing

    def runFinished(self, run, job):
        if run in self.runs:
            self.runs.remove(run)
        self.history.append(run)
        del self.history[:-10]
        for sid, reason in run.reasons.items():
            if run.status.get(sid) in ("failed", "locked", "skipped"):
                self.failures[sid] = reason
        if not self.closed:
            self.merge()
        done = sum(1 for state in run.status.values() if state in ("done", "reused"))
        failed = sum(1 for state in run.status.values() if state == "failed")
        if self.closed:
            return
        studyObject = self.session.study
        review = sum(1 for sid in run.sids if sid in studyObject and toReview(studyObject.subject(sid)) and run.status.get(sid) in ("done", "reused"))
        if job.state == "cancelled":
            self.stopped = True
            self.lastSummary = _("Stopped: {done} of {total} done").format(done=done, total=len(run.sids))
            _toast(self.lastSummary, 4000, level="consequence")
        else:
            if job.state == "done" or done:
                self._clearQueue(run)
            self.lastSummary = (_("Normalized {n} subjects").format(n=done) if done != 1 else _("Normalized 1 subject")) + (
                " · " + _("{n} to review").format(n=review) if review else "") + (" · " + _("{n} failed").format(n=failed) if failed else "")
            if failed or (job.state == "failed" and not done):
                reason = next(iter(run.reasons.values()), None) or run.error or job.failureReason or ""
                _toast(self.lastSummary + (f": {reason}" if reason else ""), 8000, level="warning")
            elif not run.auto or review:
                _toast(self.lastSummary, 4000)
        _refreshLists()
        self.scheduleUi()

    # Queue file (an interrupted batch can be resumed)

    def _queuePath(self):
        return os.path.join(self.session.study.path, ".studio", QUEUE_FILE)

    def _writeQueue(self, run):
        try:
            os.makedirs(os.path.dirname(self._queuePath()), exist_ok=True)
            _lnm("schema").write_text_atomic(self._queuePath(), json.dumps({
                "format": QUEUE_FORMAT, "version": 1, "sids": run.sids, "options": run.options, "started": _now()}, indent=2) + "\n")
        except OSError:
            logging.warning(f"Studio Library: the normalization queue could not be written:\n{traceback.format_exc()}")

    def _clearQueue(self, run=None):
        with contextlib.suppress(OSError):
            os.remove(self._queuePath())

    def interrupted(self):
        """``(sids, options)`` of a batch that did not finish and has subjects left, else None (no job is live)."""
        if self.isBusy():
            return None
        try:
            with open(self._queuePath(), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("format") != QUEUE_FORMAT:
            return None
        studyObject = self.session.study
        left = [sid for sid in data.get("sids", []) if sid in studyObject and canNormalizeRow(studyObject.subject(sid))]
        if not left:
            self._clearQueue()
            return None
        return left, data.get("options") or {}

    def resume(self):
        pending = self.interrupted()
        if pending is None:
            return None
        sids, options = pending
        return self.submit(sids, options)

    # Verdicts and audit

    def _snapshotQc(self):
        self._qcSeen = {row["participant_id"]: (row.get("norm_qc"), row.get("norm_fingerprint")) for row in self.session.study.subjects if isNormalized(row)}

    def followQc(self):
        """Audit the P / X keys (and other changes of norm_qc made outside this module): one record per pass / fail."""
        records = []
        for row in self.session.study.subjects:
            if not isNormalized(row):
                continue
            sid = row["participant_id"]
            seen = self._qcSeen.get(sid)
            current = (row.get("norm_qc"), row.get("norm_fingerprint"))
            if seen is not None and seen != current and seen[1] == current[1] and current[0] in ("pass", "fail") and self._expectQc.get(sid) != current[0]:
                records.append((sid, seen[0], current[0]))
            self._qcSeen[sid] = current
        if records:
            qt.QTimer.singleShot(0, lambda: self._postVerdicts(records, "list"))

    def _postVerdicts(self, records, via):
        session = self.session
        if self.closed or _session() is not session:
            return
        nc = _nc()
        rater = nc.raterName() if nc is not None else "unknown"
        studyObject = session.study
        audits = []
        for sid, old, new in records:
            if sid in studyObject:
                audits.append({"event": "norm_qc", "sid": sid, "from": old, "to": new, "by": rater, "at": _now(),
                               "fingerprint": studyObject.subject(sid).get("norm_fingerprint"), "via": via})
                _syncReview(sid, new, rater)
        if audits:
            _lnm("study").Study.post_results(studyObject.path, {audit["sid"]: {} for audit in audits}, "normalize-review", audit=audits)
            session.applyInbox()
            self._snapshotQc()

    # UI notification

    def scheduleUi(self):
        if not self._uiTimer.isActive() and not self.closed:
            self._uiTimer.start()

    def _notifyUi(self):
        for callback in list(self.changed):
            try:
                callback()
            except Exception:
                logging.error(f"Studio Library: a normalization view failed:\n{traceback.format_exc()}")
        if self.isBusy():
            _refreshLists()  # the chips show the running percentage (coalesced)


_manager = None
_listening = [False]


def manager(session=None):
    """The manager of the open study session (created on first use), or None without a study."""
    global _manager
    session = session or _session()
    if session is None:
        return None
    if _manager is not None and _manager.session is not session:
        _manager.close()
        _manager = None
    if _manager is None:
        _manager = _Manager(session)
        _manager.attach()
    return _manager


def _onStudy(session):
    """Listener of the study session: the hooks, the stale check and the audit of verdicts follow the open study."""
    global _manager
    try:
        if session is None:
            if _manager is not None:
                _manager.close()
                _manager = None
            _stale.clear()
            return
        current = manager(session)
        current.followQc()
        _checker().schedule()
    except Exception:
        logging.error(f"Studio Library: the normalization state could not follow the study:\n{traceback.format_exc()}")


def _install():
    """Follow the study session (once); handles a study that is already open."""
    if _listening[0]:
        return
    studyUi = library._studyUi()
    if studyUi is None:
        return
    studyUi.addListener(_onStudy)
    _listening[0] = True
    session = studyUi.session()
    if session is not None:
        _onStudy(session)


@contextlib.contextmanager
def _subjectClosed(session, sid, deferReopen=False):
    """Leave ``sid`` if it is the open subject (its edits are written), run the body, and open it again.

    With ``deferReopen`` the subject is opened again in the next event-loop turn (when the user has not moved on), so that
    the two subject switches are not one long freeze of the window.
    """
    neighbor = None
    if sid is not None and session.current() == sid:
        ids = list(session.study.subject_ids)
        others = [other for other in ids if other != sid]
        if others:
            index = ids.index(sid)
            neighbor = ids[index + 1] if index + 1 < len(ids) else ids[index - 1]
            if not session.open(neighbor, switchToDraw=False):
                neighbor = None
    try:
        yield
    finally:
        if neighbor is not None and deferReopen:
            qt.QTimer.singleShot(0, lambda: _reopen(session, sid, neighbor))
        elif neighbor is not None and sid in session.study:
            session.open(sid, switchToDraw=False)


def _reopen(session, sid, neighbor):
    if _session() is session and sid in session.study and session.current() == neighbor:
        session.open(sid, switchToDraw=False)


def normalizeSubjects(sids, options=None, auto=False, redo=False):
    """Normalize subjects of the open study in the background (the entry point of the Draw card and the palette).

    One queued job of the process-wide JobRunner; the rows that are not native (or not normalized with ``redo``) are
    left out. Return the Job, or None (a toast says why).
    """
    current = manager()
    if current is None:
        return None
    return current.submit(sids, options, auto=auto, redo=redo)


def normalizeAll():
    """Normalize every native subject that has its scan and lesion (the primary button and the palette)."""
    session = _session()
    if session is None:
        return None
    return normalizeSubjects([row["participant_id"] for row in session.study.subjects if canNormalizeRow(row)])


def stopAll():
    current = manager()
    if current is not None:
        current.stop()


def _openHook(sid, ctx):
    """A subject was opened: a review of the previous one is over (its layers are gone with its nodes).

    The nodes of a normalized row are marked as normalization results (``LNM.Normalization``, the study-relative
    path of normalization.json): the Draw card then does not offer to normalize them again.
    """
    if _controller is not None and _controller.reviewing:
        _controller.discardReview()
    row = ctx.get("row") or {}
    nc = _nc()
    if nc is not None and isNormalized(row):
        mark = f"derivatives/normalization/{sid}/{_lnm('normalize').PROVENANCE_JSON}"
        for node in (ctx.get("segmentationNode"), ctx.get("volumeNode")):
            if node is not None and not node.GetAttribute(nc.NORMALIZATION_ATTRIBUTE) and node.GetAttribute("Studio.StudySubject") == sid:
                node.SetAttribute(nc.NORMALIZATION_ATTRIBUTE, mark)
                node.SetAttribute(nc.SPACE_ATTRIBUTE, str(row.get("space") or ""))


def _dropController():
    global _controller
    controller, _controller = _controller, None
    _qcResults.clear()
    if controller is not None:
        controller.discardReview()
        controller.dispose()


def _doneHook(sid, labelmap, ijkToRas, ctx):
    """Done hook (order 90): with the automatic mode on, normalize the subject once its lesion is written. Returns at once.

    It never raises and never blocks Done: the work starts from a timer (``AUTO_DELAY_MS``), after the lesion was written.
    """
    try:
        current = _manager
        stored = current.session.study.metadata.get(STUDY_KEY) if current is not None and not current.closed else None
        if isinstance(stored, dict) and stored.get("auto_after_done") is True:
            qt.QTimer.singleShot(AUTO_DELAY_MS, lambda: _autoEnqueue(sid))
    except Exception:
        logging.error(f"Studio Library: the normalization Done hook failed:\n{traceback.format_exc()}")
    return {}


def _autoEnqueue(sid):
    current = _manager
    if current is None or current.closed or _session() is not current.session or sid not in current.session.study:
        return
    if not readOptions(current.session.study)["auto_after_done"]:
        return
    row = current.session.study.subject(sid)
    if isNative(row) and canNormalizeRow(row):
        current.submit([sid], auto=True)


#
# Verdicts, review and revert (main thread)
#


def _syncReview(sid, verdict, rater=None):
    """Keep ``review`` of normalization.json in step with norm_qc (a rater's verdict; None clears it)."""
    session = _session()
    nc = _nc()
    if session is None or nc is None or (_manager is not None and _manager.liveRun(sid) is not None):
        return
    outDir = _outputFolder(session.study, sid)
    normalize = _lnm("normalize")
    record = normalize.read_result(outDir)
    if record is None:
        return
    verdict = verdict if verdict in ("pass", "fail") else None
    record["review"] = {"verdict": verdict, "by": (rater or nc.raterName()) if verdict else None,
                        "at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds") if verdict else None}
    with contextlib.suppress(OSError):
        nc._writeJsonAtomic(os.path.join(outDir, normalize.PROVENANCE_JSON), record)
    for key, result in list(_qcResults.items()):
        if key[0] == sid:
            result.record = record


def setNormQc(sid, verdict, via="button"):
    """Pass / Fail (or unreviewed): set ``norm_qc`` through the session and append the audit record. Return True when it changed."""
    session = _session()
    current = manager(session)
    if session is None or current is None or sid not in session.study:
        return False
    row = session.study.subject(sid)
    old = row.get("norm_qc")
    if verdict not in ("pass", "fail", "unreviewed") or old == verdict or not isNormalized(row):
        return False
    nc = _nc()
    rater = nc.raterName() if nc is not None else "unknown"
    audit = {"event": "norm_qc", "sid": sid, "from": old, "to": verdict, "by": rater, "at": _now(), "fingerprint": row.get("norm_fingerprint"), "via": via}
    _lnm("study").Study.post_result(session.study.path, sid, {"norm_qc": verdict}, "normalize-review", audit=audit)
    current._expectQc[sid] = verdict
    try:
        session.applyInbox()
    finally:
        current._expectQc.pop(sid, None)
    current._snapshotQc()
    _syncReview(sid, verdict, rater)
    return True


_REVERT_KEYS = ("scan", "lesion", "space", "norm_method", "norm_qc", "norm_suggestion", "norm_fingerprint", "scan_native", "lesion_native", "volume_ml",
                "volume_native_ml")


def revertToNative(sid, announce=True):
    """Back to the native scan and lesion: scan, lesion and space are the native ones, the norm_* columns are cleared, the
    normalization files stay in derivatives/ (recorded in the audit). Return True when the row was reverted.
    """
    session = _session()
    current = manager(session)
    if session is None or current is None or sid not in session.study:
        return False
    studyObject = session.study
    row = studyObject.subject(sid)
    if not isNormalized(row) or _missing(row.get("scan_native")) or _missing(row.get("lesion_native")):
        _toast(_("{sid} has no native scan and lesion to go back to").format(sid=sid), 4000, level="warning")
        return False
    if current.liveRun(sid) is not None:
        _toast(_("{sid} is being normalized: stop it first").format(sid=sid), 4000, level="warning")
        return False
    previous = {key: row.get(key) for key in _REVERT_KEYS}
    values = {"scan": row["scan_native"], "lesion": row["lesion_native"], "space": "native", "norm_method": None, "norm_qc": None,
              "norm_suggestion": None, "norm_fingerprint": None, "scan_native": None, "lesion_native": None,
              "volume_ml": None if _missing(row.get("volume_native_ml")) else row["volume_native_ml"], "volume_native_ml": None}
    nc = _nc()
    audit = {"event": "norm_revert", "sid": sid, "from_space": row.get("space"), "norm_method": row.get("norm_method"), "fingerprint": row.get("norm_fingerprint"),
             "kept": studyObject.relpath(_outputFolder(studyObject, sid)), "by": nc.raterName() if nc is not None else "unknown", "at": _now()}
    _apply(session, sid, values, "normalize-revert", audit, reload=True)
    _stale.pop(sid, None)
    if announce:
        _toast(_("{sid} is back in native space (the normalization files are kept)").format(sid=sid), 8000, level="consequence",
               action=lambda: undoRevert(sid, previous), actionText=_("Undo"))
    return True


def undoRevert(sid, previous):
    """Put back the row values that ``revertToNative`` replaced."""
    session = _session()
    if session is None or sid not in session.study:
        return False
    audit = {"event": "norm_revert_undo", "sid": sid, "at": _now()}
    _apply(session, sid, previous, "normalize-revert", audit, reload=True)
    _refreshLists()
    return True


def _apply(session, sid, values, source, audit, reload=False):
    """Post values and an audit record to the inbox and merge them now (the session writes the table)."""
    _lnm("study").Study.post_result(session.study.path, sid, values, source, audit=audit)
    current = _manager
    with _subjectClosed(session, sid if reload else None):
        session.applyInbox()
    if current is not None:
        current._snapshotQc()
    _checker().schedule()


def templateDisplayName(templateId):
    """The name of a template to show (V-02): a user template "user:<id>" by its name ("ICBM 2009b 0.5 mm"), else the id."""
    if templateId and ":" in str(templateId):
        try:
            from . import templates

            template = templates.templateById(templateId)
            if template is not None and getattr(template, "name", None):
                return str(template.name)
        except Exception:
            pass
        return str(templateId).split(":", 1)[1]
    return str(templateId)


def rewarpSubject(sid, onDone=None):
    """Re-warp the native lesion of a normalized subject with its transform (``normalize warp``, a light job): the row's
    lesion then points to the new warped file (in the run's ``warps/``), norm_qc is kept.
    """
    session = _session()
    current = manager(session)
    nc = _nc()
    if session is None or current is None or nc is None or sid not in session.study:
        return None
    studyObject = session.study
    row = studyObject.subject(sid)
    if not isNormalized(row) or _missing(row.get("lesion_native")):
        return None
    if current.liveRun(sid) is not None:
        _toast(_("{sid} is being normalized: wait for it").format(sid=sid), 3000, level="warning")
        return None
    out = _outputFolder(studyObject, sid)
    record = _lnm("normalize").read_result(out)
    if not record:
        _toast(_("{sid} has no normalization result to warp with").format(sid=sid), 4000, level="warning")
        return None
    template = (record.get("template") or {}).get("id") or DEFAULT_TEMPLATE
    stamp = time.strftime("%Y%m%d%H%M%S")
    # The BIDS space label of the template: a user template "user:<id>" is "user<Id>" (no ':' in a file name) on its own grid
    normalizeLib = _lnm("normalize")
    label = normalizeLib.template_label(template) if hasattr(normalizeLib, "template_label") else template.replace(":", "")
    res = "" if ":" in template else "_res-1"
    target = os.path.join(out, "warps", f"{sid}_edit-{stamp}_space-{label}{res}_desc-lesion_mask.nii.gz")
    lesion = studyObject.abspath(row["lesion_native"])
    args = ["warp", "--run", out, "--image", lesion, "--kind", "lesion", "--out", target, "--events", "json", "--parent-pid", str(os.getpid())]
    for folder in nc.templateDirectories():
        args += ["--template-dir", folder]
    module = getattr(slicer.modules, "brainsresample", None)
    if module is not None and module.path:
        args += ["--brainsresample", module.path]

    def finished(job):
        if not job.succeeded() or not os.path.isfile(target) or _session() is not session:
            if _session() is session:
                _toast(_("The lesion of {sid} could not be warped: {reason}").format(sid=sid, reason=job.failureReason or job.state), 6000, level="warning")
            if onDone is not None:
                onDone(False)
            return
        audit = {"event": "norm_rewarp", "sid": sid, "fingerprint": row.get("norm_fingerprint"), "lesion_native": row["lesion_native"],
                 "output": studyObject.relpath(target), "by": nc.raterName(), "at": _now()}
        _apply(session, sid, {"lesion": studyObject.relpath(target)}, "normalize-rewarp", audit, reload=True)
        _stale.pop(sid, None)
        _checker().schedule(100)
        _toast(_("The lesion of {sid} was warped again with its transform").format(sid=sid), 4000)
        if onDone is not None:
            onDone(True)

    spec = _jobrunner().JobSpec(JOB_MODULE, args, title=_("Updating the MNI lesion"), heavy=False, kind="normalization-warp",
                                runRoot=os.path.join(studyObject.path, ".studio", "jobs"), onDone=finished, name="normalize-warp")
    return _jobrunner().instance().submit(spec)


def commandLineFor(sid):
    """The command line of the last batch that included ``sid`` (for Copy command line), else a plain one."""
    current = _manager
    for run in current._newestFirst() if current is not None else []:
        if sid in run.sids:
            return shlex.join(["PythonSlicer", "-m", JOB_MODULE, *run.args])
    session = _session()
    path = session.study.path if session is not None else "<study>"
    return shlex.join(["PythonSlicer", "-m", JOB_MODULE, "batch", "--study", path, "--subjects", sid])


#
# QC panel of a normalized subject (A-03b's buildQcWidget)
#

#: {(sid, fingerprint, modified time): NormalizeResult} of the QC panels that were built
_qcResults = {}
_controller = None


def _reviewController():
    """The NormalizeController that builds the QC panels of the SUBJECT card; Review in views opens the subject in Draw first."""
    global _controller
    nc = _nc()
    if nc is None:
        return None
    if _controller is None:

        class _LibraryController(nc.NormalizeController):
            def reviewInViews(self, result=None):
                result = result or self.result
                return reviewInDraw(result.sid, result) if result is not None and getattr(result, "sid", None) else False

            def discardReview(self):
                """Forget a review without restoring the layers (the subject changed: its nodes are gone, the new ones are shown)."""
                template = self._reviewTemplateNode
                self._reviewState = self._reviewDisplayState = self._reviewTemplateNode = None
                self._reviewHidden = []
                if template is not None and slicer.mrmlScene.IsNodePresent(template):
                    self._removeWithHelpers(template)
                if self.reviewing:
                    self.reviewing = False
                    self._notify("reviewChanged", self.result)

            def setVerdict(self, verdict, result=None):
                result = result or self.result
                sid = getattr(result, "sid", None)
                if sid is None or verdict not in ("pass", "fail"):
                    return super().setVerdict(verdict, result)
                setNormQc(sid, verdict)
                self._notify("reviewChanged", result)
                return True

        _controller = _LibraryController(name="library")
    return _controller


def reviewInDraw(sid, result=None):
    """Open the subject in Draw (its template-space scan and lesion are the row's) and show the review layers (A-03b)."""
    session = _session()
    controller = _reviewController()
    if session is None or controller is None or sid not in session.study:
        return False
    if not session.open(sid):
        return False
    result = result or _qcResultFor(sid) or _plainResult(sid)
    if result is None:
        return False
    segmentation = session.segmentationNode()
    result.volumeNode = session.volumeNode()
    result.segmentationNode = segmentation
    result.segmentId = segmentation.GetSegmentation().GetNthSegmentID(0) if segmentation is not None and segmentation.GetSegmentation().GetNumberOfSegments() else None
    controller.result = result
    return bool(_baseReviewInViews(controller, result))


def _baseReviewInViews(controller, result):
    """``NormalizeController.reviewInViews`` itself (the subclass routes the QC panel's button here through ``reviewInDraw``)."""
    nc = _nc()
    return nc.NormalizeController.reviewInViews(controller, result)


def _plainResult(sid):
    """A result without a mosaic, read from normalization.json (Review in Draw before the QC panel was built)."""
    session = _session()
    nc = _nc()
    normalize = _lnm("normalize")
    outDir = _outputFolder(session.study, sid)
    record = normalize.read_result(outDir)
    if nc is None or not record:
        return None
    outputs = {key: os.path.join(outDir, *str(item["path"]).split("/")) for key, item in (record.get("outputs") or {}).items()}
    result = nc.NormalizeResult(outDir, record, outputs, "done")
    result.sid = sid
    return result


def _qcResultFor(sid):
    for key, result in _qcResults.items():
        if key[0] == sid:
            return result
    return None


def _loadQcData(sid, outDir):
    """Worker: the record, the outputs and the QC mosaic (with its thumbnail) of a normalization folder."""
    nc = _nc()
    normalize = _lnm("normalize")
    record = normalize.read_result(outDir)
    if not record or record.get("status") != "done":
        raise RuntimeError(f"{outDir} holds no complete normalization")
    outputs = {key: os.path.join(outDir, *str(item["path"]).split("/")) for key, item in (record.get("outputs") or {}).items()}
    png = thumbnail = None
    path = outputs.get("qc_png")
    if path and os.path.isfile(path):
        with open(path, "rb") as f:
            png = f.read()
        thumbnail = nc._thumbnailPng(png)
    stamp = os.stat(os.path.join(outDir, normalize.PROVENANCE_JSON)).st_mtime_ns
    return record, outputs, png, thumbnail, stamp


#
# Cards
#

_cards = {}


def _sectionLabel(text, objectName):
    label = qt.QLabel(text)
    label.setObjectName(objectName)
    label.setProperty("slicerRole", "sectionHeader")
    library._setFont(label, 12, qt.QFont.DemiBold)
    return label


def _checkBox(text, objectName, callback):
    box = qt.QCheckBox(text)
    box.setObjectName(objectName)
    box.connect("toggled(bool)", callback)
    return box


class _SelectionView:
    """Registered as a study view of Library: a change of the selection refreshes the 'selected' row of the card."""

    def __init__(self, card):
        self.card = card
        self.widget = card.widget

    def refresh(self):
        pass  # the STUDY card refreshes its extras itself

    def onSelectionChanged(self):
        self.card.refreshSelection()


class NormalizationCard:
    """The 'Normalization' part of the STUDY card (CARD id 'normalization', order 40)."""

    def __init__(self, session):
        self.session = session
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioNormalizeLibraryCard")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(_sectionLabel(_("Normalization"), "StudioNormalizeLibraryTitle"))
        self.summary = library._cardLabel("StudioNormalizeSummary", 12, "text")
        layout.addWidget(self.summary)
        self.note = library._cardLabel("StudioNormalizeTemplateNote", 12, "warning")
        layout.addWidget(self.note)
        self.primaryRow = library._buttonRow()
        self.primary = library._roleButton(_("Normalize native subjects"), "StudioNormalizeAllButton", self.normalizeAll, "primary")
        self.primaryRow.layout().addWidget(self.primary, 1)
        self.optionsLink = library._linkButton(_("Options…"), "StudioNormalizeOptionsButton", self.showOptions)
        self.primaryRow.layout().addWidget(self.optionsLink)
        layout.addWidget(self.primaryRow)
        self.autoCheck = _checkBox(_("Normalize native lesions automatically after Done → next"), "StudioNormalizeAutoCheck", self._onAuto)
        self.autoCheck.setToolTip(_("After Done, a native lesion is normalized in the background (one at a time, with a lower priority); "
                                    "the result waits for your review"))
        layout.addWidget(self.autoCheck)
        from LesionNetworkMappingUI import widgets

        self.progress = widgets.ProgressRow(_("Stop"), "StudioNormalize")
        self.progress.stopButton.setToolTip(_("Stop normalizing (subjects that are done keep their results)"))
        self.progress.stopButton.disconnect("clicked()")
        self.progress.stopButton.connect("clicked()", self.stop)
        layout.addWidget(self.progress.widget)
        self.resumeLabel = library._cardLabel("StudioNormalizeResumeLabel", 12, "text")
        layout.addWidget(self.resumeLabel)
        self.resumeRow, self.resumeButton = library._rowButton(_("Resume"), "StudioNormalizeResumeButton", self.resume)
        layout.addWidget(self.resumeRow)
        self.failureLabel = library._cardLabel("StudioNormalizeFailures", 12, "warning")
        layout.addWidget(self.failureLabel)
        self.reviewRow, self.reviewButton = library._rowButton(_("Review →"), "StudioNormalizeReviewButton", showReviewQueue)
        layout.addWidget(self.reviewRow)
        selectionRow = library._buttonRow()
        self.selectedButton = library._roleButton(_("Normalize selected"), "StudioNormalizeSelectedButton", self.normalizeSelected, "secondary")
        self.redoButton = library._roleButton(_("Redo selected"), "StudioNormalizeRedoSelectedButton", self.redoSelected, "secondary")
        selectionRow.layout().addWidget(self.selectedButton)
        selectionRow.layout().addWidget(self.redoButton)
        self.selectionRow = selectionRow
        layout.addWidget(selectionRow)
        self.options = None
        self._settingChecks = False
        self._view = _SelectionView(self)
        library._studyViews.append(self._view)
        current = manager(session)
        if current is not None:
            current.changed.append(self.updateProgress)

    # Content

    def _rebind(self, session):
        """Another study was opened: this card (created for the first one, Library keeps it) follows the open session."""
        self.session = session
        if self.options is not None:
            library._discard(self.options.widget)
            self.options = None

    def refresh(self, selectedIds=None):
        session = _session()
        if session is None:
            return
        if session is not self.session:
            self._rebind(session)
        current = manager(session)
        if current is not None and self.updateProgress not in current.changed:
            current.changed.append(self.updateProgress)
        studyObject = session.study
        numbers = counts(studyObject)
        relevant = numbers["native"] or numbers["normalized"] or (current is not None and current.isBusy())
        self.widget.setVisible(bool(relevant))
        if not relevant:
            return
        parts = [_("{n} native").format(n=numbers["native"])]
        if numbers["normalized"]:
            parts.append(_("{n} normalized ({review} to review, {passed} pass, {failed} fail)").format(
                n=numbers["normalized"], review=numbers["review"], passed=numbers["pass"], failed=numbers["fail"]))
        if numbers["stale"]:
            parts.append(_("{n} stale").format(n=numbers["stale"]))
        if numbers["needScan"]:
            parts.append(_("{n} need a scan").format(n=numbers["needScan"]))
        self.summary.setText(" · ".join(parts))
        options = readOptions(studyObject)
        template, note = resolveTemplate(studyObject, options)
        templateName = templateDisplayName(template)
        nc = _nc()
        available = nc is not None and nc.templateAvailable(template)
        busy = current is not None and current.isBusy()
        eligible = numbers["eligible"]
        if eligible:
            self.primary.setText(_("Normalize {n} native subjects").format(n=eligible) if eligible != 1 else _("Normalize 1 native subject"))
        else:
            self.primary.setText(_("Normalize native subjects"))
        reason = ""
        if nc is None:
            reason = _("Normalization is not available in this installation")
        elif not eligible:
            reason = _("No native subject with a scan and a lesion is waiting")
        elif not available:
            reason = _("The {template} template is not on this computer (Options → download it)").format(template=templateName)
        else:
            availability = nc.normalizationAvailable()
            if not availability:
                reason = availability[1]
        self.primary.setEnabled(not reason and not busy)
        self.primary.setToolTip(reason or (_("Normalize every native lesion in the background, one at a time ({template}, {quality}); "
                                              "stale ones are normalized again").format(template=templateName, quality=options["quality"])))
        self.note.setText(note if note else (_("The {template} template is not on this computer (Options → download it)").format(template=templateName)
                                              if nc is not None and not available else ""))
        self.note.setVisible(bool(self.note.text))
        self._settingChecks = True
        try:
            if self.autoCheck.isChecked() != options["auto_after_done"]:
                self.autoCheck.setChecked(options["auto_after_done"])
        finally:
            self._settingChecks = False
        self._refreshStatus(current, numbers)
        self.refreshSelection()

    def _refreshStatus(self, current, numbers):
        busy = current is not None and current.isBusy()
        self.progress.widget.setVisible(busy)
        if busy:
            self.updateProgress()
        pending = current.interrupted() if current is not None and not busy else None
        self.resumeLabel.setVisible(pending is not None)
        self.resumeRow.setVisible(pending is not None)
        if pending is not None:
            label = _("Stopped") if current.stopped else _("Interrupted")
            self.resumeLabel.setText(_("{label} · {n} subjects left").format(label=label, n=len(pending[0])))
        failures = {sid: reason for sid, reason in (current.failures if current is not None else {}).items() if sid in self.session.study}
        self.failureLabel.setVisible(bool(failures) and not busy)
        if failures:
            first_sid, first_reason = next(iter(failures.items()))
            self.failureLabel.setText(_("{n} could not be normalized: {sid} ({reason})").format(n=len(failures), sid=first_sid, reason=first_reason[:120]))
        self.reviewRow.setVisible(numbers["review"] > 0)
        self.reviewButton.setText(_("Review {n} →").format(n=numbers["review"]))

    def refreshSelection(self):
        session = _session()
        if session is None or session is not self.session:
            return
        studyObject = session.study
        sids = [sid for sid in library.studySelection() if sid in studyObject]
        rows = [studyObject.subject(sid) for sid in sids]
        eligible = [row["participant_id"] for row in rows if canNormalizeRow(row)]
        redo = [row["participant_id"] for row in rows if canRedoRow(row)]
        current = manager(session)
        busy = current is not None and any(current.liveRun(sid) is not None for sid in sids)
        self.selectionRow.setVisible(bool(eligible) or bool(redo))
        self.selectedButton.setVisible(bool(eligible))
        self.selectedButton.setText(_("Normalize {n} selected").format(n=len(eligible)))
        self.selectedButton.setEnabled(not busy)
        self.redoButton.setVisible(bool(redo))
        self.redoButton.setText(_("Redo {n} selected").format(n=len(redo)))
        self.redoButton.setEnabled(not busy)

    def updateProgress(self):
        """The progress row (called at most every UI_INTERVAL_MS)."""
        current = manager(self.session) if _session() is self.session else None
        if current is None:
            return
        runs = [run for run in current.runs if run.isLive()]
        self.progress.widget.setVisible(bool(runs))
        if not runs:
            return
        run = next((run for run in runs if run.job.isRunning()), runs[0])
        text = run.text()
        if len(runs) > 1:
            text += " · " + _("{n} more queued").format(n=len(runs) - 1)
        nc = _nc()
        fraction = None
        if run.job.isRunning() and run.total:
            fraction = min(1.0, (run.finished() + run.fraction) / max(run.total, 1))
        if nc is not None and fraction is not None:
            nc.setRowProgress(self.progress, fraction, text)
        else:
            self.progress.setProgress(0.0, text)
        self.progress.stopButton.setEnabled(not run.stopRequested)

    # Actions

    def normalizeAll(self):
        return library._runAction(normalizeAll, _("Failed to start the normalization."))

    def stop(self):
        stopAll()

    def resume(self):
        current = manager(self.session)
        if current is not None:
            library._runAction(current.resume, _("Failed to resume the normalization."))

    def normalizeSelected(self):
        sids = library.studySelection()
        library._runAction(lambda: normalizeSubjects(sids), _("Failed to start the normalization."))

    def redoSelected(self):
        sids = library.studySelection()
        library._runAction(lambda: normalizeSubjects(sids, redo=True), _("Failed to start the normalization."))

    def _onAuto(self, checked):
        if self._settingChecks:
            return
        setOptions(self.session, auto_after_done=bool(checked))

    # Options

    def showOptions(self):
        """The Options popover under the Options link: template, quality and margin (stored in study.json)."""
        if self.options is None:
            self.options = _OptionsPopover(self)
        self.options.show()
        return self.options.widget

    def dispose(self):
        if self._view in library._studyViews:
            library._studyViews.remove(self._view)
        current = _manager
        if current is not None and self.updateProgress in current.changed:
            current.changed.remove(self.updateProgress)


class _OptionsPopover:
    """Template, quality and lesion margin of the study's normalizations (objectName StudioNormalizeOptions)."""

    def __init__(self, card):
        self.card = card
        popover = qt.QFrame(card.widget, qt.Qt.Popup)
        popover.setObjectName("StudioNormalizeOptions")
        popover.setFrameShape(qt.QFrame.StyledPanel)
        popover.setMinimumWidth(320)
        layout = qt.QVBoxLayout(popover)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        form = qt.QFormLayout()
        self.templateCombo = qt.QComboBox()
        self.templateCombo.setObjectName("StudioNormalizeTemplate")
        self.templateCombo.addItem(_("Automatic (the study's space)"), "auto")
        for templateId in TEMPLATE_IDS:
            self.templateCombo.addItem(templateId, templateId)
        self.templateCombo.connect("currentIndexChanged(int)", self._changed)
        form.addRow(_("Template"), self.templateCombo)
        self.qualityCombo = qt.QComboBox()
        self.qualityCombo.setObjectName("StudioNormalizeQuality")
        for quality, label in (("standard", _("Standard (affine + coarse nonlinear)")), ("fine", _("Fine (slower)")), ("affine", _("Affine only (fastest)"))):
            self.qualityCombo.addItem(label, quality)
        self.qualityCombo.connect("currentIndexChanged(int)", self._changed)
        form.addRow(_("Quality"), self.qualityCombo)
        self.marginSpin = qt.QDoubleSpinBox()
        self.marginSpin.setObjectName("StudioNormalizeMargin")
        self.marginSpin.setRange(*MARGIN_RANGE_MM)
        self.marginSpin.setSingleStep(1.0)
        self.marginSpin.setDecimals(0)
        self.marginSpin.setSuffix(" mm")
        self.marginSpin.setToolTip(_("Lesion margin excluded from matching (cost-function masking)"))
        self.marginSpin.connect("valueChanged(double)", self._changed)
        form.addRow(_("Lesion margin"), self.marginSpin)
        layout.addLayout(form)
        self.noteLabel = library._cardLabel("StudioNormalizeOptionsNote", 12, "warning")
        layout.addWidget(self.noteLabel)
        self.downloadButton = qt.QPushButton(_("Download NLin6Asym 2 mm (FSL licence terms: non-commercial)"))
        self.downloadButton.setObjectName("StudioNormalizeDownloadNLin6")
        ui.setRole(self.downloadButton, "secondary")
        self.downloadButton.setToolTip(_("Downloaded from TemplateFlow into the application cache, checked by SHA-256, only after this click."))
        self.downloadButton.connect("clicked()", self._download)
        layout.addWidget(self.downloadButton)
        self.statusLabel = library._cardLabel("StudioNormalizeOptionsStatus", 12, "text2")
        layout.addWidget(self.statusLabel)
        self.widget = popover
        self._loading = False
        self._load()

    def _load(self):
        options = readOptions(self.card.session.study)
        self._loading = True
        try:
            self.templateCombo.setCurrentIndex(max(0, self.templateCombo.findData(options["template"])))
            self.qualityCombo.setCurrentIndex(max(0, self.qualityCombo.findData(options["quality"])))
            self.marginSpin.setValue(options["dilate_mm"])
        finally:
            self._loading = False
        self._refreshNote()

    def _refreshNote(self):
        nc = _nc()
        template, note = resolveTemplate(self.card.session.study)
        missing = nc is not None and not nc.templateAvailable(template)
        self.noteLabel.setText(note or (_("{template} is not on this computer").format(template=templateDisplayName(template)) if missing else ""))
        self.noteLabel.setVisible(bool(self.noteLabel.text))
        self.downloadButton.setVisible(missing and template == TEMPLATE_IDS[1])

    def _changed(self, *args):
        if self._loading:
            return
        setOptions(self.card.session, template=self.templateCombo.itemData(self.templateCombo.currentIndex),
                   quality=self.qualityCombo.itemData(self.qualityCombo.currentIndex), dilate_mm=float(self.marginSpin.value))
        self._refreshNote()
        self.card.refresh()

    def _download(self):
        nc = _nc()
        if nc is None:
            return
        if not slicer.app.testingEnabled() and not slicer.util.confirmOkCancelDisplay(
                _("MNI152NLin6Asym is distributed under the FSL licence terms (non-commercial use). Download its 2 mm template "
                  "(about 1.4 MB) from TemplateFlow?"), windowTitle=_("Normalization template")):
            return

        def finished(paths):
            self.statusLabel.setText(_("Downloaded {template}.").format(template=TEMPLATE_IDS[1]))
            self._refreshNote()
            self.card.refresh()

        def failed(message):
            self.statusLabel.setText(message)

        self.statusLabel.setText(_("Downloading {template}…").format(template=TEMPLATE_IDS[1]))
        nc.downloadTemplateFiles(TEMPLATE_IDS[1], ("fixed", "mask"), finished, failed)

    def show(self):
        self._load()
        popover = self.widget
        popover.adjustSize()
        popover.move(self.card.optionsLink.mapToGlobal(qt.QPoint(0, self.card.optionsLink.height)))
        popover.show()


def _createCard(session):
    card = NormalizationCard(session)
    _cards[card.widget] = card
    card.widget.connect("destroyed()", lambda: (card.dispose(), _cards.pop(card.widget, None)))
    return card.widget


def _refreshCard(widget, session, selectedIds):
    card = _cards.get(widget)
    if card is not None and library._isAlive(widget):
        card.refresh(selectedIds)


def showReviewQueue():
    """"Review N →": show the 'Normalization to review' filter with its first row selected. Return False when nothing is to review."""
    session = _session()
    if session is None:
        return False
    rows = [row for row in session.study.subjects if toReview(row)]
    if not rows:
        _toast(_("No normalization is waiting for review"))
        return False
    shell = library._shell()
    if shell is not None and shell.currentWorkspace() != "library":
        shell.switchTo("library")
    lists = [view for view in list(library._studyViews) if hasattr(view, "setFilter") and hasattr(view, "visibleRows")]
    first = rows[0]["participant_id"]
    for view in lists:
        view.setFilter("norm_review")
    if lists:
        shown = lists[0].visibleRows(session)
        first = shown[0]["participant_id"] if shown else first
        lists[0].selectIds([first])
    return True


#
# SUBJECT section
#


class SubjectSection:
    """The 'Normalization' section of the SUBJECT card (native or normalized rows only)."""

    def __init__(self, session):
        self.session = session
        self.sid = None
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioNormalizeSubjectSection")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(_sectionLabel(_("Normalization"), "StudioNormalizeSubjectTitle"))
        self.status = library._cardLabel("StudioNormalizeSubjectStatus", 12, "text")
        layout.addWidget(self.status)
        self.normalizeRow, self.normalizeButton = library._rowButton(_("Normalize to MNI"), "StudioNormalizeSubjectButton", self._normalize, "primary")
        layout.addWidget(self.normalizeRow)
        from LesionNetworkMappingUI import widgets

        self.progress = widgets.ProgressRow(_("Stop"), "StudioNormalizeSubject")
        self.progress.stopButton.disconnect("clicked()")
        self.progress.stopButton.connect("clicked()", stopAll)
        layout.addWidget(self.progress.widget)
        self.rewarpRow, self.rewarpButton = library._rowButton(_("Re-warp the edited lesion"), "StudioNormalizeRewarpButton", self._rewarp)
        self.rewarpButton.setToolTip(_("The native lesion changed after the normalization: warp it again with the same transform"))
        layout.addWidget(self.rewarpRow)
        self.qcHolder = qt.QVBoxLayout()
        self.qcHolder.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.qcHolder)
        self.qcPanel = None
        self.qcKey = None
        self._loadingKey = None
        self.nextRow, self.nextButton = library._rowButton(_("Next to review →"), "StudioNormalizeNextButton", self._next)
        layout.addWidget(self.nextRow)
        current = manager(session)
        if current is not None:
            current.changed.append(self.updateProgress)

    # Content

    def refresh(self, sid):
        session = _session()
        if session is not None and session is not self.session:
            self.session = session  # another study was opened (Library keeps this section)
            self._clearQc()
        if session is None or sid is None or sid not in session.study:
            self.widget.hide()
            return
        self.sid = sid
        row = session.study.subject(sid)
        current = manager(session)
        if current is not None and self.updateProgress not in current.changed:
            current.changed.append(self.updateProgress)
        live = current.liveState(sid) if current is not None else None
        if not (isNative(row) or isNormalized(row) or live is not None):
            self._clearQc()
            self.widget.hide()
            return
        self.widget.show()
        failure = (current.failures.get(sid) if current is not None else None)
        self.progress.widget.setVisible(live is not None)
        self.normalizeRow.setVisible(False)
        self.rewarpRow.setVisible(False)
        self.nextRow.setVisible(False)
        if live is not None:
            self._clearQc()
            self.status.setText(_("Waiting for the normalization job") if live[0] == "queued" else _("Normalizing in the background"))
            self.updateProgress()
            return
        if isNormalized(row):
            self.status.setText(self._normalizedText(row, failure))
            self.rewarpRow.setVisible(isStale(row))
            self._showQc(sid, row)
            others = [other for other in session.study.subjects if toReview(other) and other["participant_id"] != sid]
            self.nextRow.setVisible(row.get("norm_qc") in ("pass", "fail") and bool(others))
            self.nextButton.setText(_("Next to review → ({n} left)").format(n=len(others)))
            return
        self._clearQc()
        if _missing(row.get("scan")):
            self.status.setText(_("Native space. The scan this lesion was traced on is needed to normalize it."))
        elif _missing(row.get("lesion")):
            self.status.setText(_("Native space. Trace the lesion first, or normalize the scan from Draw."))
        else:
            text = _("Native space: normalize it to use it for mapping.")
            if failure:
                text += " " + _("Last try: {reason}").format(reason=failure[:140])
            self.status.setText(text)
            self.normalizeRow.setVisible(True)
            template, _note = resolveTemplate(session.study)
            self.normalizeButton.setToolTip(_("Normalize this lesion to {template} in the background").format(template=templateDisplayName(template)))

    @staticmethod
    def _normalizedText(row, failure=None):
        space = str(row.get("space") or "").replace("MNI152", "")
        qc = {"pass": _("reviewed: looks right"), "fail": _("reviewed: does not look right")}.get(row.get("norm_qc"), _("not reviewed yet"))
        text = _("Normalized · {space} · {method} · {qc}").format(space=space, method=row.get("norm_method"), qc=qc)
        if isStale(row):
            text += "\n" + _("The native lesion changed after the normalization.")
        if failure:
            text += "\n" + _("Last try: {reason}").format(reason=failure[:140])
        return text

    # The QC panel (the mosaic is read in a worker thread)

    def _clearQc(self):
        if self.qcPanel is not None:
            library._discard(self.qcPanel)
            self.qcPanel = None
        self.qcKey = None
        self._loadingKey = None

    def _showQc(self, sid, row):
        key = (sid, row.get("norm_fingerprint"))
        if self.qcKey == key and self.qcPanel is not None:
            return
        if self._loadingKey == key:
            return
        outDir = _outputFolder(self.session.study, sid)
        self._loadingKey = key
        if self.qcPanel is not None:
            library._discard(self.qcPanel)
            self.qcPanel = None
            self.qcKey = None

        def finished(data, error):
            if self._loadingKey != key:
                return
            self._loadingKey = None
            if error is not None or data is None:
                self.status.setText(self.status.text + "\n" + _("The QC mosaic could not be read."))
                return
            if _session() is not self.session or self.sid != sid:
                return
            self._buildQc(sid, outDir, data, key)

        _Background(lambda: _loadQcData(sid, outDir), finished).start()

    def _buildQc(self, sid, outDir, data, key):
        nc = _nc()
        controller = _reviewController()
        if nc is None or controller is None:
            return
        record, outputs, png, thumbnail, stamp = data
        result = nc.NormalizeResult(outDir, record, outputs, "done")
        result.sid = sid
        result.pngBytes, result.thumbnailBytes = png, thumbnail
        result.commandLine = commandLineFor(sid)
        for stale in [other for other in _qcResults if other[0] == sid]:
            del _qcResults[stale]
        _qcResults[(sid, key[1], stamp)] = result
        controller.result = result
        panel = controller.buildQcWidget(result, QC_WIDTH_PX, lambda changes: self._redo(sid, changes), QC_MAX_HEIGHT_PX)
        menu = panel.findChild("QMenu", "LNMNormalizeMenu")
        if menu is not None:
            menu.addSeparator()
            menu.addAction(_("Review in Draw")).connect("triggered()", lambda: reviewInDraw(sid, result))
            menu.addAction(_("Re-warp edited native lesion")).connect("triggered()", lambda: rewarpSubject(sid))
            menu.addAction(_("Revert to native…")).connect("triggered()", lambda: revertToNative(sid))
        self.qcHolder.addWidget(panel)
        self.qcPanel = panel
        self.qcKey = key

    # Actions

    def _normalize(self):
        if self.sid is not None:
            sid = self.sid
            library._runAction(lambda: normalizeSubjects([sid]), _("Failed to start the normalization."))

    def _redo(self, sid, changes):
        options = {}
        if "quality" in changes:
            options["quality"] = changes["quality"]
        if "dilateMm" in changes:
            options["dilate_mm"] = changes["dilateMm"]
        library._runAction(lambda: normalizeSubjects([sid], options=options, redo=True), _("Failed to start the normalization."))

    def _rewarp(self):
        if self.sid is not None:
            rewarpSubject(self.sid)

    def _next(self):
        session = _session()
        if session is None or self.sid is None:
            return
        sid = self.sid
        if session.current() == sid:
            library._runAction(lambda: session.next(queue="norm_review"), _("Failed to open the next normalization."))
            return
        ids = session.study.subject_ids
        after = ids[ids.index(sid) + 1:] + ids[:ids.index(sid)]
        target = next((other for other in after if toReview(session.study.subject(other))), None)
        if target is not None:
            library._runAction(lambda: session.open(target, switchToDraw=False), _("Failed to open {sid}.").format(sid=target))
            library._setStudySelection([target])

    def updateProgress(self):
        current = manager(self.session) if _session() is self.session else None
        if current is None or self.sid is None:
            return
        run = current.liveRun(self.sid)
        if run is None:
            return
        nc = _nc()
        if run.current == self.sid:
            text = " · ".join(part for part in (_("Normalizing"), _(STAGE_LABELS.get(run.stage, run.stage or "")) + f" {round(100 * run.fraction)}%" if run.stage else "",
                                                _jobrunner().formatEta(run.eta)) if part)
            fraction = run.fraction
        else:
            text, fraction = _("Waiting for the normalization job"), 0.0
        if nc is not None:
            nc.setRowProgress(self.progress, fraction, text)
        self.progress.stopButton.setEnabled(not run.stopRequested)

    def dispose(self):
        current = _manager
        if current is not None and self.updateProgress in current.changed:
            current.changed.remove(self.updateProgress)
        self._clearQc()


_sections = {}


def _createSection(session):
    section = SubjectSection(session)
    _sections[section.widget] = section
    section.widget.connect("destroyed()", lambda: (section.dispose(), _sections.pop(section.widget, None)))
    return section.widget


def _refreshSection(widget, session, sid):
    section = _sections.get(widget)
    if section is not None and library._isAlive(widget):
        section.refresh(sid)


#
# Library extension points
#

CARD = {"section": "study", "order": 40, "create": _createCard, "refresh": _refreshCard}
SUBJECT_SECTION = {"order": 40, "create": _createSection, "refresh": _refreshSection}
COLUMNS = [{"key": "norm", "header": "Norm", "width": 76, "valueFn": normCell}]
FILTERS = [
    {"key": "needs_norm", "label": "Needs normalization", "predicate": needsNormalization},
    {"key": "norm_review", "label": "Normalization to review", "predicate": toReview, "qcColumn": "norm_qc"},
]
QUEUES = [{"key": "norm_review", "label": "Normalization review", "predicate": toReview}]


def _paletteEnabled():
    session = _session()
    return session is not None and any(canNormalizeRow(row) for row in session.study.subjects)


def _paletteReviewEnabled():
    session = _session()
    return session is not None and any(toReview(row) for row in session.study.subjects)


#: Search entries (dict form of the provider contract of StudioLib.palette)
PALETTE_ACTIONS = [
    {"id": "lnm.normalize-all", "title": "Normalize all native lesions", "category": "Library", "synonyms": ["register", "mni", "batch"],
     "callback": normalizeAll, "enabled": _paletteEnabled, "description": "Normalize every native-space lesion of the open study in the background"},
    {"id": "lnm.normalize-review", "title": "Review normalizations", "category": "Library", "synonyms": ["qc", "pass", "fail"],
     "callback": showReviewQueue, "enabled": _paletteReviewEnabled, "description": "Show the normalizations that wait for review"},
]


def _registerColumns():
    """Register the columns of the batch (A-03 registers most of them already); nothing is replaced."""
    try:
        schema = _lnm("schema")
    except Exception:
        return
    for name, kwargs in (
        ("scan_native", {"type": "path", "role": "bookkeeping", "section": "imaging", "description": "Scan in native space, before normalization"}),
        ("lesion_native", {"type": "path", "role": "bookkeeping", "section": "imaging", "description": "Lesion in native space, before normalization"}),
        ("norm_suggestion", {"type": "categorical", "role": "bookkeeping", "section": "workflow", "levels": {"looks-ok": None, "review": None},
                             "description": "Automatic normalization QC suggestion"}),
        ("norm_fingerprint", {"type": "text", "role": "bookkeeping", "section": "workflow", "description": "Fingerprint of the normalization inputs and settings"}),
        ("volume_native_ml", {"type": "numeric", "role": "metadata", "section": "analysis", "units": "mL", "description": "Lesion volume in native space"}),
    ):
        try:
            if schema.registered(name) is None and hasattr(schema, "register_column"):
                schema.register_column(schema._spec(name, **kwargs))
        except Exception:
            logging.warning(f"Studio Library: the column {name} could not be registered:\n{traceback.format_exc()}")


_registerColumns()
_install()

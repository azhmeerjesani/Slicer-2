r"""Scene autosave and crash recovery of Slicer Studio (A-01, SPEC 9.9), in Classic and in Studio.

While the application runs, the current scene is autosaved incrementally to a private per-user folder
(``QStandardPaths::AppLocalDataLocation/StudioAutosave``, D-A1), never to a user folder. Each autosave is a
standard Slicer scene (see ``autosave_io``). A per-process lock file detects crashes: at the next startup a
non-modal dialog lists the sessions that can be recovered (``recovery``).

Autosave is not Save: the scene keeps its "modified" state, ``Ctrl+S`` and the exit prompt keep their meaning,
and every message says "Autosaved", never "Saved".

Autosave never runs under ``--testing``, ``--no-main-window``, ``--exit-after-startup`` or with
``SLICER_STUDIO_AUTOSAVE=0``; the setting ``Studio/Autosave/Enabled=false`` stops autosaving but keeps
recovery available. ``SLICER_STUDIO_AUTOSAVE_DIR`` overrides the folder.

Public API: ``onStartupCompleted()`` (called by the Studio module), ``instance()``, ``suspended(reason)``,
``autosaveNow(reason)``, ``unsavedSummary()``, and the ``AutosaveService`` methods ``status()``,
``addStatusListener()``/``removeStatusListener()``, ``setRecoveryPresenter()``, ``pendingRecoveries()``,
``registerRecoveryHandler()``, ``registerContextProvider()``/``unregisterContextProvider()``,
``sessionDirectory()``, ``unsavedSummary()`` and ``backgroundWriter()``.

This module imports neither numpy nor xml: ``autosave_io`` and ``recovery`` are imported after startup.
"""

import contextlib
import logging
import os
import sys
import time
import traceback
import uuid

import qt
import slicer
from slicer.i18n import tr as _

#: Delay after startupCompleted before the menu actions, the settings panel and the poll timer are installed
INSTALL_DELAY_MS = 1000
#: Delay after startupCompleted before the autosave folder is scanned for sessions to recover
SCAN_DELAY_MS = 1500
POLL_INTERVAL_MS = 3000
#: Poll interval while the scene is modified and the service waits for the user to be idle: changes and input are
#: then timed to about 1 s, and the generation starts IdleSeconds after the last one (acceptance: within IdleSeconds + 3 s)
FAST_POLL_INTERVAL_MS = 1000
MIN_POLL_INTERVAL_MS = 50
#: Interval between the main-thread ticks of a generation
TICK_INTERVAL_MS = 15
HEARTBEAT_S = 600
#: Autosaving pauses this long after a disk error
ERROR_RETRY_S = 600
THUMBNAIL_INTERVAL_S = 300
THUMBNAIL_SIZE = 240
#: Main-thread budget left in a tick for a thumbnail
THUMBNAIL_BUDGET_MS = 15
#: A generation is aborted when a modal dialog is shown longer than this
MODAL_ABORT_S = 2.0
#: Slots of earlier scenes of a session closed without saving that are kept
MAX_CLOSED_SLOTS = 2
#: Largest estimated main-thread cost of the final generation when a scene is closed without saving
FINAL_GENERATION_BUDGET_MS = 300
#: Estimated main-thread write speed, for that estimate (raw C++ NRRD writes run at 2-3 GB/s)
_ESTIMATED_WRITE_BYTES_PER_MS = 500 * 1024
MIN_FREE_BYTES = 1 << 30
#: While the autosave folder is larger than MaxDiskMB, the retention rules run again at most this often
RUNTIME_PRUNE_INTERVAL_S = 600

_service = None


def instance():
    """The running autosave service, or None (testing, no main window, disabled by the environment)."""
    return _service


def isDisabledByEnvironment():
    """True when autosave must not run in this process (SPEC 9.1, 9.9)."""
    if os.environ.get("SLICER_STUDIO_AUTOSAVE") == "0":
        return True
    if slicer.app.testingEnabled():
        return True
    options = slicer.app.commandOptions()
    if options.noMainWindow or options.exitAfterStartup or getattr(options, "runPythonAndExit", False):
        return True
    return slicer.util.mainWindow() is None


def onStartupCompleted():
    """Start the autosave service (called by the Studio module after the application started)."""
    global _service
    if _service is not None or isDisabledByEnvironment():
        return
    _service = AutosaveService()
    _service.start()


@contextlib.contextmanager
def suspended(reason=""):
    """Context manager: no autosave generation starts inside it (no-op without a service)."""
    service = _service
    if service is None:
        yield
        return
    service._suspendCount += 1
    try:
        yield
    finally:
        service._suspendCount -= 1


def autosaveNow(reason=""):
    """Autosave now (without waiting for the user to be idle). Return True if a generation started."""
    return _service.autosaveNow(reason) if _service is not None else False


def unsavedSummary():
    """The line that the close prompt appends (U-03), or '' (USABILITY 8 rule 10)."""
    return _service.unsavedSummary() if _service is not None else ""


def _io():
    from StudioLib import autosave_io

    return autosave_io


def _recovery():
    from StudioLib import recovery

    return recovery


class AutosaveStatus:
    """State of the autosave, for status displays (A-01b)."""

    def __init__(self):
        #: idle, saving, paused, disabled or error
        self.state = "idle"
        self.lastSaveTime = None
        self.dirty = False
        self.bytes = 0
        self.message = ""
        self.sessionDir = None

    def copy(self):
        status = AutosaveStatus()
        status.__dict__.update(self.__dict__)
        return status


class AutosaveService:
    """Autosaves the scene of this process and offers the recovery of crashed sessions.

    ``force=True`` creates a service also under ``--testing`` (tests); ``root`` overrides the autosave folder.
    Timers start with ``start()``; tests may instead drive ``poll()``, ``autosaveNow()`` and ``waitForIdle()``.
    """

    def __init__(self, force=False, root=None):
        self.force = force
        self._rootOverride = root
        #: SlotWriter options that replace the defaults (tests)
        self.writerOptionsOverride = {}
        self.root = None
        self.sessionId = None
        self.sessionDir = None
        self._lock = None
        self.started = time.time()
        self.slotIndex = 1
        self.writer = None
        self.plan = None
        self.slots = {}
        self._savedFingerprint = None
        self._lastPolledFingerprint = None
        self.dirty = False
        self.firstDirtyTime = None
        self.lastChangeTime = time.time()
        self.lastInputTime = time.time()
        self.lastGenerationTime = None
        self._lastPollTime = None
        self._nextPollMs = None
        self._pollStart = 0.0
        #: (URL, root directory) of the scene at the last poll, given back to the scene when an autosave is added to it
        self._sceneLocation = None
        self._importIntoData = False
        #: slot folders being recovered: never deleted by the closed-slot limit or the retention rules
        self._protectedSlots = set()
        #: size of the other sessions of the autosave folder (measured by the retention rules)
        self._otherSessionsBytes = 0
        self._lastRuntimePrune = 0.0
        self._lastCursor = None
        self._modalSince = None
        self._pausedUntil = 0.0
        self._suspendCount = 0
        self._recovering = 0
        self._lastThumbnailTime = 0.0
        self._lastUnsavedAutosaved = False
        self._statusListeners = []
        self._contextProviders = []
        self._recoveryHandlers = []
        self._presenter = None
        self._pendingRecoveries = []
        self._observations = []
        self._planObservations = []
        self._installed = False
        self._quitting = False
        self.metrics = {"last": None, "history": [], "pollMs": None}
        self._status = AutosaveStatus()
        # Read when the service is installed (start() + 1 s) or used: autosave_io is not imported at startup
        self.settings = {}
        self._pollTimer = qt.QTimer()
        self._pollTimer.setInterval(POLL_INTERVAL_MS)
        self._pollTimer.connect("timeout()", self.poll)
        self._tickTimer = qt.QTimer()
        self._tickTimer.setInterval(TICK_INTERVAL_MS)
        self._tickTimer.connect("timeout()", self._tick)
        self._heartbeatTimer = qt.QTimer()
        self._heartbeatTimer.setInterval(HEARTBEAT_S * 1000)
        self._heartbeatTimer.connect("timeout()", self._heartbeat)
        self._observeScene()

    #
    # Life cycle
    #

    def start(self):
        """Install the user interface and the timers after startup (at most 3 ms here)."""
        qt.QTimer.singleShot(INSTALL_DELAY_MS, self._install)
        qt.QTimer.singleShot(SCAN_DELAY_MS, self.scanAtStartup)
        slicer.app.connect("aboutToQuit()", self._onAboutToQuit)

    def _install(self):
        try:
            self.reloadSettings()
            self.installActions()
            self.registerPaletteProvider()
            from StudioLib import autosave_settings

            autosave_settings.install(self)
        except Exception:
            logging.error("Autosave: installing the user interface failed:\n" + traceback.format_exc())
        # Changes before the first poll are counted from now
        self._lastPollTime = time.time()
        self._pollTimer.start()
        self._heartbeatTimer.start()

    def dispose(self):
        """Stop the timers and remove the observers (tests)."""
        for timer in (self._pollTimer, self._tickTimer, self._heartbeatTimer):
            timer.stop()
        if self.plan is not None and self.writer is not None:
            self.writer.abort(self.plan, "disposed")
            self.plan = None
        self._removeObservers()
        self._removePlanObservers()
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None

    def installActions(self):
        """File menu action 'Recover Autosaved Work…' and the main-window action 'Autosave now'."""
        mainWindow = slicer.util.mainWindow()
        if mainWindow is None or self._installed:
            return
        self._installed = True
        recoverAction = qt.QAction(_("Recover Autosaved Work…"), mainWindow)
        recoverAction.setObjectName("StudioRecoverAutosaveAction")
        recoverAction.setToolTip(_("Recover unsaved work that was autosaved before a crash or before closing without saving"))
        recoverAction.connect("triggered()", self.showRecoveryDialog)
        fileMenu = mainWindow.findChild(qt.QMenu, "FileMenu")
        if fileMenu is not None:
            actions = list(fileMenu.actions())
            recent = [action for action in actions if action.menu() is not None]
            # After the "Recent" submenu (Add Data, Add DICOM Data, Recent, ...)
            before = None
            if recent:
                index = actions.index(recent[0])
                before = actions[index + 1] if index + 1 < len(actions) else None
            if before is not None:
                fileMenu.insertAction(before, recoverAction)
            else:
                fileMenu.addAction(recoverAction)
        self.recoverAction = recoverAction
        nowAction = qt.QAction(_("Autosave now"), mainWindow)
        nowAction.setObjectName("StudioAutosaveNowAction")
        nowAction.setProperty("studioDescription", _("Autosave now"))
        nowAction.connect("triggered()", lambda: self.autosaveNow("action"))
        mainWindow.addAction(nowAction)
        self.autosaveNowAction = nowAction

    def registerPaletteProvider(self):
        """Search (W3-06) entries, when this build has the palette provider contract (SPEC 2.12)."""
        import importlib.util

        try:
            if importlib.util.find_spec("StudioLib.palette") is None:
                return False
            from StudioLib import palette
        except Exception:
            return False
        if not hasattr(palette, "registerProvider"):
            return False
        palette.registerProvider(self.paletteItems)
        return True

    def paletteItems(self, shell=None):
        return [
            {"id": "autosave.recover", "title": _("Recover autosaved work…"), "category": "File", "callback": self.showRecoveryDialog,
             "synonyms": ["restore", "crash", "unsaved", "autosave"]},
            {"id": "autosave.now", "title": _("Autosave now"), "category": "File", "callback": lambda: self.autosaveNow("search"),
             "synonyms": ["autosave"]},
            {"id": "autosave.settings", "title": _("Autosave settings…"), "category": "File", "callback": showAutosaveSettings,
             "synonyms": ["autosave", "recovery"]},
        ]

    #
    # Settings
    #

    def reloadSettings(self, *args):
        io = _io()
        self.settings = {name: io.settingsValue(name) for name in io.DEFAULT_SETTINGS}
        self._updateStatus()

    def enabled(self):
        if not self.settings:
            self.reloadSettings()
        return bool(self.settings.get("Enabled", True))

    def rootDirectory(self):
        """The autosave folder for new sessions."""
        return self._rootOverride or _io().defaultRoot()

    #
    # Scene observation
    #

    def _observeScene(self):
        scene = slicer.mrmlScene
        for event, callback in (
            (slicer.vtkMRMLScene.StartCloseEvent, self._onSceneStartClose),
            (slicer.vtkMRMLScene.StartImportEvent, self._onSceneStartImport),
            (slicer.vtkMRMLScene.EndImportEvent, self._onSceneEndImport),
        ):
            self._observations.append((scene, scene.AddObserver(event, callback)))

    def _removeObservers(self):
        for caller, tag in self._observations:
            caller.RemoveObserver(tag)
        self._observations = []

    def _observePlan(self):
        scene = slicer.mrmlScene
        self._planObservations.append((scene, scene.AddObserver(slicer.vtkMRMLScene.NodeRemovedEvent, self._onNodeRemoved)))

    def _removePlanObservers(self):
        for caller, tag in self._planObservations:
            caller.RemoveObserver(tag)
        self._planObservations = []

    def _onNodeRemoved(self, caller, event):
        plan = self.plan
        if plan is None:
            return
        for nodeId in list(plan.nodeIds):
            if caller.GetNodeByID(nodeId) is None:
                self._abortGeneration(f"{nodeId} was removed")
                return

    #
    # Scheduling
    #

    def poll(self):
        """Detect changes (MTimes only) and start a generation when the user is idle. At most 2 ms at 150 nodes."""
        self._pollStart = time.perf_counter()
        self.metrics["pollMs"] = None
        self._nextPollMs = None
        try:
            self._poll()
        except Exception:
            logging.error("Autosave: poll failed:\n" + traceback.format_exc())
        if self.metrics["pollMs"] is None:
            self.metrics["pollMs"] = (time.perf_counter() - self._pollStart) * 1000.0
        if self._pollTimer.isActive():
            interval = self._nextPollMs or POLL_INTERVAL_MS
            if self._pollTimer.interval != interval:
                self._pollTimer.start(interval)

    def _poll(self):
        now = time.time()
        previousPoll = getattr(self, "_lastPollTime", None) or now
        self._lastPollTime = now
        # When a change or input happened: between the previous poll and this one. After the fast polls that run
        # while the service waits for the user to be idle (at most 1 s apart), it is counted from this poll, so the
        # user was idle for at least IdleSeconds (D-A3). After a longer gap (3 s), it is counted from the middle of
        # the gap: the generation then starts within 1.5 s of IdleSeconds after the edit (acceptance: Scene.mrml
        # within IdleSeconds + 3 s; D-A3: after IdleSeconds without input).
        gap = now - previousPoll
        observed = now if gap <= 1.5 * FAST_POLL_INTERVAL_MS / 1000.0 else previousPoll + gap / 2.0
        cursor = qt.QCursor.pos()
        cursorPosition = (cursor.x(), cursor.y())
        if cursorPosition != self._lastCursor:
            if self._lastCursor is not None:  # the first position read is not an input
                self.lastInputTime = observed
            self._lastCursor = cursorPosition
        if not self.enabled() or self.plan is not None:
            return
        scene = slicer.mrmlScene
        if self._sceneBusy(scene):
            return
        self._sceneLocation = (scene.GetURL(), scene.GetRootDirectory())
        io = _io()
        fingerprint = io.sceneFingerprint(scene, io.sentinelIds())
        if fingerprint != self._lastPolledFingerprint:
            self._lastPolledFingerprint = fingerprint
            self.lastChangeTime = observed
        dirty = fingerprint != self._savedFingerprint
        if dirty and not self.dirty:
            self.firstDirtyTime = now
        if dirty != self.dirty:
            self.dirty = dirty
            self._updateStatus()
        if not dirty or now < self._pausedUntil or self._suspendCount > 0:
            return
        if not self._hasStorableData(scene):
            return
        interval = self.settings["IntervalMinutes"] * 60.0
        idle = now - max(self.lastChangeTime, self.lastInputTime)
        # At most every IntervalMinutes (D-A3), after IdleSeconds without input, or during continuous edits
        # once the scene has been dirty for IntervalMinutes
        rateOk = self.lastGenerationTime is None or now - self.lastGenerationTime >= interval
        due = idle >= self.settings["IdleSeconds"] or now - (self.firstDirtyTime or now) >= interval
        # The poll cost does not include starting a generation
        self.metrics["pollMs"] = (time.perf_counter() - self._pollStart) * 1000.0
        if rateOk and due and self._mainThreadFree():
            if self._startGeneration("idle" if idle >= self.settings["IdleSeconds"] else "interval", fingerprint):
                return
        if rateOk:
            # Poll again when the user will have been idle for IdleSeconds (at most 1 s from now)
            remainingMs = (self.settings["IdleSeconds"] - idle) * 1000.0 if not due else FAST_POLL_INTERVAL_MS
            self._nextPollMs = int(min(FAST_POLL_INTERVAL_MS, max(MIN_POLL_INTERVAL_MS, remainingMs + 10)))

    @staticmethod
    def _sceneBusy(scene):
        return scene.IsClosing() or scene.IsImporting() or scene.IsBatchProcessing() or scene.IsRestoring()

    @staticmethod
    def _hasStorableData(scene):
        """True if the scene has data stored in files (not only views and cameras, which are storable nodes too)."""
        io = _io()
        nodes = scene.GetNodesByClass("vtkMRMLStorableNode")
        for index in range(nodes.GetNumberOfItems()):
            node = nodes.GetItemAsObject(index)
            if node.GetSaveWithScene() and not node.GetHideFromEditors() and (node.GetStorageNode() is not None or io.defaultStorageNodeClassName(node)):
                return True
        return False

    def _mainThreadFree(self):
        if _mouseButtonsPressed():
            return False
        if slicer.app.activeModalWidget() is not None or qt.QApplication.activePopupWidget() is not None:
            return False
        return qt.QApplication.overrideCursor() is None

    def autosaveNow(self, reason="", synchronous=False):
        """Start a generation now if anything changed (bypasses the idle and interval conditions only).

        Return True if a generation started (or, with ``synchronous``, was written).
        """
        if not self.enabled() and not self.force:
            return False
        if self.plan is not None:
            return False
        scene = slicer.mrmlScene
        if self._sceneBusy(scene) or self._suspendCount > 0:
            return False
        io = _io()
        fingerprint = io.sceneFingerprint(scene, io.sentinelIds())
        if fingerprint == self._savedFingerprint or not self._hasStorableData(scene):
            return False
        if not synchronous and not self._mainThreadFree():
            return False
        return self._startGeneration(reason or "now", fingerprint, synchronous=synchronous)

    def waitForIdle(self, timeoutS=60.0):
        """Process events until no generation runs and the background writer is idle (tests). Return True if idle."""
        deadline = time.monotonic() + timeoutS
        writer = _io().backgroundWriter()
        while time.monotonic() < deadline:
            slicer.app.processEvents()
            if self.plan is None and writer.pending() == 0:
                return True
            time.sleep(0.005)
        return False

    #
    # Generations
    #

    def _ensureSession(self):
        """Create the session folder and its lock at the first generation."""
        if self.sessionDir is not None:
            return True
        io = _io()
        root = self.rootDirectory()
        io.ensureRoot(root)
        sessionId = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{uuid.uuid4().hex[:4]}"
        sessionDir = os.path.join(root, sessionId)
        os.makedirs(sessionDir)
        lock = qt.QLockFile(os.path.join(sessionDir, io.LOCK_FILE))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            raise OSError(f"cannot lock {sessionDir}")
        self.root, self.sessionId, self.sessionDir, self._lock = root, sessionId, sessionDir, lock
        self._status.sessionDir = sessionDir
        self._writeSessionJson()
        return True

    def slotName(self, index=None):
        return f"scene-{index or self.slotIndex}"

    def slotDirectory(self):
        return os.path.join(self.sessionDir, self.slotName()) if self.sessionDir else None

    def _slotWriter(self):
        io = _io()
        if self.writer is None:
            slotDir = self.slotDirectory()
            os.makedirs(slotDir, exist_ok=True)
            self.writer = io.SlotWriter(slotDir, io.backgroundWriter(), self._writerOptions())
        else:
            self.writer.options.update(self._writerOptions())
        return self.writer

    def _writerOptions(self):
        options = {"maxNodeBytes": int(self.settings["MaxNodeMB"]) << 20, "autosaveRoot": self.root}
        options.update(self.writerOptionsOverride)
        return options

    def _startGeneration(self, reason, fingerprint, synchronous=False, mainThreadOnly=False, ignoreBusy=False):
        io = _io()
        now = time.time()
        try:
            self._ensureSession()
            writer = self._slotWriter()
            if synchronous:
                writer.executor.drain(3.0)
            idle = now - max(self.lastChangeTime, self.lastInputTime)
            plan = writer.plan(slicer.mrmlScene, fingerprint, idleSeconds=idle, ignoreBusy=ignoreBusy)
        except io.GenerationAborted:
            return False
        except OSError as error:
            self._pause(_("Autosave paused: {error}").format(error=error))
            return False
        except Exception:
            logging.error("Autosave: planning failed:\n" + traceback.format_exc())
            self._setState("error", _("Autosave failed (see the error log)."))
            return False
        plan.reason = reason
        estimated = sum(job.entry.get("bytes") or 0 for job in plan.jobs if hasattr(job, "entry"))
        if not self._enoughSpace(estimated):
            writer.abort(plan, "not enough disk space")
            self._pause(_("Autosave paused: less than {size} free on the disk.").format(size=_formatBytes(max(MIN_FREE_BYTES, 2 * estimated))))
            return False
        if mainThreadOnly:
            self._dropSnapshotJobs(plan)
        self.plan = plan
        self.lastGenerationTime = now
        self._observePlan()
        self._setState("saving")
        if synchronous:
            return self._runSynchronously(plan)
        self._tickTimer.start()
        return True

    def _dropSnapshotJobs(self, plan):
        """Final generations are short: volumes larger than MAIN_THREAD_VOLUME_MB keep their previous autosave, flagged stale."""
        kept = []
        limit = _io().MAIN_THREAD_VOLUME_MB << 20
        for job in plan.jobs:
            if type(job).__name__ == "_SnapshotJob" and (job.entry.get("bytes") or 0) > limit:
                old = plan.oldEntries.get(job.nodeId)
                plan.incomplete = True
                if old and old.get("dir") and os.path.isdir(os.path.join(plan.slotDir, old["dir"])):
                    plan.entries[job.nodeId] = dict(old, stale=True)
                else:
                    plan.entries[job.nodeId] = dict(job.entry, kind="deferred", reason="not autosaved at exit")
            else:
                kept.append(job)
        plan.jobs.clear()
        plan.jobs.extend(kept)

    def _runSynchronously(self, plan):
        writer = self.writer
        try:
            while not writer.runMainThreadStep(plan, slicer.mrmlScene, budgetMs=1e9, ignoreBusy=True):
                writer.executor.drain(10.0)
            writer.submitWorkerJobs(plan)
            writer.executor.drain(30.0)
            result = writer.finalize(plan)
            writer._finished(plan, result)
        except Exception as error:
            writer.abort(plan, str(error))
            self.plan = None
            self._removePlanObservers()
            logging.error("Autosave: final generation failed:\n" + traceback.format_exc())
            return False
        self._generationDone(plan)
        return True

    def _tick(self):
        plan = self.plan
        if plan is None:
            self._tickTimer.stop()
            return
        if plan.cancelled:
            self._abortGeneration(plan.abortReason or "cancelled")
            return
        # Wait while the user interacts; abort if a modal dialog stays open
        if slicer.app.activeModalWidget() is not None:
            if self._modalSince is None:
                self._modalSince = time.monotonic()
            elif time.monotonic() - self._modalSince > MODAL_ABORT_S:
                self._abortGeneration("a dialog is open")
            return
        self._modalSince = None
        if _mouseButtonsPressed() or qt.QApplication.activePopupWidget() is not None:
            return
        io = _io()
        start = time.perf_counter()
        try:
            done = self.writer.runMainThreadStep(plan, slicer.mrmlScene)
        except io.GenerationAborted as error:
            self._abortGeneration(str(error))
            return
        except OSError as error:
            self._abortGeneration(str(error))
            self._pause(_("Autosave paused: {error}").format(error=error))
            return
        except Exception:
            logging.error("Autosave: writing failed:\n" + traceback.format_exc())
            self._abortGeneration("error")
            self._setState("error", _("Autosave failed (see the error log)."))
            return
        if not done:
            return
        if self._thumbnailDue(plan):
            elapsed = (time.perf_counter() - start) * 1000.0
            if io.SLICE_BUDGET_MS - elapsed < THUMBNAIL_BUDGET_MS:
                return  # in the next tick (the main-thread jobs are done)
            plan.thumbnailChecked = True
            thumbnailStart = time.perf_counter()
            self._updateThumbnail(plan)
            thumbnailMs = (time.perf_counter() - thumbnailStart) * 1000.0
            plan.metrics["mainThreadMs"] += thumbnailMs
            plan.metrics["maxSliceMs"] = max(plan.metrics["maxSliceMs"], elapsed + thumbnailMs)
        self._tickTimer.stop()
        self.writer.submitFinalize(plan, onDone=self._generationDone, onError=self._generationFailed)

    def _abortGeneration(self, reason):
        plan = self.plan
        self._tickTimer.stop()
        self._removePlanObservers()
        self.plan = None
        self._modalSince = None
        if plan is not None and self.writer is not None:
            self.writer.abort(plan, reason)
            logging.debug(f"Autosave generation {plan.generation} aborted: {reason}")
        # The scene stays dirty: a later poll starts a new generation
        self.lastGenerationTime = None if self._savedFingerprint is None else self.lastGenerationTime
        self._setState("idle")

    def _generationFailed(self, plan, error):
        if plan is not self.plan:
            return
        self._removePlanObservers()
        self.plan = None
        message = str(error)
        if isinstance(error, OSError):
            self._pause(_("Autosave paused: {error}").format(error=message))
        else:
            logging.error(f"Autosave generation failed: {message}")
            self._setState("error", _("Autosave failed (see the error log)."))

    def _generationDone(self, plan):
        if plan is not self.plan:
            return
        self._removePlanObservers()
        self.plan = None
        result = plan.result or {}
        plan.metrics["workerMs"] += result.get("workerMs", 0.0)
        metrics = dict(plan.metrics)
        # Main-thread time of the whole generation: planning (scene XML, classification) and the ticks
        metrics["mainThreadTotalMs"] = metrics["planMs"] + metrics["mainThreadMs"]
        metrics["reason"] = getattr(plan, "reason", "")
        self.metrics["last"] = metrics
        self.metrics["history"] = (self.metrics["history"] + [metrics])[-20:]
        logging.debug(f"Autosave generation {plan.generation}: {metrics}")
        if not plan.incomplete:
            self._savedFingerprint = plan.fingerprint
        self.dirty = _io().sceneFingerprint(slicer.mrmlScene, _io().sentinelIds()) != self._savedFingerprint
        self.firstDirtyTime = time.time() if self.dirty else None
        self._lastUnsavedAutosaved = plan.unsaved
        slot = self.slots.setdefault(self.slotName(), {"name": self.slotName(), "state": "current"})
        slot.update({
            "generation": plan.generation,
            "updated": _isoTime(),
            "bytes": result.get("bytes", 0),
            "summary": plan.summary,
            "unsaved": plan.unsaved,
            "thumbnail": _io().THUMBNAIL_FILE if os.path.exists(os.path.join(plan.slotDir, _io().THUMBNAIL_FILE)) else None,
        })
        slot.update(self._context())
        self._status.lastSaveTime = time.time()
        self._status.bytes = sum(s.get("bytes", 0) for s in self.slots.values())
        self._writeSessionJson()
        self._setState("idle")
        self._maybePrune()
        if plan.wroteData:
            self._showClassicStatusMessage()

    def _enoughSpace(self, estimatedBytes):
        import shutil

        try:
            free = shutil.disk_usage(self.sessionDir or self.rootDirectory()).free
        except OSError:
            return True
        return free >= max(MIN_FREE_BYTES, 2 * estimatedBytes)

    def _pause(self, message):
        self._pausedUntil = time.time() + ERROR_RETRY_S
        self._setState("paused", message)
        logging.warning(message)

    #
    # Thumbnails and context
    #

    def _context(self):
        context = {}
        shell = self._shell()
        if shell is not None:
            try:
                context["workspace"] = shell.currentWorkspace()
            except Exception:
                pass
        for provider in list(self._contextProviders):
            try:
                context.update(provider() or {})
            except Exception:
                logging.error("Autosave: context provider failed:\n" + traceback.format_exc())
        return context

    @staticmethod
    def _shell():
        module = sys.modules.get("StudioLib.shell")
        return module.instance() if module is not None else None

    def _thumbnailDue(self, plan):
        if getattr(plan, "thumbnailChecked", False):
            return False
        path = os.path.join(plan.slotDir, _io().THUMBNAIL_FILE)
        return time.time() - self._lastThumbnailTime >= THUMBNAIL_INTERVAL_S or not os.path.exists(path)

    def _updateThumbnail(self, plan):
        """Thumbnail of the first visible slice view: at most every 5 min, never a network map (LNM blinding)."""
        now = time.time()
        io = _io()
        path = os.path.join(plan.slotDir, io.THUMBNAIL_FILE)
        try:
            image = self.grabThumbnail()
        except Exception:
            logging.debug("Autosave: thumbnail failed:\n" + traceback.format_exc())
            return
        if image is None:
            return
        if image.save(path, "PNG"):
            self._lastThumbnailTime = now

    def grabThumbnail(self):
        """A 240 px image of the first visible slice view, or None (map shown, rendering paused, no view)."""
        shell = self._shell()
        if shell is not None:
            if shell.currentWorkspace() == "map":
                return None
            if hasattr(shell, "isRenderPausedByCanvas") and shell.isRenderPausedByCanvas():
                return None
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return None
        io = _io()
        target = None
        for name in layoutManager.sliceViewNames():
            sliceWidget = layoutManager.sliceWidget(name)
            if sliceWidget is None or not sliceWidget.isVisible() or sliceWidget.width < 2 or sliceWidget.height < 2:
                continue
            composite = sliceWidget.mrmlSliceCompositeNode()
            scene = slicer.mrmlScene
            for volumeId in (composite.GetBackgroundVolumeID(), composite.GetForegroundVolumeID()):
                if volumeId and io.isNetworkMapNode(scene.GetNodeByID(volumeId)):
                    return None
            if target is None:
                target = sliceWidget.sliceView()
        if target is None:
            return None
        pixmap = target.grab()
        return pixmap.toImage().scaled(THUMBNAIL_SIZE, THUMBNAIL_SIZE, qt.Qt.KeepAspectRatio, qt.Qt.SmoothTransformation)

    #
    # Scene close, import and quit
    #

    def _onSceneStartClose(self, caller=None, event=None):
        try:
            self._closeSlot()
        except Exception:
            logging.error("Autosave: closing the scene failed:\n" + traceback.format_exc())

    def _closeSlot(self):
        if self.plan is not None:
            self._abortGeneration("the scene is closed")
        scene = slicer.mrmlScene
        writer = self.writer
        if self._recovering == 0 and self.enabled() and scene.GetStorableNodesModifiedSinceRead() and self._hasStorableData(scene):
            # The user discarded unsaved changes: keep them recoverable (final generation, if cheap enough)
            final = writer is not None and writer.hasContent()
            if self._estimateFinalMs(scene) <= FINAL_GENERATION_BUDGET_MS:
                io = _io()
                fingerprint = io.sceneFingerprint(scene, io.sentinelIds())
                if fingerprint != self._savedFingerprint or not final:
                    final = self._startGeneration("close", fingerprint, synchronous=True, mainThreadOnly=True, ignoreBusy=True) or final
            if final and self.writer is not None:
                slot = self.slots.setdefault(self.slotName(), {"name": self.slotName()})
                slot["state"] = "closed-unsaved"
                slot["closed"] = _isoTime()
                self._limitClosedSlots()
                self._writeSessionJson()
                self._nextSlot()
                return
        if writer is not None:
            writer.deleteSlot()
            self.slots.pop(self.slotName(), None)
            self._writeSessionJson()
        self._nextSlot()

    def _estimateFinalMs(self, scene):
        """Estimated main-thread cost of a final generation of the modified data."""
        io = _io()
        entries = self.writer.entries if self.writer is not None else {}
        total = 0.0
        nodes = scene.GetNodesByClass("vtkMRMLStorableNode")
        for index in range(nodes.GetNumberOfItems()):
            node = nodes.GetItemAsObject(index)
            if not node.GetSaveWithScene() or not node.GetModifiedSinceRead():
                continue
            entry = entries.get(node.GetID()) or {}
            if entry.get("kind") == "write" and entry.get("contentMTime") == io.contentMTime(node):
                continue
            size = io.estimateBytes(node)
            if size > io.MAIN_THREAD_VOLUME_MB << 20 and node.IsA("vtkMRMLScalarVolumeNode"):
                continue  # large volumes keep their previous autosave
            total += entry.get("lastWriteMs") or size / _ESTIMATED_WRITE_BYTES_PER_MS
        return total

    def _limitClosedSlots(self):
        # A slot being recovered is not counted: one extra closed slot is kept until the next scene is closed
        closed = sorted((s for s in self.slots.values() if s.get("state") == "closed-unsaved" and not self.isSlotProtected(s["name"])),
                        key=lambda s: s.get("closed") or "")
        writer = _io().backgroundWriter()
        for slot in closed[:-MAX_CLOSED_SLOTS] if len(closed) > MAX_CLOSED_SLOTS else []:
            writer.submit(_io().removeTree, os.path.join(self.sessionDir, slot["name"]))
            self.slots.pop(slot["name"], None)

    def _nextSlot(self):
        if self.writer is not None:
            self.slotIndex += 1
        self.writer = None
        self._savedFingerprint = None
        self._lastPolledFingerprint = None
        self.dirty = False
        self.firstDirtyTime = None
        self.lastGenerationTime = None
        self._lastUnsavedAutosaved = False
        self._lastThumbnailTime = 0.0
        self._updateStatus()

    def _onSceneStartImport(self, caller=None, event=None):
        # Load Scene clears the scene before the import starts; adding a scene file keeps the data
        self._importIntoData = not self._recovering and self._hasStorableData(slicer.mrmlScene)

    def _onSceneEndImport(self, caller=None, event=None):
        """Loading a Scene.mrml of the autosave folder (Load Scene): link the files back to their original locations."""
        if self._recovering:
            return
        try:
            recovery = _recovery()
            slotDir = recovery.autosaveSlotOfScene(slicer.mrmlScene, self.knownRoots())
            if slotDir is None:
                return
            # Added to a scene with data: that scene keeps its location (the import set the URL to the autosave file)
            restoreUrl = self._sceneLocation if self._importIntoData and self._sceneLocation else None
            report = recovery.rehomeAfterLoad(slicer.mrmlScene, slotDir, restoreUrl=restoreUrl)
            recovery.notify(_("Opened an autosave: files re-linked to their original locations."), report)
        except Exception:
            logging.error("Autosave: re-linking an opened autosave failed:\n" + traceback.format_exc())

    def knownRoots(self):
        roots = [self.rootDirectory(), _io().builtinRoot()]
        if self.root:
            roots.append(self.root)
        return [root for index, root in enumerate(roots) if root and root not in roots[:index]]

    def _onAboutToQuit(self):
        if self._quitting:
            return
        self._quitting = True
        try:
            self._quit()
        except Exception:
            logging.error("Autosave: exit failed:\n" + traceback.format_exc())

    def _quit(self):
        for timer in (self._pollTimer, self._heartbeatTimer):
            timer.stop()
        io = _io()
        writer = io.backgroundWriter()
        if self.plan is not None:
            self._abortGeneration("exit")
        writer.drain(3.0)
        scene = slicer.mrmlScene
        unsaved = bool(scene.GetStorableNodesModifiedSinceRead()) and self._hasStorableData(scene)
        if unsaved and self.enabled():
            # The user chose "Exit (discard modifications)": keep the changes recoverable (main-thread jobs only)
            fingerprint = io.sceneFingerprint(scene, io.sentinelIds())
            if fingerprint != self._savedFingerprint and self._estimateFinalMs(scene) <= FINAL_GENERATION_BUDGET_MS:
                self._startGeneration("exit", fingerprint, synchronous=True, mainThreadOnly=True, ignoreBusy=True)
        if self.sessionDir is not None:
            slot = self.slots.get(self.slotName())
            if slot is not None and not unsaved and slot.get("state") == "current":
                slot["unsaved"] = False
            self._writeSessionJson(state="closed", unsavedAtExit=unsaved and self.writer is not None and self.writer.hasContent())
        # The final session.json is queued after the other jobs. If a long write (a large snapshot) is still running
        # after 3 s, the exit goes on: the session is then reported as closed unexpectedly at the next startup,
        # with its last complete autosave (a crash-safe state)
        writer.drain(3.0)
        self._removeObservers()
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None

    #
    # Session file
    #

    def _sessionJson(self, state="running", unsavedAtExit=None):
        io = _io()
        return {
            "format": io.FORMAT,
            "version": io.VERSION,
            "sessionId": self.sessionId,
            "application": {
                "name": slicer.app.applicationName,
                "version": slicer.app.applicationVersion,
                "revision": slicer.app.repositoryRevision,
                "platform": sys.platform,
            },
            "pid": os.getpid(),
            "host": qt.QSysInfo.machineHostName() if hasattr(qt.QSysInfo, "machineHostName") else "",
            "started": _isoTime(self.started),
            "updated": _isoTime(),
            "heartbeat": _isoTime(),
            "state": state,
            "unsavedAtExit": unsavedAtExit,
            "presentedCount": 0,
            "slots": [dict(slot) for slot in sorted(self.slots.values(), key=lambda s: s["name"])],
        }

    def _writeSessionJson(self, state="running", unsavedAtExit=None):
        if self.sessionDir is None:
            return
        io = _io()
        data = self._sessionJson(state, unsavedAtExit)
        io.backgroundWriter().submit(io.atomicWriteJson, os.path.join(self.sessionDir, io.SESSION_FILE), data)

    def _heartbeat(self):
        if self.sessionDir is not None and not self._quitting:
            self._writeSessionJson()

    #
    # Recovery
    #

    def scanAtStartup(self):
        """Present crashed sessions with content (at most twice each), and prune old sessions."""
        try:
            self.enabled()  # reads the settings if needed
            recovery = _recovery()
            sessions = recovery.scan(self.rootDirectory(), self.sessionId)
            self._pendingRecoveries = [s for s in sessions if s.presentable()]
            toPresent = [s for s in self._pendingRecoveries if s.presentedCount < 2]
            self._lastRuntimePrune = time.time()
            recovery.pruneInBackground(self.rootDirectory(), sessions, self.settings, onDone=self._onPruned,
                                       protectedPaths=self._protectedSlots)
            if toPresent:
                for session in toPresent:
                    recovery.incrementPresented(session)
                presenter = self._presenter or recovery.showRecoveryDialog
                presenter(toPresent, startup=True)
        except Exception:
            logging.error("Autosave: scanning for recoverable sessions failed:\n" + traceback.format_exc())

    def showRecoveryDialog(self):
        _recovery().showRecoveryDialog(None, startup=False)

    def pendingRecoveries(self):
        """Sessions found at startup that can be recovered."""
        return list(self._pendingRecoveries)

    def setRecoveryPresenter(self, presenter):
        """Replace the startup presenter ``presenter(sessions, startup=True)`` (A-01b); None restores the dialog."""
        self._presenter = presenter

    def registerRecoveryHandler(self, canHandle, handle):
        """``canHandle(slot) -> bool``, ``handle(slot)``: a recovery action offered first (W3-07, study context)."""
        self._recoveryHandlers.append((canHandle, handle))

    def recoveryHandlers(self):
        return list(self._recoveryHandlers)

    def registerContextProvider(self, provider):
        """``provider() -> dict`` merged into the slot description (A-01b: workspace, study)."""
        if provider not in self._contextProviders:
            self._contextProviders.append(provider)

    def unregisterContextProvider(self, provider):
        if provider in self._contextProviders:
            self._contextProviders.remove(provider)

    def protectSlot(self, path):
        """Keep the slot folder ``path`` (a ``scene-<n>`` folder) while it is recovered, until ``releaseSlot(path)``."""
        self._protectedSlots.add(_canonicalPath(path))

    def releaseSlot(self, path):
        self._protectedSlots.discard(_canonicalPath(path))

    def isSlotProtected(self, slotName):
        return bool(self.sessionDir) and _canonicalPath(os.path.join(self.sessionDir, slotName)) in self._protectedSlots

    def _maybePrune(self):
        """Apply the retention rules again while the autosave folder exceeds MaxDiskMB (at most every 10 min)."""
        if self.root is None:
            return
        cap = int(self.settings.get("MaxDiskMB", 2048)) << 20
        now = time.time()
        if self._status.bytes + self._otherSessionsBytes <= cap or now - self._lastRuntimePrune < RUNTIME_PRUNE_INTERVAL_S:
            return
        self._lastRuntimePrune = now
        recovery = _recovery()
        sessions = recovery.scan(self.root, self.sessionId)
        recovery.pruneInBackground(self.root, sessions, self.settings, onDone=self._onPruned, protectedPaths=self._protectedSlots)

    def _onPruned(self, result):
        """Result of the retention rules: ``{"deleted": [paths], "sizes": {session path: bytes}}`` (main thread)."""
        deleted = {_canonicalPath(path) for path in result.get("deleted") or []}
        own = _canonicalPath(self.sessionDir) if self.sessionDir else None
        self._otherSessionsBytes = sum(size for path, size in (result.get("sizes") or {}).items() if _canonicalPath(path) != own)
        if self.sessionDir and deleted:
            # Closed slots of this session deleted to respect MaxDiskMB
            removed = [name for name in self.slots if _canonicalPath(os.path.join(self.sessionDir, name)) in deleted]
            for name in removed:
                self.slots.pop(name, None)
            if removed:
                self._writeSessionJson()

    def beginRecovery(self):
        self._recovering += 1

    def endRecovery(self):
        self._recovering = max(0, self._recovering - 1)
        # The recovered scene is unsaved: autosave it as a new slot
        self._savedFingerprint = None

    #
    # Status
    #

    def status(self):
        return self._status.copy()

    def sessionDirectory(self):
        return self.sessionDir

    @staticmethod
    def backgroundWriter():
        return _io().backgroundWriter()

    def addStatusListener(self, callback):
        if callback not in self._statusListeners:
            self._statusListeners.append(callback)

    def removeStatusListener(self, callback):
        if callback in self._statusListeners:
            self._statusListeners.remove(callback)

    def _setState(self, state, message=""):
        self._status.state = state
        self._status.message = message
        self._updateStatus()

    def _updateStatus(self):
        status = getattr(self, "_status", None)
        if status is None:
            return
        status.dirty = self.dirty
        if not self.enabled():
            status.state = "disabled"
        elif status.state == "disabled":
            status.state = "idle"
        for callback in list(self._statusListeners):
            try:
                callback(status.copy())
            except Exception:
                logging.error("Autosave: status listener failed:\n" + traceback.format_exc())

    def _showClassicStatusMessage(self):
        mainWindow = slicer.util.mainWindow()
        statusBar = mainWindow.statusBar() if mainWindow is not None else None
        if statusBar is not None and statusBar.isVisible():
            statusBar.showMessage(_("Autosaved {time}").format(time=time.strftime("%H:%M")), 3000)

    def unsavedSummary(self):
        """'' or the autosave line of the close prompt (at most 1 ms: cached status only)."""
        if self._status.lastSaveTime is None or not self._lastUnsavedAutosaved:
            return ""
        return _("Unsaved changes were autosaved at {time}. After quitting they stay recoverable for {days} days (File › Recover Autosaved Work…).").format(  # noqa: RUF001
            time=time.strftime("%H:%M", time.localtime(self._status.lastSaveTime)), days=self.settings.get("MaxAgeDays", 14))


def showAutosaveSettings():
    """Open the application settings at the Autosave panel."""
    dialog = slicer.app.settingsDialog()
    mainWindow = slicer.util.mainWindow()
    if dialog is None or mainWindow is None:
        return
    dialog.setCurrentPanel(_("Autosave"))
    action = mainWindow.findChild(qt.QAction, "EditApplicationSettingsAction")
    if action is not None:
        action.trigger()


def _mouseButtonsPressed():
    buttons = qt.QApplication.mouseButtons()
    try:
        return int(buttons) != 0
    except TypeError:
        return buttons != qt.Qt.NoButton


def _canonicalPath(path):
    return os.path.normcase(os.path.realpath(path))


def _isoTime(seconds=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(seconds if seconds is not None else time.time()))


def _formatBytes(size):
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"

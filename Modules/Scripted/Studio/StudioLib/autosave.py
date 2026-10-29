r"""Scene autosave and crash recovery of Slicer Studio (A-01, SPEC 9.9), and the continuous saving of projects (A-09).

**Projects (A-09, D-A16).** In Studio, as soon as the scene holds user data and no study is open, the scene becomes a
*project* in the Slicer Studio folder the user chose (``projects``): ``Projects/<name>/<name>.mrml`` and its kind
folders. The same engine then writes the project in place (``autosave_io.ProjectWriter``): only changed nodes, after
``IdleSeconds`` idle and at most every ``IntervalMinutes``, and at once on ``saveNow()`` (Ctrl+S), on closing the scene,
on renaming the project and on quitting. In a project autosave *is* saving (Lightroom model): written nodes are no
longer "modified", the status says "Saved", and quitting asks nothing when the last save succeeded. A per-project lock
(``.studio/lock``) detects crashes: the next start offers to open the project again. A project stays a project in
Classic (it keeps saving there), but Classic never creates one.

**Private recovery autosave (A-01).** Without a project (Classic, a study, ``SLICER_STUDIO_PROJECTS=0``), the current
scene is autosaved incrementally to a private per-user folder (``QStandardPaths::AppLocalDataLocation/StudioAutosave``,
D-A1), never to a user folder. Each autosave is a standard Slicer scene (see ``autosave_io``). A per-process lock file
detects crashes: at the next startup a non-modal dialog lists the sessions that can be recovered (``recovery``). There,
autosave is not Save: the scene keeps its "modified" state, ``Ctrl+S`` and the exit prompt keep their meaning, and
every message says "Autosaved", never "Saved".

Autosave never runs under ``--testing``, ``--no-main-window``, ``--exit-after-startup`` or with
``SLICER_STUDIO_AUTOSAVE=0``; the setting ``Studio/Autosave/Enabled=false`` stops autosaving but keeps
recovery available. ``SLICER_STUDIO_AUTOSAVE_DIR`` overrides the folder.

Public API: ``onStartupCompleted()`` (called by the Studio module), ``instance()``, ``suspended(reason)``,
``autosaveNow(reason)``, ``unsavedSummary()``, and the ``AutosaveService`` methods ``status()``,
``addStatusListener()``/``removeStatusListener()``, ``setRecoveryPresenter()``, ``pendingRecoveries()``,
``registerRecoveryHandler()``, ``registerContextProvider()``/``unregisterContextProvider()``,
``sessionDirectory()``, ``unsavedSummary()`` and ``backgroundWriter()``; for projects ``currentProject()``,
``attachProject()``, ``releaseProject()``, ``createProject()``, ``saveNow()``, ``renameProject()``, ``moveRoot()``,
``closeProjectForNewProject()``, ``setProjectPresenter()`` and ``pendingProjectRecoveries()``.

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
import vtk
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
#: The thumbnail is grabbed in a tick of its own (one that did no other work than this): grabbing a view renders it, about
#: 15 ms but up to 45 ms with software rendering
THUMBNAIL_ALONE_MS = 5
#: Ticks the thumbnail waits for a quiet tick; after them it is skipped in this generation (the next one tries again)
THUMBNAIL_MAX_DEFERRALS = 20
#: A generation is aborted when a modal dialog is shown longer than this
MODAL_ABORT_S = 2.0
#: Slots of earlier scenes of a session closed without saving that are kept
MAX_CLOSED_SLOTS = 2
#: Largest estimated main-thread cost of the final generation when a scene is closed without saving
FINAL_GENERATION_BUDGET_MS = 300
#: Estimated main-thread write speed, for that estimate (raw C++ NRRD writes run at 2-3 GB/s)
_ESTIMATED_WRITE_BYTES_PER_MS = 500 * 1024
#: The private recovery autosave (A-01) pauses while less than max(this, 2 x the estimate) is free on the disk
MIN_FREE_BYTES = 1 << 30
#: Saving a project is the user's actual saving (D-A16), so it never has that floor: it needs 2 x the estimate (atomic
#: writes keep the old file until the new one is complete) plus this margin for the scene file and project.json
PROJECT_FREE_MARGIN_BYTES = 1 << 20
#: While the autosave folder is larger than MaxDiskMB, the retention rules run again at most this often
RUNTIME_PRUNE_INTERVAL_S = 600
#: A synchronous save of a project (Save now, closing, quitting) shows a progress dialog after this many seconds ...
PROJECT_PROGRESS_AFTER_S = 1.0
#: ... and gives up after this many (the classic prompt then asks, with the reason)
PROJECT_SAVE_TIMEOUT_S = 600.0

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
    # The Studio surfaces were installed before the service existed (the folder chooser, A-09, or the update check came
    # first): connect them now
    for name in ("StudioLib.autosave_ui", "StudioLib.projects_ui"):
        module = sys.modules.get(name)
        plugin = module.instance() if module is not None and hasattr(module, "instance") else None
        if plugin is not None and hasattr(plugin, "attach"):
            try:
                plugin.attach()
            except Exception:
                logging.error(f"Autosave: {name} could not be connected:\n" + traceback.format_exc())


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


def saveNow(reason="save", progress=True):
    """Save the open project now (Ctrl+S). Return True if everything is saved; False without a project or on failure."""
    return _service.saveNow(reason, progress=progress) if _service is not None else False


def _relocateSavedLesions(oldFolder, newFolder):
    """Draw's Save lesion follows a moved folder (only when Draw was loaded: else it saved nothing in this session)."""
    draw = sys.modules.get("StudioLib.draw")
    if draw is None or not hasattr(draw, "relocateSavedLesions"):
        return
    try:
        draw.relocateSavedLesions(oldFolder, newFolder)
    except Exception:
        logging.error("Slicer Studio: following the moved folder for Save lesion failed:\n" + traceback.format_exc())


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
        #: the open project (A-09): its name and folder, None without a project
        self.projectName = None
        self.projectPath = None

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
        #: the open project (A-09) and its writer; None without a project
        self.project = None
        self.projectWriter = None
        self._projectPresenter = None
        self._pendingProjectRecoveries = []
        self._lastProjectCheck = None
        self._projectPausedReason = ""
        #: tests: True/False replaces the "Studio is shown" condition of creating projects (None: ask the shell)
        self.studioActiveOverride = None
        self._closeSummaryConnected = False
        self._projectAnnouncer = None
        self._pendingSceneSource = None
        self._forceProjectOpen = False
        self._lastProjectPlan = None
        #: (project folder, ``thumbnails.ProjectThumbnailSource.key``) of the last project thumbnail (V-02)
        self._projectThumbnailKey = None
        #: (saved, reason) of the save made when the application was closed (``flushForClose()``), None without a project
        self.closeFlushResult = None
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
            self.connectCloseSummary()
            from StudioLib import projects_ui

            projects_ui.attachService(self)
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
        if self.plan is not None and self._planWriter(self.plan) is not None:
            self._planWriter(self.plan).abort(self.plan, "disposed")
            self.plan = None
        if self.project is not None:
            self.releaseProject()
        if self._closeSummaryConnected:
            mainWindow = slicer.util.mainWindow()
            if mainWindow is not None:
                mainWindow.disconnect("closeSummaryRequested()", self.flushForClose)
            self._closeSummaryConnected = False
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
            {"id": "autosave.settings", "title": _("Saving settings…"), "category": "File", "callback": showAutosaveSettings,
             "synonyms": ["autosave", "recovery", "saving", "save settings", "slicer studio folder"]},
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
        try:
            self._pollProject(scene, fingerprint)
        except Exception:
            logging.error("Slicer Studio: project handling failed:\n" + traceback.format_exc())
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
        nodes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
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
        if self.project is not None:
            if not self.projectNeedsSave(fingerprint):
                return False
            if not synchronous and not self._mainThreadFree():
                return False
            return self._startProjectGeneration(reason or "now", fingerprint, synchronous=synchronous, force=synchronous)
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
        if self.project is not None:
            return self._startProjectGeneration(reason, fingerprint, synchronous=synchronous, ignoreBusy=ignoreBusy, force=synchronous)
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
            self._pause(_("Autosave paused: less than {size} free on the disk.").format(size=_formatBytes(_requiredFreeBytes(estimated))))
            return False
        if mainThreadOnly:
            self._dropSnapshotJobs(plan)
        plan.writerRef = writer
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

    def _planWriter(self, plan):
        """The writer of a generation (the slot writer, or the project writer)."""
        return getattr(plan, "writerRef", None) or self.writer

    def _runSynchronously(self, plan):
        writer = self._planWriter(plan)
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
        writer = self._planWriter(plan)
        try:
            done = writer.runMainThreadStep(plan, slicer.mrmlScene)
        except io.GenerationAborted as error:
            self._abortGeneration(str(error))
            return
        except OSError as error:
            self._abortGeneration(str(error))
            if isinstance(plan, io.ProjectPlan):
                # A project that could not be saved (a full disk): "Not saved" with the reason, as for Save now
                self._projectFailed(plainOSError(error))
            else:
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
            if io.SLICE_BUDGET_MS - elapsed < THUMBNAIL_BUDGET_MS or elapsed >= THUMBNAIL_ALONE_MS:
                plan.thumbnailDeferrals = getattr(plan, "thumbnailDeferrals", 0) + 1
                if plan.thumbnailDeferrals <= THUMBNAIL_MAX_DEFERRALS:
                    return  # in the next tick (the main-thread jobs are done)
                plan.thumbnailChecked = True  # no quiet tick: finalize without a new thumbnail
            else:
                plan.thumbnailChecked = True
                thumbnailStart = time.perf_counter()
                self._updateThumbnail(plan)
                thumbnailMs = (time.perf_counter() - thumbnailStart) * 1000.0
                plan.metrics["mainThreadMs"] += thumbnailMs
                plan.metrics["maxSliceMs"] = max(plan.metrics["maxSliceMs"], elapsed + thumbnailMs)
        self._tickTimer.stop()
        writer.submitFinalize(plan, onDone=self._generationDone, onError=self._generationFailed)

    def _abortGeneration(self, reason):
        plan = self.plan
        self._tickTimer.stop()
        self._removePlanObservers()
        self.plan = None
        self._modalSince = None
        writer = self._planWriter(plan) if plan is not None else None
        if plan is not None and writer is not None:
            writer.abort(plan, reason)
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
        if isinstance(plan, _io().ProjectPlan):
            self._projectFailed(message)
            return
        if isinstance(error, OSError):
            self._pause(_("Autosave paused: {error}").format(error=message))
        else:
            logging.error(f"Autosave generation failed: {message}")
            self._setState("error", _("Autosave failed (see the error log)."))

    def _generationDone(self, plan):
        if plan is not self.plan:
            return
        if getattr(plan, "thumbnailPath", None) is not None and isinstance(plan, _io().ProjectPlan):
            self._projectGenerationDone(plan)
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

    def _enoughSpace(self, estimatedBytes, folder=None, project=False):
        import shutil

        try:
            free = shutil.disk_usage(folder or self.sessionDir or self.rootDirectory()).free
        except OSError:
            return True
        return free >= _requiredFreeBytes(estimatedBytes, project)

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
        if isinstance(plan, _io().ProjectPlan):
            # Projects render their thumbnails from the image arrays in the background (``_queueProjectThumbnail()``)
            return False
        path = getattr(plan, "thumbnailPath", None) or os.path.join(plan.slotDir, _io().THUMBNAIL_FILE)
        return time.time() - self._lastThumbnailTime >= THUMBNAIL_INTERVAL_S or not os.path.exists(path)

    def _updateThumbnail(self, plan):
        """Thumbnail of the first visible slice view: at most every 5 min, never a network map (LNM blinding)."""
        now = time.time()
        io = _io()
        path = getattr(plan, "thumbnailPath", None) or os.path.join(plan.slotDir, io.THUMBNAIL_FILE)
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
        if self.project is not None:
            self._closeProjectOnSceneClose()
            return
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
        nodes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
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
        """Loading a Scene.mrml of the autosave folder (Load Scene): link the files back to their original locations.

        Loading a project of the Slicer Studio folder continues it in place; in Studio, a scene from elsewhere becomes a
        new project that starts as a copy (A-09).
        """
        if self._recovering:
            return
        try:
            recovery = _recovery()
            slotDir = recovery.autosaveSlotOfScene(slicer.mrmlScene, self.knownRoots())
            if slotDir is None:
                self._afterSceneImport()
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
        if self.project is not None:
            self._quitProject()
            writer.drain(5.0)
            self._removeObservers()
            if self._lock is not None:
                self._lock.unlock()
                self._lock = None
            return
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
        if self.project is not None and not self._quitting:
            self.project.writeJson(state="open", executor=_io().backgroundWriter())
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
        try:
            self.scanProjectsAtStartup()
        except Exception:
            logging.error("Slicer Studio: scanning the projects for a crash failed:\n" + traceback.format_exc())

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
    # Projects (A-09, D-A16)
    #

    @staticmethod
    def _projects():
        from StudioLib import projects

        return projects

    def currentProject(self):
        """The project the scene is saved in, or None."""
        return self.project

    def setProjectPresenter(self, presenter):
        """``presenter(projectInfos)``: offers to open projects of a crashed session (projects_ui); None: the dialog."""
        self._projectPresenter = presenter

    def setProjectAnnouncer(self, announcer):
        """``announcer(event, project, **info)``: "created", "copy", "copy-incomplete", "opened", "locked", "conflict", "failed",
        "saved".
        """
        self._projectAnnouncer = announcer

    def pendingProjectRecoveries(self):
        """Projects of a session that closed unexpectedly (found at startup), as ``projects.ProjectInfo``."""
        return list(self._pendingProjectRecoveries)

    def _announce(self, event, project=None, **info):
        announcer = getattr(self, "_projectAnnouncer", None)
        if announcer is None:
            if event in ("failed", "conflict"):
                logging.warning(f"Slicer Studio: {event} {info}")
            return
        try:
            announcer(event, project if project is not None else self.project, **info)
        except Exception:
            logging.error("Slicer Studio: project message failed:\n" + traceback.format_exc())

    def _studioActive(self):
        if self.studioActiveOverride is not None:
            return bool(self.studioActiveOverride)
        if slicer.app.testingEnabled():
            return False  # tests say explicitly when a service acts as in Studio
        module = sys.modules.get("StudioLib.shell")
        shell = module.instance() if module is not None else None
        return shell is not None and shell.isActive()

    @staticmethod
    def _studyOpen():
        module = sys.modules.get("StudioLib.study_ui")
        try:
            return module is not None and module.session() is not None
        except Exception:
            return False

    def projectsActive(self):
        """True if a project may be created now: Studio is shown, projects are on, the folder was chosen, no study is open."""
        projects = self._projects()
        return projects.isEnabled() and self.enabled() and self._studioActive() and not projects.needsChooser() and not self._studyOpen()

    def _pollProject(self, scene, fingerprint):
        """Create a project as soon as the scene holds user data (Studio, no study); stop it when a study opens."""
        if self.project is not None:
            if self._studyOpen():
                # A study saves its own files (journal, lesions, participants.tsv): the project is saved and stops here
                self.saveNow("study", progress=False)
                self.releaseProject()
            return
        if fingerprint == self._lastProjectCheck or self._recovering or not self.projectsActive():
            return
        if self._sceneBusy(scene):
            return
        self._lastProjectCheck = fingerprint
        projects = self._projects()
        if not projects.sceneHasUserData(scene):
            return
        url = scene.GetURL()
        projectPath = projects.projectOfPath(url) if url and os.path.isfile(url) else None
        if projectPath is not None and self._sceneReadFrom(scene, projectPath):
            # A project of the folder that was loaded in Classic (official Load Scene): it continues in place, never as
            # a copy
            project = projects.Project(projectPath)
            if not self.attachProject(project, inPlace=True):
                self._announce("locked", project)
                return
            self._announce("opened", project)
            if self.dirty:
                qt.QTimer.singleShot(0, lambda: self.autosaveNow("open"))
            return
        self.createProject(projects.defaultProjectName(scene))

    @staticmethod
    def _sceneReadFrom(scene, projectPath):
        """True if data of the scene was read from files of the project (its scene was loaded), not only that the scene
        URL still names it (``vtkMRMLScene::Clear`` keeps the URL).
        """
        io = _io()
        projectDir = os.path.normcase(os.path.realpath(projectPath))
        rootDirectory = scene.GetRootDirectory()
        nodes = scene.GetNodesByClass("vtkMRMLStorableNode")
        nodes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
        for index in range(nodes.GetNumberOfItems()):
            node = nodes.GetItemAsObject(index)
            storageNode = node.GetStorageNode()
            if node.GetHideFromEditors() or storageNode is None or not storageNode.GetFileName():
                continue
            for path in io.storageFiles(storageNode, rootDirectory)[:1]:
                if os.path.normcase(os.path.realpath(path)).startswith(projectDir + os.sep) and os.path.exists(path):
                    return True
        return False

    def createProject(self, name=None, announce="created"):
        """Create a project in the Slicer Studio folder and save the scene there from now on. Return it, or None."""
        projects = self._projects()
        try:
            project = projects.createProject(name)
        except OSError as error:
            self._pause(_("Saving paused: the Slicer Studio folder cannot be written ({error}).").format(error=error))
            self._announce("failed", None, reason=str(error))
            return None
        if not self.attachProject(project):
            return None
        if announce:
            self._announce(announce, project)
        return project

    def _projectFilesInPlace(self, scene):
        """True if every file of the scene's data is inside the project (not at its top level) and nothing is modified."""
        io = _io()
        project = self.project
        rootDirectory = scene.GetRootDirectory()
        projectDir = os.path.normcase(os.path.realpath(project.path))
        nodes = scene.GetNodesByClass("vtkMRMLStorableNode")
        nodes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
        for index in range(nodes.GetNumberOfItems()):
            node = nodes.GetItemAsObject(index)
            if not node.GetSaveWithScene() or node.GetHideFromEditors():
                continue
            storageNode = node.GetStorageNode()
            if storageNode is None and not io.defaultStorageNodeClassName(node):
                continue
            if node.GetModifiedSinceRead():
                return False
            files = io.storageFiles(storageNode, rootDirectory) if storageNode is not None and storageNode.GetFileName() else []
            for path in files[:1]:
                real = os.path.normcase(os.path.realpath(path))
                if not real.startswith(projectDir + os.sep) or os.path.dirname(real) == projectDir:
                    return False
        return True

    def attachProject(self, project, inPlace=False):
        """Save the current scene into ``project`` from now on (its lock is taken). ``inPlace``: the scene was just loaded
        from the project's own scene file. Return False if another Slicer Studio has the project open.
        """
        io = _io()
        projects = self._projects()
        if self.project is not None and self.project is not project:
            self.releaseProject()
        if not project.lock():
            return False
        if self.plan is not None:
            self._abortGeneration("a project was opened")
        if self.writer is not None:
            # The private recovery autosave of this scene: the project replaces it
            self.writer.deleteSlot()
            self.slots.pop(self.slotName(), None)
            self._writeSessionJson()
            self.writer = None
            self.slotIndex += 1
        scene = slicer.mrmlScene
        self.project = project
        self.projectWriter = io.ProjectWriter(project, io.backgroundWriter())
        scene.SetURL(project.scenePath)
        scene.SetRootDirectory(project.path)
        self.projectWriter.recordSceneFiles(scene)
        project.writeJson(state="open", executor=io.backgroundWriter())
        io.backgroundWriter().submit(projects.pruneRemoved, project.path)
        leftovers = projects.temporaryLeftovers(project.path)
        if leftovers:
            io.backgroundWriter().submit(projects.removePaths, leftovers)
        fingerprint = io.sceneFingerprint(scene, io.sentinelIds())
        clean = inPlace and os.path.isfile(project.scenePath) and self._projectFilesInPlace(scene)
        self._savedFingerprint = fingerprint if clean else None
        self._lastPolledFingerprint = fingerprint
        self.dirty = not clean
        self.firstDirtyTime = time.time() if self.dirty else None
        self.lastGenerationTime = None
        self._lastThumbnailTime = 0.0
        self._lastUnsavedAutosaved = False
        self._projectPausedReason = ""
        self._status.lastSaveTime = os.path.getmtime(project.scenePath) if clean else None
        project.lastSaveTime = self._status.lastSaveTime
        projects.setCurrentProject(project)
        self._setState("idle")
        return True

    def releaseProject(self, state="closed"):
        """Stop saving into the open project (its lock is released; the scene keeps its data)."""
        project = self.project
        if project is None:
            return
        io = _io()
        if self.plan is not None and isinstance(self.plan, io.ProjectPlan):
            self._abortGeneration("the project is closed")
        try:
            project.writeJson(state=state, executor=io.backgroundWriter())
        except Exception:
            logging.error("Slicer Studio: project.json could not be written:\n" + traceback.format_exc())
        project.unlock()
        self.project = None
        self.projectWriter = None
        self._savedFingerprint = None
        self._lastPolledFingerprint = None
        self._lastProjectCheck = None
        self.dirty = False
        self.firstDirtyTime = None
        self.lastGenerationTime = None
        self._status.lastSaveTime = None
        self._projects().setCurrentProject(None)
        self._setState("idle")

    def _modifiedProjectNodes(self, scene):
        """Data nodes saved in a project that are modified since they were read or written."""
        io = _io()
        modified = []
        nodes = scene.GetNodesByClass("vtkMRMLStorableNode")
        nodes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
        for index in range(nodes.GetNumberOfItems()):
            node = nodes.GetItemAsObject(index)
            if not node.GetSaveWithScene() or node.GetHideFromEditors() or not node.GetModifiedSinceRead():
                continue
            if node.GetStorageNode() is None and not io.defaultStorageNodeClassName(node):
                continue
            modified.append(node)
        return modified

    def projectNeedsSave(self, fingerprint=None, includeScene=False):
        """True if the open project is not up to date on disk (``includeScene``: also views, layout and other scene state)."""
        if self.project is None or self.projectWriter is None:
            return False
        if not self.projectWriter.hasContent():
            return True
        scene = slicer.mrmlScene
        io = _io()
        if fingerprint is None:
            fingerprint = io.sceneFingerprint(scene, io.sentinelIds())
        if fingerprint != self._savedFingerprint or self._modifiedProjectNodes(scene):
            return True
        return bool(includeScene and scene.GetModifiedSinceRead())

    def _startProjectGeneration(self, reason, fingerprint, synchronous=False, ignoreBusy=False, force=False, progress=False,
                                timeoutS=PROJECT_SAVE_TIMEOUT_S):
        io = _io()
        now = time.time()
        writer = self.projectWriter
        try:
            if synchronous:
                writer.executor.drain(10.0)
            idle = now - max(self.lastChangeTime, self.lastInputTime)
            plan = writer.plan(slicer.mrmlScene, fingerprint, idleSeconds=idle, ignoreBusy=ignoreBusy, force=force)
        except io.GenerationAborted:
            return False
        except OSError as error:
            self._projectFailed(plainOSError(error))
            return False
        except Exception:
            logging.error("Slicer Studio: planning the save failed:\n" + traceback.format_exc())
            self._projectFailed(_("see the error log"))
            return False
        plan.reason = reason
        estimated = sum(entry.get("bytes") or 0 for entry in plan.entries.values() if entry.get("kind") in ("write", "import"))
        if not self._enoughSpace(estimated, self.project.path, project=True):
            writer.abort(plan, "not enough disk space")
            self._projectFailed(_("less than {size} free on the disk").format(size=_formatBytes(_requiredFreeBytes(estimated, project=True))))
            return False
        plan.writerRef = writer
        self.plan = plan
        self.lastGenerationTime = now
        self._observePlan()
        self._setState("saving")
        if synchronous:
            return self._runProjectSynchronously(plan, progress, timeoutS)
        self._tickTimer.start()
        return True

    def _runProjectSynchronously(self, plan, progress=False, timeoutS=PROJECT_SAVE_TIMEOUT_S):
        """Run a whole project generation now (Save now, closing, quitting): a progress dialog after a second."""
        writer = self._planWriter(plan)
        start = time.monotonic()
        dialog = None
        try:
            while not writer.runMainThreadStep(plan, slicer.mrmlScene, budgetMs=1e9, ignoreBusy=True):
                writer.executor.drain(0.1)
                elapsed = time.monotonic() - start
                if elapsed > timeoutS:
                    raise OSError(_("saving took longer than {seconds} s").format(seconds=int(timeoutS)))
                if progress and dialog is None and elapsed > PROJECT_PROGRESS_AFTER_S:
                    dialog = self._progressDialog()
                if dialog is not None:
                    slicer.app.processEvents(qt.QEventLoop.ExcludeUserInputEvents)
            result = writer.finalize(plan)
            writer._finished(plan, result)
        except Exception as error:
            writer.abort(plan, str(error))
            self.plan = None
            self._removePlanObservers()
            if isinstance(error, OSError):
                # An anticipated I/O failure (a folder that cannot be written, a full disk, a timeout): plain words, no traceback
                reason = plainOSError(error)
            else:
                logging.error("Slicer Studio: saving the project failed:\n" + traceback.format_exc())
                reason = str(error)
            self._projectFailed(reason)
            return False
        finally:
            if dialog is not None:
                dialog.close()
                dialog.deleteLater()
        self._lastProjectPlan = plan
        self._generationDone(plan)
        return True

    def _progressDialog(self):
        name = self.project.relativeName() if self.project is not None else ""
        dialog = qt.QProgressDialog(_("Saving {name}…").format(name=name), "", 0, 0, slicer.util.mainWindow())
        dialog.setObjectName("StudioProjectSaveProgress")
        dialog.setWindowTitle(_("Saving"))
        dialog.setCancelButton(None)
        dialog.setMinimumDuration(0)
        dialog.setWindowModality(qt.Qt.ApplicationModal)
        dialog.show()
        slicer.app.processEvents(qt.QEventLoop.ExcludeUserInputEvents)
        return dialog

    def _projectFailed(self, reason):
        self._projectPausedReason = reason
        if self.project is not None:
            self.project.lastError = reason
        self._pausedUntil = time.time() + 30.0
        self._setState("error", _("Not saved: {reason}").format(reason=reason))
        logging.warning(f"Slicer Studio: the project could not be saved: {reason}")
        self._announce("failed", reason=reason)

    def _projectGenerationDone(self, plan):
        self._removePlanObservers()
        self.plan = None
        io = _io()
        result = plan.result or {}
        plan.metrics["workerMs"] += result.get("workerMs", 0.0)
        metrics = dict(plan.metrics)
        metrics["mainThreadTotalMs"] = metrics["planMs"] + metrics["mainThreadMs"]
        metrics["reason"] = getattr(plan, "reason", "")
        metrics["incomplete"] = plan.incomplete
        self.metrics["last"] = metrics
        self.metrics["history"] = (self.metrics["history"] + [metrics])[-20:]
        logging.debug(f"Slicer Studio project generation {plan.generation}: {metrics}")
        if not plan.incomplete:
            self._savedFingerprint = plan.fingerprint
        self.dirty = io.sceneFingerprint(slicer.mrmlScene, io.sentinelIds()) != self._savedFingerprint or plan.incomplete
        self.firstDirtyTime = time.time() if self.dirty else None
        self._lastProjectPlan = plan
        now = time.time()
        self._status.lastSaveTime = now
        if self.project is not None:
            self.project.lastSaveTime = now
            self.project.lastError = ""
        self._projectPausedReason = ""
        self._pausedUntil = 0.0
        self._setState("idle")
        conflicts = self.projectWriter.takeConflicts() if self.projectWriter is not None else []
        if conflicts:
            self._announce("conflict", conflicts=conflicts)
        self._queueProjectThumbnail(plan)
        self._projects().notifyChanged()
        if plan.wroteData:
            self._showClassicStatusMessage()

    def _queueProjectThumbnail(self, plan):
        """Render the thumbnails of the project in the background writer after a save that changed what they show (V-02):
        the axial, coronal and sagittal slices through the lesion, from the image arrays (not a view grab, so no
        main-thread render and it works with Library shown). Skipped when nothing visible changed, and when quitting.
        """
        project = self.project
        if project is None or getattr(plan, "reason", "") == "quit":
            return
        try:
            from StudioLib import thumbnails

            privateFolder = os.path.join(project.path, ".studio")
            source = thumbnails.projectThumbnailSource(slicer.mrmlScene)
            if source is None:
                return
            key = (project.path, source.key)
            if key == self._projectThumbnailKey and os.path.isfile(os.path.join(privateFolder, thumbnails.PROJECT_VIEW_FILES[0][1])):
                source.release()
                return
            self._projectThumbnailKey = key
        except Exception:
            logging.debug("Slicer Studio: project thumbnail source failed:\n" + traceback.format_exc())
            return

        def done(paths, source=source):
            source.release()
            if paths:
                self._projects().notifyChanged()

        def failed(error, details=None, source=source, key=key):
            source.release()
            if self._projectThumbnailKey == key:
                self._projectThumbnailKey = None
            logging.debug(f"Slicer Studio: project thumbnail failed: {error}")

        _io().backgroundWriter().submit(thumbnails.renderProjectViews, source, privateFolder, onDone=done, onError=failed)

    def lastSaveComplete(self):
        """True if the last project generation saved everything (no heavy node deferred, nothing changed meanwhile)."""
        plan = getattr(self, "_lastProjectPlan", None)
        return plan is not None and not plan.incomplete

    def saveNow(self, reason="save", progress=True, ignoreBusy=False, timeoutS=PROJECT_SAVE_TIMEOUT_S):
        """Save the open project now, every node (also heavy ones). Return True if the project is up to date on disk."""
        if self.project is None or self.projectWriter is None:
            return False
        io = _io()
        if self.plan is not None:
            self._abortGeneration("save now")
        io.backgroundWriter().drain(10.0)
        scene = slicer.mrmlScene
        if not ignoreBusy and self._sceneBusy(scene):
            return False
        fingerprint = io.sceneFingerprint(scene, io.sentinelIds())
        if not self.projectNeedsSave(fingerprint, includeScene=True):
            return True
        if not self._startProjectGeneration(reason, fingerprint, synchronous=True, ignoreBusy=True, force=True, progress=progress, timeoutS=timeoutS):
            return False
        return self.lastSaveComplete() and not self._modifiedProjectNodes(scene)

    def connectCloseSummary(self):
        """Save the project before the main window decides whether to ask about unsaved changes (Classic and Studio)."""
        mainWindow = slicer.util.mainWindow()
        if self._closeSummaryConnected or mainWindow is None or not hasattr(mainWindow, "closeSummaryRequested"):
            return False
        mainWindow.connect("closeSummaryRequested()", self.flushForClose)
        self._closeSummaryConnected = True
        return True

    def flushForClose(self):
        """The application is closing: save the project now (nothing is asked when this succeeds). Return the result."""
        if self.project is None:
            self.closeFlushResult = None
            return None
        try:
            ok = self.saveNow("quit", progress=True)
        except Exception:
            logging.error("Slicer Studio: saving before quitting failed:\n" + traceback.format_exc())
            ok = False
        self.closeFlushResult = (ok, self._projectPausedReason)
        return ok

    def _closeProjectOnSceneClose(self):
        """The scene is closed (New project, Close scene, loading another scene): save the project, then release it."""
        if self.plan is not None:
            self._abortGeneration("the scene is closed")
        # Loading a scene sets the scene URL to the new file before it clears the scene (vtkSlicerSceneReader), and
        # vtkMRMLScene::Connect then imports from that URL. Saving the project points the URL at the project scene file:
        # the URL is restored afterwards, or the open project would be read again instead of the scene being loaded.
        scene = slicer.mrmlScene
        url = scene.GetURL()
        rootDirectory = scene.GetRootDirectory()
        try:
            if self._recovering == 0 and self.enabled():
                self.saveNow("close", progress=True, ignoreBusy=True)
        except Exception:
            logging.error("Slicer Studio: saving the project before closing failed:\n" + traceback.format_exc())
        finally:
            if scene.GetURL() != url:
                scene.SetURL(url)
            if scene.GetRootDirectory() != rootDirectory:
                scene.SetRootDirectory(rootDirectory)
            self.releaseProject()
            self._nextSlot()

    def _quitProject(self):
        try:
            if self.enabled():
                self.saveNow("exit", progress=True, ignoreBusy=True)
        except Exception:
            logging.error("Slicer Studio: saving the project when quitting failed:\n" + traceback.format_exc())
        self.releaseProject()

    def setPendingSceneSource(self, path):
        """The file a scene is about to be loaded from (a .mrb is unpacked first): names the project of a copy."""
        self._pendingSceneSource = str(path) if path else None

    def openProject(self, path):
        """Open a project folder (its scene file): the current project is saved and closed first. Return True if loaded."""
        projects = self._projects()
        scenePath = projects.sceneFile(path)
        if scenePath is None:
            self._announce("failed", None, reason=_("{name} has no scene file").format(name=os.path.basename(path)))
            return False
        if projects.isLockedByOther(path):
            self._announce("locked", projects.Project(path))
            return False
        try:
            if _io().repairBareAmpersands(scenePath):
                logging.warning(f"Slicer Studio: escaped the bare '&' of the file names in {os.path.basename(scenePath)}")
        except Exception:
            logging.error("Slicer Studio: repairing the project scene failed:\n" + traceback.format_exc())
        self._forceProjectOpen = True
        try:
            # The scene is replaced (Load Scene): the open project is saved and closed first (StartCloseEvent)
            slicer.util.loadScene(scenePath, {"clear": True})
        except RuntimeError as error:
            if self.project is None:
                raise
            # Some data files of the project could not be read (missing or damaged): the project opened with the rest, and
            # _afterSceneImport left their nodes out and named them
            logging.warning(f"Slicer Studio: {error}")
        finally:
            self._forceProjectOpen = False
        return self.project is not None

    def opensSceneAsProject(self):
        """True if a scene file opened now (Open…, drop, Open folder…) replaces the open project instead of being added to it.

        In Studio with projects, every scene opens as its own project (A-09): a project of the Slicer Studio folder continues
        in place, a scene from elsewhere opens as a new project that starts as a copy. Classic, a study and a scene without a
        project keep 3D Slicer's behavior (the scene's data is added to the open scene).
        """
        return self.project is not None and self.projectsActive()

    def openSceneFile(self, path):
        """Open a scene file (.mrml/.mrb) as its own project while a project is open (``opensSceneAsProject()``).

        The open project is saved and closed first (Load Scene clears the scene). Opening the scene file of the open project
        again keeps it as it is. Return True if the scene is open.
        """
        projects = self._projects()
        path = os.path.abspath(str(path))
        projectPath = projects.projectOfPath(path)
        scenePath = projects.sceneFile(projectPath) if projectPath is not None else None
        if scenePath is not None and os.path.normcase(os.path.realpath(scenePath)) == os.path.normcase(os.path.realpath(path)):
            current = self.project
            if current is not None and os.path.normcase(os.path.realpath(current.path)) == os.path.normcase(os.path.realpath(projectPath)):
                return True  # the open project: nothing to load (adding it again would duplicate every node)
            return self.openProject(projectPath)
        self.setPendingSceneSource(path)
        slicer.util.loadScene(path, {"clear": True})
        return True

    def _afterSceneImport(self):
        """A scene was loaded: a project of the folder continues in place; in Studio a scene from elsewhere is copied."""
        projects = self._projects()
        source = getattr(self, "_pendingSceneSource", None)
        self._pendingSceneSource = None
        if not projects.isEnabled() or self._importIntoData:
            return  # data added to the open scene: the open project (if any) saves it with the rest
        scene = slicer.mrmlScene
        url = scene.GetURL()
        if not url or self.project is not None:
            return
        projectPath = projects.projectOfPath(url)
        if projectPath is not None:
            if not (self._studioActive() or getattr(self, "_forceProjectOpen", False)) or not self.enabled():
                return  # Classic loads a project like any scene (official behavior)
            project = projects.Project(projectPath)
            # A data file of the project is missing or damaged (deleted in Finder, a partial copy): its node holds no data,
            # could never be saved and would make quitting ask about it. It is left out before the project's files are
            # recorded, so its file stays where it is (the scene file keeps naming it until the project is saved again).
            unread = self._removeUnreadNodes(scene)
            if not self.attachProject(project, inPlace=True):
                self._announce("locked", project)
                return
            if unread:
                self._announce("opened", project, unread=unread)
            else:
                self._announce("opened", project)
            if self.dirty:
                qt.QTimer.singleShot(0, lambda: self.autosaveNow("open"))
            return
        if not self.projectsActive():
            return
        unread = self._removeUnreadNodes(scene)
        if not projects.sceneHasUserData(scene):
            return  # nothing could be loaded (for example a truncated scene file): no empty project (A-09)
        name = projects.displayName(os.path.basename(source or url))
        project = self.createProject(name, announce=None if unread else "copy")
        if project is not None:
            if unread:
                self._announce("copy-incomplete", project, unread=unread)
            # Copy the files now (in the background): the original is never changed
            qt.QTimer.singleShot(0, lambda: self.autosaveNow("copy"))

    @staticmethod
    def _removeUnreadNodes(scene):
        """Remove the nodes whose file could not be read by the scene load that just ended (a missing or broken file) and
        return their names. They hold no data, so a project cannot save them and closing the scene would ask about them.
        """
        unread = []
        nodes = scene.GetNodesByClass("vtkMRMLStorableNode")
        nodes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
        for index in range(nodes.GetNumberOfItems()):
            node = nodes.GetItemAsObject(index)
            for storageIndex in range(node.GetNumberOfStorageNodes()):
                storageNode = node.GetNthStorageNode(storageIndex)
                if storageNode is None or storageNode.GetStoredTime().GetMTime() != 0:
                    continue  # read (a successful read sets the stored time)
                messages = storageNode.GetUserMessages()
                if messages is not None and messages.GetNumberOfMessagesOfType(vtk.vtkCommand.ErrorEvent) > 0:
                    unread.append(node)
                    break
        names = []
        for node in unread:
            names.append(node.GetName() or node.GetID())
            related = [node.GetNthStorageNode(index) for index in range(node.GetNumberOfStorageNodes())]
            if node.IsA("vtkMRMLDisplayableNode"):
                related += [node.GetNthDisplayNode(index) for index in range(node.GetNumberOfDisplayNodes())]
            scene.RemoveNode(node)
            for other in related:
                if other is not None and other.GetScene() is not None:
                    scene.RemoveNode(other)
        if names:
            logging.warning("Slicer Studio: left out (their files could not be read): " + ", ".join(names))
        return names

    def renameProject(self, newName):
        """Rename the open project: its folder, its scene file, project.json and every file path of the scene."""
        projects = self._projects()
        project = self.project
        if project is None:
            raise projects.ProjectError(_("No project is open."))
        newName = projects.sanitizeName(newName)
        if newName == project.name:
            return project.path
        parent = os.path.dirname(project.path)
        newPath = os.path.join(parent, newName)
        if os.path.exists(newPath) and os.path.normcase(os.path.realpath(newPath)) != os.path.normcase(os.path.realpath(project.path)):
            raise projects.ProjectError(_("A project named {name} already exists.").format(name=newName))
        self.saveNow("rename", progress=True)
        _io().backgroundWriter().drain(10.0)
        oldPath = project.path
        oldScene = projects.sceneFile(oldPath)
        project.unlock()
        try:
            os.rename(oldPath, newPath)
        except OSError as error:
            project.lock()
            raise projects.ProjectError(_("The project could not be renamed: {error}").format(error=error))
        projects.renameSceneFile(newPath, oldScene)
        self._relocateProject(oldPath, newPath)
        return newPath

    def _relocateProject(self, oldPath, newPath):
        """The folder of the open project moved: follow it (lock, scene file, file paths of the scene, writer)."""
        project = self.project
        project.path = os.path.normpath(newPath)
        project.name = os.path.basename(project.path)
        project.lock()
        scene = slicer.mrmlScene
        oldNorm = os.path.normpath(oldPath)
        rootDirectory = scene.GetRootDirectory()

        def moved(path):
            if not path:
                return path
            absolute = os.path.normpath(path if os.path.isabs(path) else os.path.join(rootDirectory or oldNorm, path))
            if absolute == oldNorm or absolute.startswith(oldNorm + os.sep):
                return project.path + absolute[len(oldNorm):]
            return path

        storageNodes = scene.GetNodesByClass("vtkMRMLStorageNode")
        storageNodes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
        for index in range(storageNodes.GetNumberOfItems()):
            storageNode = storageNodes.GetItemAsObject(index)
            names = [storageNode.GetNthFileName(i) for i in range(storageNode.GetNumberOfFileNames())]
            fileName = storageNode.GetFileName()
            if fileName:
                storageNode.SetFileName(moved(fileName))
            if names:
                storageNode.ResetFileNameList()
                for name in names:
                    storageNode.AddFileName(moved(name))
        scene.SetURL(project.scenePath)
        scene.SetRootDirectory(project.path)
        _relocateSavedLesions(oldNorm, project.path)
        if self.projectWriter is not None:
            self.projectWriter.relocate(oldPath, project.path)
        project.writeJson(state="open", executor=_io().backgroundWriter())
        self._savedFingerprint = None  # the scene file is written again (same content, new name)
        self.dirty = True
        self.firstDirtyTime = time.time()
        self._projects().setCurrentProject(project)
        self._updateStatus()

    def moveRoot(self, newRoot, moveContent=True, run=None):
        """Change the Slicer Studio folder; with ``moveContent``, move Projects/, Studies/, LNM/ and Templates/ there first.

        The open project is saved first and followed. ``run(function)`` runs the move (for example in a thread with a
        progress dialog); by default it runs here. Return the list of moved items. Raise OSError when moving fails (what was
        moved stays moved; the setting is then not changed), and ``projects.OpenElsewhereError`` before anything is moved
        when another running Slicer Studio has a project of the folder open.
        """
        projects = self._projects()
        oldRoot = projects.root()
        newRoot = os.path.normpath(os.path.abspath(newRoot))
        moved = []
        if moveContent and os.path.normcase(os.path.realpath(oldRoot)) != os.path.normcase(os.path.realpath(newRoot)):
            projects.checkMoveTarget(oldRoot, newRoot)  # before the open project is saved and unlocked
            # A project that another Slicer Studio has open would move away from under it (it would go on saving there)
            projects.checkNotOpenElsewhere(oldRoot)
            if self._studyOpen():
                # The open study writes its journal and participants into Studies/ while it is open
                raise OSError(_("Close the open study first."))
            project = self.project
            if project is not None:
                self.saveNow("move", progress=True)
                _io().backgroundWriter().drain(10.0)
                project.unlock()
            # Nothing is written into the old folder while it moves (poll, heartbeat of project.json)
            timers = [timer for timer in (self._pollTimer, self._heartbeatTimer) if timer.isActive()]
            for timer in timers:
                timer.stop()
            self._suspendCount += 1
            succeeded = False
            try:
                projects.checkNotOpenElsewhere(oldRoot)  # again: one may have been opened elsewhere while this one saved
                function = lambda: projects.moveContent(oldRoot, newRoot)  # noqa: E731
                moved = run(function) if run is not None else function()
                succeeded = True
                for name in moved:  # lesions saved in this session in a moved folder (outside the open project too)
                    _relocateSavedLesions(os.path.join(oldRoot, name), os.path.join(newRoot, name))
            finally:
                self._suspendCount -= 1
                for timer in timers:
                    timer.start()
                if project is not None:
                    newPath = projects.pathAfterMove(project.path, oldRoot, newRoot)
                    # Follow the project only when it really moved: after a failed move its folder may still be in the
                    # old folder (where Library lists it), whatever exists in the new one
                    hasMoved = succeeded or not os.path.isdir(project.path)
                    if hasMoved and os.path.isdir(newPath) and os.path.normpath(newPath) != os.path.normpath(project.path):
                        self._relocateProject(project.path, newPath)
                    else:
                        project.lock()
            _updateRecentStudies(oldRoot, newRoot)
        projects.setRoot(newRoot)
        return moved

    def newProject(self):
        """New project (Ctrl+N): save and close the open project; the next data creates the next project."""
        if self.project is not None:
            self.saveNow("new project", progress=True)
        mainWindow = slicer.util.mainWindow()
        action = mainWindow.findChild(qt.QAction, "FileCloseSceneAction") if mainWindow is not None else None
        if action is not None:
            action.trigger()
        else:
            slicer.mrmlScene.Clear(0)

    def scanProjectsAtStartup(self):
        """Offer the projects that were open when Slicer Studio closed unexpectedly (each one once)."""
        projects = self._projects()
        if not projects.isEnabled():
            return []
        io = _io()
        crashed = []
        for info in projects.listProjects():
            if info.state != "open" or info.isCurrent() or projects.isLockedByOther(info.path):
                continue
            crashed.append(info)
            data = dict(info.data, state="crashed")
            io.backgroundWriter().submit(io.atomicWriteJson, projects.projectFile(info.path), data)
        self._pendingProjectRecoveries = crashed
        if crashed:
            presenter = self._projectPresenter
            if presenter is None:
                from StudioLib import projects_ui

                presenter = projects_ui.showCrashOfferDialog
            presenter(crashed)
        return crashed

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
        project = getattr(self, "project", None)
        status.projectName = project.name if project is not None else None
        status.projectPath = project.path if project is not None else None
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
            if self.project is not None:
                statusBar.showMessage(_("Saved to {project} {time}").format(project=self.project.relativeName(), time=time.strftime("%H:%M")), 3000)
            else:
                statusBar.showMessage(_("Autosaved {time}").format(time=time.strftime("%H:%M")), 3000)

    def unsavedSummary(self):
        """'' or the autosave line of the close prompt (at most 1 ms: cached status only)."""
        if self.project is not None:
            return ""
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
    from StudioLib import autosave_settings

    dialog.setCurrentPanel(_(autosave_settings.PANEL_NAME))
    action = mainWindow.findChild(qt.QAction, "EditApplicationSettingsAction")
    if action is not None:
        action.trigger()


def _updateRecentStudies(oldRoot, newRoot):
    """Recent studies that moved with the Slicer Studio folder point at their new place (fork-only key)."""
    try:
        from StudioLib import projects, study_ui
    except ImportError:
        return
    import json

    try:
        records = json.loads(str(slicer.util.settingsValue(study_ui.RECENT_STUDIES_KEY, "") or "[]"))
    except ValueError:
        return
    if not isinstance(records, list):
        return
    changed = False
    for record in records:
        path = record.get("path") if isinstance(record, dict) else None
        newPath = projects.pathAfterMove(path, oldRoot, newRoot) if path else path
        if newPath != path:
            record["path"] = newPath
            changed = True
    if changed:
        qt.QSettings().setValue(study_ui.RECENT_STUDIES_KEY, json.dumps(records))


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


def _requiredFreeBytes(estimatedBytes, project=False):
    """Free space a generation needs: a project save needs room for its data only, the private autosave keeps 1 GB free."""
    if project:
        return 2 * estimatedBytes + PROJECT_FREE_MARGIN_BYTES
    return max(MIN_FREE_BYTES, 2 * estimatedBytes)


def plainOSError(error):
    """An I/O error in plain words: "Permission denied", without the error number and the (temporary) file path."""
    return (getattr(error, "strerror", None) or str(error)).strip()


def _formatBytes(size):
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"

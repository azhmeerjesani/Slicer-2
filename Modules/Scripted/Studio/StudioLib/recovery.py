r"""Recovery of autosaved scenes (A-01, SPEC 9.9): scan, recover, discard, retention and the recovery dialog.

The autosave folder holds one folder per application session (``<date>-<time>-<pid>-<id>``) with
``session.json``, a ``session.lock`` held by the running process, and ``scene-<n>`` slots (see ``autosave_io``).

- ``scan()`` classifies the sessions: running in another Slicer, crashed (the lock could be taken while the
  session says "running"), closed without saving, closed (clean), recovered or discarded.
- ``recover()`` loads a slot and puts every storage node back on its original file, or on no file, never on
  the autosave folder (``rehomeAfterLoad()``); the scene URL is restored too.
- ``discard()`` moves a session to the trash of the operating system.
- ``prune()`` applies the retention settings (``Studio/Autosave/KeepSessions``, ``MaxDiskMB``, ``MaxAgeDays``).

``RecoveryDialog`` (``StudioRecoveryDialog``) is non-modal. Studio may replace it at startup with its own
presenter (``autosave.instance().setRecoveryPresenter()``, A-01b), which reuses these functions.
"""

import calendar
import logging
import os
import time
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import autosave_io as io
from . import ui

#: A locked session whose heartbeat is older than this is "possibly crashed" (its PID was reused)
STALE_HEARTBEAT_S = 2 * 3600
#: Folders without session.json are deleted when older than this
ORPHAN_AGE_S = 24 * 3600
#: Crashed sessions that were never presented are kept at least this long
UNPRESENTED_KEEP_S = 30 * 24 * 3600
#: Missing referenced files are checked on the main thread for at most this many files
MAX_MISSING_CHECKS = 50
THUMBNAIL_ICON_SIZE = 96

_STATUS_TEXT = {
    "crashed": "closed unexpectedly",
    "closed-for-update": "closed for the update",
    "possibly-crashed": "possibly closed unexpectedly",
    "closed-unsaved": "closed without saving",
    "running": "open in another Slicer",
    "closed": "closed normally",
    "recovered": "recovered",
    "discarded": "discarded",
    "unknown": "incomplete",
    "own-closed": "earlier scene of this session",
    "own-current": "current scene",
}


def _statusText(status):
    return _(_STATUS_TEXT.get(status, status))


def _parseTime(text):
    """Seconds since the epoch of a session time: UTC ("...Z", as written now) or local time (no "Z", earlier versions); 0.0 if unreadable."""
    try:
        if text.endswith("Z"):
            return float(calendar.timegm(time.strptime(text[:-1], "%Y-%m-%dT%H:%M:%S")))
        return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%S"))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0.0


class SlotInfo:
    """One autosaved scene (``scene-<n>``) of a session."""

    def __init__(self, session, data):
        self.session = session
        self.name = data.get("name", "")
        self.path = os.path.join(session.path, self.name)
        self.state = data.get("state", "current")
        self.generation = data.get("generation", 0)
        self.updated = data.get("updated") or session.updated
        self.bytes = data.get("bytes", 0)
        self.summary = data.get("summary") or {}
        self.unsaved = bool(data.get("unsaved", True))
        self.workspace = data.get("workspace")
        self.study = data.get("study")
        thumbnail = os.path.join(self.path, data.get("thumbnail") or io.THUMBNAIL_FILE)
        self.thumbnail = thumbnail if os.path.exists(thumbnail) else None
        self.missing = []
        self.data = data

    def scenePath(self):
        return os.path.join(self.path, io.SCENE_FILE)

    def hasContent(self):
        return os.path.exists(self.scenePath())

    def status(self):
        if self.session.own:
            return "own-current" if self.state == "current" else "own-closed"
        if self.state == "closed-unsaved":
            return "closed-unsaved"
        if self.session.status == "crashed" and self.session.closedForUpdate:
            return "closed-for-update"
        return self.session.status

    def contentsText(self):
        return describeContents(self.summary)


class SessionInfo:
    """One application session of the autosave folder."""

    def __init__(self, path, data, status, own=False):
        self.path = path
        self.sessionId = os.path.basename(path)
        self.data = data or {}
        self.status = status
        self.own = own
        self.started = self.data.get("started")
        self.updated = self.data.get("updated") or self.data.get("heartbeat")
        self.heartbeat = self.data.get("heartbeat") or self.updated
        self.presentedCount = int(self.data.get("presentedCount") or 0)
        #: Closed by Slicer Studio itself to install an update (offered like a crashed session, but not described as one)
        self.closedForUpdate = bool(self.data.get("closedForUpdate"))
        self.lockInfo = ""
        self.slots = [SlotInfo(self, slot) for slot in self.data.get("slots") or [] if slot.get("name")]
        self.slots = [slot for slot in self.slots if slot.hasContent()]

    def updatedSeconds(self):
        return _parseTime(self.updated) or _mtime(self.path)

    def recoverableSlots(self):
        return [slot for slot in self.slots if slot.unsaved]

    def presentable(self):
        """Crashed (or possibly crashed) with unsaved content: offered at startup."""
        return self.status in ("crashed", "possibly-crashed") and bool(self.recoverableSlots())

    def bytes(self):
        return sum(slot.bytes for slot in self.slots)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _readLockInfo(lockPath):
    """The text of a lock file (pid, application name, host); QLockFile.getLockInfo() is not usable from Python."""
    try:
        with open(lockPath, encoding="utf-8", errors="replace") as f:
            return " ".join(line.strip() for line in f.read().splitlines()[:3] if line.strip())
    except OSError:
        return ""


def lockState(sessionDir):
    """'locked' (a live process holds the lock) or 'free' (no lock, or the holder is gone). Main thread."""
    lockPath = os.path.join(sessionDir, io.LOCK_FILE)
    if not os.path.exists(lockPath):
        return "free"
    lock = qt.QLockFile(lockPath)
    lock.setStaleLockTime(0)
    if lock.tryLock(0):
        lock.unlock()
        return "free"
    return "locked"


def scan(root, ownSessionId=None):
    """Return the sessions of the autosave folder ``root`` (main thread; small JSON reads), newest first."""
    sessions = []
    if not root or not os.path.isdir(root):
        return sessions
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        data = io.readJson(os.path.join(path, io.SESSION_FILE))
        own = name == ownSessionId
        if own:
            status = "running"
        elif data is None or data.get("format") != io.FORMAT:
            status = "unknown"
        else:
            locked = lockState(path) == "locked"
            state = data.get("state")
            if locked:
                heartbeat = _parseTime(data.get("heartbeat") or data.get("updated"))
                status = "possibly-crashed" if heartbeat and time.time() - heartbeat > STALE_HEARTBEAT_S else "running"
            elif state == "running":
                status = "crashed"
            elif state == "closed":
                status = "closed-unsaved" if data.get("unsavedAtExit") else "closed"
            elif state in ("recovered", "discarded"):
                status = state
            else:
                status = "unknown"
        session = SessionInfo(path, data, status, own=own)
        if status in ("running", "possibly-crashed"):
            session.lockInfo = _readLockInfo(os.path.join(path, io.LOCK_FILE))
        sessions.append(session)
    sessions.sort(key=lambda s: s.updatedSeconds(), reverse=True)
    checked = 0
    for session in sessions:
        if session.own or not session.presentable():
            continue
        for slot in session.recoverableSlots():
            checked = _checkMissingFiles(slot, MAX_MISSING_CHECKS - checked)
    return sessions


def _checkMissingFiles(slot, budget):
    """Referenced original files that no longer exist (at most ``budget`` checks). Return the number checked."""
    manifest = io.readJson(os.path.join(slot.path, io.MANIFEST_FILE)) or {}
    checked = 0
    for entry in (manifest.get("entries") or {}).values():
        if entry.get("kind") != "reference":
            continue
        for path in entry.get("files") or []:
            if checked >= budget:
                return checked
            checked += 1
            if not os.path.exists(path):
                slot.missing.append(path)
    return checked


def incrementPresented(session):
    """Count a startup presentation of a session (sessions are presented at most twice)."""
    data = dict(session.data)
    data["presentedCount"] = session.presentedCount + 1
    session.presentedCount += 1
    session.data = data
    io.backgroundWriter().submit(io.atomicWriteJson, os.path.join(session.path, io.SESSION_FILE), data)


def _sessionRecord(session):
    """Plain data of a session for the background prune (no Qt objects)."""
    return {
        "path": session.path,
        "status": session.status,
        "own": session.own,
        "updated": session.updatedSeconds(),
        "presentedCount": session.presentedCount,
        "mtime": _mtime(session.path),
        "slots": [(slot.path, slot.state) for slot in session.slots],
    }


def prune(records, settings, now=None, sizesOut=None):
    """Apply the retention rules to the sessions described by ``records`` (see ``_sessionRecord``). Worker-safe.

    1. delete clean and discarded sessions, and folders without session.json older than a day;
    2. delete sessions not updated for MaxAgeDays, except never-presented crashed sessions younger than 30 days;
    3. keep the newest KeepSessions sessions (never-presented crashed ones are deleted last);
    4. while the total exceeds MaxDiskMB, delete the oldest sessions, then the closed slots of the live session.
    The live session and sessions locked by a running process are never deleted. Return the deleted paths.
    ``sizesOut`` (a dict) receives the size of each session left, by path.
    """
    now = time.time() if now is None else now
    maxAge = float(settings.get("MaxAgeDays", 14)) * 86400.0
    keep = int(settings.get("KeepSessions", 5))
    maxBytes = int(settings.get("MaxDiskMB", 2048)) << 20
    deleted = []

    def delete(record):
        io.removeTree(record["path"])
        deleted.append(record["path"])

    candidates = []
    for record in records:
        if record["own"] or record["status"] in ("running", "possibly-crashed"):
            continue
        status = record["status"]
        if status in ("closed", "discarded") or (status == "unknown" and now - record["mtime"] > ORPHAN_AGE_S):
            delete(record)
            continue
        if status == "unknown":
            continue
        unpresentedCrash = status == "crashed" and record["presentedCount"] == 0
        age = now - (record["updated"] or record["mtime"])
        if age > maxAge and not (unpresentedCrash and age < UNPRESENTED_KEEP_S):
            delete(record)
            continue
        candidates.append(record)
    # Newest first; never-presented crashed sessions are deleted last
    candidates.sort(key=lambda r: (r["status"] == "crashed" and r["presentedCount"] == 0, r["updated"]), reverse=True)
    while len(candidates) > keep:
        victim = min(candidates, key=lambda r: (r["status"] == "crashed" and r["presentedCount"] == 0, r["updated"]))
        candidates.remove(victim)
        delete(victim)
    sizes = {record["path"]: io.treeSize(record["path"]) for record in records if record["path"] not in deleted}
    total = sum(sizes.values())
    while total > maxBytes and candidates:
        victim = min(candidates, key=lambda r: (r["status"] == "crashed" and r["presentedCount"] == 0, r["updated"]))
        candidates.remove(victim)
        total -= sizes.get(victim["path"], 0)
        delete(victim)
    if total > maxBytes:
        for record in records:
            if not record["own"]:
                continue
            for slotPath, state in record["slots"]:
                if total <= maxBytes:
                    break
                if state == "closed-unsaved":
                    size = io.treeSize(slotPath)
                    io.removeTree(slotPath)
                    deleted.append(slotPath)
                    total -= size
                    sizes[record["path"]] = sizes.get(record["path"], 0) - size
    if sizesOut is not None:
        sizesOut.update({path: size for path, size in sizes.items() if path not in deleted})
    return deleted


def pruneInBackground(root, sessions, settings, onDone=None, protectedPaths=()):
    """Run ``prune()`` in the background writer.

    ``protectedPaths``: slot folders of the live session that are never deleted (being recovered).
    ``onDone({"deleted": [paths], "sizes": {session path: bytes}})`` runs on the main thread.
    """
    protected = {_canonical(path) for path in protectedPaths}
    records = []
    for session in sessions:
        record = _sessionRecord(session)
        record["slots"] = [(path, state) for path, state in record["slots"] if _canonical(path) not in protected]
        records.append(record)
    settings = dict(settings)

    def job():
        sizes = {}
        deleted = prune(records, settings, sizesOut=sizes)
        return {"deleted": deleted, "sizes": sizes}

    io.backgroundWriter().submit(job, onDone=onDone)


#
# Recover
#


def _canonical(path):
    return os.path.normcase(os.path.realpath(path))


def autosaveSlotOfScene(scene, roots):
    """The slot folder (``scene-<n>``) of the autosave folder that the storage nodes of ``scene`` point into, or None."""
    canonicalRoots = [_canonical(root) for root in roots if root]
    storageNodes = scene.GetNodesByClass("vtkMRMLStorageNode")
    storageNodes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
    for index in range(storageNodes.GetNumberOfItems()):
        fileName = storageNodes.GetItemAsObject(index).GetFileName()
        if not fileName:
            continue
        path = _canonical(fileName)
        if not any(path.startswith(root.rstrip(os.sep) + os.sep) for root in canonicalRoots):
            continue
        directory = os.path.dirname(fileName)
        while directory and directory != os.path.dirname(directory):
            if os.path.exists(os.path.join(directory, io.MANIFEST_FILE)):
                return directory
            directory = os.path.dirname(directory)
    return None


def rehomeAfterLoad(scene, slotDir, manifest=None, restoreUrl=None):
    """Point the storage nodes that a loaded autosave created back at the original files, or at no file.

    ``restoreUrl``: ``(url, rootDirectory)`` to give the scene (for "add to scene"); by default the URL and root
    directory of the scene that was autosaved (the root directory falls back to the default scene folder).
    Return a report: ``{"restored", "written", "copied", "unlinked", "changed": [...], "missing": [...]}``.
    """
    if manifest is None:
        manifest = io.readJson(os.path.join(slotDir, io.MANIFEST_FILE)) or {}
    entries = manifest.get("entries") or {}
    entriesByDir = {entry["dir"]: entry for entry in entries.values() if entry.get("dir")}
    slotCanonical = _canonical(slotDir)
    report = {"restored": 0, "written": 0, "copied": 0, "unlinked": 0, "changed": [], "missing": []}
    storableOfStorage = {}
    storableNodes = scene.GetNodesByClass("vtkMRMLStorableNode")
    storableNodes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
    for index in range(storableNodes.GetNumberOfItems()):
        node = storableNodes.GetItemAsObject(index)
        for storageIndex in range(node.GetNumberOfStorageNodes()):
            storageNode = node.GetNthStorageNode(storageIndex)
            if storageNode is not None:
                storableOfStorage[storageNode.GetID()] = node
    storageNodes = scene.GetNodesByClass("vtkMRMLStorageNode")
    storageNodes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
    for index in range(storageNodes.GetNumberOfItems()):
        storageNode = storageNodes.GetItemAsObject(index)
        fileName = storageNode.GetFileName()
        if not fileName:
            continue
        canonical = _canonical(fileName)
        if not canonical.startswith(slotCanonical.rstrip(os.sep) + os.sep):
            continue
        relative = os.path.relpath(canonical, slotCanonical).replace(os.sep, "/")
        entry = entriesByDir.get("/".join(relative.split("/")[:2]))
        node = storableOfStorage.get(storageNode.GetID())
        kind = entry.get("kind") if entry else None
        source = (entry.get("files") or [None])[0] if entry else None
        if kind == "copy" and source and os.path.exists(source):
            # Unchanged data: back on the original file, not modified
            storageNode.ResetFileNameList()
            storageNode.SetFileName(source)
            report["copied"] += 1
            report["restored"] += 1
            continue
        original = entry.get("original") if entry and kind == "write" else None
        originalClass = entry.get("originalStorageClass") if entry else None
        if original and node is not None:
            if originalClass and originalClass != storageNode.GetClassName():
                # The autosave used another format: give the node a storage node of the original class
                replacement = scene.AddNewNodeByClass(originalClass)
                replacement.SetFileName(original)
                node.SetAndObserveStorageNodeID(replacement.GetID())
                scene.RemoveNode(storageNode)
            else:
                storageNode.ResetFileNameList()
                storageNode.SetFileName(original)
            node.StorableModified()
            report["written"] += 1
            report["restored"] += 1
            continue
        # New data (no original file): no file, so that Save asks where to save it
        if node is not None:
            for storageIndex in range(node.GetNumberOfStorageNodes()):
                if node.GetNthStorageNodeID(storageIndex) == storageNode.GetID():
                    node.SetAndObserveNthStorageNodeID(storageIndex, None)
                    break
            node.StorableModified()
        scene.RemoveNode(storageNode)
        report["unlinked"] += 1
        report["restored"] += 1
    for entry in entries.values():
        if entry.get("kind") != "reference":
            continue
        for path, stat in zip(entry.get("files") or [], entry.get("firstStat") or entry.get("stat") or [], strict=False):
            if not os.path.exists(path):
                report["missing"].append(path)
            else:
                try:
                    status = os.stat(path)
                    if [status.st_size, status.st_mtime_ns] != list(stat):
                        report["changed"].append(path)
                except OSError:
                    report["missing"].append(path)
    if restoreUrl is None:
        sceneInfo = manifest.get("scene") or {}
        url = sceneInfo.get("url") or ""
        rootDirectory = sceneInfo.get("rootDirectory") or ""
        if not rootDirectory or not os.path.isdir(rootDirectory) or _canonical(rootDirectory).startswith(slotCanonical):
            rootDirectory = slicer.app.defaultScenePath
        restoreUrl = (url, rootDirectory)
    scene.SetURL(restoreUrl[0] or "")
    scene.SetRootDirectory(restoreUrl[1] or slicer.app.defaultScenePath)
    return report


def sceneHasDisplayableData(scene):
    nodes = scene.GetNodesByClass("vtkMRMLDisplayableNode")
    nodes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
    for index in range(nodes.GetNumberOfItems()):
        node = nodes.GetItemAsObject(index)
        if node.GetSaveWithScene() and not node.GetHideFromEditors() and not node.IsA("vtkMRMLSliceNode"):
            return True
    return False


def _execDialog(dialog):
    """Run a dialog modally: ``exec()`` in Qt 6 PythonQt, ``exec_()`` in Qt 5."""
    run = getattr(dialog, "exec", None) or getattr(dialog, "exec_")
    return run()


def askReplaceOrAdd(parent=None):
    """'replace', 'add' or None (cancel)."""
    box = qt.QMessageBox(parent or slicer.util.mainWindow())
    box.setWindowTitle(_("Recover autosaved work"))
    box.setIcon(qt.QMessageBox.Question)
    box.setText(_("The current scene has data. Replace it with the autosaved scene, or add the autosaved data to it?"))
    replaceButton = box.addButton(_("Replace current scene"), qt.QMessageBox.AcceptRole)
    addButton = box.addButton(_("Add to current scene"), qt.QMessageBox.ActionRole)
    box.addButton(qt.QMessageBox.Cancel)
    box.setDefaultButton(replaceButton)
    _execDialog(box)
    clicked = box.clickedButton()
    if clicked == replaceButton:
        return "replace"
    if clicked == addButton:
        return "add"
    return None


def _service():
    from . import autosave

    return autosave.instance()


def recover(slot, mode=None, parent=None):
    """Load an autosaved slot and re-link its data to the original files. Return the report, or None if cancelled."""
    scene = slicer.mrmlScene
    if mode is None:
        mode = askReplaceOrAdd(parent) if sceneHasDisplayableData(scene) else "replace"
    if mode is None:
        return None
    manifest = io.readJson(os.path.join(slot.path, io.MANIFEST_FILE)) or {}
    restoreUrl = None
    service = _service()
    if service is not None:
        # A closed slot of this session is not deleted while it is recovered (closing the current scene below
        # keeps a new closed slot, and only MAX_CLOSED_SLOTS are kept)
        service.protectSlot(slot.path)
    report = None
    try:
        if mode == "replace":
            # Before beginRecovery(): unsaved changes of the replaced scene stay recoverable (closed-unsaved slot)
            scene.Clear(0)
        else:
            restoreUrl = (scene.GetURL(), scene.GetRootDirectory())
        if service is not None:
            service.beginRecovery()
        try:
            with slicer.util.tryWithErrorDisplay(_("Failed to recover the autosaved scene."), waitCursor=True):
                slicer.util.loadScene(slot.scenePath())
                report = rehomeAfterLoad(scene, slot.path, manifest, restoreUrl)
        finally:
            if service is not None:
                service.endRecovery()
    finally:
        if service is not None:
            service.releaseSlot(slot.path)
    if report is None:
        return None
    if not slot.session.own:
        markSession(slot.session, "recovered")
    updated = _shortTime(slot.updated, timeOnlyToday=True)
    text = _("Recovered {count} items from the autosave of {time}. Use File › Save to keep them.").format(count=report["restored"], time=updated)  # noqa: RUF001
    notify(text, report)
    return report


def markSession(session, state, synchronous=False, closedForUpdate=None):
    """Record the state of a session (recovered, discarded) in its session.json.

    ``closedForUpdate=True`` also records that Slicer Studio closed the session to install an update.

    The write goes through the background writer, after the other writes of that file (``incrementPresented``).
    """
    data = dict(session.data)
    data["state"] = state
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if closedForUpdate is not None:
        data["closedForUpdate"] = bool(closedForUpdate)
        session.closedForUpdate = bool(closedForUpdate)
    session.data = data
    session.status = state
    path = os.path.join(session.path, io.SESSION_FILE)
    writer = io.backgroundWriter()
    if synchronous:
        writer.drain(5.0)
        try:
            io.atomicWriteJson(path, data)
        except OSError:
            logging.warning(f"Autosave: cannot update {session.path}")
        return
    writer.submit(io.atomicWriteJson, path, data, onError=lambda error: logging.warning(f"Autosave: cannot update {session.path}: {error}"))


def notify(text, report=None):
    """Tell the user what was recovered: a toast in Studio, a message box in Classic."""
    details = []
    if report:
        if report.get("changed"):
            details.append(_("Changed on disk since the autosave: {files}").format(files=", ".join(os.path.basename(p) for p in report["changed"][:5])))
        if report.get("missing"):
            details.append(_("Missing files: {files}").format(files=", ".join(os.path.basename(p) for p in report["missing"][:5])))
    import sys

    shellModule = sys.modules.get("StudioLib.shell")
    shell = shellModule.instance() if shellModule is not None else None
    if shell is not None and not details:
        shell.toast(text, 5000)
        return
    if slicer.app.testingEnabled():
        logging.info(" ".join([text] + details))
        return
    slicer.util.infoDisplay(text, windowTitle=_("Recover autosaved work"), detailedText="\n".join(details) if details else None)


def discard(session, confirm=True, parent=None):
    """Move a session to the trash of the operating system (after a confirmation). Return True if discarded."""
    if session.own or session.status in ("running", "possibly-crashed"):
        return False
    if confirm:
        when = _shortTime(session.updated)
        answer = qt.QMessageBox.question(parent or slicer.util.mainWindow(), _("Discard autosaved work"),
                                         _("Discard autosaved work from {time}? This cannot be undone.").format(time=when),
                                         qt.QMessageBox.Discard | qt.QMessageBox.Cancel, qt.QMessageBox.Cancel)
        if answer != qt.QMessageBox.Discard:
            return False
    markSession(session, "discarded", synchronous=True)
    moved = False
    if hasattr(qt.QFile, "moveToTrash"):
        try:
            # The instance method: PythonQt does not expose the static overload
            moved = bool(qt.QFile(session.path).moveToTrash())
        except Exception:
            moved = False
    if not moved:
        io.backgroundWriter().submit(io.removeTree, session.path)
    return True


def deleteAll(root, ownSessionId=None):
    """Delete every session that is not running (after a confirmation in the caller). Return the number deleted."""
    count = 0
    for session in scan(root, ownSessionId):
        if session.own or session.status in ("running", "possibly-crashed"):
            continue
        io.backgroundWriter().submit(io.removeTree, session.path)
        count += 1
    return count


#
# Text helpers
#


def _shortTime(isoText, timeOnlyToday=False):
    """'Today 14:32' (or '14:32'), else the short date and time of the locale."""
    seconds = _parseTime(isoText)
    if not seconds:
        return isoText or ""
    dateTime = qt.QDateTime.fromSecsSinceEpoch(int(seconds))
    locale = qt.QLocale()
    if dateTime.date() == qt.QDate.currentDate():
        clock = time.strftime("%H:%M", time.localtime(seconds))
        return clock if timeOnlyToday else _("Today {time}").format(time=clock)
    return locale.toString(dateTime, qt.QLocale.ShortFormat)


def formatBytes(size):
    size = float(size or 0)
    if size < 1024 * 1024:
        return _("{size} KB").format(size=max(1, round(size / 1024)))
    if size < 1024 ** 3:
        return _("{size} MB").format(size=f"{size / 1024 ** 2:.1f}")
    return _("{size} GB").format(size=f"{size / 1024 ** 3:.1f}")


def describeContents(summary):
    """'MR Head · Lesions (1 segment) · 1 point list'."""
    parts = []
    if summary.get("title"):
        parts.append(summary["title"])
    for segmentation in summary.get("segmentations") or []:
        count = segmentation.get("count", 0)
        text = _("{name} (1 segment)").format(name=segmentation["name"]) if count == 1 else _("{name} ({count} segments)").format(name=segmentation["name"], count=count)
        if segmentation["name"] != summary.get("title"):
            parts.append(text)
        else:
            parts[0] = text
    volumes = [name for name in summary.get("volumes") or [] if name != summary.get("title")]
    if volumes:
        parts.append(_("1 more volume") if len(volumes) == 1 else _("{count} more volumes").format(count=len(volumes)))
    for key, one, many in (("markups", _("1 point list"), _("{count} point lists")), ("tables", _("1 table"), _("{count} tables")), ("models", _("1 model"), _("{count} models"))):
        count = summary.get(key) or 0
        if count:
            parts.append(one if count == 1 else many.format(count=count))
    notAutosaved = summary.get("notAutosaved") or []
    if notAutosaved:
        parts.append(_("not autosaved: {names}").format(names=", ".join(item.get("name") or _("1 item") for item in notAutosaved[:3])))
    return " · ".join(parts) or _("(empty scene)")


#
# Dialog
#

_dialog = None


class RecoveryDialog(qt.QDialog):
    """Non-modal list of the autosaved sessions, with Recover, Discard, Later and Open autosave folder."""

    def __init__(self, parent=None):
        qt.QDialog.__init__(self, parent or slicer.util.mainWindow())
        self.setObjectName("StudioRecoveryDialog")
        self.setWindowTitle(_("Recover unsaved work"))
        self.setModal(False)
        self.resize(760, 460)
        self._slots = {}
        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 22)
        layout.setSpacing(14)
        # Header (VISUAL 4.7): the icon tile beside the title and the body text, which says why the dialog is shown
        header, self.titleLabel, self.textLabel = ui.dialogHeader("archive-restore", _("Recover unsaved work"), " ", "StudioRecovery")
        layout.addLayout(header)
        self.list = qt.QTreeWidget()
        self.list.setObjectName("StudioRecoveryList")
        self.list.setColumnCount(4)
        self.list.setHeaderLabels([_("Last autosave"), _("Contents"), _("Size"), _("Status")])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(False)
        self.list.setIconSize(qt.QSize(THUMBNAIL_ICON_SIZE, THUMBNAIL_ICON_SIZE))
        self.list.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        self.list.setAllColumnsShowFocus(True)
        self.list.connect("itemSelectionChanged()", self._updateButtons)
        self.list.connect("itemDoubleClicked(QTreeWidgetItem*,int)", lambda item, column: self.onRecover())
        layout.addWidget(self.list, 1)
        # Shown instead of the list when nothing was autosaved
        self.emptyLabel = ui.label(_("Nothing was autosaved yet. Slicer Studio saves your open work in the background."), "body", "text3",
                                   "StudioRecoveryEmpty", wrap=True)
        self.emptyLabel.setAlignment(qt.Qt.AlignCenter)
        self.emptyLabel.hide()
        layout.addWidget(self.emptyLabel, 1)
        self.missingLabel = ui.label("", "caption", "text3", "StudioRecoveryMissing", wrap=True)
        self.missingLabel.hide()
        layout.addWidget(self.missingLabel)
        # Footer (VISUAL 4.7): the links on the left, the single primary button on the right
        buttons = qt.QHBoxLayout()
        buttons.setSpacing(10)
        self.settingsLink = ui.linkButtonWidget(_("Autosave settings…"), self.onSettings, "StudioRecoverySettingsLink")
        buttons.addWidget(self.settingsLink)
        buttons.addSpacing(6)
        self.openFolderButton = ui.linkButtonWidget(_("Open autosave folder"), self.onOpenFolder, "StudioRecoveryOpenFolderLink")
        buttons.addWidget(self.openFolderButton)
        buttons.addStretch(1)
        self.discardButton = qt.QPushButton(_("Discard…"))
        self.discardButton.setObjectName("StudioRecoveryDiscardButton")
        self.discardButton.setMinimumHeight(36)
        ui.setRole(self.discardButton, "secondary")
        self.discardButton.connect("clicked()", self.onDiscard)
        buttons.addWidget(self.discardButton)
        self.laterButton = qt.QPushButton(_("Later"))
        self.laterButton.setObjectName("StudioRecoveryLaterButton")
        self.laterButton.setMinimumHeight(36)
        ui.setRole(self.laterButton, "secondary")
        self.laterButton.connect("clicked()", self.close)
        buttons.addWidget(self.laterButton)
        self.recoverButton = qt.QPushButton(_("Recover"))
        self.recoverButton.setObjectName("StudioRecoveryRecoverButton")
        self.recoverButton.setDefault(True)
        self.recoverButton.setMinimumHeight(36)
        self.recoverButton.setMinimumWidth(96)
        ui.setRole(self.recoverButton, "primary")
        self.recoverButton.connect("clicked()", self.onRecover)
        buttons.addWidget(self.recoverButton)
        layout.addLayout(buttons)
        # Dialog chrome (VISUAL 4.7). Non-modal, so without the backdrop: the main window stays usable behind it
        ui.dialogChrome(self, backdrop=False, styleSheet=self._styleSheet)
        self.startup = False
        self.root = None

    def setSessions(self, sessions, startup=False, root=None):
        self.startup = startup
        self.root = root
        self.textLabel.setText(self._introText(sessions, startup))
        self.laterButton.setText(_("Later") if startup else _("Close"))
        self.list.clear()
        self._slots = {}
        for session in sessions:
            slots = session.recoverableSlots() if startup else session.slots
            for slot in slots:
                item = qt.QTreeWidgetItem()
                item.setText(0, _shortTime(slot.updated))
                item.setText(1, slot.contentsText())
                item.setToolTip(1, slot.contentsText())
                item.setText(2, formatBytes(slot.bytes))
                item.setText(3, _statusText(slot.status()))
                if slot.thumbnail:
                    pixmap = qt.QPixmap(slot.thumbnail)
                    if not pixmap.isNull():
                        item.setIcon(0, qt.QIcon(pixmap.scaled(THUMBNAIL_ICON_SIZE, THUMBNAIL_ICON_SIZE, qt.Qt.KeepAspectRatio, qt.Qt.SmoothTransformation)))
                key = f"{session.sessionId}/{slot.name}"
                item.setData(0, qt.Qt.UserRole, key)
                self._slots[key] = slot
                self.list.addTopLevelItem(item)
        for column in (0, 2, 3):
            self.list.resizeColumnToContents(column)
        # The contents column takes the remaining width
        self.list.header().setStretchLastSection(False)
        self.list.header().setSectionResizeMode(1, qt.QHeaderView.Stretch)
        if self.list.topLevelItemCount > 0:
            self.list.setCurrentItem(self.list.topLevelItem(0))
        empty = self.list.topLevelItemCount == 0
        self.list.setVisible(not empty)
        self.emptyLabel.setVisible(empty)
        self._updateButtons()

    @staticmethod
    def _introText(sessions, startup):
        if not startup:
            return _("Work that Slicer Studio autosaved. Recover what you need and discard the rest.")
        crashed = [s for s in sessions if s.status in ("crashed", "possibly-crashed")]
        if crashed and all(s.status == "crashed" and s.closedForUpdate for s in crashed):
            return _("Slicer Studio closed to install an update. Work from these sessions was autosaved:")
        if any(s.closedForUpdate for s in crashed):
            return _("Slicer did not close normally, or closed to install an update. Work from these sessions was autosaved:")
        return _("Slicer did not close normally. Work from these sessions was autosaved:")

    @staticmethod
    def _styleSheet():
        return (f"QTreeWidget#StudioRecoveryList {{ background-color: {ui.token('card-low').name()}; border: 1px solid {ui.token('hairline').name()};"
                " border-radius: 10px; padding: 2px; }")

    def currentSlot(self):
        item = self.list.currentItem()
        if item is None:
            return None
        return self._slots.get(item.data(0, qt.Qt.UserRole))

    def _updateButtons(self):
        slot = self.currentSlot()
        recoverable = slot is not None and slot.status() != "own-current"
        self.recoverButton.setEnabled(recoverable)
        self.discardButton.setEnabled(slot is not None and not slot.session.own and slot.session.status not in ("running", "possibly-crashed"))
        if slot is not None and slot.missing:
            self.missingLabel.setText(_("Some original files no longer exist: {files}").format(files=", ".join(os.path.basename(p) for p in slot.missing[:5])))
            self.missingLabel.show()
        else:
            self.missingLabel.hide()

    def onRecover(self):
        slot = self.currentSlot()
        if slot is None or slot.status() == "own-current":
            return
        handlers = _service().recoveryHandlers() if _service() is not None else []
        for canHandle, handle in handlers:
            try:
                if canHandle(slot):
                    handle(slot)
                    self.close()
                    return
            except Exception:
                logging.error("Autosave: recovery handler failed:\n" + traceback.format_exc())
        report = recover(slot, parent=self)
        if report is not None:
            self.close()

    def onDiscard(self):
        slot = self.currentSlot()
        if slot is None:
            return
        if discard(slot.session, parent=self):
            self.refresh()

    def onOpenFolder(self):
        slot = self.currentSlot()
        path = slot.session.path if slot is not None else (self.root or io.defaultRoot())
        if os.path.isdir(path):
            from . import projects

            projects.showInFileManager(path)

    def onSettings(self):
        from . import autosave

        autosave.showAutosaveSettings()

    def refresh(self):
        service = _service()
        root = self.root or (service.rootDirectory() if service is not None else io.defaultRoot())
        sessions = scan(root, service.sessionId if service is not None else None)
        if self.startup:
            sessions = [s for s in sessions if s.presentable()]
        self.setSessions(sessions, self.startup, root)
        if self.startup and not self._slots:
            self.close()


def showRecoveryDialog(sessions=None, startup=False):
    """Show the non-modal recovery dialog with ``sessions`` (default: every session of the autosave folder)."""
    global _dialog
    service = _service()
    root = service.rootDirectory() if service is not None else io.defaultRoot()
    if sessions is None:
        sessions = scan(root, service.sessionId if service is not None else None)
    if _dialog is None:
        _dialog = RecoveryDialog()
    _dialog.setSessions(sessions, startup=startup, root=root)
    _dialog.show()
    _dialog.raise_()
    return _dialog

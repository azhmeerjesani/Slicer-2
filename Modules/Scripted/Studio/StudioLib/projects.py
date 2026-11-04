r"""Projects of Slicer Studio: one folder that the user chose, where all work is saved continuously (A-09, D-A16).

The user chooses the *Slicer Studio folder* once, at the first start (``Documents/Slicer Studio`` by default,
``/data/Slicer Studio`` in the web build). Everything is then saved there, as standard files organized by project::

  Slicer Studio/
    About this folder.txt     a short plain-language map of the folder (written once, never overwritten)
    Projects/<Project name>/  one folder per project: a standard Slicer scene that official 3D Slicer opens
      <Project name>.mrml     the scene (relative paths only)
      Images/ Segmentations/ Lesions/ Maps/ Markups/ Models/ Tables/ Transforms/ Other/ Exports/
      .studio/                project.json, thumbnail.png, lock (hidden, safe to ignore)
    Studies/<Study name>/     lesion network mapping studies ("New study" proposes this folder)
    LNM/                      the default Studio/LNM/DataDirectory (normalization outputs, control sets, recipes)

A folder is created only when something is written into it. Connectomes and template downloads keep their own
cache locations: never inside ``Projects/``.

This module is the model: the folder, the names, the listing and the file operations (rename, duplicate, trash,
moving the folder). Writing the scene into a project is the autosave engine's job (``autosave.AutosaveService`` with
``autosave_io.ProjectWriter``); the user interface is ``projects_ui``.

Settings (fork-only, SPEC 9.7 rule 5): ``Studio/Projects/Root`` (the folder), ``Studio/Projects/RootChosen`` and, while
the content of the folder is moved, ``Studio/Projects/MovePending`` (``recordPendingMove``). The
environment variable ``SLICER_STUDIO_PROJECTS_ROOT`` overrides both and skips the chooser (tests, gates, harnesses);
``SLICER_STUDIO_PROJECTS=0`` turns projects off (the private recovery autosave of A-01 then runs in Studio too).

No function here imports numpy or runs at startup (except a move that merges two reference-map libraries, which needs
``LesionNetworkMappingLib.refmaps``). Worker-safe functions (only the standard library) are marked so.
"""

import calendar
import contextlib
import errno
import json
import logging
import os
import re
import shutil
import sys
import threading
import time

import qt
import slicer
from slicer.i18n import tr as _

#: Settings keys (fork-only)
ROOT_KEY = "Studio/Projects/Root"
ROOT_CHOSEN_KEY = "Studio/Projects/RootChosen"
#: A move of the folder's content that has started and not finished yet (JSON ``{"from": old folder, "to": new folder}``,
#: ``recordPendingMove``): a move that is interrupted (a crash, a kill) has deleted originals in the old folder while the
#: setting still points there, so the next start finishes it (``AutosaveService.finishInterruptedMove``)
MOVE_PENDING_KEY = "Studio/Projects/MovePending"
#: Environment: the folder (skips the chooser), and "0" to turn projects off
ROOT_ENVIRONMENT = "SLICER_STUDIO_PROJECTS_ROOT"
ENABLED_ENVIRONMENT = "SLICER_STUDIO_PROJECTS"

ROOT_FOLDER_NAME = "Slicer Studio"
WEB_ROOT = "/data/Slicer Studio"
PROJECTS_DIR = "Projects"
STUDIES_DIR = "Studies"
LNM_DIR = "LNM"
#: Your templates (A-10): the same name as ``templates.TEMPLATES_DIR`` (not imported here: templates imports this module)
TEMPLATES_DIR = "Templates"
ABOUT_FILE = "About this folder.txt"
#: Hidden folder of a project
PRIVATE_DIR = ".studio"
PROJECT_FILE = "project.json"
THUMBNAIL_FILE = "thumbnail.png"
#: The thumbnails of a project (V-02, rendered by ``thumbnails.renderProjectViews()``): the axial view (Library cards, the
#: inspector) and the coronal and sagittal views (the 3-view strip of the Continue card)
THUMBNAIL_VIEW_FILES = (("axial", THUMBNAIL_FILE), ("coronal", "thumbnail-coronal.png"), ("sagittal", "thumbnail-sagittal.png"))
LOCK_FILE = "lock"
#: Files of nodes that left the project are moved here (kept REMOVED_KEEP_DAYS days), never deleted at once
REMOVED_DIR = "removed"
REMOVED_KEEP_DAYS = 14
SCENE_EXTENSION = ".mrml"
FORMAT = "slicer-studio-project"
VERSION = 1

#: Kind folders of a project
IMAGES = "Images"
SEGMENTATIONS = "Segmentations"
LESIONS = "Lesions"
MAPS = "Maps"
MARKUPS = "Markups"
MODELS = "Models"
TABLES = "Tables"
TRANSFORMS = "Transforms"
OTHER = "Other"
EXPORTS = "Exports"
KIND_FOLDERS = (IMAGES, SEGMENTATIONS, LESIONS, MAPS, MARKUPS, MODELS, TABLES, TRANSFORMS, OTHER, EXPORTS)

UNTITLED_NAME = "Untitled project"
#: Longest file or folder name written (characters)
MAX_NAME_LENGTH = 80
#: Longest file or folder name written (UTF-8 bytes). File systems limit names in bytes (255 on Linux and APFS), not in
#: characters, and Studio adds to a name: ".studio-tmp-xxxxxxxx.mrml" (25 bytes) on the scene file it writes, " (Slicer Studio)"
#: (16) when another program changed it, " 99" for a taken name and ".studio-part" (12) on a copy in progress
MAX_NAME_BYTES = 200

#: Compound file extensions, longest first (the part after the base name)
COMPOUND_EXTENSIONS = (".seg.nrrd", ".seg.nhdr", ".seg.vtm", ".nii.gz", ".mrk.json", ".schema.tsv", ".schema.csv", ".seq.nrrd", ".seq.nhdr",
                       ".tar.gz", ".vtk.gz")

#: Folders synced to a cloud service: (path fragment, service name). Matched on the path components, case-insensitively.
SYNCED_FOLDERS = (
    (("library", "mobile documents"), "iCloud Drive"),
    (("icloud drive",), "iCloud Drive"),
    (("icloudrive",), "iCloud Drive"),
    (("dropbox",), "Dropbox"),
    (("onedrive",), "OneDrive"),
    (("google drive",), "Google Drive"),
    (("googledrive",), "Google Drive"),
    (("my drive",), "Google Drive"),
    (("box",), "Box"),
    (("box sync",), "Box"),
)

_ABOUT_TEXT = """Slicer Studio folder
====================

Slicer Studio saves your work here automatically, as standard files that you can open,
back up or share. You never need to press Save.

Projects/<name>/        One folder per project. Each one is a 3D Slicer scene:
  <name>.mrml           the scene. Official 3D Slicer opens it with File > Add Data
                        (or drag the .mrml file onto 3D Slicer).
  Images/               scans and other images
  Segmentations/        drawings (Slicer segmentations, .seg.nrrd)
  Lesions/              lesion masks saved with "Save lesion" (NIfTI + a .json note)
  Maps/                 network maps and statistic maps
  Markups/ Models/ Tables/ Transforms/ Other/   other data, by kind
  Exports/              screenshots, figures, reports and tables made from the project
  .studio/              Slicer Studio's own notes (thumbnail, lock). Safe to ignore.

Studies/<name>/         Lesion network mapping studies (study.json, participants.tsv, lesions/, ...).
LNM/                    Normalization outputs, control sets and recipes that belong to no study.
Templates/<name>/       Your own templates (Settings > Templates), each with a template.json.

Back up or share a project by copying its whole folder (or use "Save a copy as..." in Slicer Studio
to make a single .mrb file). Scans can contain patient data: keep this folder private.
"""

_current = None
_listeners = []


#
# The Slicer Studio folder
#


def isEnabled():
    """False when projects are turned off (``SLICER_STUDIO_PROJECTS=0``)."""
    return os.environ.get(ENABLED_ENVIRONMENT) != "0"


def isWebBuild():
    """True in the web (Docker) build of Slicer Studio, where the folder is /data/Slicer Studio, used without asking."""
    return bool(os.environ.get("SLICER_STUDIO_WEB")) or os.path.exists("/.dockerenv")


def documentsFolder():
    documents = qt.QStandardPaths.writableLocation(qt.QStandardPaths.DocumentsLocation)
    return documents or os.path.join(os.path.expanduser("~"), "Documents")


def defaultRoot():
    """The proposed folder: /data/Slicer Studio in the web build, else <Documents>/Slicer Studio."""
    if isWebBuild():
        return WEB_ROOT
    return os.path.join(documentsFolder(), ROOT_FOLDER_NAME)


def rootFromEnvironment():
    value = os.environ.get(ROOT_ENVIRONMENT)
    return os.path.normpath(os.path.abspath(os.path.expanduser(value))) if value else None


def _setting(key, default=None):
    value = qt.QSettings().value(key)
    return default if value is None or value == "" else value


def root():
    """The Slicer Studio folder: SLICER_STUDIO_PROJECTS_ROOT, else the setting Studio/Projects/Root, else ``defaultRoot()``."""
    environment = rootFromEnvironment()
    if environment:
        return environment
    configured = _setting(ROOT_KEY)
    if configured:
        return os.path.normpath(os.path.abspath(os.path.expanduser(str(configured))))
    return os.path.normpath(defaultRoot())


def isRootFromEnvironment():
    return bool(rootFromEnvironment())


def isRootChosen():
    """True once the user answered the chooser (or the folder is set by the environment or the web build)."""
    if isRootFromEnvironment() or isWebBuild():
        return True
    return slicer.util.toBool(_setting(ROOT_CHOSEN_KEY, False))


def needsChooser():
    """True if the chooser must be shown before the first project is created (first start of Studio)."""
    return isEnabled() and not isRootChosen()


def setRoot(path, chosen=True):
    """Store the chosen folder (only an explicit choice writes the keys; never under --testing)."""
    settings = qt.QSettings()
    path = os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))
    settings.setValue(ROOT_KEY, path)
    if chosen:
        settings.setValue(ROOT_CHOSEN_KEY, True)
    # A move that was pending ends here: it finished, or another folder was chosen since
    settings.remove(MOVE_PENDING_KEY)
    discardMoveJournal()
    # Written now: after a move, the content is already in the new folder, and a crash before Qt writes the settings by
    # itself would start the next time in the old, now empty folder
    settings.sync()
    _notify()
    return path


def recordPendingMove(oldRoot, newRoot):
    """Record that the content of ``oldRoot`` is being moved to ``newRoot`` (written now: a kill during the move must find it).

    ``setRoot`` (the end of a move) and ``clearPendingMove`` (a move that failed and was reported) remove it.
    """
    settings = qt.QSettings()
    settings.setValue(MOVE_PENDING_KEY, json.dumps({"from": os.path.normpath(oldRoot), "to": os.path.normpath(newRoot)}))
    settings.sync()


def pendingMove():
    """``(old folder, new folder)`` of a move that started and did not finish (the app was closed during it), or None."""
    try:
        record = json.loads(str(_setting(MOVE_PENDING_KEY, "") or "null"))
    except ValueError:
        return None
    if not isinstance(record, dict) or not record.get("from") or not record.get("to"):
        return None
    return os.path.normpath(str(record["from"])), os.path.normpath(str(record["to"]))


def clearPendingMove():
    settings = qt.QSettings()
    if settings.contains(MOVE_PENDING_KEY):
        settings.remove(MOVE_PENDING_KEY)
        settings.sync()


#: The file (in the private application data folder, never in a shared folder) where a move of the Slicer Studio folder
#: writes down, as it goes, what the folders alone cannot tell once the move is killed (``MoveJournal``)
MOVE_JOURNAL_FILE = "StudioMoveJournal.json"


def moveJournalPath():
    """Where ``MoveJournal`` keeps its file, or None without an application data folder (main thread)."""
    base = str(qt.QStandardPaths.writableLocation(qt.QStandardPaths.AppLocalDataLocation) or "")
    return os.path.join(base, MOVE_JOURNAL_FILE) if base else None


def discardMoveJournal():
    """Delete the journal of a move (the move finished, or another folder was chosen since). Main thread."""
    path = moveJournalPath()
    if path:
        with contextlib.suppress(OSError):
            os.remove(path)


#: The lock file (next to the move journal) that a running move of the Slicer Studio folder holds from its start until the
#: setting points at the new folder: ``Studio/Projects/MovePending`` alone cannot tell a move that another running Slicer
#: Studio of this user is doing now from one that was interrupted (``isMoveRunningElsewhere``)
MOVE_LOCK_FILE = "StudioMove.lock"
#: The move lock that this process holds (``takeMoveLock``), or None
_moveLock = None


def moveLockPath():
    """Where a running move keeps its lock file, or None without an application data folder (main thread)."""
    path = moveJournalPath()
    return os.path.join(os.path.dirname(path), MOVE_LOCK_FILE) if path else None


def takeMoveLock():
    """Mark the move of the folder as running in this process. Return False when another running Slicer Studio of this user
    is moving it now (main thread: a Qt lock file; a process that was killed holds nothing).
    """
    global _moveLock
    if _moveLock is not None:
        return True
    path = moveLockPath()
    if not path:
        return True
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = _tryLockAt(path)
    if lock is None:
        # No lock file, so nobody holds it: it could not be created (the move goes on without the mark)
        return not os.path.lexists(path)
    _moveLock = lock
    _heldLocks.add(_lockKey(path))
    return True


def releaseMoveLock():
    """End the mark of ``takeMoveLock`` (nothing when this process holds none)."""
    global _moveLock
    lock, _moveLock = _moveLock, None
    if lock is not None:
        path = moveLockPath()
        lock.unlock()
        if path:
            _heldLocks.discard(_lockKey(path))


def isMoveRunningElsewhere():
    """True while another running Slicer Studio of this user moves the Slicer Studio folder (``takeMoveLock``): its
    ``MovePending`` record is not an interrupted move, and the projects it moves are not crashed. Main thread.
    """
    if _moveLock is not None:
        return False
    path = moveLockPath()
    if not path or not os.path.lexists(path):
        return False
    lock = _tryLockAt(path)
    if lock is None:
        return os.path.lexists(path)
    lock.unlock()
    return False


def checkMoveNotRunningElsewhere():
    """Raise MoveRunningElsewhereError while another running Slicer Studio moves the folder (``isMoveRunningElsewhere``)."""
    if isMoveRunningElsewhere():
        raise MoveRunningElsewhereError(_("Slicer Studio is moving your projects and studies in another window. Wait until it "
                                          "finishes, then try again."))


class MoveJournal:
    """What a move of the Slicer Studio folder (``moveContent``) did that the folders cannot tell after a kill.

    An uninterrupted move remembers, in memory, the items that moved under another name (" 2") because the new folder had
    one with theirs, and the moved templates that got another id: paths (the recent study, the Cohort study, registered
    connectomes) and the default template follow them, and a moved study is named after its new folder. A kill loses that,
    so the move finished at the next start would point those paths at the new folder's own items that kept the names, and
    leave two templates with one id. So the move writes them here before each item moves (``recordRename``), with the
    item names Templates/ of the new folder had before (``setTemplatesBefore``) and each new template id before it is
    written (``recordId``), and the move that finishes it reads them back (``resume``).

    Holds local paths (as the settings do): kept in the application data folder, deleted when the move ends
    (``projects.setRoot``) or another move starts. Worker-safe (files only); never raises.
    """

    def __init__(self, path, oldRoot, newRoot, resume=False):
        self.path = path
        self.oldRoot = os.path.normpath(oldRoot)
        self.newRoot = os.path.normpath(newRoot)
        self._lock = threading.Lock()
        self._data = {"from": self.oldRoot, "to": self.newRoot, "renamed": {}, "renamedIds": {}, "templatesBefore": None}
        if not path:
            return
        if resume:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = None
            # Only the journal of this move (from and to the same folders)
            def same(path, other):
                return bool(path) and os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(other))

            if isinstance(data, dict) and same(str(data.get("from") or ""), self.oldRoot) and same(str(data.get("to") or ""), self.newRoot):
                if isinstance(data.get("renamed"), dict):
                    self._data["renamed"] = {str(key): str(value) for key, value in data["renamed"].items()}
                if isinstance(data.get("renamedIds"), dict):
                    self._data["renamedIds"] = {str(key): str(value) for key, value in data["renamedIds"].items()}
                if isinstance(data.get("templatesBefore"), list):
                    self._data["templatesBefore"] = [str(name) for name in data["templatesBefore"]]
                return
        # A new move: what an earlier one wrote is not this move's
        with contextlib.suppress(OSError):
            os.remove(path)

    def _write(self):
        if not self.path:
            return
        staging = self.path + ".writing"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(staging, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(staging, self.path)
        except OSError:
            logging.warning("Slicer Studio: the journal of the move could not be written", exc_info=True)
            with contextlib.suppress(OSError):
                os.remove(staging)

    def recordRename(self, source, destination):
        """``source`` (in the old folder) is about to move to ``destination`` (in the new folder, under another name)."""
        try:
            key = os.path.normpath(os.path.relpath(source, self.oldRoot))
            value = os.path.normpath(os.path.relpath(destination, self.newRoot))
        except ValueError:
            return  # another drive (Windows): not inside the folders
        with self._lock:
            if self._data["renamed"].get(key) != value:
                self._data["renamed"][key] = value
                self._write()

    def recordId(self, oldId, newId):
        """A moved template is about to get the id ``newId`` instead of ``oldId``."""
        with self._lock:
            if self._data["renamedIds"].get(oldId) != newId:
                self._data["renamedIds"][oldId] = newId
                self._write()

    def templatesBefore(self):
        """The item names of Templates/ of the new folder before the move (a set), or None when not recorded."""
        names = self._data["templatesBefore"]
        return None if names is None else set(names)

    def setTemplatesBefore(self, names):
        with self._lock:
            self._data["templatesBefore"] = sorted(names)
            self._write()

    def completedRenames(self):
        """``{source: destination}`` (absolute) of the recorded items that moved: the source is gone, the destination is there.

        An item the kill came before is still in the old folder, and the move that finishes records it again.
        """
        result = {}
        for key, value in self._data["renamed"].items():
            source = os.path.normpath(os.path.join(self.oldRoot, key))
            destination = os.path.normpath(os.path.join(self.newRoot, value))
            if not os.path.lexists(source) and os.path.lexists(destination):
                result[source] = destination
        return result

    def completedIds(self, templatesFolder):
        """``{old id: new id}`` of the recorded template ids that a template of ``templatesFolder`` has now."""
        recorded = self._data["renamedIds"]
        if not recorded:
            return {}
        try:
            from . import templates

            names = os.listdir(templatesFolder)
            ids = set()
            for name in names:
                record = templates.readRecord(os.path.join(templatesFolder, name)) if not name.startswith(".") else None
                if record is not None:
                    ids.add(templates.USER_PREFIX + str(record.get("id")))
        except Exception:
            logging.warning("Slicer Studio: the ids of the moved templates could not be read", exc_info=True)
            return {}
        return {oldId: newId for oldId, newId in recorded.items() if newId in ids}


class _JournaledRenames(dict):
    """The ``renamed`` dict of ``moveContent`` that writes each item into the journal before it moves under its new name."""

    def __init__(self, journal):
        super().__init__()
        self._journal = journal

    def __setitem__(self, source, destination):
        self._journal.recordRename(source, destination)
        super().__setitem__(source, destination)

    def update(self, *args, **kwargs):
        for source, destination in dict(*args, **kwargs).items():
            self[source] = destination


def projectsFolder(rootPath=None):
    return os.path.join(rootPath or root(), PROJECTS_DIR)


def studiesFolder(rootPath=None):
    return os.path.join(rootPath or root(), STUDIES_DIR)


def lnmFolder(rootPath=None):
    """<root>/LNM: the default of Studio/LNM/DataDirectory when that setting is unset (``LesionNetworkMapping``)."""
    return os.path.join(rootPath or root(), LNM_DIR)


def syncedService(path):
    """The name of the cloud service that syncs ``path`` ("iCloud Drive", "Dropbox", ...), or None."""
    parts = [part.lower() for part in re.split(r"[\\/]+", os.path.normpath(str(path or ""))) if part]
    for fragment, service in SYNCED_FOLDERS:
        for index in range(len(parts) - len(fragment) + 1):
            if tuple(parts[index:index + len(fragment)]) == fragment:
                return service
        # Cloud folders are often named "Dropbox (Company)", "OneDrive - Company", "GoogleDrive-name@..."
        if len(fragment) == 1 and fragment[0] != "box" and any(part.startswith(fragment[0]) for part in parts):
            return service
    return None


def webFolderNotice(path, web=None):
    """A warning for a Slicer Studio folder outside ``/data`` in the web build (it is lost with the container), else ""."""
    web = isWebBuild() if web is None else web
    if not web or not path:
        return ""
    normalized = os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))
    if normalized == WEB_DATA_FOLDER or normalized.startswith(WEB_DATA_FOLDER + "/"):
        return ""
    return _("{path} is inside the container but outside {data}: projects saved there are lost when the container is removed. "
             "Use a folder under {data}.").format(path=normalized, data=WEB_DATA_FOLDER)


def ensureRoot(rootPath=None):
    """Create the folder and its "About this folder.txt" (written once: never overwritten). Return the folder."""
    rootPath = rootPath or root()
    os.makedirs(rootPath, exist_ok=True)
    about = os.path.join(rootPath, ABOUT_FILE)
    if not os.path.exists(about):
        try:
            with open(about, "w", encoding="utf-8", newline="\n") as f:
                f.write(_ABOUT_TEXT)
        except OSError:
            logging.warning(f"Slicer Studio: cannot write {about}")
    return rootPath


def isStudioFolder(path):
    """True if ``path`` is a Slicer Studio folder already: named "Slicer Studio", or holding what Studio writes there.

    Subfolders are made only when something is written into them, so a Studio folder may have no Projects/ yet: its
    "About this folder.txt" (written when the folder is chosen), a study (Studies/<name>/study.json), a template
    (Templates/<name>/template.json) or Studio's LNM/ folders count too. The folder chooser uses such a folder as is.
    """
    if not path:
        return False
    path = os.path.normpath(path)
    if os.path.basename(path) == ROOT_FOLDER_NAME:
        return True
    if os.path.isdir(os.path.join(path, PROJECTS_DIR)) or os.path.isfile(os.path.join(path, ABOUT_FILE)):
        return True
    if listStudies(path):
        return True
    try:
        templateNames = os.listdir(os.path.join(path, TEMPLATES_DIR))
    except OSError:
        templateNames = []
    if any(os.path.isfile(os.path.join(path, TEMPLATES_DIR, name, "template.json")) for name in templateNames):
        return True
    return any(os.path.isdir(os.path.join(path, LNM_DIR, name)) for name in _LNM_APP_FOLDERS)


def isInside(path, folder):
    """True if ``path`` is ``folder`` or inside it (real paths, case-insensitive where the platform is). Worker-safe."""
    if not path or not folder:
        return False
    path = os.path.normcase(os.path.realpath(path))
    folder = os.path.normcase(os.path.realpath(folder))
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


#
# Names (macOS, Windows and Linux)
#

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
#: ".studio-" starts the name part of Studio's own temporary files and folders (``TEMPORARY_MARKERS``, ``COPY_MARKER``,
#: ``MOVE_MARKER``): a name that contained it would be hidden from the Library, deleted at the next start as an interrupted
#: Duplicate, or lose its scene file as a leftover of a save. ``sanitizeName`` writes it with a space instead of the dot.
_INTERNAL_MARKER = re.compile(r"\.(?=studio-)", re.IGNORECASE)


def sanitizeName(name, maxLength=MAX_NAME_LENGTH, fallback=UNTITLED_NAME, maxBytes=MAX_NAME_BYTES):
    r"""A file or folder name valid on macOS, Windows and Linux: no / \ : * ? " < > |, no trailing dot or space, and
    none of Studio's temporary name parts (".studio-copy" is written " studio-copy").

    At most ``maxLength`` characters and ``maxBytes`` UTF-8 bytes (cut on a character boundary: 80 Japanese characters are
    240 bytes). Worker-safe.
    """
    name = _UNSAFE.sub(" ", str(name or ""))
    name = _INTERNAL_MARKER.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name[:maxLength]
    while len(name.encode("utf-8", "replace")) > maxBytes:
        name = name[:-1]
    name = name.rstrip(". ").lstrip(". ")
    if not name:
        return fallback
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name


def splitExtension(fileName):
    """``("Lesions", ".seg.nrrd")``: the base name and the (compound) extension. Worker-safe."""
    lower = fileName.lower()
    for extension in COMPOUND_EXTENSIONS:
        if lower.endswith(extension) and len(fileName) > len(extension):
            return fileName[: -len(extension)], fileName[-len(extension):]
    base, extension = os.path.splitext(fileName)
    return base, extension


def uniqueName(folder, name, reserved=(), isFile=True):
    """``name``, else "name 2", "name 3"... (before the extension of a file), so that nothing in ``folder`` is replaced.

    ``reserved``: names already planned (compared case-insensitively, as on macOS and Windows). Worker-safe.
    """
    reservedLower = {str(item).lower() for item in reserved}
    try:
        existing = {entry.lower() for entry in os.listdir(folder)} if os.path.isdir(folder) else set()
    except OSError:
        existing = set()
    taken = existing | reservedLower
    base, extension = splitExtension(name) if isFile else (name, "")
    candidate = name
    index = 2
    while candidate.lower() in taken:
        candidate = f"{base} {index}{extension}"
        index += 1
    return candidate


def displayName(text):
    """A project name from a scan or file name: "sub-012_T1w.nii.gz" -> "sub-012 T1w"."""
    base, _extension = splitExtension(os.path.basename(str(text or "")))
    base = base.replace("_", " ")
    return sanitizeName(base)


#
# Nodes: which folder, which name, is it the user's data
#


def _role(node):
    return node.GetAttribute("Studio.Role") or ""


def isMapNode(node):
    """A network map or statistic map (Map results, LNM outputs, statistical maps): saved in Maps/."""
    if node is None or not node.IsA("vtkMRMLVolumeNode"):
        return False
    if node.GetAttribute("LNM.Result") == "1" or node.GetAttribute("LNM.Statistic"):
        return True
    return _role(node) == "statmap"


def isTemplateNode(node):
    """A template volume (built-in or the user's own, A-10) or a 3D brain surface of a template: not user data by itself."""
    if node is None:
        return False
    if node.IsA("vtkMRMLModelNode"):
        return _role(node) == "template-surface"
    if not node.IsA("vtkMRMLScalarVolumeNode"):
        return False
    if _role(node) == "template":
        return True
    # A template of a scene saved before the tag existed (as library.volumeKind() and niftiexport.isTemplateNode() judge it).
    # Not every name that starts with "MNI152": the user's own scan (FSL's MNI152_T1_2mm_brain) is user data.
    from StudioLib import space

    return (node.GetName() or "").startswith(space.TEMPLATE_SPACE)


def kindFolder(node):
    """The kind folder of a project where the data of ``node`` is saved."""
    if node.IsA("vtkMRMLVolumeNode"):
        return MAPS if isMapNode(node) else IMAGES
    if node.IsA("vtkMRMLSegmentationNode"):
        return SEGMENTATIONS
    if node.IsA("vtkMRMLMarkupsNode"):
        return MARKUPS
    if node.IsA("vtkMRMLModelNode"):
        return MODELS
    if node.IsA("vtkMRMLTableNode"):
        return TABLES
    if node.IsA("vtkMRMLTransformNode"):
        return TRANSFORMS
    return OTHER


def _storageFile(node):
    storageNode = node.GetStorageNode() if hasattr(node, "GetStorageNode") else None
    fileName = storageNode.GetFileName() if storageNode is not None else None
    return str(fileName) if fileName else None


def _segmentationEmpty(node):
    try:
        from . import draw

        return draw.isLesionEmpty(node)
    except Exception:
        segmentation = node.GetSegmentation()
        return segmentation is None or segmentation.GetNumberOfSegments() == 0


def _isUserNode(node):
    """True if ``node`` holds data of the user: read from a file (a scan, sample data, DICOM), or new or changed (an
    empty drawing and the template are not).
    """
    if not node.GetSaveWithScene() or node.GetHideFromEditors():
        return False
    if not any(node.IsA(name) for name in ("vtkMRMLVolumeNode", "vtkMRMLSegmentationNode", "vtkMRMLMarkupsNode", "vtkMRMLModelNode",
                                            "vtkMRMLTableNode")):
        return False
    if isTemplateNode(node):
        return False
    if not node.GetModifiedSinceRead():
        return bool(_storageFile(node))
    if node.IsA("vtkMRMLSegmentationNode"):
        return not _segmentationEmpty(node)
    if node.IsA("vtkMRMLMarkupsNode"):
        return node.GetNumberOfControlPoints() > 0
    if node.IsA("vtkMRMLTableNode"):
        return node.GetNumberOfRows() > 0
    return True


def sceneHasUserData(scene=None):
    """True if the scene holds data of the user (a scan, drawing, figure, map...): a project is then created."""
    scene = scene or slicer.mrmlScene
    nodes = scene.GetNodesByClass("vtkMRMLStorableNode")
    nodes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
    for index in range(nodes.GetNumberOfItems()):
        if _isUserNode(nodes.GetItemAsObject(index)):
            return True
    return False


def defaultProjectName(scene=None):
    """The name of a new project: the first loaded scan ("sub-012 T1w"), else the scene file, else "Untitled project"."""
    scene = scene or slicer.mrmlScene
    volumes = scene.GetNodesByClass("vtkMRMLScalarVolumeNode")
    volumes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
    for index in range(volumes.GetNumberOfItems()):
        node = volumes.GetItemAsObject(index)
        if node.GetHideFromEditors() or node.IsA("vtkMRMLLabelMapVolumeNode") or isTemplateNode(node) or isMapNode(node):
            continue
        if _role(node) == "figure":
            continue
        fileName = _storageFile(node)
        return displayName(os.path.basename(fileName) if fileName else node.GetName())
    url = scene.GetURL()
    if url:
        return displayName(os.path.basename(url))
    return UNTITLED_NAME


def counts(scene=None):
    """What a project holds, for its card: {"scans", "lesions", "maps", "markups", "tables", "models"}."""
    scene = scene or slicer.mrmlScene
    result = {"scans": 0, "lesions": 0, "maps": 0, "markups": 0, "tables": 0, "models": 0}
    nodes = scene.GetNodesByClass("vtkMRMLStorableNode")
    nodes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
    for index in range(nodes.GetNumberOfItems()):
        node = nodes.GetItemAsObject(index)
        if node.GetHideFromEditors() or not node.GetSaveWithScene():
            continue
        if node.IsA("vtkMRMLVolumeNode"):
            if isMapNode(node):
                result["maps"] += 1
            elif not isTemplateNode(node) and not node.IsA("vtkMRMLLabelMapVolumeNode") and _role(node) != "figure":
                result["scans"] += 1
        elif node.IsA("vtkMRMLSegmentationNode"):
            result["lesions"] += 1
        elif node.IsA("vtkMRMLMarkupsNode"):
            result["markups"] += 1
        elif node.IsA("vtkMRMLTableNode"):
            result["tables"] += 1
        elif node.IsA("vtkMRMLModelNode"):
            result["models"] += 1
    return result


def contentLine(countsValue):
    """"1 scan · 1 lesion" (an empty project: "Empty")."""
    words = (
        ("scans", _("1 scan"), _("{n} scans")),
        ("lesions", _("1 lesion"), _("{n} lesions")),
        ("maps", _("1 map"), _("{n} maps")),
        ("markups", _("1 point list"), _("{n} point lists")),
        ("tables", _("1 table"), _("{n} tables")),
        ("models", _("1 model"), _("{n} models")),
    )
    parts = []
    for key, one, many in words:
        try:
            n = int((countsValue or {}).get(key) or 0)
        except (AttributeError, TypeError, ValueError, OverflowError):
            n = 0  # a damaged project.json
        if n:
            parts.append(one if n == 1 else many.format(n=n))
    return " · ".join(parts) if parts else _("Empty")


#
# Projects on disk
#


def privateFolder(projectPath):
    return os.path.join(projectPath, PRIVATE_DIR)


def projectFile(projectPath):
    return os.path.join(projectPath, PRIVATE_DIR, PROJECT_FILE)


def thumbnailFile(projectPath):
    return os.path.join(projectPath, PRIVATE_DIR, THUMBNAIL_FILE)


def thumbnailViews(projectPath):
    """``{view: path}`` of the thumbnails of a project that exist ("axial", "coronal", "sagittal")."""
    views = {}
    for view, fileName in THUMBNAIL_VIEW_FILES:
        path = os.path.join(projectPath, PRIVATE_DIR, fileName)
        if os.path.isfile(path):
            views[view] = path
    return views


def readJson(path):
    """Read a JSON file; None if it does not exist or cannot be read. Worker-safe."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def readProjectJson(projectPath):
    """The content of the .studio/project.json of a project; None if it does not exist, cannot be read, or is not a
    JSON object (a file damaged by hand, by a sync tool or by another program). Worker-safe.
    """
    data = readJson(projectFile(projectPath))
    return data if isinstance(data, dict) else None


def _validCounts(value):
    """The "counts" of a project.json: ``{kind: number}``, without the entries that are not whole numbers."""
    if not isinstance(value, dict):
        return {}
    return {str(key): number for key, number in value.items() if isinstance(number, int) and not isinstance(number, bool)}


def sceneFile(projectPath):
    """The .mrml of a project folder: "<folder name>.mrml", else the only .mrml in it, else the newest one; None if none."""
    projectPath = str(projectPath)
    expected = os.path.join(projectPath, os.path.basename(os.path.normpath(projectPath)) + SCENE_EXTENSION)
    if os.path.isfile(expected):
        return expected
    try:
        scenes = [os.path.join(projectPath, name) for name in os.listdir(projectPath)
                  if name.lower().endswith(SCENE_EXTENSION) and ".studio-tmp" not in name]
    except OSError:
        return None
    if not scenes:
        return None
    return max(scenes, key=os.path.getmtime)


def projectOfPath(path, rootPath=None):
    """The project folder that holds ``path`` (``<root>/Projects/<name>``), or None."""
    if not path:
        return None
    projects = os.path.normcase(os.path.realpath(projectsFolder(rootPath)))
    real = os.path.normcase(os.path.realpath(str(path)))
    if not real.startswith(projects.rstrip(os.sep) + os.sep):
        return None
    name = real[len(projects.rstrip(os.sep)) + 1:].split(os.sep)[0]
    # The folder with its real spelling
    candidate = os.path.join(projectsFolder(rootPath), name)
    if os.path.isdir(candidate):
        return candidate
    for entry in os.listdir(projectsFolder(rootPath)):
        if os.path.normcase(entry) == name:
            return os.path.join(projectsFolder(rootPath), entry)
    return None


def projectOfSceneFile(path, rootPath=None):
    """The project folder whose own scene file (``sceneFile``) is ``path``, or None.

    Only that scene continues the project in place. Another scene inside a project folder (one saved into a subfolder such
    as Exports/, or a second .mrml next to the project's) is not the project: it opens as a copy like a scene from
    elsewhere, or loading it would replace the project's scene.
    """
    projectPath = projectOfPath(path, rootPath)
    if projectPath is None:
        return None
    scenePath = sceneFile(projectPath)
    if scenePath is None or os.path.normcase(os.path.realpath(scenePath)) != os.path.normcase(os.path.realpath(str(path))):
        return None
    return projectPath


def isProjectFolder(path):
    return bool(path) and os.path.isdir(path) and (os.path.isdir(privateFolder(path)) or sceneFile(path) is not None)


def _isoTime(seconds=None):
    """An ISO 8601 time in UTC ("2026-11-01T05:30:00Z"): unambiguous after a time zone change and in the repeated DST hour."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds if seconds is not None else time.time()))


_ISO_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?")


def parseTime(text):
    """Seconds since the epoch of an ISO time written by ``_isoTime`` (UTC, "Z"), or of one with a UTC offset; a time
    without either (written by earlier versions) is local time. None when ``text`` is not a time.
    """
    try:
        match = _ISO_TIME.match(str(text))
        if match is None:
            return None
        stamp, zone = match.groups()
        parsed = time.strptime(stamp, "%Y-%m-%dT%H:%M:%S")
        if zone is None:
            return time.mktime(parsed)
        seconds = calendar.timegm(parsed)
        if zone != "Z":
            offset = int(zone[1:3]) * 3600 + int(zone[-2:]) * 60
            seconds -= offset if zone[0] == "+" else -offset
        return float(seconds)
    except (TypeError, ValueError, OverflowError):
        return None


class ProjectInfo:
    """A project folder as the Library lists it (read from disk only: nothing is loaded)."""

    def __init__(self, path):
        self.path = os.path.normpath(str(path))
        self.name = os.path.basename(self.path)
        data = readProjectJson(self.path) or {}
        self.data = data
        self.scene = sceneFile(self.path)
        # The later of Studio's last save and the scene file's time (official 3D Slicer does not update project.json)
        modified = parseTime(data.get("modified"))
        try:
            sceneTime = os.path.getmtime(self.scene or self.path)
        except OSError:
            sceneTime = None
        candidates = [value for value in (modified, sceneTime) if value is not None]
        self.modified = max(candidates) if candidates else 0.0
        self.created = parseTime(data.get("created"))
        self.counts = _validCounts(data.get("counts"))
        #: {view: path} of the thumbnails ("axial" is ``thumbnail``)
        self.views = thumbnailViews(self.path)
        self.thumbnail = self.views.get("axial")
        self.state = data.get("state") or "closed"

    def contentLine(self):
        return contentLine(self.counts) if self.counts else (_("Slicer scene") if self.scene else _("Empty"))

    def isCurrent(self):
        current = currentProject()
        return current is not None and os.path.normcase(os.path.realpath(current.path)) == os.path.normcase(os.path.realpath(self.path))

    def __repr__(self):
        return f"ProjectInfo({self.name!r})"


def listProjects(rootPath=None):
    """The projects of the folder, the most recently modified first."""
    folder = projectsFolder(rootPath)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    projects = []
    for name in names:
        if name.startswith(".") or (COPY_MARKER in name and not _hasOwnScene(os.path.join(folder, name))):
            continue  # hidden folders, and a Duplicate in progress (or one that an earlier version left after a failure)
        path = os.path.join(folder, name)
        if os.path.isdir(path):
            projects.append(ProjectInfo(path))
    projects.sort(key=lambda info: info.modified or 0.0, reverse=True)
    return projects


def listStudies(rootPath=None):
    """The study folders of <root>/Studies (folders with a study.json), the most recently modified first."""
    folder = studiesFolder(rootPath)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    studies = [os.path.join(folder, name) for name in names if os.path.isfile(os.path.join(folder, name, "study.json"))]
    studies.sort(key=lambda path: os.path.getmtime(os.path.join(path, "study.json")), reverse=True)
    return studies


def folderSummary(rootPath=None):
    """(number of projects, number of studies) of the folder."""
    return len(listProjects(rootPath)), len(listStudies(rootPath))


#
# Locks
#


#: The project locks that this process holds (``_lockKey``), whichever ``Project`` object holds them
_heldLocks = set()


def _lockPath(projectPath):
    return os.path.join(projectPath, PRIVATE_DIR, LOCK_FILE)


def _lockKey(lockPath):
    return os.path.normcase(os.path.realpath(lockPath))


def _bytesText(value):
    """The text of a QByteArray (or str) that PythonQt returns, with Qt 5 and Qt 6."""
    if isinstance(value, str):
        return value.strip()
    try:
        return bytes(value.data() if hasattr(value, "data") else value).decode("utf-8", "replace").strip()
    except (TypeError, ValueError):
        return str(value).strip()


def _lockLeftByEarlierProcess(lockPath):
    """True if the lock file names this process (its PID, on this host as QLockFile sees it) but this process does not hold it.

    Such a lock was left by an earlier Slicer Studio that had the same PID and was stopped or crashed: each start of the
    web container is a new PID namespace (the same small PID again) with the machine ID of the image, so QLockFile finds
    a running process with that PID and application name (this one) and never counts the lock as stale.
    """
    if _lockKey(lockPath) in _heldLocks:
        return False
    try:
        with open(lockPath, encoding="utf-8", errors="replace") as f:
            lines = [line.strip() for line in f.read().split("\n")]
    except OSError:
        return False
    try:
        if int(lines[0]) != os.getpid():
            return False
    except (ValueError, IndexError):
        return False
    # "This host" as QLockFile decides it: the machine ID when the lock has one, else the host name
    hostName = lines[2] if len(lines) > 2 else ""
    hostId = lines[3] if len(lines) > 3 else ""
    sameHost = not hostName or hostName == str(qt.QSysInfo.machineHostName())
    ownHostId = _bytesText(qt.QSysInfo.machineUniqueId())
    if hostId and ownHostId:
        sameHost = hostId == ownHostId
    return sameHost


def _tryLock(projectPath):
    """Take the lock of the project: the locked QLockFile, or None if another running Slicer Studio holds it."""
    return _tryLockAt(_lockPath(projectPath))


def _tryLockAt(lockPath):
    """Take the lock file ``lockPath``: the locked QLockFile, or None if another running Slicer Studio holds it."""
    lock = qt.QLockFile(lockPath)
    # Never stale by age: a lock is stale only when its process is gone (QLockFile checks the process on this host)
    lock.setStaleLockTime(0)
    if lock.tryLock(0):
        return lock
    if not _lockLeftByEarlierProcess(lockPath):
        return None
    lock.removeStaleLockFile()
    return lock if lock.tryLock(0) else None


def isWritable(projectPath):
    """True if Slicer Studio can save into the project folder: it (and its .studio folder, if any) can be written.

    False on a read-only share or medium, or without write permission. Windows does not use the read-only attribute of
    a folder for writing into it, so a folder there counts as writable (``Project.lock()`` still finds it out).
    """
    if os.name == "nt":
        return True
    private = privateFolder(projectPath)
    for folder in (projectPath, private):
        if os.path.isdir(folder) and not os.access(folder, os.W_OK | os.X_OK):
            return False
    return True


def _readOnlyFileSystem(path):
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except (OSError, AttributeError):
        return False


def isLockedByOther(projectPath):
    """True if another running Slicer Studio has the project open."""
    current = currentProject()
    if current is not None and os.path.normcase(os.path.realpath(current.path)) == os.path.normcase(os.path.realpath(projectPath)):
        return False
    if not os.path.exists(_lockPath(projectPath)):
        return False
    lock = _tryLock(projectPath)
    if lock is None:
        return True
    lock.unlock()
    return False


class Project:
    """An open project: its folder, its scene file and its lock."""

    def __init__(self, path):
        self.path = os.path.normpath(os.path.abspath(str(path)))
        self.name = os.path.basename(self.path)
        self.created = None
        self.lastSaveTime = None
        self.lastError = ""
        #: True when ``lock()`` failed because the folder cannot be written (not because another Slicer Studio holds it)
        self.readOnly = False
        self._lock = None
        self._lockKey = None
        data = readProjectJson(self.path) or {}
        self.created = data.get("created")

    @property
    def scenePath(self):
        """The scene file that Studio writes: "<folder name>.mrml"."""
        return os.path.join(self.path, self.name + SCENE_EXTENSION)

    def folder(self, kind):
        return os.path.join(self.path, kind)

    def relativeName(self):
        """"Projects/<name>" for messages."""
        return f"{PROJECTS_DIR}/{self.name}"

    def lock(self):
        """Take the lock of the project. Return False if another Slicer Studio holds it, or if the folder cannot be
        written (a read-only share or medium): ``readOnly`` is then True and ``lastError`` names the reason.
        """
        if self._lock is not None:
            return True
        self.readOnly = False
        if not isWritable(self.path):
            return self._cannotWrite(os.strerror(errno.EROFS if _readOnlyFileSystem(self.path) else errno.EACCES))
        try:
            os.makedirs(privateFolder(self.path), exist_ok=True)
        except OSError as error:
            return self._cannotWrite(error.strerror or str(error))
        lock = _tryLock(self.path)
        if lock is None:
            if not os.path.lexists(_lockPath(self.path)):
                # No lock file, so nobody holds the lock: the lock file could not be created
                return self._cannotWrite(os.strerror(errno.EROFS))
            return False
        self._lock = lock
        self._lockKey = _lockKey(_lockPath(self.path))
        _heldLocks.add(self._lockKey)
        return True

    def _cannotWrite(self, reason):
        self.readOnly = True
        self.lastError = str(reason)
        return False

    def unlock(self):
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None
            _heldLocks.discard(self._lockKey)
            self._lockKey = None

    def isLocked(self):
        return self._lock is not None

    def jsonData(self, state="open", scene=None, savedAt=None):
        """The content of .studio/project.json (no absolute path, no process id: the lock holds those)."""
        data = readProjectJson(self.path) or {}
        now = _isoTime()
        data.update({
            "format": FORMAT,
            "version": VERSION,
            "name": self.name,
            "created": data.get("created") or self.created or now,
            "state": state,
            "heartbeat": now,
            "application": {"name": slicer.app.applicationName, "version": slicer.app.applicationVersion},
        })
        if savedAt is not None:
            data["modified"] = _isoTime(savedAt)
            data["savedAt"] = _isoTime(savedAt)
        if scene is not None:
            data["counts"] = counts(scene)
        data.setdefault("modified", data["created"])
        self.created = data["created"]
        return data

    def writeJson(self, state="open", scene=None, savedAt=None, executor=None):
        """Write .studio/project.json (in ``executor``, the background writer, when given)."""
        from . import autosave_io

        data = self.jsonData(state, scene, savedAt)
        os.makedirs(privateFolder(self.path), exist_ok=True)
        if executor is not None:
            executor.submit(autosave_io.atomicWriteJson, projectFile(self.path), data)
        else:
            autosave_io.atomicWriteJson(projectFile(self.path), data)
        return data

    def __repr__(self):
        return f"Project({self.name!r})"


def createProject(name=None, rootPath=None):
    """Create ``<root>/Projects/<name>`` (a unique name) with its .studio folder; return the ``Project`` (not locked)."""
    rootPath = ensureRoot(rootPath or root())
    folder = projectsFolder(rootPath)
    os.makedirs(folder, exist_ok=True)
    name = uniqueName(folder, sanitizeName(name or UNTITLED_NAME), isFile=False)
    path = os.path.join(folder, name)
    os.makedirs(privateFolder(path))
    project = Project(path)
    project.writeJson(state="open")
    return project


def pruneRemoved(projectPath, keepDays=REMOVED_KEEP_DAYS):
    """Delete the files of removed nodes older than ``keepDays`` (.studio/removed/<time>/). Worker-safe."""
    folder = os.path.join(projectPath, PRIVATE_DIR, REMOVED_DIR)
    try:
        names = os.listdir(folder)
    except OSError:
        return
    limit = time.time() - keepDays * 86400
    for name in names:
        path = os.path.join(folder, name)
        try:
            if os.path.getmtime(path) < limit:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


#: Name part of the temporary folders and files of a save (``autosave_io.PROJECT_TEMPORARY_PREFIX``, copies in progress)
TEMPORARY_MARKERS = (".studio-tmp-", ".studio-part", ".studio-copy")


def temporaryLeftovers(projectPath):
    """What a save that was interrupted (a crash) left in the project: temporary folders of the kind folders, temporary
    scene files, partial copies. Listed when the project is opened, before its first save (main thread; a few folder
    listings). A temporary folder that the scene file refers to is kept (the next save writes its node into its kind
    folder, then moves it to .studio/removed).
    """
    try:
        top = os.listdir(projectPath)
    except OSError:
        return []
    candidates = [os.path.join(projectPath, name) for name in top]
    for name in top:
        folder = os.path.join(projectPath, name)
        if name != PRIVATE_DIR and not name.startswith(".") and os.path.isdir(folder):
            try:
                candidates.extend(os.path.join(folder, entry) for entry in os.listdir(folder))
            except OSError:
                pass
    # The project's own scene file is never a leftover, even when the project's name contains a marker (a name given
    # before ``sanitizeName`` reserved them)
    ownScenes = {os.path.normcase(os.path.join(projectPath, os.path.basename(os.path.normpath(projectPath)) + SCENE_EXTENSION))}
    if sceneFile(projectPath):
        ownScenes.add(os.path.normcase(sceneFile(projectPath)))
    candidates = [path for path in candidates if any(marker in os.path.basename(path) for marker in TEMPORARY_MARKERS)
                  and os.path.normcase(path) not in ownScenes]
    if not candidates:
        return []
    try:
        with open(sceneFile(projectPath), encoding="utf-8", errors="replace") as f:
            sceneText = f.read()
    except (OSError, TypeError):
        sceneText = ""
    return [path for path in candidates if os.path.basename(path) not in sceneText]


def removePaths(paths):
    """Delete files and folders (no error if one is gone). Worker-safe."""
    for path in paths:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError:
            pass


#
# The open project (set by the autosave service)
#


def currentProject():
    """The project the scene is saved in, or None."""
    return _current


def setCurrentProject(project):
    """Called by the autosave service when it attaches or releases a project."""
    global _current
    _current = project
    _notify()


def addListener(callback):
    """``callback()`` when the open project changes, is renamed or saved, or the folder changes."""
    if callback not in _listeners:
        _listeners.append(callback)


def removeListener(callback):
    while callback in _listeners:
        _listeners.remove(callback)


def _notify():
    for callback in list(_listeners):
        try:
            callback()
        except Exception:
            import traceback

            logging.error(f"Slicer Studio projects: listener failed:\n{traceback.format_exc()}")


def notifyChanged():
    _notify()


def exportsFolder(create=True):
    """Exports/ of the open project (created when asked), or None without a project."""
    project = currentProject()
    if project is None:
        return None
    folder = project.folder(EXPORTS)
    if create:
        os.makedirs(folder, exist_ok=True)
    return folder


def lesionsFolder(create=False):
    """Lesions/ of the open project, or None without a project."""
    project = currentProject()
    if project is None:
        return None
    folder = project.folder(LESIONS)
    if create:
        os.makedirs(folder, exist_ok=True)
    return folder


#
# File operations (Library)
#


class ProjectError(Exception):
    """A project operation that cannot be done (the message is shown to the user)."""


def isSamePath(first, second):
    """True when both paths are the same folder or file, also when only the letter case differs on a case-insensitive file system
    (APFS and HFS+ on macOS: ``normcase`` does not fold case there, so only the file system can tell).
    """
    try:
        return os.path.samefile(first, second)
    except OSError:
        return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def renameFolder(path, newName):
    """Rename a project folder (not the open one: see ``AutosaveService.renameProject``) and its scene file.

    Return the new folder. Raise ProjectError when the name is taken or another Slicer Studio has the project open.
    """
    newName = sanitizeName(newName)
    parent = os.path.dirname(os.path.normpath(path))
    if newName == os.path.basename(os.path.normpath(path)):
        return path
    if isLockedByOther(path):
        raise ProjectError(_("{name} is open in another Slicer Studio.").format(name=os.path.basename(path)))
    newPath = os.path.join(parent, newName)
    if os.path.exists(newPath) and not isSamePath(newPath, path):
        raise ProjectError(_("A project named {name} already exists.").format(name=newName))
    scene = sceneFile(path)
    try:
        os.rename(path, newPath)
    except OSError as error:
        from .autosave import plainOSErrorIn

        raise ProjectError(_("The project could not be renamed: {error}").format(error=plainOSErrorIn(error, parent))) from error
    renameSceneFile(newPath, scene)
    data = readProjectJson(newPath)
    if data is not None:
        from . import autosave_io

        data["name"] = newName
        autosave_io.atomicWriteJson(projectFile(newPath), data)
    return newPath


def renameSceneFile(projectPath, oldScene):
    """Rename <old name>.mrml (and its <old name>_Private folder) to <folder name>.mrml after the folder was renamed."""
    if not oldScene:
        return None
    name = os.path.basename(os.path.normpath(projectPath))
    oldBase = os.path.basename(oldScene)
    oldPath = os.path.join(projectPath, oldBase)
    newPath = os.path.join(projectPath, name + SCENE_EXTENSION)
    if os.path.isfile(oldPath) and oldPath != newPath and (not os.path.exists(newPath) or isSamePath(oldPath, newPath)):
        os.rename(oldPath, newPath)
        # vtkMRMLScene keeps hidden nodes in "<scene file name up to the first dot>_Private": follow the new name
        oldPrivate = os.path.join(projectPath, oldBase.split(".")[0] + "_Private")
        newPrivate = os.path.join(projectPath, name.split(".")[0] + "_Private")
        if os.path.isdir(oldPrivate) and oldPrivate != newPrivate and (not os.path.exists(newPrivate) or isSamePath(oldPrivate, newPrivate)):
            os.rename(oldPrivate, newPrivate)
            _rewritePrivatePaths(newPath, os.path.basename(oldPrivate), os.path.basename(newPrivate))
    return newPath


def adoptRenamedScene(projectPath):
    """Follow a project folder that was renamed outside Slicer Studio (file manager, copy and rename).

    A Studio project folder (it has ``.studio/project.json``) without "<folder name>.mrml" but with exactly one other scene
    file gets that scene renamed like ``renameFolder`` does, and its ``project.json`` name updated. Otherwise the old-named
    scene would stay next to the one that every save writes, and official 3D Slicer would open the outdated one. A folder with
    several scenes is left alone (which one is the project's is not known). Return the scene file of the project (as
    ``sceneFile``); it never raises.
    """
    projectPath = os.path.normpath(str(projectPath))
    try:
        expected = os.path.join(projectPath, os.path.basename(projectPath) + SCENE_EXTENSION)
        if not os.path.isfile(expected) and os.path.isfile(projectFile(projectPath)) and not isLockedByOther(projectPath):
            scenes = [name for name in os.listdir(projectPath)
                      if name.lower().endswith(SCENE_EXTENSION) and ".studio-tmp" not in name
                      and os.path.isfile(os.path.join(projectPath, name))]
            if len(scenes) == 1:
                renameSceneFile(projectPath, os.path.join(projectPath, scenes[0]))
                data = readProjectJson(projectPath)
                if data is not None and data.get("name") != os.path.basename(projectPath):
                    from . import autosave_io

                    data["name"] = os.path.basename(projectPath)
                    autosave_io.atomicWriteJson(projectFile(projectPath), data)
    except Exception:
        logging.warning(f"Slicer Studio: cannot rename the scene of {projectPath} to the folder name", exc_info=True)
    return sceneFile(projectPath)


def _rewritePrivatePaths(scenePath, oldFolder, newFolder):
    """Replace the folder of hidden nodes in the file names of a scene file (relative paths only)."""
    from . import autosave_io

    try:
        with open(scenePath, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return
    # MRML writes file names URL-encoded and then XML-escaped ("&" is not URL-encoded; it is written as "&amp;")
    encodedOld, encodedNew = (autosave_io.urlEncode(folder).replace("&", "&amp;") for folder in (oldFolder, newFolder))
    updated = re.sub(r'(fileName="|fileListMember\d+=")' + re.escape(encodedOld) + "/", lambda m: m.group(1) + encodedNew + "/", text)
    if updated != text:
        autosave_io.atomicWriteText(scenePath, updated)


#: Name part of the folder that a Duplicate copies into before it gets the name of the copy (never listed as a project)
COPY_MARKER = ".studio-copy"
#: End of the name of the lock file of a copy that a Duplicate is making (``_copyLockPath``)
COPY_LOCK_SUFFIX = ".lock"

#: The copies of Duplicates that are queued or running in this process ({(normcase(parent folder), name)}): their names
#: are taken, so a second Duplicate before the first one finished makes "<name> copy 2", not a second "<name> copy"
_pendingCopies = set()
_pendingCopiesLock = threading.Lock()


def _reserveCopyName(parent, name):
    """A name for a new folder in ``parent`` that neither exists nor is reserved by a Duplicate in progress; reserve it."""
    key = os.path.normcase(os.path.abspath(parent))
    with _pendingCopiesLock:
        reserved = [entry for folder, entry in _pendingCopies if folder == key]
        newName = uniqueName(parent, name, reserved=reserved, isFile=False)
        _pendingCopies.add((key, newName))
    return newName


def _releaseCopyName(parent, name):
    with _pendingCopiesLock:
        _pendingCopies.discard((os.path.normcase(os.path.abspath(parent)), name))


def _copyLockPath(temporary):
    """The lock file of the copy of a Duplicate: ".<name>.studio-copy.lock" next to the copy (hidden, never listed).

    It is made and locked before the copy folder, and deleted after the copy got its name, so a copy folder without a held
    lock is never one that a Duplicate is still making, in this Slicer Studio or another one on the same folder.
    """
    temporary = os.path.normpath(temporary)
    return os.path.join(os.path.dirname(temporary), "." + os.path.basename(temporary) + COPY_LOCK_SUFFIX)


def _lockCopy(lockPath, create=True):
    """Lock the lock file of a copy (non-blocking; the system releases it when the process ends, even when it is killed).

    Return ``(descriptor, busy)``: the open descriptor of the locked file (``_unlockCopy`` releases it), or None with busy
    True when a running Duplicate (of any process) holds it, or busy False when there is no such file (``create`` False)
    or it cannot be opened. A file system without locks gives the descriptor unlocked (``create``) or busy (a check: a
    copy is never deleted when that cannot be known). Worker-safe (no Qt).
    """
    for _attempt in range(5):
        try:
            descriptor = os.open(lockPath, os.O_RDWR | (os.O_CREAT if create else 0), 0o644)
        except OSError:
            return None, False
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            contended = os.name == "nt" or error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES)
            if create and not contended:
                return descriptor, False  # no locks on this file system: copy without one
            os.close(descriptor)
            return None, True
        # Locked: unless the file was deleted (or replaced) by the Duplicate or the clean-up that held it before
        try:
            same = os.path.samestat(os.fstat(descriptor), os.stat(lockPath))
        except OSError:
            same = False
        if same:
            return descriptor, False
        os.close(descriptor)
        if not create:
            return None, False
    return None, True


def _unlockCopy(descriptor, lockPath):
    """Delete the lock file of a copy and release the lock (``_lockCopy``). Worker-safe."""
    if os.name == "nt" and descriptor is not None:
        os.close(descriptor)  # an open file cannot be deleted on Windows
        descriptor = None
    with contextlib.suppress(OSError):
        os.remove(lockPath)
    if descriptor is not None:
        os.close(descriptor)


def isCopyLeftover(name):
    """True for the name of a copy that a Duplicate made and was interrupted before it renamed it ("<name>.studio-copy")."""
    return name.endswith(COPY_MARKER) and len(name) > len(COPY_MARKER)


def _hasOwnScene(path):
    """True if the folder has "<folder name>.mrml": a project, not the copy of a Duplicate (its scene file keeps the name of
    the project that it copies until the copy is renamed).
    """
    name = os.path.basename(os.path.normpath(path))
    return os.path.isfile(os.path.join(path, name + SCENE_EXTENSION))


def _onlyTemporaryFiles(path):
    """True if ``path`` is a file or folder of temporary names only (a folder: every file in it has one)."""
    if any(marker in os.path.basename(path) for marker in TEMPORARY_MARKERS):
        return True
    if os.path.islink(path) or not os.path.isdir(path):
        return False
    try:
        entries = os.listdir(path)
    except OSError:
        return False
    return all(_onlyTemporaryFiles(os.path.join(path, entry)) for entry in entries)


def isNeverSaved(projectPath):
    """True if the project folder never got its first save: a .studio/project.json without "savedAt", no scene file, and
    nothing else in the folder but temporary files of the interrupted save (the folder of a project is created when its
    first scan is loaded, and the first scene is written seconds later: a kill, or a full disk, in between left it).
    Such a folder holds nothing of the user's, and it cannot be opened. Worker-safe.
    """
    projectPath = str(projectPath)
    if not os.path.isfile(projectFile(projectPath)):
        return False  # not a folder that Studio made (or one that was emptied by hand): never touched
    data = readProjectJson(projectPath)
    if data is not None and data.get("savedAt"):
        return False
    if sceneFile(projectPath) is not None:
        return False
    try:
        names = os.listdir(projectPath)
    except OSError:
        return False
    return all(name == PRIVATE_DIR or _onlyTemporaryFiles(os.path.join(projectPath, name)) for name in names)


def removeUnsavedProjects(rootPath=None):
    """Delete the project folders that never got a first save (``isNeverSaved``) and are not open anywhere: they were left
    by a Slicer Studio that was killed or ran out of disk space before its first save, and the Library lists them as an
    "Empty" project that cannot be opened. Return the names that were deleted.
    """
    folder = projectsFolder(rootPath)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    removed = []
    for name in names:
        path = os.path.join(folder, name)
        if name.startswith(".") or not os.path.isdir(path) or os.path.islink(path) or not isNeverSaved(path):
            continue
        current = currentProject()
        if current is not None and os.path.normcase(os.path.realpath(current.path)) == os.path.normcase(os.path.realpath(path)):
            continue
        if isLockedByOther(path):
            continue
        removePaths([path])
        if not os.path.exists(path):
            removed.append(name)
    if removed:
        notifyChanged()
    return removed


def removeCopyLeftovers(rootPath=None):
    """Delete the partial copies of Duplicates that were interrupted (a crash, a kill, a power loss) in Projects/.

    A Duplicate copies into "<new name>.studio-copy" and renames it when it is complete, so what stays under that name
    was never complete: it is not a project (``listProjects`` hides it) and nothing else would remove it, or tell about it,
    while it takes up as much disk space as the project. The copies of the Duplicates of this process that are queued or
    running (``_pendingCopies``), and those that a Duplicate of another Slicer Studio on the same folder is making (their
    lock file is held, ``_copyLockPath``), are kept. Return the names that were deleted. Worker-safe.
    """
    folder = projectsFolder(rootPath)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    key = os.path.normcase(os.path.abspath(folder))
    with _pendingCopiesLock:
        reserved = {entry + COPY_MARKER for parent, entry in _pendingCopies if parent == key}
    # A folder with "<its name>.mrml" is a project whose name ends with the marker (named before ``sanitizeName`` reserved it)
    candidates = [name for name in names if isCopyLeftover(name) and name not in reserved
                  and not _hasOwnScene(os.path.join(folder, name))]
    # Lock files of copies that are gone (a Duplicate killed after it renamed its copy)
    for name in names:
        copyName = name[1:-len(COPY_LOCK_SUFFIX)]
        if name.startswith(".") and name.endswith(COPY_LOCK_SUFFIX) and isCopyLeftover(copyName) and copyName not in names:
            candidates.append(copyName)
    leftovers = []
    for name in candidates:
        path = os.path.join(folder, name)
        lockPath = _copyLockPath(path)
        descriptor, busy = _lockCopy(lockPath, create=False)
        if busy:
            continue  # a Duplicate of another Slicer Studio is making this copy
        try:
            if os.path.lexists(path):
                removePaths([path])
                leftovers.append(name)
        finally:
            _unlockCopy(descriptor, lockPath)
    return leftovers


def _firstCopyFailure(error):
    """(errno, reason without paths) of a failed copy: its first reason (``shutil.Error`` lists every file that failed)."""
    number = error.errno
    reason = error.strerror or str(error)
    if isinstance(error, shutil.Error) and error.args and isinstance(error.args[0], list) and error.args[0]:
        first = error.args[0][0]
        if isinstance(first, tuple) and len(first) == 3:
            reason = str(first[2])  # "[Errno 28] No space left on device: 'source' -> 'destination'"
            match = re.match(r"\[Errno (\d+)\]", reason)
            if match:
                number = int(match.group(1))
                reason = os.strerror(number)
    return number, reason


def duplicateFolder(path, onDone=None, onError=None):
    """Copy a project folder to "<name> copy" (in the background writer; the lock is not copied). Return the new folder.

    The copy is made in "<new name>.studio-copy" and renamed when it is complete; a copy that fails is deleted, and
    ``onError`` gets a ``ProjectError`` that says why in plain words (no paths). ``onDone`` gets the new folder.
    """
    from . import autosave_io

    name = os.path.basename(os.path.normpath(path))
    parent = os.path.dirname(os.path.normpath(path))
    newName = _reserveCopyName(parent, sanitizeName(_("{name} copy").format(name=name)))
    newPath = os.path.join(parent, newName)
    scene = sceneFile(path)
    # Translated here: ``copy`` runs in the background writer
    notCopied = _("The project could not be duplicated: {reason}")
    diskFull, gone, missing = _("not enough disk space"), _("{name} is gone").format(name=name), _("a file of the project is missing")

    def copy():
        target, targetName = newPath, newName
        temporary = newPath + COPY_MARKER
        # Lock the copy before its folder exists, so that the startup of another Slicer Studio on the same folder
        # (``removeCopyLeftovers``) never takes it for the leftover of an interrupted Duplicate; a copy of the same name
        # that another Slicer Studio is making keeps its folder, and this one copies into another
        lockPath = _copyLockPath(temporary)
        descriptor, busy = _lockCopy(lockPath)
        index = 2
        while busy and index < 100:
            temporary = f"{newPath} {index}{COPY_MARKER}"
            lockPath = _copyLockPath(temporary)
            descriptor, busy = _lockCopy(lockPath)
            index += 1
        if busy:
            _releaseCopyName(parent, newName)
            raise ProjectError(notCopied.format(reason=os.strerror(errno.EBUSY)))
        try:
            # Left by a Duplicate that was interrupted (a crash): the name is reserved by this one now
            if os.path.lexists(temporary):
                removePaths([temporary])
            shutil.copytree(path, temporary, ignore=shutil.ignore_patterns(LOCK_FILE, "*.studio-tmp*", ".studio-tmp-*", REMOVED_DIR))
            # Only a complete copy gets the name of the copy: its scene and project.json are there (they would be missing
            # if its folder was deleted while it was copied)
            for required in [os.path.join(PRIVATE_DIR, PROJECT_FILE)] + ([os.path.basename(scene)] if scene else []):
                if os.path.isfile(os.path.join(path, required)) and not os.path.isfile(os.path.join(temporary, required)):
                    raise OSError(errno.ENOENT, os.strerror(errno.ENOENT), required)
            if os.path.lexists(target):
                # Made meanwhile by another program (another Slicer Studio): take the next free name
                targetName = uniqueName(parent, newName, isFile=False)
                target = os.path.join(parent, targetName)
            os.rename(temporary, target)
        except OSError as error:
            removePaths([temporary])
            _unlockCopy(descriptor, lockPath)
            number, reason = _firstCopyFailure(error)
            if number in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
                reason = diskFull
            elif number == errno.ENOENT:
                reason = missing if os.path.isdir(path) else gone
            raise ProjectError(notCopied.format(reason=reason)) from error
        except BaseException:
            removePaths([temporary])
            _unlockCopy(descriptor, lockPath)
            raise
        finally:
            _releaseCopyName(parent, newName)
        _unlockCopy(descriptor, lockPath)
        renameSceneFile(target, scene)
        data = readProjectJson(target) or {}
        data.update(name=targetName, state="closed", created=_isoTime(), modified=_isoTime())
        autosave_io.atomicWriteJson(projectFile(target), data)
        return target

    autosave_io.backgroundWriter().submit(copy, onDone=onDone, onError=onError)
    return newPath


def moveToTrash(path):
    """Move a folder to the trash of the system (QFile.moveToTrash, Qt 5.15 and later). Return True if it was moved."""
    if not hasattr(qt.QFile, "moveToTrash"):
        return False
    if sys.platform.startswith("linux"):
        # Qt puts the trash in <data folder>/Trash; when the data folder (~/.local/share) does not exist yet it cannot make
        # that folder and moves the file into files/ and info/ of the current folder instead
        dataFolder = qt.QStandardPaths.writableLocation(qt.QStandardPaths.GenericDataLocation)
        if dataFolder:
            with contextlib.suppress(OSError):
                os.makedirs(dataFolder, mode=0o700, exist_ok=True)
    try:
        # The instance method: PythonQt does not expose the static QFile.moveToTrash(fileName)
        result = qt.QFile(path).moveToTrash()
    except Exception:
        logging.warning(f"Slicer Studio: cannot move {path} to the trash")
        return False
    if isinstance(result, tuple):
        result = result[0]
    return bool(result) and not os.path.exists(path)


def trashFolder(path, confirmDelete=None):
    """Move a project to the trash; where there is no trash, delete it after ``confirmDelete()`` returns True.

    Return True if the project is gone. Raise ProjectError for the open project or one open elsewhere.
    """
    current = currentProject()
    if current is not None and os.path.normcase(os.path.realpath(current.path)) == os.path.normcase(os.path.realpath(path)):
        raise ProjectError(_("{name} is open. Close it first (New project).").format(name=os.path.basename(path)))
    if isLockedByOther(path):
        raise ProjectError(_("{name} is open in another Slicer Studio.").format(name=os.path.basename(path)))
    if moveToTrash(path):
        return True
    if confirmDelete is not None and not confirmDelete():
        return False
    shutil.rmtree(path)
    return not os.path.exists(path)


def treeSize(path):
    """Total size of the files of a folder (bytes). Worker-safe."""
    total = 0
    for directory, _folders, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(directory, name))
            except OSError:
                pass
    return total


#
# Moving the folder
#


#: The folders of a Slicer Studio folder that a move takes along (merged into the same folder of the new one)
_MOVABLE_FOLDERS = (PROJECTS_DIR, STUDIES_DIR, LNM_DIR, TEMPLATES_DIR)
#: The reference-map library in LNM/ (``LesionNetworkMappingLib.refmaps.REGISTRY_DIR``: a registry file and its files)
LNM_REFERENCE_MAPS_DIR = "ReferenceMaps"
#: The folders of LNM/ (the data directory) that the Map module finds by their name there: reference maps, normalization
#: results (``LNM.Normalization`` is "normalization/<scan>/..."), control sets and connectomes. A move into a folder whose
#: LNM/ has them too merges each of them (an item inside is renamed only when its name is taken there)
_LNM_APP_FOLDERS = (LNM_REFERENCE_MAPS_DIR, "normalization", "ControlSets", "connectomes", "Connectomes")


def movableItems(rootPath):
    """The items of a Slicer Studio folder that "Move your projects, studies and templates" moves (those that exist)."""
    items = [name for name in (*_MOVABLE_FOLDERS, ABOUT_FILE) if os.path.exists(os.path.join(rootPath, name))]
    return items


def _hasVisibleEntries(folder):
    try:
        return any(not name.startswith(".") for name in os.listdir(folder))
    except OSError:
        return False


def hasContent(rootPath):
    """True if the folder holds projects, studies, templates or LNM/ files (what a move would take along)."""
    if listProjects(rootPath) or listStudies(rootPath):
        return True
    return any(_hasVisibleEntries(os.path.join(rootPath, name)) for name in (LNM_DIR, TEMPLATES_DIR))


def _sameVolume(first, second):
    try:
        existing = second
        while not os.path.exists(existing):
            parent = os.path.dirname(existing)
            if parent == existing:
                break
            existing = parent
        return os.stat(first).st_dev == os.stat(existing).st_dev
    except OSError:
        return False


def _copyError(source, error):
    """A short OSError for a failed copy of ``source``: its first reason (``shutil.Error`` lists every file that failed)."""
    number, reason = _firstCopyFailure(error)
    result = OSError(_("Copying {name} failed: {reason}").format(name=os.path.basename(source), reason=reason))
    result.errno = number
    return result


#: Name part of the hidden copy (".<name>.studio-move") that a move to another volume makes next to the destination and
#: renames to its name once it is complete and checked: a move that is interrupted (a crash, a kill) never leaves a partial
#: copy under the real name, and the next move there deletes what it left (``_removeMoveLeftovers``)
MOVE_MARKER = ".studio-move"
#: Name part of the hidden name (".<name>.studio-moved") that the original of a folder copied to another volume gets
#: before it is deleted: a delete that is interrupted never leaves half an original under its name (the next move would
#: move it as "<name> 2" next to its complete copy)
MOVED_MARKER = ".studio-moved"


def _hiddenName(path, marker):
    return os.path.join(os.path.dirname(path), "." + os.path.basename(path) + marker)


def _moveTemporary(destination):
    """The hidden name next to ``destination`` that a copy for a move goes to until it is complete."""
    return _hiddenName(destination, MOVE_MARKER)


def isMoveLeftover(name):
    """True for what an interrupted move left: a partial copy, or an original whose complete copy is in the new folder."""
    return name.startswith(".") and name.endswith((MOVE_MARKER, MOVED_MARKER))


def _deleteCopiedFolder(source):
    """Delete the original of a folder whose copy is complete: first hide it under another name, then delete it."""
    hidden = _hiddenName(source, MOVED_MARKER)
    try:
        if os.path.lexists(hidden):
            removePaths([hidden])
        os.rename(source, hidden)
    except OSError:
        hidden = source
    shutil.rmtree(hidden)


def _removeMoveLeftovers(folder):
    """Delete what an interrupted move left in ``folder`` (hidden, never listed). Worker-safe."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    removePaths([os.path.join(folder, name) for name in names if isMoveLeftover(name)])


def _mergeMove(source, destination, sameVolume, progress=None, renamed=None):
    """Move ``source`` (a file or folder) to ``destination``: a rename on the same volume, else copy, check the sizes and
    delete the original. When ``source`` and ``destination`` are both folders (a kind folder such as Projects/, as
    ``moveContent`` passes them) their items are merged: an item whose name exists there moves under another name (" 2"),
    whatever its name, so a project, study or folder is never merged into another one of the same name (a project named
    "LNM" or "Studies" is a project like "MRHead"). Worker-safe.

    A copy is made under a hidden name (``MOVE_MARKER``) and gets its name only once it is complete and checked, so a
    move that is interrupted (a crash during the copy) leaves no partial copy under the real name: a retry would keep
    that one and move the complete original as " 2". Only then is the original hidden (``MOVED_MARKER``) and deleted,
    so an interrupted delete leaves no half original under its name either. What an interrupted move left (hidden) is
    deleted by the next move.

    ``renamed`` (a dict) receives ``{source: destination}`` for each item that moved under another name (" 2") because
    its name was taken there.
    """
    if os.path.isdir(source) and os.path.isdir(destination):
        _removeMoveLeftovers(destination)
        for name in os.listdir(source):
            if isMoveLeftover(name):
                removePaths([os.path.join(source, name)])  # what an interrupted move left here (copied elsewhere, or partial)
                continue
            target = os.path.join(destination, name)
            if os.path.exists(target):
                target = os.path.join(destination, uniqueName(destination, name, isFile=os.path.isfile(os.path.join(source, name))))
                if renamed is not None:
                    renamed[os.path.join(source, name)] = target
            _mergeMove(os.path.join(source, name), target, sameVolume, progress, renamed)
        try:
            os.rmdir(source)
        except OSError:
            pass
        return
    if os.path.exists(destination):
        if os.path.isfile(source) and os.path.basename(source) == ABOUT_FILE:
            os.remove(source)  # the new folder has its own
            return
        destination = os.path.join(os.path.dirname(destination), uniqueName(os.path.dirname(destination), os.path.basename(destination),
                                                                            isFile=os.path.isfile(source)))
        if renamed is not None:
            renamed[source] = destination  # where it really goes (replaces what the caller may have recorded)
    if sameVolume:
        try:
            os.rename(source, destination)
        except OSError as error:
            # The same file system under two mount points (a bind mount, a Docker volume and a "-v" folder) has one
            # st_dev, but rename(2) refuses to cross mount points: copy, check and delete instead
            if error.errno != errno.EXDEV:
                raise
        else:
            if progress:
                progress(os.path.basename(source))
            return
    temporary = _moveTemporary(destination)
    if os.path.lexists(temporary):
        removePaths([temporary])  # left by a move that was interrupted
    if os.path.isdir(source):
        try:
            shutil.copytree(source, temporary)
        except OSError as error:
            # copytree goes on after a file that fails (a full disk) and leaves a partial copy: never keep it, the
            # original stays the only copy
            shutil.rmtree(temporary, ignore_errors=True)
            raise _copyError(source, error) from error
        if treeSize(source) != treeSize(temporary):
            shutil.rmtree(temporary, ignore_errors=True)
            raise OSError(_("Copying {name} failed: the sizes differ").format(name=os.path.basename(source)))
        try:
            os.rename(temporary, destination)
        except OSError as error:
            shutil.rmtree(temporary, ignore_errors=True)
            raise _copyError(source, error) from error
        _deleteCopiedFolder(source)
    else:
        try:
            shutil.copy2(source, temporary)
        except OSError as error:
            removePaths([temporary])
            raise _copyError(source, error) from error
        if os.path.getsize(source) != os.path.getsize(temporary):
            removePaths([temporary])
            raise OSError(_("Copying {name} failed: the sizes differ").format(name=os.path.basename(source)))
        try:
            os.rename(temporary, destination)
        except OSError as error:
            removePaths([temporary])
            raise _copyError(source, error) from error
        os.remove(source)
    if progress:
        progress(os.path.basename(source))


def _mergeReferenceMaps(source, destination, sameVolume, progress=None, renamed=None):
    """Merge the reference-map library ``source`` into the library ``destination`` (``refmaps.merge_into``: both
    libraries' maps stay listed, their files and fetch records follow them). Return False, with nothing moved, when that
    cannot be done (no Map module, or a registry that cannot be read): the caller then moves the folder under another
    name, as any other item. Worker-safe (files only).
    """
    try:
        from LesionNetworkMappingLib import refmaps
    except ImportError:
        return False

    def move(item, target):
        _mergeMove(item, target, sameVolume, progress, renamed)

    try:
        moved = refmaps.merge_into(source, destination, move)
    except refmaps.RefmapsError:
        logging.warning("Slicer Studio: the reference-map libraries could not be merged", exc_info=True)
        return False
    if renamed is not None:
        renamed.update(moved)
    return True


def _mergeLnm(source, destination, sameVolume, progress=None, renamed=None):
    """Move LNM/ (the data directory of the Map module) to ``destination``: its app folders (``_LNM_APP_FOLDERS``) are
    merged into the ones there, whose names the Map module looks for; any other item whose name is taken moves under
    another name (" 2"), as in ``_mergeMove``. Worker-safe.
    """
    if not (os.path.isdir(source) and os.path.isdir(destination)):
        _mergeMove(source, destination, sameVolume, progress, renamed)
        return
    _removeMoveLeftovers(destination)
    for name in os.listdir(source):
        item = os.path.join(source, name)
        if isMoveLeftover(name):
            removePaths([item])
            continue
        target = os.path.join(destination, name)
        if name in _LNM_APP_FOLDERS and os.path.isdir(item) and os.path.isdir(target):
            if name != LNM_REFERENCE_MAPS_DIR:
                _mergeMove(item, target, sameVolume, progress, renamed)  # its items, " 2" when the name is taken
                continue
            if _mergeReferenceMaps(item, target, sameVolume, progress, renamed):
                continue
        if os.path.exists(target):
            target = os.path.join(destination, uniqueName(destination, name, isFile=os.path.isfile(item)))
            if renamed is not None:
                renamed[item] = target
        _mergeMove(item, target, sameVolume, progress, renamed)
    try:
        os.rmdir(source)
    except OSError:
        pass


def checkMoveTarget(oldRoot, newRoot):
    """Raise OSError (a plain message, nothing created) when the content of ``oldRoot`` cannot be moved to ``newRoot``.

    A new folder inside the old one (inside a project, or inside Projects/) would have to receive the folder that holds
    it. Worker-safe.
    """
    if newRoot and oldRoot and os.path.normcase(os.path.realpath(oldRoot)) != os.path.normcase(os.path.realpath(newRoot)) and isInside(newRoot, oldRoot):
        raise OSError(_("The new folder is inside the current Slicer Studio folder, so your projects cannot be moved into it. "
                        "Choose a folder outside {folder}.").format(folder=oldRoot))


class OpenElsewhereError(OSError):
    """The folder cannot be moved: another running Slicer Studio has one of its projects or studies open."""


class MoveRunningElsewhereError(OpenElsewhereError):
    """The folder cannot be moved, or its interrupted move finished: another running Slicer Studio is moving it now."""


def projectsOpenElsewhere(rootPath=None):
    """The names of the projects of the folder that another running Slicer Studio has open (main thread: Qt lock files)."""
    folder = projectsFolder(rootPath)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [name for name in names
            if not name.startswith(".") and os.path.isdir(os.path.join(folder, name)) and isLockedByOther(os.path.join(folder, name))]


#: How deep below Studies/ a study folder is looked for (a study may be in a subfolder of Studies/)
_STUDY_SEARCH_DEPTH = 3


def studiesOpenElsewhere(rootPath=None):
    """The studies of the folder (paths relative to Studies/, "/" separated) that another running Slicer Studio has open.

    An open study writes a heartbeat into its ``.studio/session.json`` (``study_ui``); a live heartbeat of another
    process is what ``study_ui._liveOtherSession`` recognises (the same check that warns about a study open in two
    windows). Main thread.
    """
    folder = studiesFolder(rootPath)
    if not os.path.isdir(folder):
        return []
    try:
        from . import study_ui
    except ImportError:
        return []
    names = []
    for current, subfolders, files in os.walk(folder):
        relative = os.path.relpath(current, folder)
        depth = 0 if relative == os.curdir else relative.count(os.sep) + 1
        if depth > 0 and "study.json" in files:
            subfolders[:] = []  # a study holds no other study
            try:
                with open(os.path.join(current, PRIVATE_DIR, study_ui.SESSION_FILE), encoding="utf-8") as f:
                    record = json.load(f)
            except (OSError, ValueError):
                continue
            if study_ui._liveOtherSession(record) is not None:
                names.append(relative.replace(os.sep, "/"))
            continue
        subfolders[:] = [name for name in subfolders if not name.startswith(".")] if depth < _STUDY_SEARCH_DEPTH else []
    return sorted(names)


def checkNotOpenElsewhere(rootPath):
    """Raise OpenElsewhereError (a plain message that names them) when another running Slicer Studio has a project or a
    study of the folder open: moving Projects/ would move that project, lock included, away from under it, and that
    window would go on saving into the old folder; moving Studies/ would move a study away from under the window that
    traces it (its journal, participants and heartbeat would re-create a half study in the old folder, and its lesions
    could no longer be written). A move of the folder that another running Slicer Studio is doing now raises
    MoveRunningElsewhereError (a subclass): the projects it moves are not locked while they move.
    """
    checkMoveNotRunningElsewhere()
    names = projectsOpenElsewhere(rootPath)
    if len(names) == 1:
        raise OpenElsewhereError(_("{name} is open in another Slicer Studio. Close it there first, then change the folder.").format(name=names[0]))
    if names:
        raise OpenElsewhereError(_("{names} are open in another Slicer Studio. Close them there first, then change the folder.").format(
            names=", ".join(names)))
    studies = studiesOpenElsewhere(rootPath)
    if len(studies) == 1:
        raise OpenElsewhereError(_("The study {name} is open in another Slicer Studio. Close it there first, then change the folder.").format(
            name=studies[0]))
    if studies:
        raise OpenElsewhereError(_("The studies {names} are open in another Slicer Studio. Close them there first, then change the folder.").format(
            names=", ".join(studies)))


def _movedTemplateIdsUnique(templatesFolder, before, renamedIds, journal=None):
    """After Templates/ was merged into ``templatesFolder`` (whose item names were ``before``): the moved templates whose id
    or name a template there already had get their own (``templates.makeMovedIdsUnique``), each new id written into
    ``journal`` (a ``MoveJournal``) before the template has it. Worker-safe.
    """
    try:
        movedNames = set(os.listdir(templatesFolder)) - before
    except OSError:
        return
    if not movedNames or not before:
        return
    try:
        from . import templates

        changed = templates.makeMovedIdsUnique(templatesFolder, movedNames, recordId=journal.recordId if journal is not None else None)
    except Exception:
        logging.warning("Slicer Studio: the ids of the moved templates could not be checked", exc_info=True)
        return
    if renamedIds is not None:
        renamedIds.update(changed)


def moveContent(oldRoot, newRoot, progress=None, renamed=None, renamedIds=None, journal=None):
    """Move Projects/, Studies/, LNM/, Templates/ and "About this folder.txt" from ``oldRoot`` to ``newRoot``. Worker-safe.

    Return the list of moved items. Raise OSError on failure (what was moved stays moved; nothing is deleted before it was
    copied and checked), and before anything is created when ``newRoot`` is inside ``oldRoot``.

    Each kind folder is merged into the one of ``newRoot``; LNM/ one level deeper, because the Map module finds its
    folders there by name (``_mergeLnm``: the reference maps of both libraries stay listed, and the normalization
    results stay where the scans' ``LNM.Normalization`` points).

    ``renamed`` (a dict) receives the items that moved under another name because ``newRoot`` already had one with that
    name (a project "MRHead" lands as "MRHead 2"): ``{path relative to oldRoot: path relative to newRoot}``. Pass it to
    ``pathAfterMove`` so a path follows the item, never the other one that kept the name.

    ``renamedIds`` (a dict) receives ``{old id: new id}`` of the moved templates that got another id because a template of
    ``newRoot`` already had theirs (``templates.followRenamedIds`` makes the default and the scene follow them).

    ``journal`` (a ``MoveJournal``) receives both as they happen, so a move that was killed and is finished later (a journal
    read back) returns them too, the items that moved before the kill included: the result is that of an uninterrupted move.
    """
    checkMoveTarget(oldRoot, newRoot)
    created = not os.path.isdir(newRoot)
    os.makedirs(newRoot, exist_ok=True)
    # What a move that was interrupted left: partial copies here (".Projects.studio-move"), originals there that were
    # being deleted after their copy was complete (".Projects.studio-moved")
    _removeMoveLeftovers(newRoot)
    _removeMoveLeftovers(oldRoot)
    sameVolume = _sameVolume(oldRoot, newRoot)
    moved = []
    absoluteRenames = _JournaledRenames(journal) if journal is not None else {}
    templatesBefore = None
    templatesChecked = False
    if journal is not None:
        # What the interrupted run of this move renamed before it was killed (already in the journal)
        dict.update(absoluteRenames, journal.completedRenames())
        if renamedIds is not None:
            renamedIds.update(journal.completedIds(os.path.join(newRoot, TEMPLATES_DIR)))
        templatesBefore = journal.templatesBefore()
    try:
        for name in movableItems(oldRoot):
            target = os.path.join(newRoot, name)
            if name == TEMPLATES_DIR:
                # What Templates/ there had before this move: the templates of an interrupted run moved in since
                before = templatesBefore
                if before is None:
                    try:
                        before = set(os.listdir(target)) if os.path.isdir(target) else set()
                    except OSError:
                        before = set()
                    if journal is not None:
                        journal.setTemplatesBefore(before)
                try:
                    _mergeMove(os.path.join(oldRoot, name), target, sameVolume, progress, absoluteRenames)
                finally:
                    # Also after a failed move: what moved has its own id there
                    templatesChecked = True
                    _movedTemplateIdsUnique(target, before, renamedIds, journal)
            elif name == LNM_DIR:
                _mergeLnm(os.path.join(oldRoot, name), target, sameVolume, progress, absoluteRenames)
            else:
                _mergeMove(os.path.join(oldRoot, name), target, sameVolume, progress, absoluteRenames)
            moved.append(name)
    except OSError:
        if created and not moved:
            with contextlib.suppress(OSError):
                os.rmdir(newRoot)  # nothing was moved: leave no empty folder behind
        raise
    finally:
        if not templatesChecked and templatesBefore is not None:
            # The interrupted run moved all of Templates/ and was killed before (or while) its templates got their own ids
            _movedTemplateIdsUnique(os.path.join(newRoot, TEMPLATES_DIR), templatesBefore, renamedIds, journal)
        _studiesFollowRenamedFolders(absoluteRenames, newRoot)  # also after a failed move: what moved is listed there
        if renamed is not None:
            for source, destination in absoluteRenames.items():
                renamed[os.path.normpath(os.path.relpath(source, oldRoot))] = os.path.normpath(os.path.relpath(destination, newRoot))
    return moved


def _studiesFollowRenamedFolders(absoluteRenames, newRoot):
    """A study that moved as "<name> 2" (its name was taken in ``newRoot``) is named "<name> 2" in its ``study.json`` too.

    The Library lists a study by its ``study.json`` name only, so the moved study and the one that kept the name would be two
    identical cards. Like a project (``adoptRenamedScene``), the study follows its folder. Only a study whose name was its old
    folder name follows (a name the user chose stays), and only what moved into ``Studies/``. Worker-safe; never raises.
    """
    studies = os.path.join(newRoot, STUDIES_DIR)
    for source, destination in absoluteRenames.items():
        try:
            studyJson = os.path.join(destination, "study.json")
            if not (isInside(destination, studies) and os.path.isfile(studyJson)):
                continue
            data = readJson(studyJson)
            if not isinstance(data, dict) or data.get("name") != os.path.basename(os.path.normpath(source)):
                continue
            from . import autosave_io

            data["name"] = os.path.basename(os.path.normpath(destination))
            # The text format of LesionNetworkMappingLib.study (indent 2, keys in their order, final newline)
            autosave_io.atomicWriteText(studyJson, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        except Exception:
            logging.warning(f"Slicer Studio: cannot name the moved study {destination} after its folder", exc_info=True)


def pathAfterMove(path, oldRoot, newRoot, renamed=None):
    """``path`` inside ``oldRoot`` mapped into ``newRoot`` (unchanged when it is not inside).

    ``renamed`` is what ``moveContent`` recorded: an item that moved under another name (" 2") is followed there.
    """
    if not path or not isInside(path, oldRoot):
        return path
    relative = os.path.normpath(os.path.relpath(os.path.realpath(path), os.path.realpath(oldRoot)))
    if renamed:
        # The deepest renamed item that holds ``path`` (an item inside a renamed folder moved with it, unrenamed)
        for source in sorted(renamed, key=len, reverse=True):
            if relative == source or relative.startswith(source + os.sep):
                relative = renamed[source] + relative[len(source):]
                break
    return os.path.normpath(os.path.join(newRoot, relative))


def shortPath(path):
    """``path`` with the home folder written "~" (for messages; never written to a file)."""
    home = os.path.expanduser("~")
    path = str(path or "")
    if path == home or path.startswith(home.rstrip(os.sep) + os.sep):
        return "~" + path[len(home.rstrip(os.sep)):]
    return path


#: The web build mounts the host folder of ``--data`` at this path
WEB_DATA_FOLDER = "/data"

#: Keeps the non-modal location message alive (one at a time)
_locationDialog = None


def _openDesktopUrl(url):
    """Hand ``url`` (a ``QUrl``) to the desktop (file manager or browser); return whether it was handed over.

    False in the web build, where the container has no file manager or browser (Qt would only log that it cannot detect a
    launcher), and when the desktop has no launcher. Tests replace this function.
    """
    if isWebBuild():
        return False
    return bool(qt.QDesktopServices.openUrl(url))


def _showLocationDialog(kind, text):
    """A non-modal message with ``text`` (a folder path or a link) in a read-only field and a [Copy] button.

    ``kind`` is "folder" or "link". The object names are ``StudioLocationDialog``, ``StudioLocationField`` and
    ``StudioLocationCopyButton``. Return the dialog.
    """
    global _locationDialog
    if _locationDialog is not None:
        try:
            _locationDialog.close()
            _locationDialog.deleteLater()
        except Exception:
            pass
        _locationDialog = None
    folder = kind == "folder"
    dialog = qt.QDialog(slicer.util.mainWindow())
    dialog.setObjectName("StudioLocationDialog")
    dialog.setModal(False)
    dialog.setWindowTitle(_("Folder location") if folder else _("Link"))
    layout = qt.QVBoxLayout(dialog)
    intro = _("This build cannot open it here. Its location:") if folder else _("This build cannot open a browser. Copy the link and open it in your browser:")
    label = qt.QLabel(intro)
    label.setObjectName("StudioLocationText")
    label.setWordWrap(True)
    layout.addWidget(label)
    field = qt.QLineEdit(text)
    field.setObjectName("StudioLocationField")
    field.setReadOnly(True)
    field.setMinimumWidth(420)
    layout.addWidget(field)
    if folder and isWebBuild() and (text == WEB_DATA_FOLDER or text.startswith(WEB_DATA_FOLDER + "/")):
        relative = text[len(WEB_DATA_FOLDER):].strip("/")
        hostNote = _("/data is the data folder that you shared with --data on the computer that runs this build.")
        if relative:
            hostNote += " " + _("In that folder: {relative}").format(relative=relative)
        note = qt.QLabel(hostNote)
        note.setObjectName("StudioLocationHostNote")
        note.setWordWrap(True)
        layout.addWidget(note)
    buttons = qt.QHBoxLayout()
    buttons.addStretch(1)
    copy = qt.QPushButton(_("Copy path") if folder else _("Copy link"))
    copy.setObjectName("StudioLocationCopyButton")
    copy.connect("clicked()", lambda: qt.QApplication.clipboard().setText(text))
    buttons.addWidget(copy)
    close = qt.QPushButton(_("Close"))
    close.setObjectName("StudioLocationCloseButton")
    close.connect("clicked()", dialog.close)
    buttons.addWidget(close)
    layout.addLayout(buttons)
    dialog.setMaximumWidth(720)
    dialog.show()
    _locationDialog = dialog
    return dialog


def showInFileManager(path):
    """Open ``path`` (a folder or file) in Finder, Explorer or the file manager.

    Where the desktop cannot open it (the web build, a Linux desktop without a launcher) a message shows the path, with the
    host folder in the web build, and [Copy path]. Return False when ``path`` does not exist, else True (it was opened or shown).
    """
    if not path or not os.path.exists(path):
        return False
    if not _openDesktopUrl(qt.QUrl.fromLocalFile(path)):
        _showLocationDialog("folder", str(path))
    return True


def openLink(url):
    """Open a web address in the browser; where there is none (the web build) a message shows the link and [Copy link].

    ``url`` is a string or a ``QUrl``. Return True (it was opened or shown).
    """
    qurl = url if hasattr(url, "toString") else qt.QUrl(str(url))
    if not _openDesktopUrl(qurl):
        _showLocationDialog("link", qurl.toString())
    return True


#: The hidden object that receives the web addresses of ``QDesktopServices::openUrl`` (the web build)
_webLinkRelay = None

#: The web address schemes that the web build routes to :func:`openLink`
WEB_LINK_SCHEMES = ("http", "https")


def installWebLinkHandler():
    """In the web build, send every ``QDesktopServices::openUrl`` of a web address to :func:`openLink`.

    The official actions (the Help menu: Documentation, Quick Start, Get Help, the forum ...) call ``QDesktopServices::openUrl``
    themselves, which in the web build (no browser in the container) only logs that Qt cannot detect a browser and shows nothing.
    The handler shows the link and [Copy link] instead. Elsewhere nothing changes. Safe to call again. Return whether the handler
    is installed.

    ``QDesktopServices::setUrlHandler`` needs a receiver with a method that takes a ``QUrl``, and PythonQt cannot make one; a
    hidden ``QTextBrowser`` is the receiver and its signal ``anchorClicked(QUrl)`` (which Qt invokes like a method, in Qt 5 and Qt 6)
    carries the address to Python.
    """
    global _webLinkRelay
    if not isWebBuild():
        return False
    if _webLinkRelay is None:
        relay = qt.QTextBrowser()
        relay.setObjectName("StudioWebLinkRelay")
        relay.connect("anchorClicked(QUrl)", openLink)
        _webLinkRelay = relay
        # Qt wants the handler removed before its receiver is destroyed (deprecated since Qt 6.6)
        slicer.app.connect("aboutToQuit()", uninstallWebLinkHandler)
    for scheme in WEB_LINK_SCHEMES:
        qt.QDesktopServices.setUrlHandler(scheme, _webLinkRelay, "anchorClicked")
    return True


def uninstallWebLinkHandler():
    """Undo :func:`installWebLinkHandler` (the tests)."""
    global _webLinkRelay
    for scheme in WEB_LINK_SCHEMES:
        qt.QDesktopServices.unsetUrlHandler(scheme)
    _webLinkRelay = None


def routeLinks(widget):
    """Send the links that a user clicks in ``widget`` (a ``QLabel`` or ``QTextBrowser``) through :func:`openLink`.

    ``setOpenExternalLinks(True)`` makes Qt call ``QDesktopServices::openUrl`` itself, which in the web build only logs that
    it cannot detect a browser and shows nothing. Here the widget does not open anything by itself: a clicked link goes to
    :func:`openLink`, so the browser opens it, or (web build) a message shows the link and [Copy link]. Links to a place in the same
    text (``#name``) are ignored. Return ``widget``.
    """
    if hasattr(widget, "setOpenLinks"):
        widget.setOpenLinks(False)
        widget.setOpenExternalLinks(False)

        def onAnchorClicked(url):
            qurl = url if hasattr(url, "toString") else qt.QUrl(str(url))
            text = qurl.toString()
            # A link to a place in the same text (#name) has nothing to open. The slot must not hold ``widget``: the
            # widget owns the connection, and a reference back made Slicer exit abnormally.
            if text and not text.startswith("#"):
                openLink(qurl)

        widget.connect("anchorClicked(QUrl)", onAnchorClicked)
    else:
        widget.setOpenExternalLinks(False)
        widget.connect("linkActivated(QString)", lambda link: openLink(link) if link else None)
    return widget


def fileManagerName():
    """"Finder", "Explorer" or "Files" (the name in "Show in …")."""
    if sys.platform == "darwin":
        return "Finder"
    if sys.platform.startswith("win"):
        return "Explorer"
    return "Files"

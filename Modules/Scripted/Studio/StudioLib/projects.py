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

Settings (fork-only, SPEC 9.7 rule 5): ``Studio/Projects/Root`` (the folder) and ``Studio/Projects/RootChosen``. The
environment variable ``SLICER_STUDIO_PROJECTS_ROOT`` overrides both and skips the chooser (tests, gates, harnesses);
``SLICER_STUDIO_PROJECTS=0`` turns projects off (the private recovery autosave of A-01 then runs in Studio too).

No function here imports numpy or runs at startup. Worker-safe functions (only the standard library) are marked so.
"""

import json
import logging
import os
import re
import shutil
import sys
import time

import qt
import slicer
from slicer.i18n import tr as _

#: Settings keys (fork-only)
ROOT_KEY = "Studio/Projects/Root"
ROOT_CHOSEN_KEY = "Studio/Projects/RootChosen"
#: Environment: the folder (skips the chooser), and "0" to turn projects off
ROOT_ENVIRONMENT = "SLICER_STUDIO_PROJECTS_ROOT"
ENABLED_ENVIRONMENT = "SLICER_STUDIO_PROJECTS"

ROOT_FOLDER_NAME = "Slicer Studio"
WEB_ROOT = "/data/Slicer Studio"
PROJECTS_DIR = "Projects"
STUDIES_DIR = "Studies"
LNM_DIR = "LNM"
ABOUT_FILE = "About this folder.txt"
#: Hidden folder of a project
PRIVATE_DIR = ".studio"
PROJECT_FILE = "project.json"
THUMBNAIL_FILE = "thumbnail.png"
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
    _notify()
    return path


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


def sanitizeName(name, maxLength=MAX_NAME_LENGTH, fallback=UNTITLED_NAME):
    r"""A file or folder name valid on macOS, Windows and Linux: no / \ : * ? " < > |, no trailing dot or space. Worker-safe."""
    name = _UNSAFE.sub(" ", str(name or ""))
    name = re.sub(r"\s+", " ", name).strip()
    name = name[:maxLength].rstrip(". ").lstrip(". ")
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
    if node is None or not node.IsA("vtkMRMLScalarVolumeNode"):
        return False
    if _role(node) == "template":
        return True
    return (node.GetName() or "").startswith("MNI152")


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
    for index in range(nodes.GetNumberOfItems()):
        if _isUserNode(nodes.GetItemAsObject(index)):
            return True
    return False


def defaultProjectName(scene=None):
    """The name of a new project: the first loaded scan ("sub-012 T1w"), else the scene file, else "Untitled project"."""
    scene = scene or slicer.mrmlScene
    volumes = scene.GetNodesByClass("vtkMRMLScalarVolumeNode")
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
        n = int((countsValue or {}).get(key) or 0)
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


def readJson(path):
    """Read a JSON file; None if it does not exist or cannot be read. Worker-safe."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


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


def isProjectFolder(path):
    return bool(path) and os.path.isdir(path) and (os.path.isdir(privateFolder(path)) or sceneFile(path) is not None)


def _isoTime(seconds=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(seconds if seconds is not None else time.time()))


def parseTime(text):
    """Seconds since the epoch of an ISO time written by ``_isoTime`` (local time), or None."""
    try:
        return time.mktime(time.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return None


class ProjectInfo:
    """A project folder as the Library lists it (read from disk only: nothing is loaded)."""

    def __init__(self, path):
        self.path = os.path.normpath(str(path))
        self.name = os.path.basename(self.path)
        data = readJson(projectFile(self.path)) or {}
        self.data = data
        self.scene = sceneFile(self.path)
        modified = parseTime(data.get("modified"))
        if modified is None:
            try:
                modified = os.path.getmtime(self.scene or self.path)
            except OSError:
                modified = 0.0
        self.modified = modified
        self.created = parseTime(data.get("created"))
        self.counts = data.get("counts") or {}
        thumbnail = thumbnailFile(self.path)
        self.thumbnail = thumbnail if os.path.isfile(thumbnail) else None
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
        if name.startswith("."):
            continue
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


def _lockFile(projectPath):
    lock = qt.QLockFile(os.path.join(projectPath, PRIVATE_DIR, LOCK_FILE))
    # Never stale by age: a lock is stale only when its process is gone (QLockFile checks the process on this host)
    lock.setStaleLockTime(0)
    return lock


def isLockedByOther(projectPath):
    """True if another running Slicer Studio has the project open."""
    current = currentProject()
    if current is not None and os.path.normcase(os.path.realpath(current.path)) == os.path.normcase(os.path.realpath(projectPath)):
        return False
    if not os.path.exists(os.path.join(projectPath, PRIVATE_DIR, LOCK_FILE)):
        return False
    lock = _lockFile(projectPath)
    if lock.tryLock(0):
        lock.unlock()
        return False
    return True


class Project:
    """An open project: its folder, its scene file and its lock."""

    def __init__(self, path):
        self.path = os.path.normpath(os.path.abspath(str(path)))
        self.name = os.path.basename(self.path)
        self.created = None
        self.lastSaveTime = None
        self.lastError = ""
        self._lock = None
        data = readJson(projectFile(self.path)) or {}
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
        """Take the lock of the project. Return False if another Slicer Studio holds it."""
        if self._lock is not None:
            return True
        os.makedirs(privateFolder(self.path), exist_ok=True)
        lock = _lockFile(self.path)
        if not lock.tryLock(0):
            return False
        self._lock = lock
        return True

    def unlock(self):
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None

    def isLocked(self):
        return self._lock is not None

    def jsonData(self, state="open", scene=None, savedAt=None):
        """The content of .studio/project.json (no absolute path, no process id: the lock holds those)."""
        data = readJson(projectFile(self.path)) or {}
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
    if os.path.exists(newPath) and os.path.normcase(newPath) != os.path.normcase(path):
        raise ProjectError(_("A project named {name} already exists.").format(name=newName))
    scene = sceneFile(path)
    os.rename(path, newPath)
    renameSceneFile(newPath, scene)
    data = readJson(projectFile(newPath))
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
    if os.path.isfile(oldPath) and os.path.normcase(oldPath) != os.path.normcase(newPath) and not os.path.exists(newPath):
        os.rename(oldPath, newPath)
        # vtkMRMLScene keeps hidden nodes in "<scene file name up to the first dot>_Private": follow the new name
        oldPrivate = os.path.join(projectPath, oldBase.split(".")[0] + "_Private")
        newPrivate = os.path.join(projectPath, name.split(".")[0] + "_Private")
        if os.path.isdir(oldPrivate) and not os.path.exists(newPrivate):
            os.rename(oldPrivate, newPrivate)
            _rewritePrivatePaths(newPath, os.path.basename(oldPrivate), os.path.basename(newPrivate))
    return newPath


def _rewritePrivatePaths(scenePath, oldFolder, newFolder):
    """Replace the folder of hidden nodes in the file names of a scene file (relative paths only)."""
    from . import autosave_io

    try:
        with open(scenePath, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return
    encodedOld, encodedNew = autosave_io.urlEncode(oldFolder), autosave_io.urlEncode(newFolder)
    updated = re.sub(r'(fileName="|fileListMember\d+=")' + re.escape(encodedOld) + "/", lambda m: m.group(1) + encodedNew + "/", text)
    if updated != text:
        autosave_io.atomicWriteText(scenePath, updated)


def duplicateFolder(path, onDone=None, onError=None):
    """Copy a project folder to "<name> copy" (in the background writer; the lock is not copied). Return the new folder."""
    from . import autosave_io

    name = os.path.basename(os.path.normpath(path))
    parent = os.path.dirname(os.path.normpath(path))
    newName = uniqueName(parent, sanitizeName(_("{name} copy").format(name=name)), isFile=False)
    newPath = os.path.join(parent, newName)
    scene = sceneFile(path)

    def copy():
        temporary = newPath + ".studio-copy"
        shutil.copytree(path, temporary, ignore=shutil.ignore_patterns(LOCK_FILE, "*.studio-tmp*", ".studio-tmp-*", REMOVED_DIR))
        os.rename(temporary, newPath)
        renameSceneFile(newPath, scene)
        data = readJson(projectFile(newPath)) or {}
        data.update(name=newName, state="closed", created=_isoTime(), modified=_isoTime())
        autosave_io.atomicWriteJson(projectFile(newPath), data)
        return newPath

    autosave_io.backgroundWriter().submit(copy, onDone=onDone, onError=onError)
    return newPath


def moveToTrash(path):
    """Move a folder to the trash of the system (QFile.moveToTrash, Qt 5.15 and later). Return True if it was moved."""
    moveToTrashFunction = getattr(qt.QFile, "moveToTrash", None)
    if moveToTrashFunction is None:
        return False
    try:
        result = moveToTrashFunction(path)
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


def movableItems(rootPath):
    """The items of a Slicer Studio folder that "Move your projects and studies" moves (those that exist)."""
    items = [name for name in (PROJECTS_DIR, STUDIES_DIR, LNM_DIR, ABOUT_FILE) if os.path.exists(os.path.join(rootPath, name))]
    return items


def hasContent(rootPath):
    """True if the folder holds projects or studies."""
    return bool(listProjects(rootPath)) or bool(listStudies(rootPath))


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


def _mergeMove(source, destination, sameVolume, progress=None):
    """Move ``source`` (a file or folder) to ``destination``: a rename on the same volume, else copy, check the sizes and
    delete the original. Folders that exist on both sides are merged; names that exist are kept (" 2"). Worker-safe.
    """
    if os.path.isdir(source) and os.path.isdir(destination):
        for name in os.listdir(source):
            target = os.path.join(destination, name)
            if os.path.exists(target) and not (os.path.isdir(target) and os.path.isdir(os.path.join(source, name)) and name in (PROJECTS_DIR, STUDIES_DIR, LNM_DIR)):
                target = os.path.join(destination, uniqueName(destination, name, isFile=os.path.isfile(os.path.join(source, name))))
            _mergeMove(os.path.join(source, name), target, sameVolume, progress)
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
    if sameVolume:
        os.rename(source, destination)
        if progress:
            progress(os.path.basename(source))
        return
    if os.path.isdir(source):
        shutil.copytree(source, destination)
        if treeSize(source) != treeSize(destination):
            shutil.rmtree(destination, ignore_errors=True)
            raise OSError(_("Copying {name} failed: the sizes differ").format(name=os.path.basename(source)))
        shutil.rmtree(source)
    else:
        shutil.copy2(source, destination)
        if os.path.getsize(source) != os.path.getsize(destination):
            os.remove(destination)
            raise OSError(_("Copying {name} failed: the sizes differ").format(name=os.path.basename(source)))
        os.remove(source)
    if progress:
        progress(os.path.basename(source))


def moveContent(oldRoot, newRoot, progress=None):
    """Move Projects/, Studies/, LNM/ and "About this folder.txt" from ``oldRoot`` to ``newRoot``. Worker-safe.

    Return the list of moved items. Raise OSError on failure (what was moved stays moved; nothing is deleted before it was
    copied and checked).
    """
    os.makedirs(newRoot, exist_ok=True)
    sameVolume = _sameVolume(oldRoot, newRoot)
    moved = []
    for name in movableItems(oldRoot):
        _mergeMove(os.path.join(oldRoot, name), os.path.join(newRoot, name), sameVolume, progress)
        moved.append(name)
    return moved


def pathAfterMove(path, oldRoot, newRoot):
    """``path`` inside ``oldRoot`` mapped into ``newRoot`` (unchanged when it is not inside)."""
    if not path or not isInside(path, oldRoot):
        return path
    relative = os.path.relpath(os.path.realpath(path), os.path.realpath(oldRoot))
    return os.path.normpath(os.path.join(newRoot, relative))


def shortPath(path):
    """``path`` with the home folder written "~" (for messages; never written to a file)."""
    home = os.path.expanduser("~")
    path = str(path or "")
    if path == home or path.startswith(home.rstrip(os.sep) + os.sep):
        return "~" + path[len(home.rstrip(os.sep)):]
    return path


def showInFileManager(path):
    """Open ``path`` (a folder) in Finder, Explorer or the file manager."""
    if path and os.path.exists(path):
        qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(path))
        return True
    return False


def fileManagerName():
    """"Finder", "Explorer" or "Files" (the name in "Show in …")."""
    if sys.platform == "darwin":
        return "Finder"
    if sys.platform.startswith("win"):
        return "Explorer"
    return "Files"

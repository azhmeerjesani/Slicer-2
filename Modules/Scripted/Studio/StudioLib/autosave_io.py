r"""Writing of the scene autosave of Slicer Studio (A-01, SPEC 9.9): incremental, MRML-standard snapshots.

An autosave "slot" is a folder that holds a standard Slicer scene: ``Scene.mrml``, ``manifest.json``,
``thumbnail.png`` and ``data/<node ID>-g<generation>/<node name>.<extension>``. The fork and official
Slicer 5.12/5.13 open ``Scene.mrml`` like any other scene (SPEC 9.7 rule 11).

- Nodes that are not modified since they were read from a user file are *referenced*: their storage
  node keeps the original file, written as an absolute path.
- Unmodified nodes read from a volatile place (temporary folder, cache, application folder) are *copied*.
- Every other node is *written*, but only when its content changed since the previous generation
  (``contentMTime()``). Unchanged nodes keep the files of an earlier generation.

The live scene is never changed: the scene XML comes from a string commit (``vtkMRMLScene::Commit`` in
string mode, which no longer marks the scene as stored, D-A4), and the data files are written through
*detached* storage nodes (``node.CreateDefaultStorageNode()``, not referenced by the node), so every
node stays "modified since read" and the exit prompt is unchanged.

Threading (SPEC D19): MRML, VTK and Qt are used on the main thread only. Functions documented as
*worker-safe* (``writeScalarNrrd``, ``atomicWriteText``/``atomicWriteBytes``, ``replaceWithRetry``,
``postProcessSceneXml``, ``SlotWriter.finalize``) use only the standard library and numpy, and run in the
single ``BackgroundWriter`` thread (``backgroundWriter()``), which other features may use too.
"""

import collections
import json
import logging
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
import zlib

import qt
import slicer

FORMAT = "slicer-studio-autosave"
VERSION = 1
SCENE_FILE = "Scene.mrml"
MANIFEST_FILE = "manifest.json"
SESSION_FILE = "session.json"
LOCK_FILE = "session.lock"
THUMBNAIL_FILE = "thumbnail.png"
DATA_DIR = "data"
README_FILE = "README.txt"
#: Name of the autosave folder in the application data folder (QStandardPaths::AppLocalDataLocation)
ROOT_FOLDER_NAME = "StudioAutosave"

#: Scalar volumes from this size on are copied into a numpy snapshot on the main thread (about 1 ms per 4 MB) and
#: written by the background writer: a main-thread C++ write of a few MB took 5 ms, but up to 23 ms with
#: disk write-back jitter. Smaller volumes and other data are written by C++ storage nodes on the main thread.
SNAPSHOT_MIN_MB = 1
#: Final generations (scene closed or application quit without saving) write volumes up to this size; larger
#: volumes keep their previous autosave (raw NRRD writes run at about 2-3 GB/s: 17 MB in 8 ms, 256 MB in 90 ms)
MAIN_THREAD_VOLUME_MB = 48
#: Snapshots are copied by slabs of at most this size: about 1 ms each, but up to 20 times longer while other threads
#: use the memory bandwidth (with software rendering of the views: up to 73 ms for 8 MB, 490 ms for 64 MB), so
#: that a tick never exceeds HARD_SLICE_MS
SNAPSHOT_CHUNK_MB = 4
#: Main-thread time used per tick of a generation
SLICE_BUDGET_MS = 40
#: A generation tick must never block the main thread longer than this
HARD_SLICE_MS = 100
#: A node whose main-thread write took longer is "heavy": written at most every HEAVY_INTERVAL_S, after HEAVY_IDLE_S idle
HEAVY_WRITE_MS = 100
HEAVY_INTERVAL_S = 600
HEAVY_IDLE_S = 30
#: Segmentations are written uncompressed when their last compressed write took longer than this
UNCOMPRESSED_SEGMENTATION_MS = 60
#: Size of the data blocks given to zlib by writeScalarNrrd
_WRITE_BLOCK_BYTES = 4 << 20

SETTINGS_GROUP = "Studio/Autosave"
#: Settings (fork-only keys Studio/Autosave/<name>, SPEC 9.7 rule 5) and their defaults (D-A1, D-A2, D-A3, D-A5).
#: The defaults live in code: SlicerDefaultSettings.ini is not changed (G0-N10).
DEFAULT_SETTINGS = {
    "Enabled": True,
    "IntervalMinutes": 2,
    "IdleSeconds": 10,
    "Directory": "",
    "KeepSessions": 5,
    "MaxDiskMB": 2048,
    "MaxAgeDays": 14,
    "MaxNodeMB": 2048,
}

#: Classes whose modification does not make the scene "changed" for the autosave (views, cameras, interaction state)
FINGERPRINT_EXCLUDED_CLASSES = (
    "vtkMRMLAbstractViewNode",
    "vtkMRMLCameraNode",
    "vtkMRMLInteractionNode",
    "vtkMRMLSelectionNode",
    "vtkMRMLCrosshairNode",
    "vtkMRMLSliceCompositeNode",
    "vtkMRMLLayoutNode",
)

#: numpy dtype name -> NRRD type (other types are written by the C++ storage node)
_NRRD_TYPES = {
    "int8": "signed char",
    "uint8": "unsigned char",
    "int16": "short",
    "uint16": "unsigned short",
    "int32": "int",
    "uint32": "unsigned int",
    "float32": "float",
    "float64": "double",
}

#: Characters encoded in MRML file names, in encoding order (vtkMRMLNode::URLEncodeString)
_URL_ENCODING = (("%", "%25"), (" ", "%20"), ("'", "%27"), (">", "%3E"), ("<", "%3C"), ('"', "%22"))

#: Prefix of the name of the fallback "unsaved changes" node (builds without the vtkMRMLScene::Commit fix)
SENTINEL_NAME_PREFIX = "Unsaved changes (autosaved)"


def settingsValue(name):
    """Value of the setting ``Studio/Autosave/<name>``, converted to the type of its default."""
    default = DEFAULT_SETTINGS[name]
    value = slicer.util.settingsValue(f"{SETTINGS_GROUP}/{name}", default)
    try:
        if isinstance(default, bool):
            return slicer.util.toBool(value)
        if isinstance(default, int):
            return int(float(value))
    except (TypeError, ValueError):
        return default
    return str(value) if value is not None else default


#
# File names
#


def urlEncode(text):
    """Encode a file name like vtkMRMLNode::URLEncodeString (not called on live nodes: it modifies them)."""
    for character, code in _URL_ENCODING:
        text = text.replace(character, code)
    return text


def urlDecode(text):
    """Decode a file name like vtkMRMLNode::URLDecodeString."""
    for character, code in reversed(_URL_ENCODING):
        text = text.replace(code, character)
    return text


_UNSAFE_FILE_NAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitizeFileName(name, maxLength=60):
    """A file name that is valid on Windows, macOS and Linux, derived from a node name."""
    name = _UNSAFE_FILE_NAME_CHARACTERS.sub("_", name or "")[:maxLength].rstrip(". ")
    if not name:
        return "node"
    if name.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        name = "_" + name
    return name


def builtinRoot():
    """The default autosave folder: the private application data folder (D-A1), never a user folder."""
    base = qt.QStandardPaths.writableLocation(qt.QStandardPaths.AppLocalDataLocation)
    return os.path.normpath(os.path.join(base, ROOT_FOLDER_NAME))


def defaultRoot():
    """The autosave folder: SLICER_STUDIO_AUTOSAVE_DIR, else the setting Studio/Autosave/Directory, else ``builtinRoot()``."""
    environment = os.environ.get("SLICER_STUDIO_AUTOSAVE_DIR")
    if environment:
        return os.path.normpath(os.path.abspath(environment))
    configured = settingsValue("Directory")
    if configured:
        return os.path.normpath(os.path.abspath(configured))
    return builtinRoot()


_README_TEXT = """Slicer Studio autosave
======================

This folder holds automatic copies of the scenes that were open in Slicer Studio, so that
unsaved work can be recovered after a crash or after closing without saving.

- Recover them with File > Recover Autosaved Work... in Slicer Studio.
- Each session folder contains scene-<n> folders. Each one is a standard Slicer scene
  (Scene.mrml and its data files) that any Slicer version can open with File > Load Scene.
- Sessions are deleted automatically after some days (Edit > Application Settings > Autosave).
- The files may contain patient data: this folder is private to your user account.

Deleting this folder is safe when Slicer is not running.
"""


def ensureRoot(root):
    """Create the autosave folder (private: mode 0700 on POSIX, because autosaves may hold patient data) and its README."""
    os.makedirs(root, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
    readme = os.path.join(root, README_FILE)
    if not os.path.exists(readme):
        try:
            atomicWriteText(readme, _README_TEXT)
        except OSError:
            pass
    return root


#
# Change detection (main thread)
#

_classIsStorable = {}
_classIsExcluded = {}


def _isStorableClass(node):
    className = node.GetClassName()
    storable = _classIsStorable.get(className)
    if storable is None:
        storable = _classIsStorable[className] = bool(node.IsA("vtkMRMLStorableNode"))
    return storable


def _isExcludedClass(node):
    className = node.GetClassName()
    excluded = _classIsExcluded.get(className)
    if excluded is None:
        excluded = _classIsExcluded[className] = any(node.IsA(name) for name in FINGERPRINT_EXCLUDED_CLASSES)
    return excluded


def contentMTime(node):
    """Modification time of the content of a storable node: it changes on data edits, not on display edits."""
    mtime = node.GetMTime()
    try:
        if node.IsA("vtkMRMLVolumeNode"):
            imageData = node.GetImageData()
            if imageData is not None:
                mtime = max(mtime, imageData.GetMTime())
        elif node.IsA("vtkMRMLSegmentationNode"):
            segmentation = node.GetSegmentation()
            if segmentation is not None:
                mtime = max(mtime, segmentation.GetMTime())
                for index in range(segmentation.GetNumberOfLayers()):
                    layer = segmentation.GetLayerDataObject(index)
                    if layer is not None:
                        mtime = max(mtime, layer.GetMTime())
                for index in range(segmentation.GetNumberOfSegments()):
                    mtime = max(mtime, segmentation.GetNthSegment(index).GetMTime())
        elif node.IsA("vtkMRMLModelNode"):
            mesh = node.GetMesh()
            if mesh is not None:
                mtime = max(mtime, mesh.GetMTime())
        elif node.IsA("vtkMRMLTableNode"):
            table = node.GetTable()
            if table is not None:
                mtime = max(mtime, table.GetMTime())
        elif node.IsA("vtkMRMLTransformNode"):
            transform = node.GetTransformToParent()
            if transform is not None:
                mtime = max(mtime, transform.GetMTime())
    except Exception:
        pass
    return mtime


def sceneFingerprint(scene, ignoreIds=()):
    """A cheap value that changes when the scene content changes (MTimes only: it computes nothing).

    ``(number of nodes, max content MTime of the storable nodes saved with the scene, max MTime of the
    other nodes, excluding views, cameras and interaction state)``.
    """
    count = scene.GetNumberOfNodes()
    storableMTime = 0
    otherMTime = 0
    ignored = 0
    for index in range(count):
        node = scene.GetNthNode(index)
        if node is None:
            continue
        if ignoreIds and node.GetID() in ignoreIds:
            ignored += 1
            continue
        if _isStorableClass(node):
            if node.GetSaveWithScene():
                storableMTime = max(storableMTime, contentMTime(node))
        elif not _isExcludedClass(node):
            otherMTime = max(otherMTime, node.GetMTime())
    return (count - ignored, storableMTime, otherMTime)


def estimateBytes(node):
    """Approximate size of the data of a storable node."""
    try:
        if node.IsA("vtkMRMLVolumeNode"):
            imageData = node.GetImageData()
            if imageData is None:
                return 0
            dimensions = imageData.GetDimensions()
            return dimensions[0] * dimensions[1] * dimensions[2] * imageData.GetScalarSize() * imageData.GetNumberOfScalarComponents()
        if node.IsA("vtkMRMLSegmentationNode"):
            segmentation = node.GetSegmentation()
            total = 0
            for index in range(segmentation.GetNumberOfLayers()):
                layer = segmentation.GetLayerDataObject(index)
                if layer is not None and layer.IsA("vtkImageData"):
                    dimensions = layer.GetDimensions()
                    total += dimensions[0] * dimensions[1] * dimensions[2] * layer.GetScalarSize()
                elif layer is not None:
                    total += layer.GetActualMemorySize() * 1024
            return total
        if node.IsA("vtkMRMLModelNode"):
            mesh = node.GetMesh()
            return mesh.GetActualMemorySize() * 1024 if mesh is not None else 0
    except Exception:
        pass
    return 64 * 1024


def _canonical(path):
    return os.path.normcase(os.path.realpath(path))


def _isUnder(path, root):
    """True if the canonical ``path`` is ``root`` or inside it (both canonical)."""
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def volatileRoots(autosaveRoot=None):
    """Folders whose files are not user files: they may disappear (temporary folder, cache) or belong to the application.

    Unmodified nodes read from these folders are copied into the autosave instead of being referenced.
    """
    roots = [slicer.app.temporaryPath, slicer.app.cachePath, slicer.app.slicerHome, autosaveRoot]
    return [_canonical(root) for root in roots if root]


def isVolatilePath(path, roots, systemTemporary=True):
    """True if ``path`` lies in one of ``roots`` (see ``volatileRoots()``) or in the system temporary folder.

    A file in the system temporary folder that is also inside the user's home folder is a user file
    (for example with a test home folder under /tmp). ``systemTemporary=False`` ignores the system
    temporary folder (tests).
    """
    canonical = _canonical(path)
    if any(_isUnder(canonical, root) for root in roots):
        return True
    return systemTemporary and _isUnder(canonical, _canonical(tempfile.gettempdir())) and not _isUnder(canonical, _canonical(os.path.expanduser("~")))


def storageFiles(storageNode, rootDirectory):
    """Absolute paths of the files of a storage node (archetype first)."""
    names = []
    fileName = storageNode.GetFileName()
    if fileName:
        names.append(fileName)
    for index in range(storageNode.GetNumberOfFileNames()):
        name = storageNode.GetNthFileName(index)
        if name and name not in names:
            names.append(name)
    return [os.path.normpath(name if os.path.isabs(name) else os.path.join(rootDirectory or "", name)) for name in names]


def _fileStat(path):
    status = os.stat(path)
    return [status.st_size, status.st_mtime_ns]


def defaultStorageNodeClassName(node):
    """``GetDefaultStorageNodeClassName()``: model nodes need an argument in Python."""
    try:
        return node.GetDefaultStorageNodeClassName() or ""
    except TypeError:
        return node.GetDefaultStorageNodeClassName("") or ""


def classify(node, oldEntry, rootDirectory, roots, maxNodeBytes, systemTemporary=True):
    """How a storable node is autosaved: ``(kind, files, stat)``.

    - ``reference``: unmodified, its storage files exist outside the volatile roots and are unchanged on disk
      since the node was first referenced: the autosave scene points at the original files;
    - ``copy``: as for reference, but a single file in a volatile root: the background writer copies it;
    - ``write``: everything else (new or modified data, missing files, multi-file data in a volatile root);
    - ``skip``: the data is larger than ``maxNodeBytes``: not autosaved (listed in the recovery user interface).
    """
    storageNode = node.GetStorageNode()
    files, stat = [], []
    kind = "write"
    if not node.GetModifiedSinceRead() and storageNode is not None and storageNode.GetFileName() and not storageNode.GetURI():
        files = storageFiles(storageNode, rootDirectory)
        known = bool(oldEntry) and oldEntry.get("kind") in ("reference", "copy") and oldEntry.get("files") == files and bool(oldEntry.get("firstStat"))
        try:
            if known and len(files) > 1 and len(oldEntry.get("stat") or []) == len(files):
                # Data of many files (a DICOM series): only the first file is checked again on each generation,
                # so that planning stays short on the main thread
                stat = [_fileStat(files[0])] + [list(item) for item in oldEntry["stat"][1:]]
            else:
                stat = [_fileStat(path) for path in files]
        except OSError:
            stat = None
        if stat is not None:
            if known:
                # The file changed on disk while the node is unmodified in memory: write the node instead
                unchanged = [list(item) for item in oldEntry["firstStat"]] == stat
            else:
                unchanged = True
            if unchanged:
                if known:
                    kind = oldEntry["kind"]  # the same files: the same folders
                elif any(isVolatilePath(path, roots, systemTemporary) for path in files):
                    kind = "copy" if len(files) == 1 else "write"
                else:
                    kind = "reference"
        if kind != "reference" and kind != "copy":
            files, stat = [], []
    if kind == "copy":
        size = stat[0][0]
    elif kind == "write":
        size = estimateBytes(node)
    else:
        size = 0
    if kind in ("write", "copy") and size > maxNodeBytes:
        kind = "skip"
    return kind, files, stat


#
# Scene XML (main thread)
#

_commitDoesNotMarkStored = None


def commitDoesNotMarkStored():
    """True if ``vtkMRMLScene::Commit`` in string mode keeps the scene "modified since read" (D-A4, cached probe)."""
    global _commitDoesNotMarkStored
    if _commitDoesNotMarkStored is None:
        scene = slicer.vtkMRMLScene()
        scene.AddNewNodeByClass("vtkMRMLScriptedModuleNode")
        before = scene.GetStoredTime()
        scene.SetSaveToXMLString(1)
        try:
            scene.Commit()
        finally:
            scene.SetSaveToXMLString(0)
        _commitDoesNotMarkStored = scene.GetStoredTime() == before
    return _commitDoesNotMarkStored


class _Sentinel:
    """Fallback for builds without the Commit fix: a hidden node that keeps the scene "modified since read"."""

    nodeId = None

    @classmethod
    def touch(cls, scene, modifiedNodeNames):
        node = scene.GetNodeByID(cls.nodeId) if cls.nodeId else None
        if node is None:
            node = scene.AddNewNodeByClass("vtkMRMLScriptedModuleNode")
            node.SetSaveWithScene(False)
            node.SetHideFromEditors(True)
            cls.nodeId = node.GetID()
        node.SetName(f"{SENTINEL_NAME_PREFIX}: {', '.join(modifiedNodeNames[:5])}")
        node.Modified()

    @classmethod
    def ignoredIds(cls):
        return (cls.nodeId,) if cls.nodeId else ()


def sentinelIds():
    """IDs of fallback sentinel nodes, ignored by the change detection."""
    return _Sentinel.ignoredIds()


def sceneXml(scene):
    """Return ``(xml, rootDirectory)``: the scene serialized like a scene file, without saving anything."""
    import vtk

    modifiedNames = None
    if not commitDoesNotMarkStored():
        modified = vtk.vtkCollection()
        if scene.GetModifiedSinceRead(modified):
            modifiedNames = [modified.GetItemAsObject(i).GetName() or "" for i in range(modified.GetNumberOfItems())]
    rootDirectory = scene.GetRootDirectory()
    scene.SetSaveToXMLString(1)
    try:
        scene.Commit()
    finally:
        scene.SetSaveToXMLString(0)
    xml = scene.GetSceneXMLString()
    if modifiedNames is not None and not scene.GetModifiedSinceRead():
        _Sentinel.touch(scene, modifiedNames)
    return xml, rootDirectory


def storageXmlFor(storageNodes, slotDir):
    """Serialize detached storage nodes whose files are in ``slotDir``: ``{key: element XML}``.

    ``storageNodes`` maps a key to a storage node. The nodes are added to a scratch scene whose root
    directory is the slot folder, so that file names are written relative to it and URL-encoded, exactly as a
    scene file would hold them. The returned elements keep the scratch IDs: the caller sets the ID and name.
    """
    if not storageNodes:
        return {}
    scratch = slicer.vtkMRMLScene()
    scratch.SetRootDirectory(slotDir)
    scratch.SetURL(os.path.join(slotDir, SCENE_FILE))
    keyOfId = {}
    for key, storageNode in storageNodes.items():
        scratch.AddNode(storageNode)
        keyOfId[storageNode.GetID()] = key
    scratch.SetSaveToXMLString(1)
    try:
        scratch.Commit()
    finally:
        scratch.SetSaveToXMLString(0)
    elements = {}
    for element in _parseXml(scratch.GetSceneXMLString()):
        key = keyOfId.get(element.get("id"))
        if key is not None:
            elements[key] = ET.tostring(element, encoding="unicode")
    for storageNode in storageNodes.values():
        scratch.RemoveNode(storageNode)
    return elements


#
# Worker-safe file writing
#


def replaceWithRetry(source, destination, attempts=5, delay=0.1):
    """``os.replace``, retried on PermissionError (Windows: antivirus or indexer holding the file). Worker-safe."""
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def _temporaryName(path):
    return f"{path}.{uuid.uuid4().hex[:8]}.part"


def atomicWriteBytes(path, data):
    """Write a file atomically (temporary file, fsync, replace). Worker-safe."""
    temporary = _temporaryName(path)
    try:
        with open(temporary, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        replaceWithRetry(temporary, path)
    except BaseException:
        _removeQuietly(temporary)
        raise


def atomicWriteText(path, text):
    """Write a UTF-8 text file atomically. Worker-safe."""
    atomicWriteBytes(path, text.encode("utf-8"))


def atomicWriteJson(path, value):
    atomicWriteText(path, json.dumps(value, indent=1, sort_keys=True, default=str))


def readJson(path):
    """Read a JSON file; None if it does not exist or cannot be read."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _removeQuietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def removeTree(path):
    """Delete a folder (no error if it does not exist). Worker-safe."""
    shutil.rmtree(path, ignore_errors=True)


def fsyncFile(path):
    """Flush a file written by another library to the disk. Worker-safe."""
    try:
        with open(path, "rb") as f:
            os.fsync(f.fileno())
    except OSError:
        pass


def treeSize(path):
    """Total size of the files in a folder, in bytes. Worker-safe."""
    total = 0
    for directory, _, fileNames in os.walk(path):
        for fileName in fileNames:
            try:
                total += os.path.getsize(os.path.join(directory, fileName))
            except OSError:
                pass
    return total


def writeScalarNrrd(path, arrayKji, ijkToRasRows, level=1):
    """Write a 3D scalar array (numpy, k-j-i order) as a NRRD file that Slicer reads as a scalar volume. Worker-safe.

    ``ijkToRasRows`` is the 4x4 IJK-to-RAS matrix as nested lists. ``level`` is the gzip level (0: raw).
    The file is written to ``<path>.part``, flushed to the disk and moved to ``path``.
    """
    import numpy as np

    nrrdType = _NRRD_TYPES.get(arrayKji.dtype.name)
    if nrrdType is None or arrayKji.ndim != 3:
        raise ValueError(f"writeScalarNrrd: unsupported array {arrayKji.dtype} {arrayKji.shape}")
    if sys.byteorder != "little":
        raise ValueError("writeScalarNrrd: big-endian hosts are not supported")
    ijkToLps = np.diag([-1.0, -1.0, 1.0, 1.0]) @ np.array(ijkToRasRows, dtype=float)
    directions = " ".join("(" + ",".join(repr(float(v)) for v in ijkToLps[:3, axis]) + ")" for axis in range(3))
    origin = "(" + ",".join(repr(float(v)) for v in ijkToLps[:3, 3]) + ")"
    header = [
        "NRRD0004",
        "# Complete NRRD file format specification at:",
        "# http://teem.sourceforge.net/nrrd/format.html",
        f"type: {nrrdType}",
        "dimension: 3",
        "space: left-posterior-superior",
        f"sizes: {arrayKji.shape[2]} {arrayKji.shape[1]} {arrayKji.shape[0]}",
        f"space directions: {directions}",
        "kinds: domain domain domain",
    ]
    if arrayKji.dtype.itemsize > 1:
        header.append("endian: little")
    header.append("encoding: " + ("gzip" if level > 0 else "raw"))
    header.append(f"space origin: {origin}")
    data = memoryview(np.ascontiguousarray(arrayKji)).cast("B")
    temporary = path + ".part"
    try:
        with open(temporary, "wb") as f:
            f.write(("\n".join(header) + "\n\n").encode("ascii"))
            compressor = zlib.compressobj(level, zlib.DEFLATED, 31) if level > 0 else None
            for start in range(0, len(data), _WRITE_BLOCK_BYTES):
                block = data[start : start + _WRITE_BLOCK_BYTES]
                f.write(compressor.compress(block) if compressor else block)
            if compressor:
                f.write(compressor.flush())
            f.flush()
            os.fsync(f.fileno())
        replaceWithRetry(temporary, path)
    except BaseException:
        _removeQuietly(temporary)
        raise
    return os.path.getsize(path)


#
# Background writer (one thread; SPEC D19)
#


class BackgroundWriter:
    """One daemon thread that runs file jobs in order. Callbacks run on the main thread.

    ``submit()`` must be called on the main thread. Jobs must not use Qt, VTK or MRML (only files and numpy).
    Results are passed back through a deque that a QTimer drains on the main thread while jobs are pending.

    The Slicer main thread keeps the Python GIL while it waits in the Qt event loop, so a Python thread only runs
    while the main thread runs Python code. While jobs are pending, the timer therefore also yields the GIL
    briefly (``GIL_YIELD_S``) every ``POLL_INTERVAL_MS``: the main thread is then blocked for at most a few
    milliseconds per tick (the Python switch interval), which keeps the user interface responsive.
    """

    POLL_INTERVAL_MS = 10
    GIL_YIELD_S = 0.001

    def __init__(self):
        self._jobs = queue.Queue()
        self._results = collections.deque()
        self._lock = threading.Lock()
        self._pending = 0
        self._timer = qt.QTimer()
        self._timer.setInterval(self.POLL_INTERVAL_MS)
        self._timer.connect("timeout()", self._tick)
        self._thread = threading.Thread(target=self._run, name="StudioAutosaveWriter", daemon=True)
        self._thread.start()

    def submit(self, function, *args, onDone=None, onError=None):
        with self._lock:
            self._pending += 1
        self._jobs.put((function, args, onDone, onError))
        if not self._timer.isActive():
            self._timer.start()

    def pending(self):
        """Number of jobs that are not finished, or whose callback did not run yet."""
        with self._lock:
            return self._pending + len(self._results)

    def _run(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            function, args, onDone, onError = job
            try:
                result = function(*args)
                self._results.append((onDone, result, None))
            except BaseException as error:
                self._results.append((onError, error, traceback.format_exc()))
            finally:
                with self._lock:
                    self._pending -= 1

    def _tick(self):
        self._drainResults()
        if self.pending() > 0:
            # Let the writer thread run (see the class documentation)
            time.sleep(self.GIL_YIELD_S)
            self._drainResults()

    def _drainResults(self):
        while self._results:
            callback, value, failure = self._results.popleft()
            if failure is not None and callback is None:
                logging.error(f"Autosave background job failed:\n{failure}")
                continue
            if callback is None:
                continue
            try:
                callback(value)
            except Exception:
                logging.error("Autosave callback failed:\n" + traceback.format_exc())
        if self.pending() == 0 and self._timer.isActive():
            self._timer.stop()

    def drain(self, timeoutS=3.0):
        """Wait (on the main thread, without processing events) until every job finished and its callback ran.

        Return True if everything finished within ``timeoutS``.
        """
        deadline = time.monotonic() + timeoutS
        while True:
            self._drainResults()
            if self.pending() == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def shutdown(self, timeoutS=3.0):
        finished = self.drain(timeoutS)
        self._jobs.put(None)
        self._thread.join(max(0.0, timeoutS))
        self._timer.stop()
        return finished


class InlineExecutor:
    """Runs "background" jobs immediately on the calling thread (synchronous generations, tests)."""

    def submit(self, function, *args, onDone=None, onError=None):
        try:
            result = function(*args)
        except Exception as error:
            if onError is None:
                raise
            onError(error)
            return
        if onDone is not None:
            onDone(result)

    def pending(self):
        return 0

    def drain(self, timeoutS=0):
        return True


_backgroundWriter = None


def backgroundWriter():
    """The process-wide background writer (created on first use, on the main thread)."""
    global _backgroundWriter
    if _backgroundWriter is None:
        _backgroundWriter = BackgroundWriter()
    return _backgroundWriter


#
# Scene XML post-processing (worker-safe)
#

_BARE_AMPERSAND = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#[0-9]+|#x[0-9a-fA-F]+);)")


def _parseXml(text):
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        # MRML writes some attribute values (file names, parameters) without escaping "&"
        return ET.fromstring(_BARE_AMPERSAND.sub("&amp;", text))


def _isUrl(path):
    return "://" in path


def postProcessSceneXml(xml, rootDirectory, redirects, additions):
    """Turn the string commit of the live scene into the autosave scene file. Worker-safe.

    - File names of storage nodes that are not redirected become absolute (they were relative to the live
      scene root directory ``rootDirectory``), so that referenced originals are found from the autosave folder.
    - ``redirects`` ``{storage node ID: element XML or {"fileName": path relative to the slot}}``: the storage
      element is replaced in place (keeping its ID and name; its class may change), or its file name is changed.
    - ``additions`` ``{node ID: (new storage node ID, element XML)}``: storage elements for storable nodes that
      have no storage node, inserted after the node element and referenced by it.
    """
    root = _parseXml(xml)
    for element in root.iter():
        if element.get("id") in redirects:
            continue
        for attribute, value in list(element.attrib.items()):
            if attribute != "fileName" and not attribute.startswith("fileListMember"):
                continue
            path = urlDecode(value)
            if path and not _isUrl(path) and not os.path.isabs(path):
                path = os.path.normpath(os.path.join(rootDirectory or "", path))
            element.set(attribute, urlEncode(path))
    children = list(root)
    for index, element in enumerate(children):
        storageId = element.get("id")
        if storageId not in redirects:
            continue
        redirect = redirects[storageId]
        if isinstance(redirect, dict):
            for attribute in [a for a in element.attrib if a.startswith("fileListMember")]:
                del element.attrib[attribute]
            element.set("fileName", urlEncode(redirect["fileName"]))
            continue
        replacement = _parseXml(redirect)
        replacement.set("id", storageId)
        replacement.set("name", element.get("name") or storageId)
        root[index] = replacement
    if additions:
        for nodeId, (storageId, elementXml) in additions.items():
            children = list(root)
            for index, element in enumerate(children):
                if element.get("id") != nodeId:
                    continue
                addition = _parseXml(elementXml)
                addition.set("id", storageId)
                addition.set("name", storageId)
                root.insert(index + 1, addition)
                element.set("references", (element.get("references") or "") + f"storage:{storageId};")
                break
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


#
# Generations
#


class GenerationAborted(Exception):
    pass


def sceneIsBusy(scene):
    """True while the scene is closed, imported, batch-processed or restored: it is not autosaved then."""
    return bool(scene.IsClosing() or scene.IsImporting() or scene.IsBatchProcessing() or scene.IsRestoring())


class GenerationPlan:
    """One autosave generation: what to write, the jobs to run and the result."""

    def __init__(self, slotDir, generation):
        self.slotDir = slotDir
        self.generation = generation
        self.xml = ""
        self.rootDirectory = ""
        self.sceneUrl = ""
        self.fingerprint = None
        self.entries = {}
        self.oldEntries = {}
        #: main-thread jobs (see SlotWriter.runMainThreadStep)
        self.jobs = collections.deque()
        #: worker jobs submitted at plan time (copies)
        self.workerJobs = []
        self.newDirs = []
        self.newFiles = []
        #: storage nodes whose XML element is needed: {node ID: detached storage node}
        self.pendingStorage = {}
        #: scene XML redirections (see postProcessSceneXml), set by the last main-thread job
        self.redirects = {}
        self.additions = {}
        self.nodeIds = set()
        self.summary = {}
        self.metrics = {"planMs": 0.0, "mainThreadMs": 0.0, "maxSliceMs": 0.0, "workerMs": 0.0, "bytesWritten": 0, "nodesWritten": 0, "generation": generation}
        #: set by ``SlotWriter.abort()`` (main thread) or by a failed background write (worker)
        self.cancelled = False
        self.abortReason = None
        #: ``SlotWriter.abort()`` ran (its clean-up is queued)
        self.aborted = False
        #: ``finalize()`` started replacing ``Scene.mrml``: the new data folders belong to the slot from then on
        self.committed = False
        #: True if some modified data could not be autosaved in this generation (heavy node deferred, snapshot changed)
        self.incomplete = False
        self.mainThreadDone = False
        self.finished = False
        self.result = None
        self.snapshotBytesInFlight = 0
        self.wroteData = False
        self.unsaved = False


class SlotWriter:
    """Writes the generations of one autosave slot (``scene-<n>/``).

    ``plan()`` (main thread) decides what to write; ``runMainThreadStep()`` (main thread, repeatedly, from a timer)
    writes small data through detached C++ storage nodes and copies large volumes into numpy snapshots by slabs;
    ``finalize()`` (worker) assembles ``Scene.mrml`` and ``manifest.json`` so that a crash at any point leaves a
    loadable scene whose files exist.
    """

    def __init__(self, slotDir, executor=None, options=None):
        self.slotDir = slotDir
        self.executor = executor if executor is not None else backgroundWriter()
        self.options = {
            "maxNodeBytes": DEFAULT_SETTINGS["MaxNodeMB"] << 20,
            "autosaveRoot": os.path.dirname(os.path.dirname(slotDir)),
            "volatileRoots": None,
            "systemTemporaryIsVolatile": True,
            "snapshotBudgetBytes": snapshotBudgetBytes(),
            "idleSeconds": None,
        }
        self.options.update(options or {})
        manifest = readJson(os.path.join(slotDir, MANIFEST_FILE)) or {}
        self.entries = dict(manifest.get("entries") or {})
        self.generation = int(manifest.get("generation") or 0)
        #: highest generation number planned: aborted generations never reuse a number, so the clean-up of an
        #: aborted generation cannot delete the folders of a later one
        self.plannedGeneration = self.generation
        #: plan whose ``finalize()`` is queued and whose result did not come back yet
        self.finalizing = None
        #: nodes whose numpy snapshot could not be written: they are written through their C++ storage node
        self.snapshotFailures = set()
        self._reportedFailures = set()
        self.lastResult = None

    def manifestPath(self):
        return os.path.join(self.slotDir, MANIFEST_FILE)

    def hasContent(self):
        return os.path.exists(os.path.join(self.slotDir, SCENE_FILE))

    #
    # Plan (main thread)
    #

    def plan(self, scene, fingerprint=None, idleSeconds=None, now=None, ignoreBusy=False):
        """Plan a generation of ``scene``; raise GenerationAborted if the scene cannot be autosaved now.

        ``ignoreBusy``: plan also while the scene is closing (final generation before a scene is closed).
        """
        startTime = time.perf_counter()
        if not ignoreBusy and sceneIsBusy(scene):
            raise GenerationAborted("the scene is busy")
        if self.finalizing is not None:
            # An aborted generation may still commit: plan from its result
            raise GenerationAborted("the previous generation is still being written")
        now = time.time() if now is None else now
        self.plannedGeneration = max(self.generation, self.plannedGeneration) + 1
        plan = GenerationPlan(self.slotDir, self.plannedGeneration)
        plan.xml, plan.rootDirectory = sceneXml(scene)
        # After the commit: writing the XML may itself modify a node once (the subject hierarchy version attribute)
        plan.fingerprint = sceneFingerprint(scene, sentinelIds())
        plan.sceneUrl = scene.GetURL() or ""
        plan.oldEntries = self.entries
        plan.unsaved = bool(scene.GetStorableNodesModifiedSinceRead())
        roots = self.options["volatileRoots"]
        if roots is None:
            roots = volatileRoots(self.options["autosaveRoot"])
        maxNodeBytes = self.options["maxNodeBytes"]
        storableNodes = scene.GetNodesByClass("vtkMRMLStorableNode")
        snapshotJobs = []
        for index in range(storableNodes.GetNumberOfItems()):
            node = storableNodes.GetItemAsObject(index)
            if not node.GetSaveWithScene():
                continue
            storageNode = node.GetStorageNode()
            if storageNode is None and not defaultStorageNodeClassName(node):
                continue  # stored in the scene file only (no data file)
            if storageNode is not None and not storageNode.GetSaveWithScene():
                continue
            nodeId = node.GetID()
            plan.nodeIds.add(nodeId)
            old = self.entries.get(nodeId)
            kind, files, stat = classify(node, old, plan.rootDirectory, roots, maxNodeBytes, self.options["systemTemporaryIsVolatile"])
            entry = {
                "kind": kind,
                "class": node.GetClassName(),
                "name": node.GetName() or "",
                "original": storageNode.GetFileName() if storageNode is not None and storageNode.GetFileName() else None,
                "storageId": storageNode.GetID() if storageNode is not None else None,
                "originalStorageClass": storageNode.GetClassName() if storageNode is not None and storageNode.GetFileName() else None,
                "contentMTime": contentMTime(node),
            }
            sameFiles = bool(old) and old.get("files") == files
            if kind == "reference":
                entry.update(files=files, stat=stat, firstStat=(old.get("firstStat") if sameFiles else None) or stat)
                plan.entries[nodeId] = entry
                continue
            if kind == "skip":
                entry.update(reason="too large", bytes=estimateBytes(node))
                if old and old.get("dir") and os.path.isdir(os.path.join(self.slotDir, old["dir"])) and old.get("kind") in ("write", "copy"):
                    # Keep the previous autosave of the node, flagged as stale
                    entry = dict(old, stale=True, reason="too large")
                plan.entries[nodeId] = entry
                plan.incomplete = plan.incomplete or old is None
                continue
            if kind == "copy":
                entry.update(files=files, stat=stat, firstStat=(old.get("firstStat") if sameFiles else None) or stat)
                if old and old.get("kind") == "copy" and old.get("files") == files and old.get("stat") == stat and self._dirExists(old):
                    entry.update(dir=old["dir"], file=old["file"], bytes=old.get("bytes", 0))
                else:
                    self._planCopy(plan, nodeId, entry, files[0])
                plan.entries[nodeId] = entry
                continue
            # write
            if old and old.get("kind") == "write" and old.get("contentMTime") == entry["contentMTime"] and old.get("class") == entry["class"] and self._dirExists(old):
                kept = dict(old)
                kept.update(name=entry["name"], original=entry["original"], storageId=entry["storageId"], originalStorageClass=entry["originalStorageClass"])
                kept.pop("stale", None)
                plan.entries[nodeId] = kept
                continue
            if old and old.get("heavy"):
                idle = idleSeconds if idleSeconds is not None else HEAVY_IDLE_S
                if now - float(old.get("writtenAt") or 0) < HEAVY_INTERVAL_S or idle < HEAVY_IDLE_S:
                    plan.incomplete = True
                    if self._dirExists(old) and old.get("kind") == "write":
                        plan.entries[nodeId] = dict(old, stale=True, name=entry["name"], original=entry["original"], storageId=entry["storageId"])
                    else:
                        plan.entries[nodeId] = dict(entry, kind="deferred", reason="heavy")
                    continue
            entry["heavy"] = bool(old and old.get("heavy"))
            entry["lastWriteMs"] = (old or {}).get("lastWriteMs")
            entry["bytes"] = estimateBytes(node)
            plan.entries[nodeId] = entry
            if self._snapshotEligible(node, entry["bytes"]):
                snapshotJobs.append(_SnapshotJob(self, plan, nodeId, entry))
            else:
                plan.jobs.append(_WriteJob(self, plan, nodeId, entry))
        # Small writes first, then the snapshots of large volumes, then the storage XML
        plan.jobs.extend(snapshotJobs)
        plan.jobs.append(_StorageXmlJob(self, plan))
        plan.metrics["planMs"] = (time.perf_counter() - startTime) * 1000.0
        return plan

    def submitWorkerJobs(self, plan):
        """Queue the copies of the plan. They run after the main-thread jobs, so that the writer thread does not take
        the Python GIL from the main thread while it writes.
        """
        jobs, plan.workerJobs = plan.workerJobs, []
        for function, args in jobs:
            self.executor.submit(function, *args)

    def _dirExists(self, entry):
        return bool(entry.get("dir")) and os.path.isdir(os.path.join(self.slotDir, entry["dir"]))

    def _newDir(self, plan, nodeId):
        relative = f"{DATA_DIR}/{sanitizeFileName(nodeId, 80)}-g{plan.generation}"
        absolute = os.path.join(self.slotDir, relative)
        if os.path.isdir(absolute):
            removeTree(absolute)  # left over by an aborted generation
        os.makedirs(absolute)
        plan.newDirs.append(absolute)
        return relative, absolute

    def _planCopy(self, plan, nodeId, entry, source):
        relative, absolute = self._newDir(plan, nodeId)
        fileName = os.path.basename(source)
        entry.update(dir=relative, file=f"{relative}/{fileName}", bytes=entry["stat"][0][0])
        destination = os.path.join(absolute, fileName)
        plan.newFiles.append(destination)
        plan.metrics["bytesWritten"] += entry["bytes"]

        def copy(plan=plan, source=source, destination=destination):
            if plan.cancelled:
                return
            try:
                shutil.copy2(source, destination)
            except BaseException as error:
                # Seen by finalize() in this thread: the scene file never references a missing copy
                plan.abortReason = f"copying {os.path.basename(source)} failed: {error}"
                plan.cancelled = True
                raise

        plan.workerJobs.append((copy, ()))

    def _snapshotEligible(self, node, size):
        """True for a single-component scalar volume of at least SNAPSHOT_MIN_MB (written by ``writeScalarNrrd``)."""
        if size < SNAPSHOT_MIN_MB << 20 or size > self.options["snapshotBudgetBytes"]:
            return False
        if not node.IsA("vtkMRMLScalarVolumeNode") or node.GetID() in self.snapshotFailures:
            return False
        # Diffusion-weighted, tensor (DTI) and vector volumes keep their own file formats (C++ storage nodes)
        if node.IsA("vtkMRMLDiffusionWeightedVolumeNode") or node.IsA("vtkMRMLTensorVolumeNode"):
            return False
        imageData = node.GetImageData()
        if imageData is None or imageData.GetPointData() is None or imageData.GetPointData().GetScalars() is None:
            return False
        if imageData.GetNumberOfScalarComponents() != 1:
            return False
        return _NRRD_TYPES.get(_numpyTypeName(imageData)) is not None

    #
    # Main-thread steps
    #

    def runMainThreadStep(self, plan, scene=None, budgetMs=SLICE_BUDGET_MS, ignoreBusy=False):
        """Run main-thread jobs for at most about ``budgetMs``. Return True when every main-thread job is done.

        Raise GenerationAborted if a node of the plan was removed or the scene became busy.
        """
        scene = scene or slicer.mrmlScene
        if plan.cancelled:
            raise GenerationAborted(plan.abortReason or "cancelled")
        if not ignoreBusy and sceneIsBusy(scene):
            raise GenerationAborted("the scene is busy")
        start = time.perf_counter()
        while plan.jobs:
            job = plan.jobs[0]
            jobStart = time.perf_counter()
            try:
                finished = job.run(scene, start, budgetMs)
            except (GenerationAborted, OSError):
                raise
            except Exception as error:
                if not hasattr(job, "entry"):
                    raise
                # One node that cannot be autosaved does not stop the autosave of the others
                self._jobFailed(plan, job, error)
                finished = True
            label = getattr(job, "entry", {}).get("name") or type(job).__name__.strip("_")
            jobTimes = plan.metrics.setdefault("jobMs", {})
            jobTimes[label] = round(jobTimes.get(label, 0.0) + (time.perf_counter() - jobStart) * 1000.0, 2)
            if finished:
                plan.jobs.popleft()
            elapsed = (time.perf_counter() - start) * 1000.0
            if elapsed >= budgetMs or getattr(job, "waiting", False) or getattr(job, "yieldTick", False):
                break
        elapsed = (time.perf_counter() - start) * 1000.0
        plan.metrics["mainThreadMs"] += elapsed
        plan.metrics["maxSliceMs"] = max(plan.metrics["maxSliceMs"], elapsed)
        if not plan.jobs:
            plan.mainThreadDone = True
        return plan.mainThreadDone

    def _jobFailed(self, plan, job, error):
        """A main-thread job raised: the node keeps its previous autosave (flagged stale) or is listed as not autosaved."""
        nodeId, entry = job.nodeId, job.entry
        if getattr(job, "inFlight", 0):
            plan.snapshotBytesInFlight -= job.inFlight
            job.inFlight = 0
        release = getattr(job, "release", None)
        if release:
            release()
        if isinstance(job, _SnapshotJob):
            self.snapshotFailures.add(nodeId)  # the next generation uses the C++ storage node
        plan.pendingStorage.pop(nodeId, None)
        plan.incomplete = True
        if nodeId not in self._reportedFailures:
            self._reportedFailures.add(nodeId)
            logging.warning(f"Autosave: {entry.get('name') or nodeId} could not be autosaved:\n" + traceback.format_exc())
        old = plan.oldEntries.get(nodeId)
        if old and old.get("kind") == "write" and self._dirExists(old):
            plan.entries[nodeId] = dict(old, stale=True, reason="autosave failed")
        else:
            for key in ("dir", "file", "storageXml", "storageClass"):
                entry.pop(key, None)
            entry.update(kind="error", reason=f"autosave failed: {type(error).__name__}")
            plan.entries[nodeId] = entry

    def submitFinalize(self, plan, onDone=None, onError=None):
        """Queue ``finalize()`` in the background writer (after the data writes of the plan).

        ``onDone``/``onError`` run on the main thread. A plan aborted while its ``finalize()`` is queued or running
        may still commit (see ``abort()``); ``_finished()`` then records its result, and no new generation is
        planned until that result came back.
        """

        def done(result):
            if self.finalizing is plan:
                self.finalizing = None
            self._finished(plan, result)
            if onDone:
                onDone(plan)

        def failed(error):
            if self.finalizing is plan:
                self.finalizing = None
            self.abort(plan, f"writing failed: {error}")
            if onError:
                onError(plan, error)

        self.submitWorkerJobs(plan)
        self.finalizing = plan
        self.executor.submit(self.finalize, plan, onDone=done, onError=failed)

    def _finished(self, plan, result):
        plan.finished = True
        plan.result = result
        self.entries = plan.entries
        self.generation = max(self.generation, plan.generation)
        self.lastResult = result

    def finalize(self, plan):
        """Write ``Scene.mrml`` and ``manifest.json`` of a generation and delete superseded data. Worker-safe."""
        start = time.perf_counter()
        if plan.cancelled:
            raise GenerationAborted(plan.abortReason or "cancelled")
        for path in plan.newFiles:
            fsyncFile(path)
        redirects, additions = plan.redirects, plan.additions
        sceneText = postProcessSceneXml(plan.xml, plan.rootDirectory, redirects, additions)
        manifestPath = os.path.join(self.slotDir, MANIFEST_FILE)
        manifest = {
            "format": FORMAT,
            "version": VERSION,
            "generation": plan.generation,
            "written": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "pending": True,
            "scene": {"url": plan.sceneUrl, "rootDirectory": plan.rootDirectory},
            "entries": dict(plan.oldEntries, **plan.entries),
        }
        if plan.cancelled:
            raise GenerationAborted(plan.abortReason or "cancelled")
        # While Scene.mrml is replaced, the manifest describes the directories of both generations
        atomicWriteJson(manifestPath, manifest)
        # Commit point. abort() may run on the main thread from here on, but its clean-up runs in this thread after
        # finalize() and keeps the folders that the new Scene.mrml references
        plan.committed = True
        try:
            atomicWriteText(os.path.join(self.slotDir, SCENE_FILE), sceneText)
        except BaseException:
            plan.committed = False  # the previous Scene.mrml is in place (atomic replacement)
            raise
        manifest["pending"] = False
        manifest["entries"] = plan.entries
        atomicWriteJson(manifestPath, manifest)
        keep = {entry["dir"] for entry in plan.entries.values() if entry.get("dir")}
        dataDir = os.path.join(self.slotDir, DATA_DIR)
        if os.path.isdir(dataDir):
            for name in os.listdir(dataDir):
                if f"{DATA_DIR}/{name}" not in keep:
                    removeTree(os.path.join(dataDir, name))
        bytesTotal = treeSize(self.slotDir)
        workerMs = (time.perf_counter() - start) * 1000.0
        return {"bytes": bytesTotal, "workerMs": workerMs, "generation": plan.generation}

    def abort(self, plan, reason):
        """Cancel a generation: its new directories are deleted (in the background writer, after its pending jobs).

        If ``finalize()`` already reached its commit point (``plan.committed``), the generation is complete on disk
        and its folders are kept: the clean-up runs in the background writer after ``finalize()``, so it sees the flag.
        """
        if plan.aborted or plan.finished:
            return
        plan.aborted = True
        if not plan.cancelled:
            plan.abortReason = reason
            plan.cancelled = True
        for job in plan.jobs:
            release = getattr(job, "release", None)
            if release:
                release()
        plan.jobs.clear()
        newDirs = list(plan.newDirs)

        def cleanup():
            if plan.committed:
                return
            for path in newDirs:
                removeTree(path)

        self.executor.submit(cleanup)

    def writeSnapshot(self, scene):
        """Run a whole generation synchronously (tests, harness, final generation). Return the plan."""
        executor = self.executor
        self.executor = InlineExecutor()
        try:
            plan = self.plan(scene, idleSeconds=HEAVY_IDLE_S, now=time.time() + HEAVY_INTERVAL_S)
            while not self.runMainThreadStep(plan, scene, budgetMs=1e9):
                pass
            self.submitWorkerJobs(plan)
            result = self.finalize(plan)
            self._finished(plan, result)
            plan.metrics["workerMs"] = result["workerMs"]
            return plan
        finally:
            self.executor = executor

    def deleteSlot(self):
        self.executor.submit(removeTree, self.slotDir)


def writeSnapshot(scene, slotDir, options=None):
    """Write an autosave of ``scene`` into ``slotDir`` synchronously (tests, compatibility harness). Return the plan."""
    os.makedirs(slotDir, exist_ok=True)
    return SlotWriter(slotDir, InlineExecutor(), options).writeSnapshot(scene)


def _numpyTypeName(imageData):
    import vtk
    from vtk.util import numpy_support

    try:
        return numpy_support.get_numpy_array_type(imageData.GetScalarType())().dtype.name
    except Exception:
        return {vtk.VTK_SHORT: "int16", vtk.VTK_UNSIGNED_CHAR: "uint8", vtk.VTK_FLOAT: "float32"}.get(imageData.GetScalarType(), "")


def snapshotBudgetBytes():
    """Memory allowed for numpy snapshots in flight: min(1 GB, 25% of the RAM); 8 GB of RAM assumed if unknown."""
    ram = None
    try:
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        elif os.name == "nt":
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(_MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                ram = status.ullTotalPhys
    except Exception:
        ram = None
    ram = ram or (8 << 30)
    return min(1 << 30, ram // 4)


class _WriteJob:
    """Write one node through a detached C++ storage node (main thread)."""

    def __init__(self, writer, plan, nodeId, entry):
        self.writer, self.plan, self.nodeId, self.entry = writer, plan, nodeId, entry

    def run(self, scene, sliceStart, budgetMs):
        node = scene.GetNodeByID(self.nodeId)
        if node is None:
            raise GenerationAborted(f"{self.nodeId} was removed")
        entry = self.entry
        storageNode = node.CreateDefaultStorageNode()
        if storageNode is None:
            entry.update(kind="skip", reason="no file format")
            return True
        extension = None
        if storageNode.IsA("vtkMRMLSegmentationStorageNode"):
            storageNode.SetCropToMinimumExtent(1)
            storageNode.SetUseCompression(0 if (entry.get("lastWriteMs") or 0) > UNCOMPRESSED_SEGMENTATION_MS else 1)
            segmentation = node.GetSegmentation()
            extension = "seg.nrrd" if segmentation is None or segmentation.IsSourceRepresentationImageData() else "seg.vtm"
        else:
            if storageNode.IsA("vtkMRMLVolumeArchetypeStorageNode") or storageNode.IsA("vtkMRMLNRRDStorageNode"):
                storageNode.SetUseCompression(0)
            try:
                extension = storageNode.GetDefaultWriteFileExtension()
            except Exception:
                extension = None
        if not extension:
            entry.update(kind="skip", reason="no file format")
            return True
        relative, absolute = self.writer._newDir(self.plan, self.nodeId)
        path = os.path.join(absolute, f"{sanitizeFileName(node.GetName())}.{extension}")
        storageNode.SetFileName(path)
        start = time.perf_counter()
        ok = storageNode.WriteData(node)
        elapsed = (time.perf_counter() - start) * 1000.0
        entry["lastWriteMs"] = round(elapsed, 2)
        entry["writtenAt"] = time.time()
        if elapsed > HEAVY_WRITE_MS:
            entry["heavy"] = True
        if not ok or not os.path.exists(path):
            # Keep the previous autosave of the node, if any; the scene stays dirty
            old = self.plan.oldEntries.get(self.nodeId)
            self.plan.incomplete = True
            if old and old.get("kind") == "write" and self.writer._dirExists(old):
                self.plan.entries[self.nodeId] = dict(old, stale=True, reason="write failed")
            else:
                entry.update(kind="error", reason="write failed")
            return True
        entry.update(dir=relative, file=f"{relative}/{os.path.basename(path)}")
        files = [os.path.join(absolute, name) for name in os.listdir(absolute)]
        entry["bytes"] = sum(os.path.getsize(f) for f in files)
        self.plan.newFiles.extend(files)
        self.plan.metrics["bytesWritten"] += entry["bytes"]
        self.plan.metrics["nodesWritten"] += 1
        entry["storageClass"] = storageNode.GetClassName()
        self.plan.pendingStorage[self.nodeId] = storageNode
        self.plan.wroteData = True
        return True


def _prefault(array, chunkBytes=SNAPSHOT_CHUNK_MB << 20):
    """Start a daemon thread that writes zeros to the (contiguous) numpy ``array`` by chunks, so that its memory pages are
    mapped before the main thread copies into it; return the thread. numpy releases the GIL while it fills large arrays, and
    the thread touches nothing but this array (SPEC D19).
    """
    flat = array.reshape(-1)
    step = max(1, chunkBytes // max(1, array.itemsize))

    def touch():
        for start in range(0, flat.size, step):
            flat[start : start + step] = 0

    thread = threading.Thread(target=touch, name="StudioAutosavePrefault", daemon=True)
    thread.start()
    return thread


class _SnapshotJob:
    """Copy a large scalar volume into a numpy array by slabs (main thread), then write it in the background."""

    def __init__(self, writer, plan, nodeId, entry):
        self.writer, self.plan, self.nodeId, self.entry = writer, plan, nodeId, entry
        self.snapshot = None
        self.source = None
        self.imageData = None
        self.scalars = None
        self.nextSlice = 0
        self.waiting = False
        self.yieldTick = False  # stopped before a slab that would end the tick after the budget
        self.maxSlabMs = 0.0  # the longest slab copy of this snapshot (the copy slows down while other threads are busy)
        self.prefault = None  # the thread that maps the pages of the snapshot buffer (_prefault())
        self.inFlight = 0

    def release(self):
        self.snapshot = self.source = self.imageData = self.scalars = None

    def run(self, scene, sliceStart, budgetMs):
        import numpy as np
        import vtk

        node = scene.GetNodeByID(self.nodeId)
        if node is None:
            raise GenerationAborted(f"{self.nodeId} was removed")
        if self.snapshot is None:
            size = self.entry["bytes"]
            if self.plan.snapshotBytesInFlight > 0 and self.plan.snapshotBytesInFlight + size > self.writer.options["snapshotBudgetBytes"]:
                # Wait until the background writer has written earlier snapshots
                self.waiting = True
                return False
            self.waiting = False
            self.imageData = node.GetImageData()
            # Python references keep the image memory alive between ticks
            self.scalars = self.imageData.GetPointData().GetScalars()
            self.source = slicer.util.arrayFromVolume(node)
            self.snapshot = np.empty_like(self.source)
            self.mtimes = (self.imageData.GetMTime(), self.scalars.GetMTime(), node.GetMTime())
            self.plan.snapshotBytesInFlight += size
            self.inFlight = size
            # The first write to new memory costs page faults (up to 150 ms per 4 MB slab measured on a virtual machine,
            # 1 ms once the pages are mapped): a worker thread maps the pages before the main thread copies into them
            self.prefault = _prefault(self.snapshot)
        if self.prefault is not None:
            if self.prefault.is_alive():
                self.waiting = True
                return False
            self.prefault = None
            self.waiting = False
        rowBytes = max(1, self.source[0].nbytes)
        rowsPerSlab = max(1, (SNAPSHOT_CHUNK_MB << 20) // rowBytes)
        depth = self.source.shape[0]
        copied = False
        self.yieldTick = False
        while self.nextSlice < depth:
            # Stop before a slab that would end the tick after the budget (judged by the longest slab so far)
            if copied and (time.perf_counter() - sliceStart) * 1000.0 + self.maxSlabMs > budgetMs:
                self.yieldTick = True
                break
            slabStart = time.perf_counter()
            end = min(depth, self.nextSlice + rowsPerSlab)
            self.snapshot[self.nextSlice : end] = self.source[self.nextSlice : end]
            self.nextSlice = end
            copied = True
            now = time.perf_counter()
            self.maxSlabMs = max(self.maxSlabMs, (now - slabStart) * 1000.0)
            if (now - sliceStart) * 1000.0 >= budgetMs:
                break
        if self.nextSlice < depth:
            return False
        # Discard the snapshot if the volume changed while it was copied
        if (self.imageData.GetMTime(), self.scalars.GetMTime(), node.GetMTime()) != self.mtimes or node.GetImageData() is not self.imageData:
            self.plan.snapshotBytesInFlight -= self.inFlight
            self.release()
            self._changedWhileCopied()
            return True
        matrix = vtk.vtkMatrix4x4()
        node.GetIJKToRASMatrix(matrix)
        rows = [[matrix.GetElement(r, c) for c in range(4)] for r in range(4)]
        return self._submit(node, rows)

    def _changedWhileCopied(self):
        """The volume changed while it was copied: the snapshot was discarded."""
        old = self.plan.oldEntries.get(self.nodeId)
        self.plan.incomplete = True
        if old and old.get("kind") == "write" and self.writer._dirExists(old):
            self.plan.entries[self.nodeId] = dict(old, stale=True)
        else:
            self.entry.update(kind="deferred", reason="changed while autosaving")

    def _submit(self, node, rows):
        """Write the complete snapshot in the background writer (main thread: plans the file, the worker writes it)."""
        relative, absolute = self.writer._newDir(self.plan, self.nodeId)
        path = os.path.join(absolute, f"{sanitizeFileName(node.GetName())}.nrrd")
        storageNode = node.CreateDefaultStorageNode()
        storageNode.SetFileName(path)
        self.entry.update(dir=relative, file=f"{relative}/{os.path.basename(path)}", writtenAt=time.time(), lastWriteMs=0.0, storageClass=storageNode.GetClassName())
        self.plan.newFiles.append(path)
        self.plan.pendingStorage[self.nodeId] = storageNode
        self.plan.metrics["nodesWritten"] += 1
        self.plan.wroteData = True
        snapshot, plan, entry, inFlight = self.snapshot, self.plan, self.entry, self.inFlight
        self.release()

        writer, nodeId = self.writer, self.nodeId

        def write():
            if plan.cancelled:
                return 0
            start = time.perf_counter()
            try:
                written = writeScalarNrrd(path, snapshot, rows, level=1)
            except BaseException as error:
                # Seen by finalize() in this thread: the scene file never references a file that was not written
                plan.abortReason = f"writing {entry.get('name')} failed: {error}"
                plan.cancelled = True
                if not isinstance(error, OSError):
                    writer.snapshotFailures.add(nodeId)  # the next generation uses the C++ storage node
                raise
            return written, (time.perf_counter() - start) * 1000.0

        def done(result):
            plan.snapshotBytesInFlight -= inFlight
            if result:
                entry["bytes"] = result[0]
                plan.metrics["bytesWritten"] += result[0]
                plan.metrics["workerMs"] += result[1]

        def failed(error):
            plan.snapshotBytesInFlight -= inFlight

        self.writer.executor.submit(write, onDone=done, onError=failed)
        return True


class _StorageXmlJob:
    """Last main-thread job: storage XML of the files written, and the redirections of the scene XML."""

    def __init__(self, writer, plan):
        self.writer, self.plan = writer, plan

    def run(self, scene, sliceStart, budgetMs):
        plan = self.plan
        elements = storageXmlFor(plan.pendingStorage, self.writer.slotDir)
        plan.pendingStorage = {}
        for nodeId, element in elements.items():
            plan.entries[nodeId]["storageXml"] = element
        redirects, additions = {}, {}
        usedIds = set()
        for nodeId, entry in plan.entries.items():
            kind = entry.get("kind")
            storageId = entry.get("storageId")
            if kind == "copy" and storageId and entry.get("file"):
                redirects[storageId] = {"fileName": entry["file"]}
            elif kind == "write" and entry.get("storageXml"):
                if storageId:
                    redirects[storageId] = entry["storageXml"]
                else:
                    storageClass = entry.get("storageClass") or "vtkMRMLStorageNode"
                    k = 1
                    while True:
                        newId = f"{storageClass}Autosave{k}"
                        if newId not in usedIds and scene.GetNodeByID(newId) is None:
                            break
                        k += 1
                    usedIds.add(newId)
                    additions[nodeId] = (newId, entry["storageXml"])
        plan.redirects, plan.additions = redirects, additions
        # After the writes: nodes that could not be written are listed as not autosaved
        plan.summary = summarizeScene(scene, plan.entries)
        return True


#
# Summary (main thread)
#


def isNetworkMapNode(node):
    """True for a lesion network mapping result volume (never shown in autosave thumbnails or names)."""
    return node is not None and node.GetAttribute("LNM.Result") == "1"


def summarizeScene(scene, entries=None):
    """What a slot contains, for the recovery user interface."""
    summary = {"title": "", "volumes": [], "segmentations": [], "markups": 0, "tables": 0, "models": 0, "other": 0, "notAutosaved": []}
    entries = entries or {}
    url = scene.GetURL()
    for nodeId, entry in entries.items():
        node = scene.GetNodeByID(nodeId)
        if node is None:
            continue
        if entry.get("kind") in ("skip", "error", "deferred") or entry.get("stale"):
            summary["notAutosaved"].append({"name": node.GetName() if not isNetworkMapNode(node) else "", "reason": entry.get("reason") or "not written"})
        if node.IsA("vtkMRMLVolumeNode"):
            if isNetworkMapNode(node):
                summary["other"] += 1
            else:
                summary["volumes"].append(node.GetName())
        elif node.IsA("vtkMRMLSegmentationNode"):
            segmentation = node.GetSegmentation()
            names = [segmentation.GetNthSegment(i).GetName() for i in range(min(5, segmentation.GetNumberOfSegments()))]
            summary["segmentations"].append({"name": node.GetName(), "segments": names, "count": segmentation.GetNumberOfSegments()})
        elif node.IsA("vtkMRMLMarkupsNode"):
            summary["markups"] += 1
        elif node.IsA("vtkMRMLTableNode"):
            summary["tables"] += 1
        elif node.IsA("vtkMRMLModelNode"):
            summary["models"] += 1
        else:
            summary["other"] += 1
    if url:
        summary["title"] = os.path.splitext(os.path.basename(url))[0]
    elif summary["volumes"]:
        summary["title"] = summary["volumes"][0]
    elif summary["segmentations"]:
        summary["title"] = summary["segmentations"][0]["name"]
    return summary


#
# Projects (A-09, D-A16): the scene saved in place in a project folder of the Slicer Studio folder
#

#: Prefix of the temporary folders and scene files of a project generation (removed when the generation ends)
PROJECT_TEMPORARY_PREFIX = ".studio-tmp-"
#: Suffix of the name of the file Studio writes when another program changed the file of a node since Studio wrote it
CONFLICT_SUFFIX = " (Slicer Studio)"
#: Files that refer to data files next to them (NRRD and MetaImage detached headers, Analyze headers, multi-block
#: surfaces): a node read from one of these outside the project is written into the project, never copied file by file
DETACHED_EXTENSIONS = (".nhdr", ".seg.nhdr", ".seq.nhdr", ".mhd", ".hdr", ".vtm", ".seg.vtm")
#: Files that official Slicer saved next to the scene file and that a generation moves into their kind folder (single
#: files only: a .nhdr, a table and its schema or a DICOM series stay where they are)
MOVABLE_EXTENSIONS = (".nrrd", ".nii", ".nii.gz", ".mha", ".seg.nrrd", ".mrk.json", ".vtk", ".vtp", ".stl", ".obj", ".ply", ".h5", ".tfm",
                      ".ctbl", ".txt")


def _projects():
    from StudioLib import projects

    return projects


def _statOrNone(path):
    try:
        return _fileStat(path)
    except OSError:
        return None


def _storagePaths(storageNode, rootDirectory):
    if storageNode is None or not storageNode.GetFileName() or storageNode.GetURI():
        return []
    return storageFiles(storageNode, rootDirectory)


def _setSingleFile(storageNode, path):
    storageNode.SetFileName(path)
    storageNode.ResetFileNameList()


def markStoredWithFile(scene, node, path):
    """Make ``node`` "not modified since read" with ``path`` as its file, without writing its data again (main thread).

    Used after the background writer wrote a snapshot of a large volume: a new storage node of the node's default class
    is marked as written by writing a 1-voxel volume of the same class to a temporary file, then it takes ``path`` as
    its file and replaces the node's storage node. The content of ``path`` is not changed.
    """
    import vtk

    newStorage = node.CreateDefaultStorageNode()
    if newStorage is None:
        return False
    try:
        marker = node.NewInstance()
    except Exception:
        marker = slicer.vtkMRMLScalarVolumeNode()
    imageData = vtk.vtkImageData()
    imageData.SetDimensions(1, 1, 1)
    imageData.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
    marker.SetAndObserveImageData(imageData)
    extension = newStorage.GetDefaultWriteFileExtension() or "nrrd"
    temporary = os.path.join(tempfile.gettempdir(), f"studio-marker-{uuid.uuid4().hex[:8]}.{extension}")
    newStorage.SetFileName(temporary)
    try:
        written = newStorage.WriteData(marker)
    finally:
        _removeQuietly(temporary)
    if not written:
        return False
    _setSingleFile(newStorage, path)
    old = node.GetStorageNode()
    scene.AddNode(newStorage)
    node.SetAndObserveStorageNodeID(newStorage.GetID())
    if old is not None and old.GetScene() is scene:
        scene.RemoveNode(old)
    return not node.GetModifiedSinceRead()


def _writeExtension(node, storageNode):
    """The file extension (".seg.nrrd", ".nrrd", ".mrk.json"...) a node is written with, or None."""
    if storageNode.IsA("vtkMRMLSegmentationStorageNode"):
        segmentation = node.GetSegmentation()
        return ".seg.nrrd" if segmentation is None or segmentation.IsSourceRepresentationImageData() else ".seg.vtm"
    try:
        extension = storageNode.GetDefaultWriteFileExtension()
    except Exception:
        extension = None
    return f".{extension}" if extension else None


class ProjectPlan(GenerationPlan):
    """One generation of a project: what to copy and write, and the scene file to replace last."""

    def __init__(self, projectDir, generation):
        GenerationPlan.__init__(self, projectDir, generation)
        #: write every changed node now, also the heavy ones (Save now, closing, quitting)
        self.force = False
        #: copies and snapshot writes of the background writer that did not call back yet
        self.workerPending = 0
        self.workerSubmitted = False
        #: [(node ID, source, source stat, destination)]: unchanged files copied into the project
        self.imports = []
        self.copyJobs = []
        self.copied = set()
        #: node ID -> (destination, check): snapshots of large volumes written by the background writer
        self.snapshots = {}
        #: folder -> names planned in this generation (lower case)
        self.reserved = {}
        self.temporary = []
        #: [(path, stat)]: files of nodes that left the scene (moved to .studio/removed after the scene file is replaced)
        self.orphans = []
        #: [(name of the file another program changed, name of the file Studio wrote)]
        self.conflicts = []
        self.projectJson = None
        self.thumbnailPath = os.path.join(projectDir, ".studio", "thumbnail.png")


class ProjectWriter(SlotWriter):
    """Writes the scene in place into a project folder (A-09): a standard scene that is valid at every moment.

    - Unchanged data read from a file outside the project is copied byte for byte (background writer), then its storage
      node points at the copy. The original is never changed.
    - New or changed data is written by its own storage node into its kind folder (``projects.kindFolder()``), into a
      temporary folder first and then moved into place, so a file is replaced atomically. Large scalar volumes are copied
      into a numpy snapshot by slabs and written by the background writer (as A-01 does). Written nodes are then
      "not modified since read", so the exit prompt does not ask about saved work.
    - The scene file is written last (``vtkMRMLScene::Commit`` to a temporary file, then replaced), with paths relative
      to the project folder; files of nodes that left the scene are moved to ``.studio/removed``.
    - A file another program changed since Studio wrote it (size or time differ) is never overwritten: Studio writes
      "<name> (Slicer Studio).<ext>" and reports it in ``conflicts``.

    The scheduling is the autosave service's (``autosave.AutosaveService``): ``plan()``, ``runMainThreadStep()`` from a
    timer, then ``submitFinalize()``.
    """

    def __init__(self, project, executor=None, options=None):
        # Not SlotWriter.__init__: a project has no manifest and no generation folders
        self.project = project
        self.slotDir = project.path
        self.executor = executor if executor is not None else backgroundWriter()
        self.options = {"snapshotBudgetBytes": snapshotBudgetBytes(), "idleSeconds": None, "maxNodeBytes": 1 << 62}
        self.options.update(options or {})
        self.entries = {}
        #: canonical path -> {"stat": [size, mtime_ns], "ours": bool}: the files of the project that Studio knows
        self.records = {}
        #: node ID -> (canonical source, source stat, destination): copies that finished (reused by a later generation)
        self.importsDone = {}
        self.generation = 0
        self.plannedGeneration = 0
        self.finalizing = None
        self.snapshotFailures = set()
        self._reportedFailures = set()
        self.lastResult = None
        #: [(other program's file name, Studio's file name)] not reported yet (``takeConflicts()``)
        self.conflicts = []

    def manifestPath(self):
        return None

    def hasContent(self):
        return os.path.isfile(self.project.scenePath)

    def deleteSlot(self):
        """A project is never deleted by the autosave."""

    def _canonical(self, path):
        return _canonical(path)

    def _record(self, path, ours):
        stat = _statOrNone(path)
        if stat is not None:
            previous = self.records.get(_canonical(path)) or {}
            self.records[_canonical(path)] = {"stat": stat, "ours": bool(ours or previous.get("ours"))}

    def takeConflicts(self):
        conflicts, self.conflicts = self.conflicts, []
        return conflicts

    def _isPrivateFile(self, path):
        """Files of the hidden nodes (vtkMRMLScene keeps them in "<scene>_Private") and of .studio are not tracked."""
        relative = os.path.relpath(_canonical(path), _canonical(self.project.path))
        first = relative.split(os.sep)[0]
        return first == ".studio" or first.endswith("_Private")

    def _isTemporaryFile(self, path):
        """A file in a temporary folder of a generation (never a node's own file: written again in its kind folder)."""
        relative = os.path.relpath(_canonical(path), _canonical(self.project.path))
        return any(part.startswith(PROJECT_TEMPORARY_PREFIX) for part in relative.split(os.sep))

    def recordSceneFiles(self, scene):
        """Remember the files of the project that the scene references (a project opened in place): another program that
        changes one of them later is detected, and a file that leaves the scene is moved to .studio/removed.
        """
        rootDirectory = scene.GetRootDirectory()
        nodes = scene.GetNodesByClass("vtkMRMLStorableNode")
        for index in range(nodes.GetNumberOfItems()):
            node = nodes.GetItemAsObject(index)
            if node.GetHideFromEditors():
                continue
            for path in _storagePaths(node.GetStorageNode(), rootDirectory):
                if _projects().isInside(path, self.project.path) and not self._isPrivateFile(path):
                    self._record(path, ours=False)

    def relocate(self, oldPath, newPath):
        """The project folder moved (renamed, or the Slicer Studio folder moved): follow it."""

        def moved(path, oldPrefix, newPrefix):
            if path == oldPrefix or path.startswith(oldPrefix.rstrip(os.sep) + os.sep):
                return newPrefix + path[len(oldPrefix):]
            return path

        oldCanonical, newCanonical = _canonical(oldPath), _canonical(newPath)
        oldPath, newPath = os.path.normpath(oldPath), os.path.normpath(newPath)
        self.records = {moved(path, oldCanonical, newCanonical): record for path, record in self.records.items()}
        self.importsDone = {nodeId: (source, stat, moved(os.path.normpath(destination), oldPath, newPath))
                            for nodeId, (source, stat, destination) in self.importsDone.items()}
        for entry in self.entries.values():
            if entry.get("files"):
                entry["files"] = [moved(os.path.normpath(path), oldPath, newPath) for path in entry["files"]]
        self.slotDir = newPath

    #
    # Plan (main thread)
    #

    def plan(self, scene, fingerprint=None, idleSeconds=None, now=None, ignoreBusy=False, force=False):
        """Plan a generation of ``scene``; raise GenerationAborted if the scene cannot be saved now."""
        startTime = time.perf_counter()
        if not ignoreBusy and sceneIsBusy(scene):
            raise GenerationAborted("the scene is busy")
        if self.finalizing is not None:
            raise GenerationAborted("the previous generation is still being written")
        projects = _projects()
        now = time.time() if now is None else now
        self.plannedGeneration = max(self.generation, self.plannedGeneration) + 1
        projectDir = self.project.path
        plan = ProjectPlan(projectDir, self.plannedGeneration)
        plan.force = force
        plan.fingerprint = fingerprint
        plan.rootDirectory = scene.GetRootDirectory()
        plan.oldEntries = self.entries
        plan.unsaved = bool(scene.GetStorableNodesModifiedSinceRead())
        projectCanonical = _canonical(projectDir)
        storableNodes = scene.GetNodesByClass("vtkMRMLStorableNode")
        snapshotJobs = []
        for index in range(storableNodes.GetNumberOfItems()):
            node = storableNodes.GetItemAsObject(index)
            if not node.GetSaveWithScene() or node.GetHideFromEditors():
                # Hidden nodes are written by vtkMRMLScene::Commit itself, into "<scene>_Private"
                continue
            storageNode = node.GetStorageNode()
            if storageNode is None and not defaultStorageNodeClassName(node):
                continue  # stored in the scene file only
            if storageNode is not None and not storageNode.GetSaveWithScene():
                continue
            nodeId = node.GetID()
            plan.nodeIds.add(nodeId)
            old = self.entries.get(nodeId) or {}
            entry = {"name": node.GetName() or "", "class": node.GetClassName(), "contentMTime": contentMTime(node),
                     "heavy": bool(old.get("heavy")), "lastWriteMs": old.get("lastWriteMs"), "writtenAt": old.get("writtenAt")}
            plan.entries[nodeId] = entry
            files = _storagePaths(storageNode, plan.rootDirectory)
            modified = bool(node.GetModifiedSinceRead())
            exists = bool(files) and os.path.isfile(files[0]) and (len(files) == 1 or all(os.path.exists(path) for path in files[1:2]))
            inside = bool(files) and all(_isUnder(_canonical(path), projectCanonical) for path in files)
            if inside and any(self._isTemporaryFile(path) for path in files):
                modified = True  # left in a temporary folder by an interrupted generation: written into its kind folder
            if not modified and exists and inside:
                entry.update(kind="stored", files=files)
                if _canonical(files[0]) not in self.records:
                    self._record(files[0], ours=False)
                if len(files) == 1 and os.path.dirname(_canonical(files[0])) == projectCanonical and self._movable(files[0], node):
                    plan.jobs.append(_ProjectMoveJob(self, plan, nodeId, entry, files[0]))
                continue
            if not modified and exists and len(files) == 1 and not self._hasDataFiles(files[0]):
                entry.update(kind="import", files=files)
                self._planImport(plan, node, nodeId, entry, files[0])
                continue
            # New or changed data (or data whose file is missing, or a series of files outside the project): written
            if entry["heavy"] and not force:
                idle = idleSeconds if idleSeconds is not None else HEAVY_IDLE_S
                if now - float(entry.get("writtenAt") or 0) < HEAVY_INTERVAL_S or idle < HEAVY_IDLE_S:
                    plan.incomplete = True
                    entry.update(kind="deferred", reason="heavy")
                    continue
            entry.update(kind="write", bytes=estimateBytes(node))
            if self._snapshotEligible(node, entry["bytes"]):
                snapshotJobs.append(_ProjectSnapshotJob(self, plan, nodeId, entry))
            else:
                plan.jobs.append(_ProjectWriteJob(self, plan, nodeId, entry))
        # Small writes first, then the snapshots of large volumes, then the scene file
        plan.jobs.extend(snapshotJobs)
        plan.jobs.append(_ProjectCommitJob(self, plan))
        plan.metrics["planMs"] = (time.perf_counter() - startTime) * 1000.0
        return plan

    @staticmethod
    def _hasDataFiles(path):
        """True for a file that refers to other files (a detached header, a multi-block surface file): copying it alone
        would break it, so its node is written into the project by its storage node instead.
        """
        _base, extension = _projects().splitExtension(os.path.basename(path))
        return extension.lower() in DETACHED_EXTENSIONS

    @staticmethod
    def _movable(path, node):
        if node.IsA("vtkMRMLTableNode") or path.lower().endswith(".mrml"):
            return False
        _base, extension = _projects().splitExtension(os.path.basename(path))
        return extension.lower() in MOVABLE_EXTENSIONS

    def _reserve(self, plan, folder, name):
        plan.reserved.setdefault(_canonical(folder), set()).add(name.lower())

    def _newPath(self, plan, folder, fileName):
        """A file name in ``folder`` that replaces nothing (on disk or planned in this generation)."""
        name = _projects().uniqueName(folder, fileName, plan.reserved.get(_canonical(folder), ()))
        self._reserve(plan, folder, name)
        return os.path.join(folder, name)

    def target(self, plan, node, extension):
        """Where ``node`` is written: its own file again when it is a file of the project with this extension, else a new
        file "<node name><extension>" in its kind folder. A file another program changed is kept: Studio writes
        "<name> (Slicer Studio)<extension>" next to it.
        """
        projects = _projects()
        projectDir = self.project.path
        storageNode = node.GetStorageNode()
        current = storageNode.GetFileName() if storageNode is not None else None
        if current and not os.path.isabs(current):
            current = os.path.normpath(os.path.join(slicer.mrmlScene.GetRootDirectory() or projectDir, current))
        if current and _isUnder(_canonical(current), _canonical(projectDir)) and not self._isPrivateFile(current) \
                and os.path.dirname(_canonical(current)) != _canonical(projectDir) and not self._isTemporaryFile(current):
            base, currentExtension = projects.splitExtension(os.path.basename(current))
            if currentExtension.lower() == extension.lower():
                record = self.records.get(_canonical(current))
                stat = _statOrNone(current)
                if record is None and stat is not None:
                    # A file of the project that code of this session gave the node (Save lesion...) and that the node
                    # was changed after: it is the node's own file
                    self._record(current, ours=False)
                    record = self.records.get(_canonical(current))
                if stat is None or (record is not None and record["stat"] == stat):
                    self._reserve(plan, os.path.dirname(current), os.path.basename(current))
                    return current
                # Changed by another program since it was read or written: keep it
                destination = self._newPath(plan, os.path.dirname(current), f"{base}{CONFLICT_SUFFIX}{extension}")
                plan.conflicts.append((os.path.basename(current), os.path.basename(destination)))
                return destination
        folder = os.path.join(projectDir, projects.kindFolder(node))
        base = projects.sanitizeName(node.GetName() or projects.kindFolder(node), fallback="data")
        return self._newPath(plan, folder, base + extension)

    def _planImport(self, plan, node, nodeId, entry, source):
        projects = _projects()
        stat = _statOrNone(source)
        done = self.importsDone.get(nodeId)
        if done is not None and done[0] == _canonical(source) and done[1] == stat and os.path.isfile(done[2]):
            plan.imports.append((nodeId, source, stat, done[2]))
            plan.copied.add(done[2])
            self._reserve(plan, os.path.dirname(done[2]), os.path.basename(done[2]))
            return
        folder = os.path.join(self.project.path, projects.kindFolder(node))
        destination = self._newPath(plan, folder, projects.sanitizeName(os.path.basename(source), fallback="data"))
        plan.imports.append((nodeId, source, stat, destination))
        plan.copyJobs.append((nodeId, source, stat, destination))
        entry["bytes"] = stat[0] if stat else 0

    def submitCopies(self, plan):
        """Queue the copies of unchanged files (background writer). Main thread."""
        jobs, plan.copyJobs = plan.copyJobs, []
        for nodeId, source, stat, destination in jobs:
            plan.workerPending += 1

            def copy(source=source, destination=destination):
                if plan.cancelled:
                    return None
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                temporary = destination + ".studio-part"
                start = time.perf_counter()
                try:
                    shutil.copy2(source, temporary)
                    fsyncFile(temporary)
                    replaceWithRetry(temporary, destination)
                except BaseException:
                    _removeQuietly(temporary)
                    raise
                return os.path.getsize(destination), (time.perf_counter() - start) * 1000.0

            def done(result, nodeId=nodeId, source=source, stat=stat, destination=destination):
                plan.workerPending -= 1
                if result:
                    plan.copied.add(destination)
                    self.importsDone[nodeId] = (_canonical(source), stat, destination)
                    plan.metrics["bytesWritten"] += result[0]
                    plan.metrics["workerMs"] += result[1]

            def failed(error, source=source):
                plan.workerPending -= 1
                plan.incomplete = True
                logging.warning(f"Slicer Studio: copying {os.path.basename(source)} into the project failed: {error}")

            self.executor.submit(copy, onDone=done, onError=failed)

    def _jobFailed(self, plan, job, error):
        """A main-thread job raised: the node keeps its file and stays modified (written by the next generation)."""
        nodeId, entry = job.nodeId, job.entry
        if getattr(job, "inFlight", 0):
            plan.snapshotBytesInFlight -= job.inFlight
            job.inFlight = 0
        release = getattr(job, "release", None)
        if release:
            release()
        if isinstance(job, _SnapshotJob):
            self.snapshotFailures.add(nodeId)
        plan.incomplete = True
        if nodeId not in self._reportedFailures:
            self._reportedFailures.add(nodeId)
            logging.warning(f"Slicer Studio: {entry.get('name') or nodeId} could not be saved:\n" + traceback.format_exc())
        entry.update(kind="error", reason=f"saving failed: {type(error).__name__}")

    #
    # Commit (main thread, last job)
    #

    def commit(self, plan, scene):
        """Point the copied and snapshot nodes at their files, then replace the scene file (relative paths)."""
        projects = _projects()
        projectDir = self.project.path
        for nodeId, source, _stat, destination in plan.imports:
            node = scene.GetNodeByID(nodeId)
            if node is None or destination not in plan.copied:
                if node is not None:
                    plan.incomplete = True
                continue
            storageNode = node.GetStorageNode()
            files = _storagePaths(storageNode, plan.rootDirectory)
            if node.GetModifiedSinceRead() or not files or _canonical(files[0]) != _canonical(source):
                plan.incomplete = True  # changed during the copy: written by the next generation
                continue
            _setSingleFile(storageNode, destination)
            self._record(destination, ours=True)
            self.importsDone.pop(nodeId, None)
            plan.entries[nodeId].update(kind="stored", files=[destination])
        for nodeId, (destination, check) in plan.snapshots.items():
            node = scene.GetNodeByID(nodeId)
            if node is None:
                continue
            self._record(destination, ours=True)
            imageData, imageMTime, scalarsMTime, rows = check
            current = node.GetImageData()
            unchanged = current is imageData and current is not None and current.GetMTime() == imageMTime
            if unchanged:
                scalars = current.GetPointData().GetScalars() if current.GetPointData() is not None else None
                unchanged = scalars is not None and scalars.GetMTime() == scalarsMTime and _ijkToRasRows(node) == rows
            if unchanged and markStoredWithFile(scene, node, destination):
                plan.entries[nodeId].update(kind="stored", files=[destination])
                continue
            # Changed while it was written: the file holds the snapshot, the node stays modified
            storageNode = node.GetStorageNode()
            if storageNode is None and node.AddDefaultStorageNode():
                storageNode = node.GetStorageNode()
            if storageNode is not None:
                _setSingleFile(storageNode, destination)
            plan.incomplete = True
        scenePath = self.project.scenePath
        if scene.GetURL() != scenePath:
            scene.SetURL(scenePath)
        if os.path.normpath(scene.GetRootDirectory() or "") != os.path.normpath(projectDir):
            scene.SetRootDirectory(projectDir)
        # vtkMRMLScene names the folder of hidden nodes after the scene file up to its first dot: the temporary scene
        # file keeps that part of the name
        stem = os.path.basename(scenePath)[: -len(projects.SCENE_EXTENSION)]
        temporary = os.path.join(projectDir, f"{stem}.{PROJECT_TEMPORARY_PREFIX.strip('.')}{uuid.uuid4().hex[:8]}{projects.SCENE_EXTENSION}")
        start = time.perf_counter()
        try:
            written = scene.Commit(temporary)
            if not written or not os.path.isfile(temporary):
                raise OSError(_tr("The scene file could not be written."))
            fsyncFile(temporary)
            replaceWithRetry(temporary, scenePath)
        finally:
            _removeQuietly(temporary)
        plan.committed = True
        plan.metrics["sceneMs"] = round((time.perf_counter() - start) * 1000.0, 2)
        # After the commit: writing the scene may modify a node once (the subject hierarchy)
        plan.fingerprint = sceneFingerprint(scene, sentinelIds())
        self._record(scenePath, ours=True)
        for nodeId in plan.nodeIds:
            node = scene.GetNodeByID(nodeId)
            entry = plan.entries.get(nodeId) or {}
            if node is not None and node.GetModifiedSinceRead() and entry.get("kind") not in ("skip",):
                plan.incomplete = True
        # Files of nodes that left the scene
        referenced = {_canonical(scenePath)}
        rootDirectory = scene.GetRootDirectory()
        nodes = scene.GetNodesByClass("vtkMRMLStorableNode")
        for index in range(nodes.GetNumberOfItems()):
            for path in _storagePaths(nodes.GetItemAsObject(index).GetStorageNode(), rootDirectory):
                referenced.add(_canonical(path))
        # Files that a storage node writes next to its file are referenced too (a table's "<name>.schema.tsv", the data
        # file of a .nhdr): a file whose name starts with "<base name of a referenced file>." in the same folder
        companions = {(os.path.dirname(path), _projects().splitExtension(os.path.basename(path))[0] + ".") for path in referenced}
        for path, record in list(self.records.items()):
            if path in referenced or not _isUnder(path, _canonical(projectDir)) or self._isPrivateFile(path):
                continue
            folder, name = os.path.dirname(path), os.path.basename(path)
            if any(folder == companionFolder and name.startswith(prefix) for companionFolder, prefix in companions):
                continue
            plan.orphans.append((path, record["stat"]))
            del self.records[path]
        self.conflicts.extend(plan.conflicts)
        plan.projectJson = self.project.jsonData("open", scene, savedAt=time.time())
        plan.summary = projects.counts(scene)

    #
    # Finalize (worker)
    #

    def finalize(self, plan):
        """Move the files of removed nodes to .studio/removed, delete temporary files, write project.json. Worker-safe."""
        start = time.perf_counter()
        if not plan.committed:
            raise GenerationAborted(plan.abortReason or "not committed")
        projectDir = self.project.path
        removedDir = None
        for path, stat in plan.orphans:
            current = _statOrNone(path)
            if current is None or (stat is not None and current != stat):
                continue  # gone, or changed by another program: left where it is
            if removedDir is None:
                removedDir = os.path.join(projectDir, ".studio", "removed", time.strftime("%Y%m%d-%H%M%S"))
                os.makedirs(removedDir, exist_ok=True)
            target = os.path.join(removedDir, _projects().uniqueName(removedDir, os.path.basename(path)))
            try:
                shutil.move(path, target)
            except OSError:
                continue
            folder = os.path.dirname(path)
            if _canonical(folder) != _canonical(projectDir):
                try:
                    os.rmdir(folder)  # never leave an empty kind folder
                except OSError:
                    pass
        for path in plan.temporary:
            removeTree(path)
        if plan.projectJson is not None:
            os.makedirs(os.path.join(projectDir, ".studio"), exist_ok=True)
            atomicWriteJson(os.path.join(projectDir, ".studio", "project.json"), plan.projectJson)
        return {"bytes": 0, "workerMs": (time.perf_counter() - start) * 1000.0, "generation": plan.generation}

    def abort(self, plan, reason):
        """Cancel a generation. Files already in place stay (the live scene references them); temporary files go."""
        if plan.aborted or plan.finished:
            return
        plan.aborted = True
        if not plan.cancelled:
            plan.abortReason = reason
            plan.cancelled = True
        for job in plan.jobs:
            release = getattr(job, "release", None)
            if release:
                release()
        plan.jobs.clear()
        temporary = list(plan.temporary)

        def cleanup():
            for path in temporary:
                removeTree(path)

        self.executor.submit(cleanup)

    def writeAll(self, scene, force=True):
        """Write a whole generation synchronously (tests, harness). Return the plan."""
        executor = self.executor
        self.executor = InlineExecutor()
        try:
            plan = self.plan(scene, force=force)
            while not self.runMainThreadStep(plan, scene, budgetMs=1e9):
                pass
            result = self.finalize(plan)
            self._finished(plan, result)
            return plan
        finally:
            self.executor = executor

    def writeSnapshot(self, scene):
        return self.writeAll(scene)


def _tr(text):
    from slicer.i18n import tr

    return tr(text)


def _ijkToRasRows(node):
    import vtk

    matrix = vtk.vtkMatrix4x4()
    node.GetIJKToRASMatrix(matrix)
    return [[matrix.GetElement(r, c) for c in range(4)] for r in range(4)]


class _ProjectWriteJob:
    """Write one node through its own storage node into the project (main thread): temporary folder, then moved in place."""

    def __init__(self, writer, plan, nodeId, entry):
        self.writer, self.plan, self.nodeId, self.entry = writer, plan, nodeId, entry

    def run(self, scene, sliceStart, budgetMs):
        writer, plan, entry = self.writer, self.plan, self.entry
        node = scene.GetNodeByID(self.nodeId)
        if node is None:
            raise GenerationAborted(f"{self.nodeId} was removed")
        storageNode = node.GetStorageNode()
        if storageNode is None:
            if not node.AddDefaultStorageNode() or node.GetStorageNode() is None:
                entry.update(kind="skip", reason="no file format")
                return True
            storageNode = node.GetStorageNode()
            if storageNode.IsA("vtkMRMLSegmentationStorageNode"):
                # Official Slicer reads cropped segmentations; on a 1 mm grid this is several times faster
                storageNode.SetCropToMinimumExtent(1)
        extension = _writeExtension(node, storageNode)
        if not extension:
            entry.update(kind="skip", reason="no file format")
            return True
        destination = writer.target(plan, node, extension)
        folder = os.path.dirname(destination)
        os.makedirs(folder, exist_ok=True)
        temporary = os.path.join(folder, f"{PROJECT_TEMPORARY_PREFIX}{uuid.uuid4().hex[:8]}")
        os.makedirs(temporary)
        plan.temporary.append(temporary)
        originalNames = [storageNode.GetFileName()] + [storageNode.GetNthFileName(i) for i in range(storageNode.GetNumberOfFileNames())]
        _setSingleFile(storageNode, os.path.join(temporary, os.path.basename(destination)))
        start = time.perf_counter()
        try:
            written = bool(storageNode.WriteData(node))
        except Exception:
            written = False
        elapsed = (time.perf_counter() - start) * 1000.0
        produced = os.listdir(temporary) if os.path.isdir(temporary) else []
        error = None
        if written and os.path.basename(destination) in produced:
            try:
                size = self._moveIntoPlace(temporary, folder, produced, os.path.basename(destination))
            except OSError as moveError:
                error = moveError
        else:
            error = "write failed"
        if error is not None:
            # The node keeps its previous file and stays modified (WriteData marked it stored): the next generation
            # writes it again
            storageNode.SetFileName(originalNames[0])
            storageNode.ResetFileNameList()
            for name in originalNames[1:]:
                if name:
                    storageNode.AddFileName(name)
            node.StorableModified()
            removeTree(temporary)
            plan.incomplete = True
            entry.update(kind="error", reason="write failed")
            if self.nodeId not in writer._reportedFailures:
                writer._reportedFailures.add(self.nodeId)
                logging.warning(f"Slicer Studio: {entry.get('name') or self.nodeId} could not be saved in the project: {error}")
            return True
        removeTree(temporary)
        _setSingleFile(storageNode, destination)
        entry.update(kind="stored", files=[destination], lastWriteMs=round(elapsed, 2), writtenAt=time.time(), bytes=size)
        if elapsed > HEAVY_WRITE_MS:
            entry["heavy"] = True
        plan.metrics["bytesWritten"] += size
        plan.metrics["nodesWritten"] += 1
        plan.wroteData = True
        return True

    def _moveIntoPlace(self, temporary, folder, produced, mainName):
        """Move what the storage node wrote into ``folder``; return the number of bytes. Raise OSError.

        Folders first (a closed-surface segmentation writes "<name>.seg/" next to "<name>.seg.vtm"): a folder that is
        already there is moved aside into the temporary folder (deleted with it), as ``os.replace`` cannot replace a
        folder that is not empty. The main file is moved last, so it never refers to files that are not in place yet.
        """
        size = 0
        order = sorted(produced, key=lambda name: (not os.path.isdir(os.path.join(temporary, name)), name == mainName))
        for name in order:
            source, target = os.path.join(temporary, name), os.path.join(folder, name)
            if os.path.isdir(source):
                aside = None
                if os.path.lexists(target):
                    aside = os.path.join(temporary, f"{PROJECT_TEMPORARY_PREFIX}previous-{uuid.uuid4().hex[:8]}")
                    replaceWithRetry(target, aside)
                try:
                    replaceWithRetry(source, target)
                except OSError:
                    if aside is not None:
                        replaceWithRetry(aside, target)
                    raise
                self.writer._record(target, ours=True)  # moved to .studio/removed with its file
                size += treeSize(target)
                continue
            replaceWithRetry(source, target)
            self.writer._record(target, ours=True)
            size += os.path.getsize(target)
        return size


class _ProjectMoveJob:
    """A file that official Slicer saved next to the scene file goes into its kind folder (a rename; main thread)."""

    def __init__(self, writer, plan, nodeId, entry, path):
        self.writer, self.plan, self.nodeId, self.entry, self.path = writer, plan, nodeId, entry, path

    def run(self, scene, sliceStart, budgetMs):
        node = scene.GetNodeByID(self.nodeId)
        if node is None or node.GetStorageNode() is None or not os.path.isfile(self.path):
            return True
        projects = _projects()
        folder = os.path.join(self.writer.project.path, projects.kindFolder(node))
        os.makedirs(folder, exist_ok=True)
        destination = self.writer._newPath(self.plan, folder, os.path.basename(self.path))
        record = self.writer.records.pop(_canonical(self.path), None)
        try:
            os.replace(self.path, destination)
        except OSError:
            if record is not None:
                self.writer.records[_canonical(self.path)] = record
            return True
        _setSingleFile(node.GetStorageNode(), destination)
        self.writer._record(destination, ours=bool(record and record.get("ours")))
        self.entry["files"] = [destination]
        return True


class _ProjectSnapshotJob(_SnapshotJob):
    """A large scalar volume of a project: copied by slabs (main thread), written into its kind folder in the background."""

    def _changedWhileCopied(self):
        self.plan.incomplete = True
        self.entry.update(kind="deferred", reason="changed while saving")

    def _submit(self, node, rows):
        writer, plan, entry, nodeId = self.writer, self.plan, self.entry, self.nodeId
        destination = writer.target(plan, node, ".nrrd")
        check = (self.imageData, self.imageData.GetMTime(), self.scalars.GetMTime(), rows)
        snapshot, inFlight = self.snapshot, self.inFlight
        self.release()
        self.inFlight = 0
        plan.workerPending += 1

        isNewFile = not os.path.exists(destination)

        def write():
            if plan.cancelled:
                return None
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            start = time.perf_counter()
            written = writeScalarNrrd(destination, snapshot, rows, level=1)
            if plan.cancelled and isNewFile:
                # The generation was cancelled while this was written: nothing refers to the new file
                _removeQuietly(destination)
                return None
            return written, (time.perf_counter() - start) * 1000.0

        def done(result):
            plan.workerPending -= 1
            plan.snapshotBytesInFlight -= inFlight
            if result:
                plan.snapshots[nodeId] = (destination, check)
                entry.update(bytes=result[0], files=[destination])
                plan.metrics["bytesWritten"] += result[0]
                plan.metrics["workerMs"] += result[1]
            else:
                plan.incomplete = True

        def failed(error):
            plan.workerPending -= 1
            plan.snapshotBytesInFlight -= inFlight
            plan.incomplete = True
            entry.update(kind="error", reason=f"saving failed: {type(error).__name__}")
            if not isinstance(error, OSError):
                writer.snapshotFailures.add(nodeId)  # the next generation writes it through its storage node
            logging.warning(f"Slicer Studio: {entry.get('name') or nodeId} could not be saved in the project: {error}")

        entry.update(writtenAt=time.time(), lastWriteMs=0.0)
        plan.metrics["nodesWritten"] += 1
        plan.wroteData = True
        writer.executor.submit(write, onDone=done, onError=failed)
        return True


class _ProjectCommitJob:
    """Last main-thread job: waits for the copies and snapshot writes, then replaces the scene file."""

    def __init__(self, writer, plan):
        self.writer, self.plan = writer, plan
        self.waiting = False

    def run(self, scene, sliceStart, budgetMs):
        plan = self.plan
        if not plan.workerSubmitted:
            plan.workerSubmitted = True
            self.writer.submitCopies(plan)
        if plan.workerPending > 0:
            self.waiting = True
            return False
        self.waiting = False
        if plan.cancelled:
            raise GenerationAborted(plan.abortReason or "cancelled")
        self.writer.commit(plan, scene)
        return True

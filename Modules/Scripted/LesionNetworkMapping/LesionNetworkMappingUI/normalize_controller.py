"""One-click normalization of a native-space lesion to MNI space (A-03b): the shared Qt controller.

The Map card (``card_normalize.py``, Studio and Classic) and the Draw card (``StudioLib.drawcard_normalize``,
Studio only) both drive a :class:`NormalizeController`:

- ``start(NormalizeRequest)`` exports the scan and the lesion without blocking the GUI (a scan whose file is
  unchanged on disk is passed as that file; otherwise its voxels are copied on the main thread and a worker
  thread writes an uncompressed NIfTI), then runs ``LesionNetworkMappingLib.normalize run`` through the
  application's L-11 ``JobRunner`` (one heavy job at a time across Map, Draw and Library; Stop through the
  cancel file). Progress events are coalesced by a 100 ms timer into ``progressChanged``.
- When the job is done a worker thread reads the template-space scan and lesion and the QC mosaic, and the
  main thread creates the volume "<scan> (<template>)" (window/level precomputed, AutoWindowLevel off) and
  the segmentation "<lesion> (<template>)" (named after the native segment, "Lesion" by default) with the node attributes ``LNM.Normalization``,
  ``LNM.NormalizedFrom``, ``LNM.NormSuggestion`` and ``Studio.Space`` (official Slicer ignores them).
- ``buildQcWidget(result)`` shows the mosaic, the suggestion and the metrics, records the reviewer's verdict
  in normalization.json, and offers Review in views (template behind, warped scan at 50 %, lesion outline;
  the existing T key flickers; ``restoreViews()`` puts every layer back), Load transform and Redo.
- Edits of the native lesion after a result offer "Update MNI lesion" (``normalize warp``, about 3 s, through
  the JobRunner as a light job), or "Re-register" when the edit leaves the dilated original lesion.

``canNormalize()`` and ``offerNormalize()`` are the public entry points of the Draw call to action and of the
Map seed refusal (USABILITY 14). Nothing here downloads a template by itself: the whole-head template and
MNI152NLin6Asym are fetched only after an explicit click, and never under ``--testing``.

Callbacks are plain Python lists (no Qt signals). Works in Classic and without a main window. Importing this
module imports only Qt, VTK, Slicer and the standard library; numpy is imported by the functions that need it.
"""

import datetime
import gc
import getpass
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import threading
import time
import uuid

import qt
import slicer
import vtk
from slicer.i18n import tr as _
from slicer.util import VTKObservationMixin

from . import jobrunner as _jobrunner

#: Job module (the A-03 command line)
JOB_MODULE = "LesionNetworkMappingLib.normalize"
DEFAULT_TEMPLATE = "MNI152NLin2009cAsym"
NLIN6_TEMPLATE = "MNI152NLin6Asym"
TEMPLATE_IDS = (DEFAULT_TEMPLATE, NLIN6_TEMPLATE)
QUALITIES = ("standard", "fine", "affine")
MODALITIES = ("T1w", "T2w", "FLAIR", "DWI", "CT")
DEFAULT_DILATE_MM = 3.0

#: Name, id and colour (#F2545B) of the template-space lesion segment
LESION_SEGMENT = "Lesion"
LESION_COLOR = (0xF2 / 255.0, 0x54 / 255.0, 0x5B / 255.0)

#: Node attributes (SPEC 9.7 rule 2: official Slicer ignores them). The file references are portable (``portableReference()``)
NORMALIZATION_ATTRIBUTE = "LNM.Normalization"
NORMALIZED_FROM_ATTRIBUTE = "LNM.NormalizedFrom"
SUGGESTION_ATTRIBUTE = "LNM.NormSuggestion"
SPACE_ATTRIBUTE = "Studio.Space"
REVIEW_TEMPLATE_ATTRIBUTE = "LNM.ReviewTemplate"
#: The run fingerprint of a result node (reused nodes match it), and the warp file of an "Update MNI lesion"
FINGERPRINT_ATTRIBUTE = "LNM.NormFingerprint"
LESION_WARP_ATTRIBUTE = "LNM.NormLesionWarp"

#: Fork-only settings (D-S1). LastOptions is written only when "Remember these options" is ticked; DataDirectory is only read.
LAST_OPTIONS_SETTING = "Studio/LNM/Normalize/LastOptions"
DATA_DIRECTORY_SETTING = "Studio/LNM/DataDirectory"
RATER_SETTING = "Studio/RaterName"

#: Coalescing interval of progress updates, and the debounce of native lesion edits
PROGRESS_INTERVAL_MS = 100
EDIT_DEBOUNCE_MS = 500
#: Poll interval of worker threads (main thread)
WORKER_POLL_MS = 15

#: Scan files passed as they are (an unchanged storage file with one of these extensions)
SCAN_FILE_EXTENSIONS = (".nii", ".nii.gz", ".nrrd", ".nhdr", ".mha", ".mhd")

STAGE_LABELS = {
    "costmask": "preparing",
    "anat": "T1w alignment",
    "affine": "affine",
    "bspline": "nonlinear",
    "warp": "warping",
    "qc": "QC",
}

#: States of a controller
IDLE, PREPARING, QUEUED, RUNNING, LOADING, DONE, FAILED, CANCELLED = ("idle", "preparing", "queued", "running", "loading", "done", "failed", "cancelled")
BUSY_STATES = (PREPARING, QUEUED, RUNNING, LOADING)


#
# Helpers
#


def _normalizeLib():
    """``LesionNetworkMappingLib.normalize`` (standard library only at import time)."""
    from LesionNetworkMappingLib import normalize

    return normalize


def _isTesting():
    app = getattr(slicer, "app", None)
    return bool(app is not None and app.testingEnabled())


def _studioShell():
    return _jobrunner._studioShell()


def _matrixToArray(matrix):
    import numpy as np

    return np.array([[matrix.GetElement(row, column) for column in range(4)] for row in range(4)])


def _arrayToMatrix(array):
    matrix = vtk.vtkMatrix4x4()
    for row in range(4):
        for column in range(4):
            matrix.SetElement(row, column, float(array[row][column]))
    return matrix


def _ijkToRas(volumeNode):
    matrix = vtk.vtkMatrix4x4()
    volumeNode.GetIJKToRASMatrix(matrix)
    return _matrixToArray(matrix)


def participantLabel(text):
    """A BIDS participant label ("sub-<letters and digits>") from a subject id or a scan name."""
    text = str(text or "")
    match = re.search(r"(?:^|[_/\\\s])sub-([A-Za-z0-9]+)", text)
    if match:
        return f"sub-{match.group(1)}"
    label = re.sub(r"[^A-Za-z0-9]", "", text[4:] if text.startswith("sub-") else text)[:40]
    return f"sub-{label or 'scan'}"


def legacyDataDirectory():
    """The default before A-09: Documents/SlicerStudio, or /data/SlicerStudio in the web or Docker build."""
    if os.environ.get("SLICER_STUDIO_WEB") or os.path.exists("/.dockerenv"):
        return "/data/SlicerStudio"
    documents = qt.QStandardPaths.writableLocation(qt.QStandardPaths.DocumentsLocation) or os.path.expanduser("~")
    return os.path.join(documents, "SlicerStudio")


def dataDirectory():
    """``Studio/LNM/DataDirectory`` (only read; an explicit value is kept as is).

    Unset: ``<Slicer Studio folder>/LNM`` (A-09, D-A16), except when the former default folder (Documents/SlicerStudio)
    already holds data, which then stays in use so that nothing seems lost. Without Studio: the former default.
    """
    value = str(qt.QSettings().value(DATA_DIRECTORY_SETTING) or "").strip()
    if value:
        return value
    legacy = legacyDataDirectory()
    try:
        if os.path.isdir(legacy) and os.listdir(legacy):
            return legacy
    except OSError:
        pass
    try:
        from StudioLib import projects
    except ImportError:
        return legacy
    return projects.lnmFolder() if projects.isEnabled() else legacy


def defaultOutputDirectory(scanNode=None, scanPath=None, subject=None, studyPath=None, sid=None):
    """``<study>/derivatives/normalization/<sid>`` with a study, else ``<DataDirectory>/normalization/<sid or scan stem>-<sha8>``.

    The 8 hex digits identify the scan (its file, else its node and scene), so that normalizing the same scan
    again reuses its folder (and its result, by fingerprint).
    """
    if studyPath and sid:
        return os.path.join(os.fspath(studyPath), "derivatives", "normalization", participantLabel(sid))
    name = subject or (scanNode.GetName() if scanNode is not None else None) or (os.path.basename(scanPath) if scanPath else "scan")
    stem = participantLabel(subject) if subject else (re.sub(r"[^A-Za-z0-9_-]+", "_", re.sub(r"\.(nii\.gz|nii|nrrd|nhdr|mha|mhd)$", "", str(name)))[:40] or "scan")
    identity = scanPath or ""
    if not identity and scanNode is not None:
        storage = scanNode.GetStorageNode()
        identity = storage.GetFileName() if storage is not None and storage.GetFileName() else ""
        if not identity:
            identity = f"{slicer.mrmlScene.GetURL()}|{scanNode.GetID()}|{scanNode.GetName()}"
    digest = hashlib.sha1(os.path.abspath(identity).encode("utf-8") if os.path.isabs(identity) else identity.encode("utf-8")).hexdigest()[:8]
    return os.path.join(dataDirectory(), "normalization", f"{stem}-{digest}")


def _isAbsoluteReference(value):
    """True for an absolute path of any platform (``/…``, a backslash, a drive letter), whatever the platform of this computer."""
    return bool(value) and (os.path.isabs(value) or re.match(r"^([A-Za-z]:)?[\\/]", value) is not None)


def portableReference(path, outDir=None):
    """The value of ``LNM.Normalization`` / ``LNM.NormLesionWarp`` for a file of a normalization result folder.

    Node attributes never hold an absolute user path (SPEC 9.7 rules 2 and 10: scenes and .mrb files are shared).
    The reference starts at the result folder: ``derivatives/normalization/<sid>/…`` inside a study (as
    ``libcard_normalize`` writes it, study-relative), ``normalization/<folder>/…`` in the data directory
    (``dataDirectory()``-relative), else ``<folder>/…``. ``outDir`` is the result folder (default: the folder of
    ``path``, or its parent for a file in ``warps``). A relative value is returned as is (with ``/``).
    """
    if not path:
        return ""
    path = str(path)
    if not _isAbsoluteReference(path):
        return path.replace("\\", "/")
    parts = [part for part in re.split(r"[\\/]+", path) if part]
    start = None
    if outDir:
        outParts = [part for part in re.split(r"[\\/]+", str(outDir)) if part]
        if outParts and parts[:len(outParts)] == outParts and len(parts) > len(outParts):
            start = len(outParts) - 1
    if start is None:
        start = max(len(parts) - 2, 0)
        if start >= 1 and parts[start] == "warps":
            start -= 1
    if start >= 1 and parts[start - 1] == "normalization":
        start -= 1
        if start >= 1 and parts[start - 1] == "derivatives":
            start -= 1
    return "/".join(parts[start:])


def resolveReference(value, studyPath=None):
    """The local path of a ``portableReference()`` (``derivatives/…`` needs ``studyPath``), an earlier absolute value as is, or None."""
    if not value:
        return None
    if _isAbsoluteReference(value):
        return value
    parts = [part for part in str(value).split("/") if part]
    if not parts:
        return None
    base = os.fspath(studyPath) if parts[0] == "derivatives" and studyPath else (dataDirectory() if parts[0] == "normalization" else None)
    return os.path.join(base, *parts) if base else None


def migrateAbsoluteReferences(scene=None):
    """Replace the absolute ``LNM.Normalization`` / ``LNM.NormLesionWarp`` values of earlier scenes by ``portableReference()``.

    Called when a scene is loaded and before it is saved (``LesionNetworkMapping.installSceneMigration``). Return
    the number of attributes changed.
    """
    scene = scene or slicer.mrmlScene
    changed = 0
    for index in range(scene.GetNumberOfNodes()):
        node = scene.GetNthNode(index)
        for attribute in (NORMALIZATION_ATTRIBUTE, LESION_WARP_ATTRIBUTE):
            value = node.GetAttribute(attribute)
            if value and _isAbsoluteReference(value):
                node.SetAttribute(attribute, portableReference(value))
                changed += 1
    return changed


def sampleDataDirectory():
    """The folder of the bundled templates (SampleData's Resources/Data), or None."""
    module = getattr(slicer.modules, "sampledata", None)
    if module is not None and module.path:
        folder = os.path.join(os.path.dirname(module.path), "Resources", "Data")
        if os.path.isdir(folder):
            return folder
    return None


def cacheDirectory():
    """Where explicit template downloads go (``slicer.app.cachePath``)."""
    app = getattr(slicer, "app", None)
    return app.cachePath if app is not None and app.cachePath else os.path.join(os.path.expanduser("~"), ".cache", "SlicerStudio")


def userTemplatesDirectory():
    """<Slicer Studio folder>/Templates: the user's own templates (A-10), or None without Studio or when it does not exist."""
    try:
        from StudioLib import templates
    except ImportError:
        return None
    try:
        folder = templates.templatesFolder()
    except Exception:
        return None
    return folder if os.path.isdir(folder) else None


def templateDirectories():
    """Folders passed with --template-dir: SampleData's data folder, the cache, Studio's template cache when it exists,
    and the Templates folder of the user's own templates (resolves "user:<id>").
    """
    folders = [sampleDataDirectory(), cacheDirectory()]
    studioCache = os.path.join(cacheDirectory(), "StudioTemplates")
    if os.path.isdir(studioCache):
        folders.append(studioCache)
    folders.append(userTemplatesDirectory())
    return [folder for folder in dict.fromkeys(folders) if folder]


def isUserTemplate(templateId):
    return str(templateId or "").startswith("user:")


def userTemplates():
    """The user's own templates that can be a normalization target (an image and a brain mask): ``[(id, name)]``."""
    try:
        from StudioLib import templates
    except ImportError:
        return []
    try:
        return [(template.id, template.name) for template in templates.userTemplates() if template.canNormalize()]
    except Exception:
        return []


def templateName(templateId):
    """The name shown for a template id: the user's name of one of their templates, else the id."""
    if isUserTemplate(templateId):
        record = _normalizeLib().resolve_user_template(templateId, templateDirectories())
        if record is not None:
            return record.get("name") or templateId
    return templateId


def spaceAttributeValue(templateId, record=None):
    """The ``Studio.Space`` value of a normalization result: the template id, or the space of the user's own template
    (its normalization.json record, else its template.json), so that Map and Draw see its MNI family.
    """
    if not isUserTemplate(templateId):
        return templateId
    space = ((record or {}).get("template") or {}).get("space")
    if not space:
        found = _normalizeLib().resolve_user_template(templateId, templateDirectories())
        space = (found or {}).get("space")
    return space or "MNI"


def templateFile(templateId, role):
    """The path of a template file found by name in the template folders (not checksummed here: the job checks), or None."""
    if isUserTemplate(templateId):
        record = _normalizeLib().resolve_user_template(templateId, templateDirectories())
        name = ((record or {}).get("files") or {}).get({"fixed": "image", "reference": "image", "mask": "mask", "head": "head"}.get(role, role))
        path = os.path.join(record["folder"], *str(name).split("/")) if record and name else None
        return path if path and os.path.isfile(path) else None
    info = _normalizeLib().TEMPLATES[templateId]["files"][role]
    for folder in templateDirectories():
        for candidate in (os.path.join(folder, info["name"]), os.path.join(folder, f"tpl-{templateId}", info["name"])):
            if os.path.isfile(candidate) and os.path.getsize(candidate) == info["bytes"]:
                return candidate
    return None


def templateAvailable(templateId):
    """Whether the registration target and brain mask of a template are present (bundled or downloaded)."""
    return all(templateFile(templateId, role) for role in ("fixed", "mask"))


def rememberedOptions():
    """Normalization options remembered with "Remember these options" (``Studio/LNM/Normalize/LastOptions``, read only here).

    Always a full dict: ``templateId`` (None: automatic), ``quality``, ``modality``, ``dilateMm`` (1 to 10 mm) and ``remember``
    (whether anything was stored). Shared by the Map card and Draw's one-click Normalize to MNI.
    """
    options = {"templateId": None, "quality": "standard", "modality": "auto", "dilateMm": DEFAULT_DILATE_MM, "remember": False}
    try:
        stored = json.loads(str(qt.QSettings().value(LAST_OPTIONS_SETTING) or "{}"))
    except ValueError:
        stored = {}
    if isinstance(stored, dict):
        for key in ("templateId", "quality", "modality", "dilateMm"):
            if key in stored:
                options[key] = stored[key]
        options["remember"] = bool(stored)
    if options["quality"] not in QUALITIES:
        options["quality"] = "standard"
    try:
        options["dilateMm"] = min(max(float(options["dilateMm"]), 1.0), 10.0)
    except (TypeError, ValueError):
        options["dilateMm"] = DEFAULT_DILATE_MM
    return options


def rememberedRequestOptions():
    """Keyword arguments for ``NormalizeRequest`` from the remembered options (empty when nothing was remembered).

    A remembered template is used only when it is still usable (built-in, or one of the user's with image and mask in the
    Templates folder); otherwise the request keeps the default template.
    """
    options = rememberedOptions()
    if not options["remember"]:
        return {}
    chosen = options["templateId"]
    templateId = DEFAULT_TEMPLATE
    if isinstance(chosen, str) and chosen and (chosen in TEMPLATE_IDS or isUserTemplate(chosen)):
        try:
            if templateAvailable(chosen):
                templateId = chosen
        except Exception:
            logging.exception("Studio: the remembered normalization template could not be checked")
    return {"templateId": templateId, "quality": options["quality"], "modality": options["modality"] or "auto", "dilateMm": options["dilateMm"]}


def headTemplateAvailable(templateId=DEFAULT_TEMPLATE):
    return templateFile(templateId, "head") is not None


def templateDownloadSize(templateId, roles):
    files = _normalizeLib().TEMPLATES[templateId]["files"]
    return sum(files[role]["bytes"] for role in dict.fromkeys(roles) if templateFile(templateId, role) is None)


def downloadTemplateFiles(templateId, roles, onFinished=None, onFailed=None, onProgress=None):
    """Download missing template files from TemplateFlow into the cache, one after the other (checked by sha256).

    Only call it after an explicit click. Never runs under ``--testing`` or without ``StudioLib.downloads``:
    then ``onFailed(message)`` explains how to get the files and None is returned. Returns the first Download.
    """
    normalize = _normalizeLib()
    files = normalize.TEMPLATES[templateId]["files"]
    missing = [role for role in dict.fromkeys(roles) if templateFile(templateId, role) is None]
    # Roles that name the same file (NLin6: fixed and head) are downloaded once
    seen, queue = set(), []
    for role in missing:
        if files[role]["name"] not in seen:
            seen.add(files[role]["name"])
            queue.append(role)
    manual = _("Download {names} from TemplateFlow ({urls}) into {folder}.").format(
        names=", ".join(files[role]["name"] for role in queue) or "-", urls=", ".join(normalize.template_url(templateId, role) for role in queue) or "-", folder=cacheDirectory())
    if _isTesting():
        if onFailed is not None:
            onFailed(_("Downloads are disabled while testing. ") + manual)
        return None
    if not queue:
        if onFinished is not None:
            qt.QTimer.singleShot(0, lambda: onFinished([]))
        return None
    try:
        from StudioLib import downloads
    except Exception:
        if onFailed is not None:
            onFailed(manual)
        return None
    written = []

    def startNext():
        if not queue:
            if onFinished is not None:
                onFinished(written)
            return None
        role = queue.pop(0)
        info = files[role]
        download = downloads.Download(normalize.template_url(templateId, role), info["sha256"], os.path.join(cacheDirectory(), info["name"]))

        def finished(path):
            written.append(path)
            startNext()

        def failed(message):
            if onFailed is not None:
                onFailed(f"{message}. {manual}")

        download.finished.connect(finished)
        download.failed.connect(failed)
        if onProgress is not None:
            download.progress.connect(onProgress)
        _keepAlive.add(download)
        download.start()
        return download

    return startNext()


_keepAlive = set()


def guessModality(volumeNode=None, path=None):
    """``(modality, guessed)`` from the node name, the DICOM series description and the file name."""
    normalize = _normalizeLib()
    names = []
    if volumeNode is not None:
        names.append(volumeNode.GetName())
        description = volumeNode.GetAttribute("DICOM.SeriesDescription") or ""
        if not description:
            try:
                shNode = slicer.mrmlScene.GetSubjectHierarchyNode()
                itemId = shNode.GetItemByDataNode(volumeNode)
                description = shNode.GetItemAttribute(shNode.GetItemParent(itemId), "DICOM.SeriesDescription") or shNode.GetItemAttribute(itemId, "DICOM.SeriesDescription") or ""
            except Exception:
                description = ""
        if description:
            names.append(description)
        storage = volumeNode.GetStorageNode()
        if storage is not None and storage.GetFileName():
            names.append(storage.GetFileName())
    if path:
        names.append(path)
    for name in names:
        modality, guessed = normalize.guess_modality(re.sub(r"\s+", "_", str(name)))
        if not guessed:
            return modality, False
    return "T1w", True


def isNormalizationResult(node):
    """True when ``node`` was made by a normalization (it carries ``LNM.Normalization``)."""
    return node is not None and bool(node.GetAttribute(NORMALIZATION_ATTRIBUTE))


def spaceOf(node):
    """``(space, resolution)`` of a volume or segmentation on an MNI grid (``StudioLib.space`` when importable), else None."""
    if node is None:
        return None
    try:
        from StudioLib import space as studioSpace
    except Exception:
        studioSpace = None
    if studioSpace is not None:
        try:
            return studioSpace.spaceOf(node)
        except Exception:
            logging.exception("Lesion network mapping: the space of a node could not be read")
            return None
    attribute = node.GetAttribute(SPACE_ATTRIBUTE)
    if attribute:
        return attribute.split(" ")[0], None
    grid = _gridOf(node)
    if grid is None:
        return None
    from LesionNetworkMappingLib import geometry

    result = geometry.detect_space(tuple(grid[0]), grid[1], origin_tolerance=0.01)
    return tuple(result) if result else None


def _gridOf(node):
    """``(dims, ijkToRas)`` of a volume or of a segmentation's reference geometry, or None."""
    import numpy as np

    if node is None:
        return None
    if node.IsA("vtkMRMLVolumeNode"):
        image = node.GetImageData()
        return (tuple(image.GetDimensions()), _ijkToRas(node)) if image is not None else None
    if node.IsA("vtkMRMLSegmentationNode"):
        geometry = node.GetSegmentation().GetConversionParameter(slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName())
        if not geometry:
            return None
        image = slicer.vtkOrientedImageData()
        if not slicer.vtkSegmentationConverter.DeserializeImageGeometry(geometry, image, False):
            return None
        extent = image.GetExtent()
        if extent[1] < extent[0]:
            return None
        matrix = vtk.vtkMatrix4x4()
        image.GetImageToWorldMatrix(matrix)
        shift = np.eye(4)
        shift[:3, 3] = extent[0::2]
        return (extent[1] - extent[0] + 1, extent[3] - extent[2] + 1, extent[5] - extent[4] + 1), _matrixToArray(matrix) @ shift
    return None


def _sameGrid(first, second, tolerance=1e-3):
    import numpy as np

    return first is not None and second is not None and tuple(first[0]) == tuple(second[0]) and np.allclose(first[1], second[1], atol=tolerance)


def _isScanVolume(node):
    return node is not None and node.IsA("vtkMRMLScalarVolumeNode") and not node.IsA("vtkMRMLLabelMapVolumeNode") and node.GetImageData() is not None


def findSourceVolume(segmentationNode):
    """The scan a native lesion was traced on: the segmentation's reference volume, the Segment Editor's source
    volume, else a scalar volume on the segmentation's reference grid. None when there is none.
    """
    if segmentationNode is None:
        return None
    reference = segmentationNode.GetNodeReference(slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole())
    if _isScanVolume(reference) and not isNormalizationResult(reference):
        return reference
    editorWidget = getattr(slicer.modules, "SegmentEditorWidget", None)
    editor = getattr(editorWidget, "editor", None) if editorWidget is not None else None
    if editor is not None:
        try:
            if editor.segmentationNode() is segmentationNode and _isScanVolume(editor.sourceVolumeNode()):
                return editor.sourceVolumeNode()
        except Exception:
            pass
    grid = _gridOf(segmentationNode)
    if grid is None:
        return None
    for volume in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        if _isScanVolume(volume) and not isNormalizationResult(volume) and _sameGrid(_gridOf(volume), grid):
            return volume
    return None


def isNativeSegmentation(segmentationNode):
    """True for a segmentation that is not on an MNI grid and is not a normalization result."""
    if segmentationNode is None or isNormalizationResult(segmentationNode):
        return False
    return spaceOf(segmentationNode) is None


class _Answer(tuple):
    """``(ok, reason)`` whose truth value is ``ok`` (callers may test it directly or unpack it)."""

    def __new__(cls, ok, reason=""):
        return super().__new__(cls, (bool(ok), str(reason or "")))

    def __bool__(self):
        return self[0]


def normalizationAvailable():
    """``(ok, reason)``: BRAINSFit and BRAINSResample are present (the normalization can run here)."""
    for moduleName, executable in (("brainsfit", "BRAINSFit"), ("brainsresample", "BRAINSResample")):
        if getattr(slicer.modules, moduleName, None) is None and shutil.which(executable) is None:
            return _Answer(False, _("{name} is not available").format(name=executable))
    return _Answer(True)


def canNormalize(segmentationNode=None, segmentId=None):
    """``(ok, reason)`` (true when ``ok``): the lesion is in native space and the scan it was traced on is found.

    Without arguments: whether normalization is available at all.
    """
    available = normalizationAvailable()
    if not available or segmentationNode is None:
        return available
    if isNormalizationResult(segmentationNode):
        return _Answer(False, _("This lesion is already a normalization result"))
    if not isNativeSegmentation(segmentationNode):
        return _Answer(False, _("This lesion is already on an MNI grid"))
    if segmentId and segmentationNode.GetSegmentation().GetSegment(segmentId) is None:
        return _Answer(False, _("The segment does not exist"))
    if findSourceVolume(segmentationNode) is None:
        return _Answer(False, _("The scan this lesion was traced on is not loaded"))
    return _Answer(True)


def lesionIsEmpty(segmentationNode, segmentId):
    """True when the segment has no voxel (its internal labelmap is read in place; a surface-only segment counts as not empty)."""
    if segmentationNode is None or not segmentId:
        return True
    segment = segmentationNode.GetSegmentation().GetSegment(segmentId)
    if segment is None:
        return True
    labelmap = segmentationNode.GetBinaryLabelmapInternalRepresentation(segmentId)
    if labelmap is None:
        # vtkSegment has no Python-usable list of its representations: look at the source one (closed surface)
        segmentation = segmentationNode.GetSegmentation()
        representation = segment.GetRepresentation(segmentation.GetSourceRepresentationName())
        if representation is None:
            representation = segment.GetRepresentation(slicer.vtkSegmentationConverter.GetSegmentationClosedSurfaceRepresentationName())
        if representation is None:
            return True
        return representation.GetNumberOfPoints() == 0 if hasattr(representation, "GetNumberOfPoints") else False
    extent = labelmap.GetExtent()
    if extent[1] < extent[0] or extent[3] < extent[2] or extent[5] < extent[4]:
        return True
    array = slicer.util.arrayFromSegmentInternalBinaryLabelmap(segmentationNode, segmentId)
    return not (array == segment.GetLabelValue()).any()


def _mapContextArgument(value):
    return value is not None and hasattr(value, "focusCard") and hasattr(value, "seed") and not hasattr(value, "GetID")


def offerNormalize(*args, **kwargs):
    """Show the normalization card for a lesion; never starts a job (the card's primary button does).

    Accepted forms: ``offerNormalize(segmentationNode, segmentId, shell=None)`` (Draw's call to action) and
    ``offerNormalize(mapContext, segmentationNode, segmentId)`` (the Map seed refusal). In Studio's Draw the
    Draw card is focused; otherwise the Map card is expanded (Studio switches to Map) with the scan preselected.
    ``preferMap=True`` always shows the Map card, also from Draw (the Draw card's "Options in Map…").
    Returns True when a card was shown.
    """
    ctx = None
    args = list(args)
    if args and _mapContextArgument(args[0]):
        ctx = args.pop(0)
    ctx = kwargs.pop("ctx", ctx)
    segmentationNode = args.pop(0) if args else kwargs.pop("segmentationNode", None)
    segmentId = args.pop(0) if args else kwargs.pop("segmentId", None)
    shell = args.pop(0) if args else kwargs.pop("shell", None)
    preferMap = bool(kwargs.pop("preferMap", False))
    if shell is None and ctx is None:
        shell = _studioShell()
    if ctx is None and shell is not None and not preferMap:
        current = None
        try:
            current = shell.currentWorkspace()
            current = getattr(current, "key", current)
        except Exception:
            current = None
        if current == "draw":
            try:
                from StudioLib import drawcard_normalize
            except Exception:
                drawcard_normalize = None
            if drawcard_normalize is not None and drawcard_normalize.focus(segmentationNode, segmentId):
                return True
    if ctx is None:
        if shell is not None:
            try:
                shell.switchTo("map")
            except Exception:
                logging.exception("Lesion network mapping: could not switch to Map")
        else:
            try:
                slicer.util.selectModule("LesionNetworkMapping")
            except Exception:
                pass
        widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
        if widget is None and getattr(slicer.modules, "lesionnetworkmapping", None) is not None:
            widget = slicer.modules.lesionnetworkmapping.widgetRepresentation().self()
        ctx = getattr(widget, "ctx", None)
    if ctx is None:
        return False
    card = ctx.card("normalize")
    if card is not None and hasattr(card, "preselect"):
        card.preselect(segmentationNode, segmentId)
    return bool(ctx.focusCard("normalize"))


#
# Worker threads
#


class _Worker:
    """Run ``fn()`` in a Python thread and call ``onDone(result, error)`` on the main thread (polled by a timer).

    The function must not touch Qt, VTK or MRML objects that the main thread uses.
    """

    _running = set()

    def __init__(self, fn, onDone, name="LNMNormalizeWorker"):
        self.fn = fn
        self.onDone = onDone
        self.result = None
        self.error = None
        self.cancelled = False
        self._finished = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._timer = qt.QTimer()
        self._timer.setInterval(WORKER_POLL_MS)
        self._timer.connect("timeout()", self._poll)

    def start(self):
        _Worker._running.add(self)
        self._thread.start()
        self._timer.start()
        return self

    def _run(self):
        try:
            self.result = self.fn()
        except BaseException as exc:  # handed to the main thread
            self.error = exc
            import traceback

            self.errorText = traceback.format_exc()
        finally:
            self._finished.set()

    def _poll(self):
        if not self._finished.is_set():
            return
        self._timer.stop()
        _Worker._running.discard(self)
        if self.error is not None:
            logging.error(f"Lesion network mapping: a normalization worker failed:\n{getattr(self, 'errorText', self.error)}")
        if not self.cancelled and self.onDone is not None:
            try:
                self.onDone(self.result, self.error)
            except Exception:
                logging.exception("Lesion network mapping: a normalization worker callback failed")

    def wait(self, timeoutS=None):
        """Block until the thread ended (tests); the callback still runs from the event loop."""
        return self._finished.wait(timeoutS)


def _writeVolumeFile(path, arrayKji, ijkToRas):
    """Worker: write a scan copied from a volume ([k, j, i]) as an uncompressed NIfTI."""
    from LesionNetworkMappingLib import nifti

    os.makedirs(os.path.dirname(path), exist_ok=True)
    nifti.write_nifti(path, arrayKji.transpose(2, 1, 0), ijkToRas, sform_code=nifti.XFORM_SCANNER, qform_code=nifti.XFORM_SCANNER)
    return path


def _lesionMask(lesion):
    """Worker: the binary mask [k, j, i] of a lesion export (``label`` selects the segment of a shared labelmap)."""

    array = lesion["array"]
    label = lesion.get("label")
    return (array == label) if label is not None else (array != 0)


def _writeLesionFile(path, lesion):
    from LesionNetworkMappingLib import nifti

    os.makedirs(os.path.dirname(path), exist_ok=True)
    mask = _lesionMask(lesion).astype("uint8")
    nifti.write_nifti(path, mask.transpose(2, 1, 0), lesion["ijkToRas"], sform_code=nifti.XFORM_SCANNER, qform_code=nifti.XFORM_SCANNER,
                      descrip="Lesion mask (Slicer Studio)")
    return path


def _readMask(path):
    """Worker: a template-space mask cropped to its bounding box: {"kji", "extent", "affine", "voxels", "labelmap"}."""
    import numpy as np

    from LesionNetworkMappingLib import nifti

    data, affine, _header = nifti.read_nifti(path, apply_scaling=False)
    data = np.asarray(data)
    if data.ndim > 3:
        data = data[..., 0]
    nonzero = np.argwhere(data != 0)
    if not len(nonzero):
        return {"kji": None, "extent": None, "affine": affine, "voxels": 0, "shape": data.shape}
    low, high = nonzero.min(axis=0), nonzero.max(axis=0)
    crop = (data[low[0]:high[0] + 1, low[1]:high[1] + 1, low[2]:high[2] + 1] != 0).astype(np.uint8)
    mask = {"kji": np.ascontiguousarray(crop.transpose(2, 1, 0)), "extent": (int(low[0]), int(high[0]), int(low[1]), int(high[1]), int(low[2]), int(high[2])),
            "affine": affine, "voxels": len(nonzero), "shape": data.shape}
    mask["labelmap"] = _orientedLabelmap(mask)  # made here: the main thread only hands it to the segmentation
    return mask


def _loadOutputs(outDir):
    """Worker: read a finished normalization (record, warped scan with its 1st/99th percentiles, cropped lesion, mosaic bytes)."""
    import numpy as np

    from LesionNetworkMappingLib import nifti

    normalize = _normalizeLib()
    record = normalize.read_result(outDir)
    if not record or record.get("status") != "done":
        raise RuntimeError(f"{outDir} holds no complete normalization")
    outputs = {key: os.path.join(outDir, *str(item["path"]).split("/")) for key, item in (record.get("outputs") or {}).items()}
    loaded = {"outDir": outDir, "record": record, "outputs": outputs}
    if "scan" in outputs:
        data, affine, _header = nifti.read_nifti(outputs["scan"], apply_scaling=False)
        data = np.asarray(data)
        if data.ndim > 3:
            data = data[..., 0]
        nonzero = data[data != 0]
        if nonzero.size:
            p1, p99 = (float(value) for value in np.percentile(nonzero, [1, 99]))
        else:
            p1, p99 = 0.0, 1.0
        if p99 <= p1:
            p99 = p1 + 1.0
        # The VTK array is filled here too: only attaching it is left to the main thread
        loaded["scan"] = {"scalars": _numpyToVtkScalars(np.ascontiguousarray(data.transpose(2, 1, 0))), "affine": affine, "window": (p1, p99), "shape": data.shape}
    if "lesion" in outputs:
        loaded["lesion"] = _readMask(outputs["lesion"])
    if "qc_png" in outputs and os.path.isfile(outputs["qc_png"]):
        with open(outputs["qc_png"], "rb") as f:
            loaded["png"] = f.read()
        loaded["thumbnail"] = _thumbnailPng(loaded["png"])
    return loaded


#: Largest side of the QC thumbnail made by the worker (decoding and scaling the full mosaic on the main thread took ~100 ms)
THUMBNAIL_PX = 480


def _thumbnailPng(data):
    """Worker: the QC mosaic scaled down to ``THUMBNAIL_PX`` (PNG bytes), or None when Pillow is not available."""
    try:
        import io

        from PIL import Image
    except ImportError:
        return None
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        image.thumbnail((THUMBNAIL_PX, THUMBNAIL_PX), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", compress_level=1)
    return buffer.getvalue()


def _numpyToVtkScalars(arrayKji):
    from vtk.util import numpy_support

    flat = arrayKji.reshape(-1)
    return numpy_support.numpy_to_vtk(flat, deep=True, array_type=numpy_support.get_vtk_array_type(flat.dtype))


def _orientedLabelmap(mask):
    """A vtkOrientedImageData of a cropped mask from ``_readMask`` (made by the worker: a new object no other thread uses)."""
    if mask.get("labelmap") is not None:
        return mask["labelmap"]
    labelmap = slicer.vtkOrientedImageData()
    extent = mask["extent"]
    labelmap.SetExtent(*extent)
    labelmap.GetPointData().SetScalars(_numpyToVtkScalars(mask["kji"]))
    labelmap.SetImageToWorldMatrix(_arrayToMatrix(mask["affine"]))
    return labelmap


def _writeJsonAtomic(path, data):
    tmpPath = f"{path}.{os.getpid()}.tmp"
    with open(tmpPath, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmpPath, path)


class _RowElider(qt.QObject):
    """Keeps the one-line status text of a ``ProgressRow`` that does not wrap ("Normalizing · nonlinear · 26% · ~28 s left") inside
    its label. The label has an Ignored width, so Qt would cut the last glyphs without an ellipsis: the text is shortened with
    an ellipsis (and the full text is the tooltip) when it is set and when the label is resized.
    """

    def __init__(self, label):
        qt.QObject.__init__(self, label)
        self.label = label
        self.full = None  # the text that was set
        self.shown = None  # what the label displays for it
        label.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Resize and self.full is not None:
            self.refresh()
        return False

    def setText(self, text):
        self.full = str(text)
        self.shown = None
        self.refresh()

    def refresh(self):
        label = self.label
        if self.shown is not None and str(label.text) != self.shown:
            self.full = self.shown = None  # another text was set (Stopping..., a failure): it is not ours any more
            return
        text = self.full
        if text is None:
            return
        width = label.width - 2 * label.margin - 2 * label.frameWidth
        if label.wordWrap or width < 40:  # a wrapped text is read in full; an unplaced label gets its width with the resize event
            shown = text
        else:
            shown = str(label.fontMetrics().elidedText(text, qt.Qt.ElideRight, width))
        if str(label.text) != shown:
            label.setText(shown)
        self.shown = shown
        if shown != text:
            label.setToolTip(text)
        elif not label.wordWrap:
            label.setToolTip("")


def _elider(progressRow):
    elider = getattr(progressRow, "_rowElider", None)
    if elider is None:
        elider = progressRow._rowElider = _RowElider(progressRow.label)
    return elider


def setRowProgress(progressRow, fraction, text=None):
    """``ProgressRow.setProgress()`` without the progress bar's synchronous repaint (measured 20-35 ms per value in
    the Map panel): the bar is repainted with the next paint of the window instead.

    A one-line status text that is wider than its label ends with an ellipsis (the full text is the tooltip).
    """
    bar = progressRow.bar
    elider = _elider(progressRow)
    if text is not None and elider.full == str(text) and elider.shown == str(progressRow.label.text):
        text = None  # an unchanged text costs nothing
    if fraction is not None and bar.maximum == 1000 and bar.value == round(min(max(float(fraction), 0.0), 1.0) * 1000):
        if text is not None:
            progressRow.label.setText(text)
            elider.setText(text)
        return
    bar.setUpdatesEnabled(False)
    try:
        progressRow.setProgress(fraction, text)
        if text is not None:
            elider.setText(text)
    finally:
        bar.setUpdatesEnabled(True)
    bar.update()


def describeFailure(info):
    """``(text, offer)`` of a failed normalization: ``offer`` is None, "head" (download the whole-head template and
    retry) or "template:<id>" (download that template's files and retry).
    """
    info = info or {}
    event = info.get("event") or {}
    code = info.get("code") or event.get("code")
    message = str(info.get("message") or _("the normalization failed"))
    if code == "alignment_failed":
        hint = event.get("hint") or _("Roughly align the scan by hand (Options) and retry")
        if event.get("head_template_missing"):
            return _("The scan could not be aligned to the template. The whole-head template (not bundled) allows a tilt recovery; or {hint}.").format(hint=hint[:1].lower() + hint[1:]), "head"
        return _("The scan could not be aligned to the template. {hint}.").format(hint=hint.rstrip(".")), None
    if code == "template_missing":
        if isUserTemplate(event.get("template")):
            return _("Your template {name} is missing its image or brain mask (Settings › Templates).").format(name=templateName(event.get("template"))), None  # noqa: RUF001
        return _("The template files are not available on this computer."), f"template:{event.get('template') or DEFAULT_TEMPLATE}"
    if code == "already_template_space":
        return _("This scan is already on a template grid: nothing to normalize."), None
    if code == "empty_lesion":
        return _("The lesion is empty: trace it first, or normalize the scan only."), None
    if code == "locked":
        return _("Another job is writing this output folder; try again when it has finished."), None
    if code == "exception":
        # an unexpected error: its text is internal (it may name local paths); the job log and the Python console have it
        return _("Normalization failed because of an unexpected error ({kind}); the log has the details.").format(kind=event.get("type") or _("internal error")), None
    return _("Normalization failed: {message}").format(message=message), None


def raterName():
    """``Studio/RaterName`` (only read), else the operating-system user."""
    name = str(qt.QSettings().value(RATER_SETTING) or "").strip()
    if name:
        return name
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


#
# Request and result
#


class NormalizeRequest:
    """What to normalize.

    The scan is ``scanNode`` or ``scanPath``; the lesion is ``segmentationNode`` + ``segmentId`` or ``lesionPath``
    (none: scan only). ``anatNode``/``anatPath`` is the same subject's T1w (FLAIR, DWI, CT). ``templateId`` is an
    explicit template id; ``quality`` standard, fine or affine; ``modality`` "auto" or a modality;
    ``initialTransformNode`` a rough manual alignment (its FromParent maps template points to scan points);
    ``outDir`` the output folder (default: ``defaultOutputDirectory()``); ``studyPath``/``sid`` come from A-03c.
    """

    def __init__(self, scanNode=None, scanPath=None, segmentationNode=None, segmentId=None, lesionPath=None, anatNode=None, anatPath=None,
                 subject=None, templateId=DEFAULT_TEMPLATE, quality="standard", modality="auto", dilateMm=DEFAULT_DILATE_MM,
                 initialTransformNode=None, outDir=None, studyPath=None, sid=None, reuse=True):
        self.scanNode = scanNode
        self.scanPath = scanPath
        self.segmentationNode = segmentationNode
        self.segmentId = segmentId
        self.lesionPath = lesionPath
        self.anatNode = anatNode
        self.anatPath = anatPath
        self.subject = subject
        self.templateId = templateId or DEFAULT_TEMPLATE
        self.quality = quality
        self.modality = modality or "auto"
        self.dilateMm = float(dilateMm)
        self.initialTransformNode = initialTransformNode
        self.outDir = outDir
        self.studyPath = studyPath
        self.sid = sid
        self.reuse = bool(reuse)

    def copy(self, **changes):
        other = NormalizeRequest.__new__(NormalizeRequest)
        other.__dict__.update(self.__dict__)
        other.__dict__.update(changes)
        return other

    def __repr__(self):
        return f"NormalizeRequest(scan={self.scanNode.GetName() if self.scanNode is not None else self.scanPath!r}, template={self.templateId}, quality={self.quality})"


def _sourceSegmentName(request):
    """The name of the native segment a request normalizes, or None (scan only, a lesion file, no such segment)."""
    node = request.segmentationNode if request is not None else None
    if node is None or not request.segmentId:
        return None
    segment = node.GetSegmentation().GetSegment(request.segmentId)
    name = segment.GetName() if segment is not None else ""
    return name.strip() or None


class NormalizeResult:
    """A loaded normalization: folder, record, outputs, QC values and the nodes made from it."""

    def __init__(self, outDir, record, outputs, status="done"):
        self.outDir = outDir
        self.record = record
        self.outputs = outputs
        self.status = status
        qc = record.get("qc") or {}
        self.templateId = (record.get("template") or {}).get("id") or DEFAULT_TEMPLATE
        self.suggestion = qc.get("suggestion")
        self.reasons = list(qc.get("reasons") or [])
        self.metrics = dict(qc.get("metrics") or {})
        self.fingerprint = record.get("fingerprint")
        self.volumeNode = None
        self.segmentationNode = None
        self.segmentId = None
        self.nativeSegmentationNode = None
        self.nativeSegmentId = None
        self.scanNode = None
        self.pngBytes = None
        self.thumbnailBytes = None
        self.transformNode = None
        self.nativeHidden = False
        self.commandLine = ""
        self.request = None

    @property
    def provenancePath(self):
        return os.path.join(self.outDir, _normalizeLib().PROVENANCE_JSON)

    @property
    def reference(self):
        """The ``LNM.Normalization`` value of the nodes made from this result (``portableReference()``, never absolute)."""
        return portableReference(self.provenancePath, self.outDir)

    def pixmap(self, thumbnail=False):
        """The QC mosaic (``thumbnail``: the small copy made by the worker, when there is one)."""
        pixmap = qt.QPixmap()
        data = (self.thumbnailBytes if thumbnail else None) or self.pngBytes
        if data:
            pixmap.loadFromData(qt.QByteArray(data))
        return pixmap

    def suggestionText(self):
        if self.suggestion == "looks-ok":
            return _("Looks consistent")
        reason = self.reasons[0] if self.reasons else _("review the alignment")
        return _("Check alignment: {reason}").format(reason=reason)

    def metricsText(self):
        parts = []
        ncc = self.metrics.get("ncc_xl")
        if isinstance(ncc, (int, float)):
            parts.append(f"NCC {ncc:.2f}")
        ratio = self.metrics.get("normalized_ratio")
        if isinstance(ratio, (int, float)):
            parts.append(_("volume ×{ratio:.2f} (scale-corrected)").format(ratio=ratio))  # noqa: RUF001 (multiplication sign)
        outside = self.metrics.get("outside_brain_pct")
        if isinstance(outside, (int, float)):
            parts.append(_("{pct:.0f}% outside brain").format(pct=outside))
        return " · ".join(parts)

    def review(self):
        return (self.record or {}).get("review") or {}


#
# Controller
#


class NormalizeController(VTKObservationMixin):
    """Runs one normalization at a time for a card (see the module description).

    Callback lists (plain callables, main thread): ``stateChanged(state)``, ``progressChanged(fraction, text,
    etaSeconds)``, ``resultReady(result)``, ``failed(info)`` (``info``: {message, code, event, job}),
    ``editStateChanged(edited, needsReregister)``, ``reviewChanged(result)``.
    """

    def __init__(self, context=None, name="normalize"):
        VTKObservationMixin.__init__(self)
        self.context = context
        self.name = name
        self.state = IDLE
        self.request = None
        self.job = None
        self.warpJob = None
        self.result = None
        self.failure = None
        self.warnings = []
        self.stage = None
        self.fraction = 0.0
        self.eta = None
        self.reviewing = False
        self.edited = False
        self.needsReregister = False
        self.timings = {}
        self.stateChanged = []
        self.progressChanged = []
        self.resultReady = []
        self.failed = []
        self.editStateChanged = []
        self.reviewChanged = []
        self._tmpDir = None
        self._worker = None
        self._stopRequested = False
        self._reviewState = None
        self._reviewTemplateNode = None
        self._reviewDisplayState = None
        self._exportedLesion = None
        self._observedNative = None
        #: What start() set aside of the previous result: a run that is stopped gives it back (nothing was changed)
        self._previousRun = None
        self._editGeneration = 0
        #: Counts scene closes: work started before a close never adds nodes to the next scene
        self._sceneGeneration = 0
        self._runGeneration = 0
        self._reviewHidden = []
        self._progressTimer = qt.QTimer()
        self._progressTimer.setSingleShot(True)
        self._progressTimer.setInterval(PROGRESS_INTERVAL_MS)
        self._progressTimer.connect("timeout()", self._emitProgress)
        self._editTimer = qt.QTimer()
        self._editTimer.setSingleShot(True)
        self._editTimer.setInterval(EDIT_DEBOUNCE_MS)
        self._editTimer.connect("timeout()", self._checkEdit)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self._onSceneStartClose)

    #
    # Notification
    #

    def _notify(self, listName, *args):
        for callback in list(getattr(self, listName)):
            try:
                callback(*args)
            except Exception:
                logging.exception(f"Lesion network mapping: a normalization {listName} callback failed")

    def _setState(self, state):
        if self.state != state:
            self.state = state
            self._notify("stateChanged", state)

    def isBusy(self):
        return self.state in BUSY_STATES

    @property
    def runner(self):
        context = self.context
        runner = getattr(context, "runner", None) if context is not None else None
        return runner if runner is not None else _jobrunner.instance()

    #
    # Start
    #

    def start(self, request):
        """Start a normalization (non-blocking); return True when it was started. Main-thread cost is measured in ``timings["start_ms"]``."""
        started = time.perf_counter()
        if self.isBusy():
            logging.warning("Lesion network mapping: a normalization is already running for this card")
            return False
        if request.scanNode is None and not request.scanPath:
            raise ValueError("A normalization needs a scan")
        self.restoreViews()
        self._previousRun = self._setAsideRun()
        self.request = request
        self.result = None
        self.failure = None
        self.warnings = []
        self.stage = None
        self.fraction = 0.0
        self.eta = None
        self._stopRequested = False
        self._runGeneration = self._sceneGeneration
        # A pending or running edit check of the previous result must not mark the new one as edited
        self._editGeneration += 1
        self._editTimer.stop()
        self._setEdited(False, False)
        self._tmpDir = os.path.join(slicer.app.temporaryPath, "LNMNormalize", uuid.uuid4().hex)
        writes = []
        try:
            scanPath = self._scanSource(request, writes)
            lesionPath = self._lesionSource(request, writes)
            anatPath = self._anatSource(request, writes)
            initialTransform = None
            if request.initialTransformNode is not None:
                os.makedirs(self._tmpDir, exist_ok=True)
                initialTransform = os.path.join(self._tmpDir, "initial_transform.h5")
                if not slicer.util.saveNode(request.initialTransformNode, initialTransform):
                    raise RuntimeError("The rough alignment could not be saved")
        except Exception:
            self._cleanupTmp()
            self._restoreRun()  # nothing was started: the previous result stays
            raise
        self.outDir = request.outDir or defaultOutputDirectory(request.scanNode, request.scanPath, request.subject, request.studyPath, request.sid)
        self._jobRunRoot = os.path.join(os.fspath(request.studyPath), ".studio", "jobs") if request.studyPath else None
        self.args = self.arguments(request, scanPath, lesionPath, anatPath, initialTransform, self.outDir)
        if writes:
            self._setState(PREPARING)

            def writeAll():
                return [write() for write in writes]

            self._worker = _Worker(writeAll, self._onInputsWritten).start()
        else:
            self._submit()
        self.timings["start_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        return True

    def _setAsideRun(self):
        """What a new run replaces of the previous result (``None`` without one): ``_restoreRun()`` puts it back."""
        if self.result is None:
            return None
        return {"result": self.result, "request": self.request, "outDir": getattr(self, "outDir", None), "args": getattr(self, "args", None),
                "warnings": self.warnings, "stage": self.stage, "exportedLesion": self._exportedLesion, "edited": (self.edited, self.needsReregister),
                "generation": self._sceneGeneration}

    def _restoreRun(self):
        """Give back the result that ``start()`` set aside, when the new run ended without a change (stopped, or never started).

        Not when the scene was closed meanwhile, or when a node of that result is gone. Returns True when it was restored.
        """
        previous, self._previousRun = self._previousRun, None
        if previous is None or previous["generation"] != self._sceneGeneration:
            return False
        result = previous["result"]
        if not all(node is None or slicer.mrmlScene.IsNodePresent(node) for node in (result.volumeNode, result.segmentationNode)):
            return False
        self.result = result
        self.request = previous["request"]
        self.outDir = previous["outDir"]
        self.args = previous["args"]
        self.warnings = previous["warnings"]
        self.stage = previous["stage"]
        self.fraction = 1.0
        self.eta = None
        self._exportedLesion = previous["exportedLesion"]
        self._trackEdits(result)
        self._setEdited(*previous["edited"])
        self._editTimer.start()  # the native lesion may have been edited since: compare it again
        return True

    def _setCancelled(self):
        """The run ended without a result: the previous one (if any) is back before the state changes."""
        self._restoreRun()
        self._setState(CANCELLED)

    def _scanSource(self, request, writes):
        """The scan file, or a NIfTI that a worker writes from a copy of the voxels (no gzip on the main thread)."""
        if request.scanPath:
            return os.path.abspath(request.scanPath)
        node = request.scanNode
        storage = node.GetStorageNode()
        fileName = storage.GetFileName() if storage is not None else None
        transform = node.GetParentTransformNode()
        roughAlignment = request.initialTransformNode is not None and transform is request.initialTransformNode
        if (fileName and os.path.isfile(fileName) and not node.GetModifiedSinceRead() and fileName.lower().endswith(SCAN_FILE_EXTENSIONS)
                and (transform is None or roughAlignment)):
            return fileName
        return self._exportVolume(node, "scan.nii", writes)

    def _exportVolume(self, node, name, writes):
        array = slicer.util.arrayFromVolume(node)
        if array.ndim != 3:
            raise ValueError(f"{node.GetName()} is not a scalar 3D volume")
        arrayCopy = array.copy()
        ijkToRas = _ijkToRas(node)
        path = os.path.join(self._tmpDir, name)
        writes.append(lambda: _writeVolumeFile(path, arrayCopy, ijkToRas))
        return path

    def _anatSource(self, request, writes):
        if request.anatPath:
            return os.path.abspath(request.anatPath)
        node = request.anatNode
        if node is None:
            return None
        storage = node.GetStorageNode()
        fileName = storage.GetFileName() if storage is not None else None
        if fileName and os.path.isfile(fileName) and not node.GetModifiedSinceRead() and fileName.lower().endswith(SCAN_FILE_EXTENSIONS) and node.GetParentTransformNode() is None:
            return fileName
        return self._exportVolume(node, "anat.nii", writes)

    def exportLesion(self, segmentationNode, segmentId):
        """Main thread: a copy of a segment's voxels on its own (internal labelmap) grid: {"array" [k, j, i], "label", "ijkToRas", "voxelsKnown"}.

        None when the segment has no voxels. The comparison with the segment's label value is left to the worker.
        """
        import numpy as np

        if segmentationNode is None or not segmentId or segmentationNode.GetSegmentation().GetSegment(segmentId) is None:
            return None
        labelmap = segmentationNode.GetBinaryLabelmapInternalRepresentation(segmentId)
        if labelmap is not None:
            extent = labelmap.GetExtent()
            if extent[1] < extent[0] or extent[3] < extent[2] or extent[5] < extent[4]:
                return None
            array = slicer.util.arrayFromSegmentInternalBinaryLabelmap(segmentationNode, segmentId).copy()
            matrix = vtk.vtkMatrix4x4()
            labelmap.GetImageToWorldMatrix(matrix)
            shift = np.eye(4)
            shift[:3, 3] = extent[0::2]
            segment = segmentationNode.GetSegmentation().GetSegment(segmentId)
            return {"array": array, "label": int(segment.GetLabelValue()), "ijkToRas": _matrixToArray(matrix) @ shift, "extent": tuple(extent),
                    "imageToWorld": _matrixToArray(matrix)}
        # Another source representation (closed surface): export through a temporary labelmap (slower, rare)
        widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
        logic = getattr(widget, "logic", None)
        if logic is None:
            import LesionNetworkMapping

            logic = LesionNetworkMapping.LesionNetworkMappingLogic()
        data, affine = logic.seedArray(segmentationNode, segmentId)
        return {"array": np.ascontiguousarray(data.transpose(2, 1, 0)), "label": None, "ijkToRas": affine, "extent": None, "imageToWorld": None}

    def _lesionSource(self, request, writes):
        if request.lesionPath:
            return os.path.abspath(request.lesionPath)
        if request.segmentationNode is None or not request.segmentId:
            self._exportedLesion = None
            return None
        lesion = self.exportLesion(request.segmentationNode, request.segmentId)
        self._exportedLesion = lesion
        if lesion is None:
            return None  # empty: scan only (the card says so before starting)
        path = os.path.join(self._tmpDir, "lesion.nii")
        writes.append(lambda: _writeLesionFile(path, lesion))
        return path

    def arguments(self, request, scanPath, lesionPath, anatPath, initialTransform, outDir):
        """Arguments of ``normalize run`` (the JobRunner adds --cancel-file and --threads)."""
        modality = request.modality
        if modality in (None, "", "auto"):
            modality, guessed = guessModality(request.scanNode, request.scanPath)
            if guessed:
                self.warnings.append({"code": "modality_guessed", "message": _("modality guessed: {modality}").format(modality=modality)})
        subject = request.sid or request.subject or (request.scanNode.GetName() if request.scanNode is not None else os.path.basename(scanPath))
        args = ["run", "--scan", scanPath]
        if lesionPath:
            args += ["--lesion", lesionPath]
        args += ["--out", outDir, "--subject", participantLabel(subject), "--template", request.templateId, "--quality", request.quality,
                 "--modality", modality, "--dilate-mm", f"{request.dilateMm:g}"]
        if anatPath:
            args += ["--anat", anatPath]
        if initialTransform:
            args += ["--initial-transform", initialTransform]
        if not request.reuse:
            args.append("--no-reuse")
        for folder in templateDirectories():
            args += ["--template-dir", folder]
        for option, moduleName in (("--brainsfit", "brainsfit"), ("--brainsresample", "brainsresample"), ("--brainsroiauto", "brainsroiauto")):
            module = getattr(slicer.modules, moduleName, None)
            if module is not None and module.path:
                args += [option, module.path]
        args += ["--events", "json", "--parent-pid", str(os.getpid()), "--nice", "5"]
        return args

    def _onInputsWritten(self, result, error):
        self._worker = None
        if self._stopRequested:
            self._cleanupTmp()
            self._setCancelled()
            return
        if error is not None:
            self._fail(_("The scan or the lesion could not be exported: {error}").format(error=error), "export_failed")
            return
        self._submit()

    def _privateRunRoot(self):
        """Folder of the job logs: the study's private ``.studio/jobs`` or the application cache, never the shared output folder (job logs hold absolute local paths)."""
        return getattr(self, "_jobRunRoot", None) or _jobrunner.defaultRunRoot()

    def _submit(self):
        spec = _jobrunner.JobSpec(JOB_MODULE, self.args, title=_("Normalizing"), heavy=True, kind="normalization", runRoot=self._privateRunRoot(),
                                  onEvent=self._onEvent, onDone=self._onJobDone, statusText=self._statusText, name="normalize")
        self._setState(QUEUED)
        self.job = self.runner.submit(spec)
        return self.job

    def commandLine(self):
        """The job's command line (for "Copy command line")."""
        job = self.job
        if job is not None and job.args:
            return shlex.join(job.args)
        return shlex.join(["PythonSlicer", "-m", JOB_MODULE, *getattr(self, "args", [])])

    #
    # Progress
    #

    def _onEvent(self, event):
        kind = event.get("event")
        if kind == "start" or (kind == "stage" and self.state == QUEUED):
            self._setState(RUNNING)
        if kind == "stage":
            self.stage = event.get("name")
            self._scheduleProgress()
        elif kind == "progress":
            try:
                self.fraction = min(max(float(event.get("fraction") or 0.0), 0.0), 1.0)
            except (TypeError, ValueError):
                pass
            self.stage = event.get("stage") or self.stage
            self.eta = event.get("eta")
            self._scheduleProgress()
        elif kind == "warning":
            self.warnings.append({"code": event.get("code"), "message": event.get("message")})
        elif kind == "queued":
            self._notify("progressChanged", 0.0, _("Waiting for {title} to finish").format(title=event.get("waitingFor") or _("the running job")), None)

    def _scheduleProgress(self):
        if not self._progressTimer.isActive():
            self._progressTimer.start()

    def progressText(self):
        parts = [_("Normalizing")]
        if self.stage:
            parts.append(STAGE_LABELS.get(self.stage, self.stage))
        parts.append(f"{int(self.fraction * 100)}%")
        if isinstance(self.eta, (int, float)) and self.eta >= 0:
            parts.append(_("~{seconds} s left").format(seconds=max(1, round(self.eta))))
        return " · ".join(parts)

    def _emitProgress(self):
        started = time.perf_counter()
        if self.state == RUNNING:
            self._notify("progressChanged", self.fraction, self.progressText(), self.eta)
        elapsed = (time.perf_counter() - started) * 1000.0
        self.timings["progress_max_ms"] = round(max(self.timings.get("progress_max_ms", 0.0), elapsed), 3)

    def _statusText(self, job):
        """Status chip text of the job (the JobRunner owns the chip): "Normalizing 40% · < 1 min left"."""
        fraction, eta = self.fraction, self.eta
        event = job.lastEvent or {}
        if event.get("event") == "progress":  # the runner asks before the event reaches this controller
            try:
                fraction, eta = min(max(float(event.get("fraction") or 0.0), 0.0), 1.0), event.get("eta")
            except (TypeError, ValueError):
                pass
        text = f"{_('Normalizing')} {int(fraction * 100)}%"
        eta = _jobrunner.formatEta(eta)
        return f"{text} · {eta}" if eta else text

    #
    # Stop
    #

    def stop(self):
        """Stop the running normalization (cancel file; the JobRunner escalates). Never bound to Esc."""
        self._stopRequested = True
        if self.state == PREPARING:
            return  # the export finishes, then nothing is submitted
        if self.job is not None and not self.job.isFinished():
            self.job.stop()
        elif self.state == LOADING and self._worker is not None:
            self._worker.cancelled = True
            self._worker = None
            self._setCancelled()

    #
    # Done
    #

    def _cleanupTmp(self):
        if self._tmpDir:
            shutil.rmtree(self._tmpDir, ignore_errors=True)
            self._tmpDir = None

    def _fail(self, message, code=None, event=None, job=None):
        self.failure = {"message": message, "code": code, "event": event or {}, "job": job}
        self._previousRun = None
        self._setState(FAILED)
        self._notify("failed", self.failure)

    def _onJobDone(self, job):
        self._progressTimer.stop()
        self._cleanupTmp()
        if job.state == _jobrunner.CANCELLED:
            self._setCancelled()
            return
        if job.state != _jobrunner.DONE:
            event = job.errorEvent or {}
            self._fail(job.failureReason or _("the normalization failed"), event.get("code"), event, job)
            return
        if self._stopRequested:
            self._setCancelled()
            return
        self._setState(LOADING)
        status = (job.doneEvent or {}).get("status") or "done"
        outDir = self.outDir
        self._worker = _Worker(lambda: _loadOutputs(outDir), lambda loaded, error: self._onOutputsLoaded(loaded, error, status)).start()

    def _onOutputsLoaded(self, loaded, error, status):
        self._worker = None
        if error is not None:
            self._fail(_("The result could not be read: {error}").format(error=error), "read_failed")
            return
        if self._stopRequested or self._runGeneration != self._sceneGeneration or not self._requestNodesPresent(self.request):
            self._setCancelled()  # stopped, or the scene was closed: no node of another scene's lesion
            return
        started = time.perf_counter()
        collecting = gc.isenabled()
        gc.disable()  # no garbage collection pause inside this short main-thread step
        try:
            result = self.createNodes(loaded, status, self.request)
        except Exception as exc:
            logging.exception("Lesion network mapping: the normalization result could not be loaded")
            self._fail(_("The result could not be loaded: {error}").format(error=exc), "load_failed")
            return
        finally:
            if collecting:
                gc.enable()
        self.timings["nodes_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        result.commandLine = self.commandLine()
        self._previousRun = None
        self.result = result
        self.fraction = 1.0
        self._setState(DONE)
        self._trackEdits(result)
        # The cards build their QC panel in the next event-loop turn: node creation and layout are two short pauses, not one long one
        qt.QTimer.singleShot(0, lambda: self.result is result and self._notify("resultReady", result))

    @staticmethod
    def _requestNodesPresent(request):
        """False when a node of the request (scan, lesion, anatomical scan) is no longer in the scene."""
        if request is None:
            return True
        nodes = (request.scanNode, request.segmentationNode, request.anatNode)
        return all(node is None or slicer.mrmlScene.IsNodePresent(node) for node in nodes)

    def _onSceneStartClose(self, caller=None, event=None):
        """The scene closes: stop the running job, forget the result and the review, so nothing reaches the next scene."""
        self._sceneGeneration += 1
        self._editGeneration += 1
        self._untrackEdits()
        self._progressTimer.stop()
        self._reviewState = None
        self._reviewDisplayState = None
        self._reviewHidden = []
        self._reviewTemplateNode = None
        self.reviewing = False
        self._exportedLesion = None
        if self.warpJob is not None and not self.warpJob.isFinished():
            self.warpJob.stop()
        self.warpJob = None
        self.result = None
        self._previousRun = None
        self._setEdited(False, False)
        if self.isBusy():
            self.stop()  # the job ends as cancelled (_onJobDone / _onOutputsLoaded create no node)
        else:
            self.request = None
            self._setState(IDLE)

    def loadResult(self, outDir, request=None):
        """Load an existing result folder (no job): the worker reads it, then the nodes are made. Returns False when busy.

        The native lesion as it is now is the original of the edit checks (as the exported lesion is for a run of
        ``start()``): without it, every later check would report an edit outside the mask.
        """
        if self.isBusy():
            return False
        self.request = request or self.request
        self.outDir = outDir
        self._stopRequested = False
        self._runGeneration = self._sceneGeneration
        request = self.request
        if request is not None and request.segmentationNode is not None and request.segmentId:
            self._exportedLesion = self.exportLesion(request.segmentationNode, request.segmentId)
        else:
            self._exportedLesion = None
        self._setState(LOADING)
        self._worker = _Worker(lambda: _loadOutputs(outDir), lambda loaded, error: self._onOutputsLoaded(loaded, error, "reused")).start()
        return True

    #
    # Nodes
    #

    @staticmethod
    def _existingNodes(provenancePath, fingerprint):
        """The volume and segmentation already made from this result (same normalization.json and fingerprint), or (None, None).

        Nodes hold ``portableReference(provenancePath)``; nodes of earlier scenes may still hold the absolute path.
        """
        volume = segmentation = None
        references = {portableReference(provenancePath, os.path.dirname(provenancePath)), provenancePath}
        for className in ("vtkMRMLScalarVolumeNode", "vtkMRMLSegmentationNode"):
            for node in slicer.util.getNodesByClass(className):
                if node.GetAttribute(NORMALIZATION_ATTRIBUTE) in references and node.GetAttribute(FINGERPRINT_ATTRIBUTE) == (fingerprint or ""):
                    if className == "vtkMRMLScalarVolumeNode" and volume is None:
                        volume = node
                    elif className == "vtkMRMLSegmentationNode" and segmentation is None:
                        segmentation = node
        return volume, segmentation

    @staticmethod
    def _previousNodes(request, result):
        """The volume and segmentation of an earlier result of the same native lesion in the same space, or (None, None).

        A new registration of an edited lesion (Normalize again, Re-register, ⋯ Redo) replaces their voxels instead of
        adding a second MNI lesion: the Map seed and every other reference keep following the lesion (one MNI lesion per
        native lesion and template). A segmentation that holds the run's own lesion before an "Update MNI lesion" comes
        first; else the most recent one. The volume is the one made with that segmentation (same reference and run).
        """
        native = request.segmentationNode if request is not None else None
        if native is None:
            return None, None
        origin = f"{native.GetID()}|{request.segmentId or ''}"
        space = spaceAttributeValue(result.templateId, result.record)
        candidates = [node for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
                      if node.GetAttribute(NORMALIZED_FROM_ATTRIBUTE) == origin and node.GetAttribute(SPACE_ATTRIBUTE) == space
                      and node.GetAttribute(NORMALIZATION_ATTRIBUTE)]
        if not candidates:
            return None, None
        runOf = lambda node: (node.GetAttribute(FINGERPRINT_ATTRIBUTE) or "").split("+edit-")[0]  # noqa: E731
        own = [node for node in candidates if runOf(node) == (result.fingerprint or "")]
        segmentation = own[0] if own else candidates[-1]
        reference, fingerprint = segmentation.GetAttribute(NORMALIZATION_ATTRIBUTE), runOf(segmentation)
        volume = next((node for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
                       if node.GetAttribute(NORMALIZATION_ATTRIBUTE) == reference and node.GetAttribute(FINGERPRINT_ATTRIBUTE) == fingerprint), None)
        return volume, segmentation

    def createNodes(self, loaded, status, request):
        """Main thread: the warped scan volume and the template-space lesion segmentation of a loaded result."""
        result = NormalizeResult(loaded["outDir"], loaded["record"], loaded["outputs"], status)
        result.request = request
        result.pngBytes = loaded.get("png")
        result.thumbnailBytes = loaded.get("thumbnail")
        templateId = result.templateId
        scanNode = request.scanNode if request is not None else None
        result.scanNode = scanNode
        if request is not None:
            result.nativeSegmentationNode = request.segmentationNode
            result.nativeSegmentId = request.segmentId
        provenance = result.provenancePath
        volume, segmentation = self._existingNodes(provenance, result.fingerprint)
        # A new registration of the same native lesion (edited, or other options) replaces the earlier result's nodes
        previousVolume, previousSegmentation = self._previousNodes(request, result) if segmentation is None else (None, None)
        scanName = scanNode.GetName() if scanNode is not None else (result.record.get("subject") or "scan")
        if volume is None and previousVolume is not None and "scan" in loaded:
            volume = previousVolume
            self._replaceScan(volume, loaded["scan"], result)
        elif volume is None and "scan" in loaded:
            scan = loaded["scan"]
            # The display node comes first, with AutoWindowLevel off: no histogram of the 8.5 M voxels on the main thread
            display = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeDisplayNode")
            display.AutoWindowLevelOff()
            display.SetWindowLevelMinMax(*scan["window"])
            display.SetAndObserveColorNodeID("vtkMRMLColorTableNodeGrey")
            volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", slicer.mrmlScene.GenerateUniqueName(f"{scanName} ({templateName(templateId)})"))
            self._tag(volume, result)
            volume.SetAndObserveDisplayNodeID(display.GetID())
            image = vtk.vtkImageData()
            shape = scan["shape"]
            image.SetDimensions(int(shape[0]), int(shape[1]), int(shape[2]))
            image.GetPointData().SetScalars(scan["scalars"])
            volume.SetIJKToRASMatrix(_arrayToMatrix(scan["affine"]))
            volume.SetAndObserveImageData(image)
        if previousSegmentation is not None:
            segmentation = previousSegmentation
            self._replaceLesion(segmentation, loaded.get("lesion"), result, volume)
        if segmentation is None:
            # The MNI lesion keeps the name of the native segment (segment ID stays LESION_SEGMENT): with several lesions
            # in one segmentation, the Map seed then names the lesion that was normalized
            lesionName = _sourceSegmentName(request) or LESION_SEGMENT
            segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", slicer.mrmlScene.GenerateUniqueName(f"{lesionName} ({templateName(templateId)})"))
            segmentation.CreateDefaultDisplayNodes()
            # Hidden until it is shown over a template-space image (showInViews): over the native scan it would mislead
            segmentation.GetDisplayNode().SetVisibility(False)
            if volume is not None:
                segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
            segmentId = segmentation.GetSegmentation().AddEmptySegment(LESION_SEGMENT, lesionName, LESION_COLOR)
            mask = loaded.get("lesion")
            if mask is not None and mask.get("kji") is not None:
                slicer.vtkSlicerSegmentationsModuleLogic.SetBinaryLabelmapToSegment(_orientedLabelmap(mask), segmentation, segmentId)
            self._tag(segmentation, result)
            if request is not None and request.segmentationNode is not None:
                segmentation.SetAttribute(NORMALIZED_FROM_ATTRIBUTE, f"{request.segmentationNode.GetID()}|{request.segmentId or ''}")
        result.volumeNode = volume
        result.segmentationNode = segmentation
        segmentIds = segmentation.GetSegmentation().GetSegmentIDs()
        result.segmentId = LESION_SEGMENT if segmentation.GetSegmentation().GetSegment(LESION_SEGMENT) is not None else (segmentIds[0] if segmentIds else None)
        return result

    @staticmethod
    def _replaceScan(volume, scan, result):
        """Replace the warped scan of an earlier result in place (``createNodes()``: same node, the new run's voxels)."""
        display = volume.GetDisplayNode()
        if display is not None:
            display.AutoWindowLevelOff()
            display.SetWindowLevelMinMax(*scan["window"])
        image = vtk.vtkImageData()
        shape = scan["shape"]
        image.SetDimensions(int(shape[0]), int(shape[1]), int(shape[2]))
        image.GetPointData().SetScalars(scan["scalars"])
        volume.SetIJKToRASMatrix(_arrayToMatrix(scan["affine"]))
        volume.SetAndObserveImageData(image)
        NormalizeController._tag(volume, result)

    @staticmethod
    def _replaceLesion(segmentation, mask, result, volume=None):
        """Replace the MNI lesion of an earlier result in place (``createNodes()``): the run's lesion, the run's tags."""
        if volume is not None:
            segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
        if segmentation.GetSegmentation().GetSegment(LESION_SEGMENT) is None:
            segmentation.GetSegmentation().AddEmptySegment(LESION_SEGMENT, LESION_SEGMENT, LESION_COLOR)
        if mask is not None and mask.get("kji") is not None:
            labelmap = _orientedLabelmap(mask)
        else:
            labelmap = slicer.vtkOrientedImageData()
            labelmap.SetExtent(0, -1, 0, -1, 0, -1)
            labelmap.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
        slicer.vtkSlicerSegmentationsModuleLogic.SetBinaryLabelmapToSegment(labelmap, segmentation, LESION_SEGMENT)
        NormalizeController._tag(segmentation, result)
        segmentation.RemoveAttribute(LESION_WARP_ATTRIBUTE)  # the warp of an "Update MNI lesion" of the earlier run

    @staticmethod
    def _tag(node, result):
        node.SetAttribute(NORMALIZATION_ATTRIBUTE, result.reference)
        node.SetAttribute(SPACE_ATTRIBUTE, spaceAttributeValue(result.templateId, result.record))
        node.SetAttribute(SUGGESTION_ATTRIBUTE, result.suggestion or "")
        node.SetAttribute(FINGERPRINT_ATTRIBUTE, result.fingerprint or "")

    @staticmethod
    def showInViews(result):
        """Show a result where it belongs: the warped scan as the background of every slice view and the MNI lesion over it."""
        if result is None:
            return False
        if result.volumeNode is not None and slicer.mrmlScene.IsNodePresent(result.volumeNode):
            slicer.util.setSliceViewerLayers(background=result.volumeNode, foreground=None, fit=True)
        segmentation = result.segmentationNode
        if segmentation is not None and segmentation.GetDisplayNode() is not None:
            segmentation.GetDisplayNode().SetVisibility(True)
        native = result.nativeSegmentationNode
        display = native.GetDisplayNode() if native is not None and slicer.mrmlScene.IsNodePresent(native) else None
        if display is not None and display.GetVisibility():
            display.SetVisibility(False)  # a scan-space lesion over a template-space image would mislead
            result.nativeHidden = True
        return True

    @staticmethod
    def showNative(result):
        """Show the native lesion again when it is edited or chosen again (``showInViews()`` hid it)."""
        if result is None or not getattr(result, "nativeHidden", False):
            return False
        result.nativeHidden = False
        native = result.nativeSegmentationNode
        display = native.GetDisplayNode() if native is not None and slicer.mrmlScene.IsNodePresent(native) else None
        if display is not None:
            display.SetVisibility(True)
        return True

    def loadTransform(self, result=None):
        """Load the native → template transform (only on request); return the node."""
        result = result or self.result
        if result is None:
            return None
        if result.transformNode is not None and slicer.mrmlScene.IsNodePresent(result.transformNode):
            return result.transformNode
        path = result.outputs.get("transform")
        if not path or not os.path.isfile(path):
            return None
        node = slicer.util.loadTransform(path)
        if node is not None:
            node.SetName(f"native → {result.templateId}")
            node.SetAttribute(NORMALIZATION_ATTRIBUTE, result.reference)
        result.transformNode = node
        return node

    #
    # Review verdict
    #

    def setVerdict(self, verdict, result=None):
        """Record "pass" (Looks right) or "fail" (Doesn't look right) in normalization.json review {verdict, by, at}."""
        result = result or self.result
        if result is None or verdict not in ("pass", "fail", None):
            return False
        record = _normalizeLib().read_result(result.outDir)
        if record is None:
            return False
        record["review"] = {"verdict": verdict, "by": raterName() if verdict else None,
                            "at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds") if verdict else None}
        _writeJsonAtomic(result.provenancePath, record)
        result.record = record
        self._notify("reviewChanged", result)
        return True

    #
    # Review in views
    #

    def _reviewTemplate(self, templateId):
        """The template shown behind the warped scan: a template volume of the scene, else the bundled 2 mm brain or a cached T1w."""
        for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            if node.GetAttribute(REVIEW_TEMPLATE_ATTRIBUTE) == templateId:
                return node, False
        if isUserTemplate(templateId):
            # The open volume of the user's template carries its id in Studio.TemplateId (its Studio.Space is the space, never "user:<slug>")
            for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
                if (node.GetAttribute("Studio.TemplateId") == templateId and not node.IsA("vtkMRMLLabelMapVolumeNode")
                        and not isNormalizationResult(node) and self._isTemplateVolume(node)):
                    return node, False
        for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            if node.GetAttribute("Studio.Role") == "template" and not isNormalizationResult(node):
                found = spaceOf(node)
                if found and found[0] == templateId:
                    return node, False
        path = templateFile(templateId, "reference") or (templateFile(templateId, "fixed") if templateId == DEFAULT_TEMPLATE else templateFile(templateId, "head"))
        if not path:
            return None, False
        node = self._readTemplateVolume(path, f"{templateName(templateId)} template")
        if node is None:
            return None, False
        node.SetAttribute(REVIEW_TEMPLATE_ATTRIBUTE, templateId)
        node.SetAttribute(SPACE_ATTRIBUTE, spaceAttributeValue(templateId))
        return node, True

    @staticmethod
    def _isTemplateVolume(node):
        """False for the volume of a removed template with the same id that was added again with another image."""
        try:
            from StudioLib import templates
            return templates.templateOfNode(node) is not None
        except Exception:
            return True

    @staticmethod
    def _readTemplateVolume(path, name):
        """Read an internal template file into a volume node that is not saved with the scene.

        Read with a storage node directly rather than ``slicer.util.loadVolume``: a loaded file would join the
        user's Recent files (the official RecentlyLoadedFiles key of the shared Slicer.ini).
        """
        scene = slicer.mrmlScene
        node = scene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", name)
        storage = scene.AddNewNodeByClass("vtkMRMLVolumeArchetypeStorageNode")
        storage.SetFileName(path)
        node.SetAndObserveStorageNodeID(storage.GetID())
        if not storage.ReadData(node):
            NormalizeController._removeWithHelpers(node)
            return None
        node.CreateDefaultDisplayNodes()
        for helper in [node, storage] + [node.GetNthDisplayNode(index) for index in range(node.GetNumberOfDisplayNodes())]:
            if helper is not None:
                helper.SetSaveWithScene(False)
        return node

    @staticmethod
    def _removeWithHelpers(node):
        """Remove a volume node and its display and storage nodes."""
        scene = slicer.mrmlScene
        helpers = [node.GetNthDisplayNode(index) for index in range(node.GetNumberOfDisplayNodes())] + [node.GetStorageNode()]
        if scene.IsNodePresent(node):
            scene.RemoveNode(node)
        for helper in helpers:
            if helper is not None and scene.IsNodePresent(helper):
                scene.RemoveNode(helper)

    @staticmethod
    def compositeState():
        """Background, foreground and label volumes and opacities of every slice view."""
        state = {}
        for composite in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
            state[composite.GetID()] = (composite.GetBackgroundVolumeID(), composite.GetForegroundVolumeID(), composite.GetLabelVolumeID(),
                                        composite.GetForegroundOpacity(), composite.GetLabelOpacity())
        return state

    def reviewInViews(self, result=None):
        """Template behind, warped scan in front at 50 %, the lesion as an outline; T over a view flickers. ``restoreViews()`` undoes it."""
        result = result or self.result
        if result is None or result.volumeNode is None:
            return False
        if self._reviewState is None:
            self._reviewState = self.compositeState()
            display = result.segmentationNode.GetDisplayNode() if result.segmentationNode is not None else None
            self._reviewDisplayState = (display, display.GetVisibility2DFill(), display.GetVisibility2DOutline(), display.GetVisibility()) if display is not None else None
            # Other segmentations (the native lesion) are hidden while the template is shown
            self._reviewHidden = []
            for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
                other = node.GetDisplayNode()
                if node is not result.segmentationNode and other is not None and other.GetVisibility():
                    other.SetVisibility(False)
                    self._reviewHidden.append(other)
        template, created = self._reviewTemplate(result.templateId)
        if created:
            self._reviewTemplateNode = template
        for composite in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
            composite.SetBackgroundVolumeID(template.GetID() if template is not None else result.volumeNode.GetID())
            composite.SetForegroundVolumeID(result.volumeNode.GetID() if template is not None else None)
            composite.SetForegroundOpacity(0.5)
            composite.SetLabelVolumeID(None)
        display = result.segmentationNode.GetDisplayNode() if result.segmentationNode is not None else None
        if display is not None:
            display.SetVisibility(True)
            display.SetVisibility2DFill(False)
            display.SetVisibility2DOutline(True)
        self.reviewing = True
        layoutManager = slicer.app.layoutManager() if slicer.app is not None else None
        if layoutManager is not None:
            for name in layoutManager.sliceViewNames():
                layoutManager.sliceWidget(name).sliceLogic().FitSliceToAll()
        shell = _studioShell()
        if shell is not None:
            try:
                shell.toast(self.reviewHint(), 4000)
            except Exception:
                pass
        self._notify("reviewChanged", result)
        return True

    @staticmethod
    def reviewHint():
        return _("Press T over a view to flicker between template and scan")

    def restoreViews(self):
        """Put back every slice view's layers and opacities, and the lesion display, as they were before ``reviewInViews()``."""
        if self._reviewState is None:
            return False
        for compositeId, (background, foreground, label, foregroundOpacity, labelOpacity) in self._reviewState.items():
            composite = slicer.mrmlScene.GetNodeByID(compositeId)
            if composite is None:
                continue
            composite.SetBackgroundVolumeID(background)
            composite.SetForegroundVolumeID(foreground)
            composite.SetLabelVolumeID(label)
            composite.SetForegroundOpacity(foregroundOpacity)
            composite.SetLabelOpacity(labelOpacity)
        if self._reviewDisplayState is not None:
            display, fill, outline, visible = self._reviewDisplayState
            if display is not None and slicer.mrmlScene.IsNodePresent(display):
                display.SetVisibility2DFill(fill)
                display.SetVisibility2DOutline(outline)
                display.SetVisibility(visible)
        for other in getattr(self, "_reviewHidden", []):
            if slicer.mrmlScene.IsNodePresent(other):
                other.SetVisibility(True)
        self._reviewHidden = []
        if self._reviewTemplateNode is not None and slicer.mrmlScene.IsNodePresent(self._reviewTemplateNode):
            self._removeWithHelpers(self._reviewTemplateNode)
        self._reviewTemplateNode = None
        self._reviewState = None
        self._reviewDisplayState = None
        self.reviewing = False
        self._notify("reviewChanged", self.result)
        return True

    #
    # Edits of the native lesion after a result
    #

    def _trackEdits(self, result):
        self._untrackEdits()
        node = result.nativeSegmentationNode
        if node is None or not slicer.mrmlScene.IsNodePresent(node):
            return
        self._observedNative = node
        for event in (slicer.vtkSegmentation.SegmentModified, slicer.vtkSegmentation.SourceRepresentationModified):
            self.addObserver(node, event, self._onNativeModified)

    def _untrackEdits(self):
        if self._observedNative is not None:
            self.removeObservers(self._onNativeModified)
        self._observedNative = None
        self._editTimer.stop()

    def _onNativeModified(self, caller=None, event=None):
        if self.result is not None and not self.isBusy():
            self._editTimer.start()

    def _setEdited(self, edited, needsReregister):
        if (edited, needsReregister) != (self.edited, self.needsReregister):
            self.edited, self.needsReregister = edited, needsReregister
            self._notify("editStateChanged", edited, needsReregister)

    def _checkEdit(self):
        """Debounced: compare the native lesion with the one that was normalized (a worker does the voxel work)."""
        result = self.result
        if result is None or result.nativeSegmentationNode is None or self.isBusy():
            return
        current = self.exportLesion(result.nativeSegmentationNode, result.nativeSegmentId)
        original = self._exportedLesion
        self._editGeneration += 1
        generation = self._editGeneration
        dilateMm = self.request.dilateMm if self.request is not None else DEFAULT_DILATE_MM

        def compare():
            return _compareLesions(original, current, dilateMm)

        def done(answer, error):
            if generation != self._editGeneration or error is not None:
                return
            changed, outside = answer
            self._setEdited(bool(changed), bool(outside))

        _Worker(compare, done).start()

    def updateMniLesion(self, onDone=None):
        """Re-warp the edited native lesion with the result's transform (``normalize warp``, a light job) and replace the MNI segment's voxels."""
        result = self.result
        if result is None or result.nativeSegmentationNode is None or (self.warpJob is not None and not self.warpJob.isFinished()):
            return False
        lesion = self.exportLesion(result.nativeSegmentationNode, result.nativeSegmentId)
        if lesion is None:
            return False
        tmpDir = os.path.join(slicer.app.temporaryPath, "LNMNormalize", uuid.uuid4().hex)
        stamp = time.strftime("%Y%m%d%H%M%S")
        lesionPath = os.path.join(tmpDir, f"lesion-edit{stamp}.nii")
        out = os.path.join(result.outDir, "warps", f"{result.record.get('subject') or 'sub-lesion'}_edit-{stamp}_space-{_normalizeLib().template_label(result.templateId)}{_normalizeLib()._res_entity(result.templateId)}_desc-lesion_mask.nii.gz")
        args = ["warp", "--run", result.outDir, "--image", lesionPath, "--kind", "lesion", "--out", out, "--events", "json", "--parent-pid", str(os.getpid())]
        for folder in templateDirectories():
            args += ["--template-dir", folder]
        module = getattr(slicer.modules, "brainsresample", None)
        if module is not None and module.path:
            args += ["--brainsresample", module.path]
        started = time.perf_counter()
        self.timings.pop("update_s", None)
        generation = self._sceneGeneration

        def finished(job):
            shutil.rmtree(tmpDir, ignore_errors=True)
            if generation != self._sceneGeneration:
                return  # the scene was closed meanwhile
            if job.state != _jobrunner.DONE:
                self._notify("failed", {"message": job.failureReason or _("the lesion could not be updated"), "code": (job.errorEvent or {}).get("code"), "event": job.errorEvent or {}, "job": job})
                if onDone is not None:
                    onDone(False)
                return

            def applied(mask, error):
                ok = error is None and generation == self._sceneGeneration and self._replaceMniVoxels(result, mask, out)
                if ok:
                    self._exportedLesion = lesion
                    self._setEdited(False, False)
                    self.timings["update_s"] = round(time.perf_counter() - started, 3)
                if onDone is not None:
                    onDone(ok)

            _Worker(lambda: _readMask(out), applied).start()

        def submit(written, error):
            if error is not None:
                self._notify("failed", {"message": _("The lesion could not be exported: {error}").format(error=error), "code": "export_failed", "event": {}, "job": None})
                return
            spec = _jobrunner.JobSpec(JOB_MODULE, args, title=_("Updating the MNI lesion"), heavy=False, kind="normalization-warp", runRoot=self._privateRunRoot(),
                                      onDone=finished, name="normalize-warp")
            self.warpJob = self.runner.submit(spec)

        _Worker(lambda: _writeLesionFile(lesionPath, lesion), submit).start()
        return True

    @staticmethod
    def _replaceMniVoxels(result, mask, warpPath=None):
        """Replace the voxels of the MNI lesion segment in one step.

        The segmentation then no longer holds the run's lesion: its fingerprint is changed so that a later reuse
        of the run (same fingerprint) does not return it as it is but puts the run's lesion back (``_previousNodes()``).
        """
        segmentation = result.segmentationNode
        if segmentation is None or not slicer.mrmlScene.IsNodePresent(segmentation) or result.segmentId is None:
            return False
        if mask.get("kji") is None:
            labelmap = slicer.vtkOrientedImageData()
            labelmap.SetExtent(0, -1, 0, -1, 0, -1)
            labelmap.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
        else:
            labelmap = _orientedLabelmap(mask)
        if not slicer.vtkSlicerSegmentationsModuleLogic.SetBinaryLabelmapToSegment(labelmap, segmentation, result.segmentId):
            return False
        stamp = uuid.uuid4().hex[:8]
        segmentation.SetAttribute(FINGERPRINT_ATTRIBUTE, f"{result.fingerprint or ''}+edit-{stamp}")
        if warpPath:
            segmentation.SetAttribute(LESION_WARP_ATTRIBUTE, portableReference(warpPath, result.outDir))
        return True

    #
    # QC widget
    #

    def buildQcWidget(self, result=None, width=280, onRedo=None, maxHeight=240):
        """The QC panel of a result (objectName LNMNormalizeQC): mosaic, suggestion, metrics, verdict and the ⋯ menu.

        ``onRedo(changes)`` restarts the normalization with changed request fields (``quality``, ``dilateMm``).
        """
        from . import widgets

        result = result or self.result
        panel = qt.QWidget()
        panel.setObjectName("LNMNormalizeQC")
        panel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        layout = qt.QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        if result is None:
            return panel

        pixmap = result.pixmap(thumbnail=True)
        if not pixmap.isNull():
            thumbnail = qt.QPushButton()
            thumbnail.setObjectName("LNMNormalizeMosaic")
            thumbnail.setFlat(True)
            thumbnail.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
            thumbnail.setToolTip(_("QC mosaic: click to open it full size"))
            scale = min(1.0, float(width) / max(1, pixmap.width()), float(maxHeight) / max(1, pixmap.height()))
            scaled = pixmap.scaled(max(40, int(pixmap.width() * scale)), max(40, int(pixmap.height() * scale)), qt.Qt.KeepAspectRatio, qt.Qt.SmoothTransformation) if scale < 1.0 else pixmap
            thumbnail.setIcon(qt.QIcon(scaled))
            thumbnail.setIconSize(scaled.size())
            thumbnail.connect("clicked()", lambda: self.showMosaic(result))
            layout.addWidget(thumbnail)

        chipRow = qt.QHBoxLayout()
        chipRow.setSpacing(6)
        text = result.suggestionText()
        suggestion = widgets.chip(text if len(text) <= 44 else text[:43] + "…", "ok" if result.suggestion == "looks-ok" else "warn")
        suggestion.setObjectName("LNMNormalizeSuggestion")
        suggestion.setToolTip("\n".join(result.reasons) or _("No metric is out of its usual range (a suggestion, not a verdict)"))
        chipRow.addWidget(suggestion)
        verdictChip = widgets.chip("", "neutral")
        verdictChip.setObjectName("LNMNormalizeVerdict")
        chipRow.addWidget(verdictChip)
        chipRow.addStretch(1)
        layout.addLayout(chipRow)

        metrics = qt.QLabel(result.metricsText())
        metrics.setObjectName("LNMNormalizeMetrics")
        metrics.setProperty("slicerRole", "secondaryText")
        metrics.setWordWrap(True)
        metrics.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        layout.addWidget(metrics)

        hint = qt.QLabel(self.reviewHint())
        hint.setObjectName("LNMNormalizeReviewHint")
        hint.setProperty("slicerRole", "secondaryText")
        hint.setWordWrap(True)
        hint.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        hint.setVisible(self.reviewing)
        layout.addWidget(hint)

        buttons = qt.QHBoxLayout()
        buttons.setSpacing(4)
        verdictButtons = qt.QHBoxLayout()
        verdictButtons.setSpacing(4)
        reviewButton = qt.QPushButton(_("Review in views"))
        reviewButton.setObjectName("LNMNormalizeReviewButton")
        reviewButton.setProperty("slicerRole", "secondary")
        buttons.addWidget(reviewButton)
        passButton = qt.QPushButton(_("Looks right"))
        passButton.setObjectName("LNMNormalizePassButton")
        passButton.setProperty("slicerRole", "ghost")
        verdictButtons.addWidget(passButton)
        failButton = qt.QPushButton(_("Doesn't look right"))
        failButton.setObjectName("LNMNormalizeFailButton")
        failButton.setProperty("slicerRole", "ghost")
        verdictButtons.addWidget(failButton)
        verdictButtons.addStretch(1)
        menuButton = qt.QToolButton()
        menuButton.setObjectName("LNMNormalizeMoreButton")
        menuButton.setText("⋯")
        menuButton.setToolTip(_("More: open the folder, load the transform, redo, copy the command line"))
        menuButton.setAutoRaise(True)
        menuButton.setPopupMode(qt.QToolButton.InstantPopup)
        menuButton.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        menu = qt.QMenu(menuButton)
        menu.setObjectName("LNMNormalizeMenu")
        menu.addAction(_("Open folder")).connect("triggered()", lambda: widgets.showInFileManager(result.outDir))
        menu.addAction(_("Load transform")).connect("triggered()", lambda: self.loadTransform(result))
        if onRedo is not None:
            menu.addSeparator()
            menu.addAction(_("Redo: affine only")).connect("triggered()", lambda: onRedo({"quality": "affine"}))
            menu.addAction(_("Redo: fine")).connect("triggered()", lambda: onRedo({"quality": "fine"}))
            menu.addAction(_("Redo with 6 mm margin")).connect("triggered()", lambda: onRedo({"dilateMm": 6.0}))
        menu.addSeparator()
        menu.addAction(_("Copy command line")).connect("triggered()", lambda: qt.QApplication.clipboard().setText(result.commandLine or self.commandLine()))
        menuButton.setMenu(menu)
        buttons.addStretch(1)
        buttons.addWidget(menuButton)
        layout.addLayout(buttons)
        layout.addLayout(verdictButtons)

        def refresh(*args):
            review = result.review()
            verdict = review.get("verdict")
            if verdict:
                widgets.setChipLevel(verdictChip, "ok" if verdict == "pass" else "danger", _("Reviewed: looks right") if verdict == "pass" else _("Reviewed: does not look right"))
                verdictChip.setToolTip(_("By {by} at {at}").format(by=review.get("by") or "?", at=review.get("at") or "?"))
                verdictChip.show()
            else:
                verdictChip.hide()
            reviewButton.setText(_("Done reviewing") if self.reviewing else _("Review in views"))
            hint.setVisible(self.reviewing)

        def toggleReview():
            if self.reviewing:
                self.restoreViews()
            else:
                self.reviewInViews(result)

        reviewButton.connect("clicked()", toggleReview)
        passButton.connect("clicked()", lambda: self.setVerdict("pass", result))
        failButton.connect("clicked()", lambda: self.setVerdict("fail", result))
        self.reviewChanged.append(refresh)
        # Forget the callback with the panel
        panel.connect("destroyed()", lambda: refresh in self.reviewChanged and self.reviewChanged.remove(refresh))
        refresh()
        return panel

    def showMosaic(self, result=None):
        """The full QC mosaic in a non-modal window."""
        result = result or self.result
        if result is None:
            return None
        window = qt.QLabel()
        window.setObjectName("LNMNormalizeMosaicWindow")
        window.setWindowTitle(_("Normalization QC · {subject}").format(subject=result.record.get("subject") or ""))
        window.setPixmap(result.pixmap())
        window.setAttribute(qt.Qt.WA_DeleteOnClose)
        parent = slicer.util.mainWindow()
        if parent is not None:
            window.setParent(parent, qt.Qt.Window)
        window.show()
        self._mosaicWindow = window
        return window

    #
    # Cleanup
    #

    def dispose(self):
        """Forget observers and timers; a running job keeps running (it belongs to the JobRunner)."""
        self.restoreViews()
        self._untrackEdits()
        self.removeObservers(self._onSceneStartClose)
        self._progressTimer.stop()
        if self._worker is not None:
            self._worker.cancelled = True
            self._worker = None
        for name in ("stateChanged", "progressChanged", "resultReady", "failed", "editStateChanged", "reviewChanged"):
            getattr(self, name).clear()


def _compareLesions(original, current, dilateMm):
    """Worker: ``(changed, outsideDilatedOriginal)`` of an edited lesion against the one that was normalized.

    Both are internal labelmaps of the same segmentation: their voxels share a lattice and differ by extent. A
    lattice change (or an unknown original) counts as changed but not as outside (Update is still offered).
    """
    import numpy as np

    if current is None:
        return original is not None, False
    if original is None:
        return True, True
    currentMask = _lesionMask(current)
    originalMask = _lesionMask(original)
    if original.get("imageToWorld") is None or current.get("imageToWorld") is None or not np.allclose(original["imageToWorld"], current["imageToWorld"], atol=1e-4):
        return True, False
    # Common box in lattice indices (k, j, i)
    originStart = np.array(original["extent"][4::-2])
    currentStart = np.array(current["extent"][4::-2])
    low = np.minimum(originStart, currentStart)
    high = np.maximum(originStart + np.array(originalMask.shape), currentStart + np.array(currentMask.shape))
    shape = tuple(int(n) for n in high - low)
    first = np.zeros(shape, dtype=bool)
    second = np.zeros(shape, dtype=bool)
    a = originStart - low
    b = currentStart - low
    first[a[0]:a[0] + originalMask.shape[0], a[1]:a[1] + originalMask.shape[1], a[2]:a[2] + originalMask.shape[2]] = originalMask
    second[b[0]:b[0] + currentMask.shape[0], b[1]:b[1] + currentMask.shape[1], b[2]:b[2] + currentMask.shape[2]] = currentMask
    if np.array_equal(first, second):
        return False, False
    added = second & ~first
    if not added.any():
        return True, False
    if not first.any():
        return True, True
    from scipy import ndimage

    spacing = np.linalg.norm(np.asarray(original["imageToWorld"])[:3, :3], axis=0)[::-1]  # (k, j, i)
    distance = ndimage.distance_transform_edt(~first, sampling=spacing)
    return True, bool((distance[added] > float(dilateMm) + 1e-6).any())

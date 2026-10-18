"""Direct loading of files in Slicer Studio, and "Start on MNI template" (SPEC 3.1, "Import rules").

Unambiguous files load directly, without the Add Data dialog:

- a name that looks like a lesion (``isLesionName()``: lesion, mask, seg, label or roi as a whole word)
  is loaded as a segmentation, whose first segment becomes "Lesion" (#F2545B);
- other NIfTI, NRRD and MetaImage files are loaded as scalar volumes and shown in all slice views;
- ``.mrb`` and ``.mrml`` files are loaded as scenes.

Anything else (other file types, several folders, Shift+drop) opens the classic Add Data dialog with the
dropped paths. A DICOM folder goes to the DICOM module; a folder with several NIfTI files is offered as
a card in the Library. In Studio, loading a scan switches to Draw.

This module is an optional plug-in of the Studio shell: ``install(shell)`` handles the files dropped on
the main window while Studio is shown, ``uninstall(shell)`` restores the classic drop handling.
The loading functions also work in Classic (``shell=None``): they only load the files there.
"""

import logging
import os
import re
import traceback

import qt
import slicer
import vtk
from slicer.i18n import tr as _

#: Whole-word lesion names (SPEC 3.1): "sub-01_lesion.nii.gz", "mask.nrrd", "brain.seg.nrrd"...
LESION_NAME_PATTERN = re.compile(r"(?i)(^|[_\-.])(lesion|mask|seg|label|roi)([_\-.]|$)")

#: Extensions of scalar volumes that load without options (lowercase)
VOLUME_EXTENSIONS = (".nii", ".nii.gz", ".nrrd", ".nhdr", ".mha", ".mhd")
NIFTI_EXTENSIONS = (".nii", ".nii.gz")
SCENE_EXTENSIONS = (".mrb", ".mrml")
#: Segmentation files that store segment names and colors: they are kept as they are
SEGMENTATION_FILE_EXTENSIONS = (".seg.nrrd", ".seg.nhdr", ".seg.vtm")

LESION_SEGMENT_NAME = "Lesion"
LESION_SEGMENTATION_NAME = "Lesions"
LESION_COLOR = (0.949, 0.329, 0.357)  # #F2545B (SPEC 4.12)
LESION_FILL_OPACITY = 0.4
LESION_OUTLINE_OPACITY = 1.0
LESION_SLICE_INTERSECTION_THICKNESS = 2

#: Sample data of "Start on MNI template" (SampleData module, "MNI152 (neuro)" category)
TEMPLATE_SAMPLE_2MM = "MNI152NLin2009cAsym 2mm brain"
TEMPLATE_SAMPLE_1MM = "MNI152NLin2009cAsym 1mm T1w"

#: Folders: at most this many files are examined, at most this deep
FOLDER_SCAN_MAX_FILES = 5000
FOLDER_SCAN_MAX_DEPTH = 4
#: Files whose header is read to find DICOM files
FOLDER_SCAN_MAX_DICOM_PROBES = 200
#: "Load all" of a folder card loads at most this many files: a cohort is not loaded into the scene (SPEC 10)
MAX_LOAD_ALL_FILES = 20


def _currentShell(shell=None):
    """The given shell, else the live Studio shell (None in Classic)."""
    if shell is not None:
        return shell if shell.isActive() else None
    from . import shell as shellModule

    return shellModule.instance()


def _extension(path):
    """Lowercase extension of a file, including the compound extensions .nii.gz and .seg.nrrd."""
    name = os.path.basename(path).lower()
    for compound in (".nii.gz", *SEGMENTATION_FILE_EXTENSIONS):
        if name.endswith(compound):
            return compound
    return os.path.splitext(name)[1]


def _baseName(path):
    """File name without its extension ("sub-01_lesion" for "sub-01_lesion.nii.gz")."""
    name = os.path.basename(path)
    extension = _extension(path)
    return name[:-len(extension)] if extension and name.lower().endswith(extension) else name


def isLesionName(path):
    """True if the file name contains lesion, mask, seg, label or roi as a whole word (SPEC 3.1)."""
    return LESION_NAME_PATTERN.search(os.path.basename(path)) is not None


def fileKind(path):
    """How a dropped or opened path is loaded: "folder", "scene", "lesion", "volume" or "other"."""
    if os.path.isdir(path):
        return "folder"
    extension = _extension(path)
    if extension in SCENE_EXTENSIONS:
        return "scene"
    if extension in SEGMENTATION_FILE_EXTENSIONS:
        return "lesion"
    if extension in VOLUME_EXTENSIONS:
        return "lesion" if isLesionName(path) else "volume"
    return "other"


def isNiftiFile(path):
    return _extension(path) in NIFTI_EXTENSIONS


def isDicomFile(path):
    """True if the file has the DICOM magic "DICM" at offset 128."""
    try:
        with open(path, "rb") as f:
            f.seek(128)
            return f.read(4) == b"DICM"
    except OSError:
        return False


class LoadResult:
    """Nodes and files of a load. ``addDataPaths`` were handed to the Add Data dialog instead."""

    def __init__(self):
        self.volumes = []
        self.segmentations = []
        self.scenes = []
        self.addDataPaths = []
        self.errors = []

    def nodes(self):
        return self.volumes + self.segmentations

    def __repr__(self):
        return (f"LoadResult(volumes={[node.GetName() for node in self.volumes]}, "
                f"segmentations={[node.GetName() for node in self.segmentations]}, scenes={self.scenes}, "
                f"addDataPaths={self.addDataPaths}, errors={self.errors})")


#
# Lesion segmentations
#


def styleLesionSegment(segmentationNode, segmentId):
    """Show a segment as a lesion: #F2545B, fill opacity 0.4, outline opacity 1.0 (SPEC 4.12)."""
    segment = segmentationNode.GetSegmentation().GetSegment(segmentId)
    if segment is None:
        return
    segment.SetColor(*LESION_COLOR)
    segmentationNode.CreateDefaultDisplayNodes()
    displayNode = segmentationNode.GetDisplayNode()
    if displayNode is not None:
        displayNode.SetSegmentOpacity2DFill(segmentId, LESION_FILL_OPACITY)
        displayNode.SetSegmentOpacity2DOutline(segmentId, LESION_OUTLINE_OPACITY)
        displayNode.SetSliceIntersectionThickness(LESION_SLICE_INTERSECTION_THICKNESS)


def _applyLesionNaming(segmentationNode, path):
    """Name the first segment "Lesion" with the lesion color.

    Files that store segment names and colors (``.seg.nrrd``...) keep them unless they hold a single segment.
    """
    segmentation = segmentationNode.GetSegmentation()
    if segmentation.GetNumberOfSegments() == 0:
        return
    if _extension(path) in SEGMENTATION_FILE_EXTENSIONS and segmentation.GetNumberOfSegments() > 1:
        return
    segmentId = segmentation.GetNthSegmentID(0)
    segmentation.GetSegment(segmentId).SetName(LESION_SEGMENT_NAME)
    styleLesionSegment(segmentationNode, segmentId)


def loadLesion(path):
    """Load a lesion file as a segmentation whose first segment is "Lesion".

    Image files that the segmentation reader cannot read are loaded as a labelmap volume and converted.
    """
    try:
        segmentationNode = slicer.util.loadSegmentation(path, {})
    except RuntimeError:
        if _extension(path) not in VOLUME_EXTENSIONS:
            raise
        logging.info(f"Studio: {path} is not readable as a segmentation, loading it as a labelmap volume")
        labelmapNode = slicer.util.loadLabelVolume(path, {"singleFile": True, "show": False})
        segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", labelmapNode.GetName())
        if not slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(labelmapNode, segmentationNode):
            slicer.mrmlScene.RemoveNode(segmentationNode)
            slicer.mrmlScene.RemoveNode(labelmapNode)
            raise RuntimeError(_("Failed to convert {path} to a segmentation").format(path=path))
        slicer.mrmlScene.RemoveNode(labelmapNode)
    baseName = _baseName(path)
    if baseName and segmentationNode.GetName() != baseName and _extension(path) in VOLUME_EXTENSIONS:
        # The segmentation reader keeps ".nii" in the node name of "x.nii.gz"
        segmentationNode.SetName(slicer.mrmlScene.GenerateUniqueName(baseName))
    _applyLesionNaming(segmentationNode, path)
    return segmentationNode


def _volumeGeometry(volumeNode):
    ijkToRas = vtk.vtkMatrix4x4()
    volumeNode.GetIJKToRASMatrix(ijkToRas)
    return slicer.vtkSegmentationConverter.SerializeImageGeometry(ijkToRas, volumeNode.GetImageData())


def _referenceGeometryMatches(segmentationNode, volumeNode):
    parameterName = slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName()
    return segmentationNode.GetSegmentation().GetConversionParameter(parameterName) == _volumeGeometry(volumeNode)


def isSegmentationEmpty(segmentationNode):
    """True if no segment of the segmentation has any voxel (or the segmentation has no segment)."""
    import vtk.util.numpy_support

    segmentation = segmentationNode.GetSegmentation()
    for index in range(segmentation.GetNumberOfSegments()):
        segmentId = segmentation.GetNthSegmentID(index)
        try:
            labelmap = segmentationNode.GetBinaryLabelmapInternalRepresentation(segmentId)
        except Exception:
            return False
        if labelmap is None:
            # No binary labelmap: the segment has another source representation (for example closed surface)
            return False
        if labelmap.IsEmpty() or labelmap.GetPointData().GetScalars() is None:
            continue
        labelValue = segmentation.GetSegment(segmentId).GetLabelValue()
        if (vtk.util.numpy_support.vtk_to_numpy(labelmap.GetPointData().GetScalars()) == labelValue).any():
            return False
    return True


def createLesionSegmentation(volumeNode, name=LESION_SEGMENTATION_NAME):
    """Create a segmentation on the geometry of ``volumeNode`` with an empty "Lesion" segment."""
    segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", name)
    segmentationNode.CreateDefaultDisplayNodes()
    segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
    segmentId = segmentationNode.GetSegmentation().AddEmptySegment("", LESION_SEGMENT_NAME, LESION_COLOR)
    styleLesionSegment(segmentationNode, segmentId)
    return segmentationNode


def _lesionSegmentationFor(volumeNode):
    """An existing "Lesions" segmentation on the geometry of ``volumeNode``, else a new one."""
    for segmentationNode in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
        if segmentationNode.GetName() == LESION_SEGMENTATION_NAME and _referenceGeometryMatches(segmentationNode, volumeNode):
            if segmentationNode.GetSegmentation().GetNumberOfSegments() == 0:
                segmentId = segmentationNode.GetSegmentation().AddEmptySegment("", LESION_SEGMENT_NAME, LESION_COLOR)
                styleLesionSegment(segmentationNode, segmentId)
            return segmentationNode
    return createLesionSegmentation(volumeNode)


def segmentEditorWidget():
    """The Segment Editor module widget (created if needed), or None if the module is not loaded."""
    if not hasattr(slicer.modules, "segmenteditor"):
        return None
    slicer.modules.segmenteditor.widgetRepresentation()
    return getattr(slicer.modules, "SegmentEditorWidget", None)


def selectInSegmentEditor(segmentationNode, sourceVolumeNode):
    """Edit ``segmentationNode`` with ``sourceVolumeNode`` in the Segment Editor, its first segment selected."""
    widget = segmentEditorWidget()
    if widget is None or widget.editor is None:
        return
    editor = widget.editor
    editor.setSegmentationNode(segmentationNode)
    if sourceVolumeNode is not None:
        editor.setSourceVolumeNode(sourceVolumeNode)
    segmentation = segmentationNode.GetSegmentation()
    if segmentation.GetNumberOfSegments() > 0:
        editor.setCurrentSegmentID(segmentation.GetNthSegmentID(0))


def _backgroundVolume():
    """The background volume of the first slice view that has one, or None."""
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None:
        return None
    for viewName in layoutManager.sliceViewNames():
        compositeNode = layoutManager.sliceWidget(viewName).mrmlSliceCompositeNode()
        volumeNode = slicer.mrmlScene.GetNodeByID(compositeNode.GetBackgroundVolumeID()) if compositeNode.GetBackgroundVolumeID() else None
        if volumeNode is not None:
            return volumeNode
    return None


def _isScan(node):
    return node.IsA("vtkMRMLScalarVolumeNode") and not node.IsA("vtkMRMLLabelMapVolumeNode")


#
# Loading
#


def openAddDataDialog(paths):
    """Open the classic Add Data dialog with ``paths`` (files or folders) already listed."""
    paths = [str(path) for path in paths]
    ioManager = slicer.app.ioManager()
    if not paths:
        return ioManager.openAddDataDialog()
    if len(paths) == 1:
        return ioManager.openAddDataDialog(paths[0])
    return ioManager.openDialog("NoFile", slicer.qSlicerFileDialog.Read, {"fileNames": paths})


def loadFiles(paths, shell=None):
    """Load files directly (see the module documentation) and return a ``LoadResult``.

    If a path is a folder or a file of another type, nothing is loaded and the Add Data dialog opens with
    all the paths. In Studio, the first scan becomes the background of the slice views, the Segment Editor
    is prepared on it (with the lesion file of the same load, if any) and Draw is shown.
    """
    shell = _currentShell(shell)
    result = LoadResult()
    paths = [os.path.abspath(str(path)) for path in paths]
    if not paths:
        return result
    kinds = [fileKind(path) for path in paths]
    if any(kind not in ("scene", "volume", "lesion") for kind in kinds):
        result.addDataPaths = paths
        openAddDataDialog(paths)
        return result

    with slicer.util.WaitCursor():
        # Scenes first (they may replace nodes), then scans, then lesions (shown over the scans)
        for wantedKind in ("scene", "volume", "lesion"):
            for path, kind in zip(paths, kinds, strict=True):
                if kind != wantedKind:
                    continue
                try:
                    if kind == "scene":
                        slicer.util.loadScene(path, {})
                        result.scenes.append(path)
                    elif kind == "volume":
                        result.volumes.append(slicer.util.loadVolume(path, {"singleFile": True}))
                    else:
                        result.segmentations.append(loadLesion(path))
                except Exception as e:
                    logging.error(f"Studio: cannot load {path}:\n{traceback.format_exc()}")
                    result.errors.append((path, str(e)))
    _afterLoad(result, shell)
    if result.errors:
        slicer.util.errorDisplay(
            _("Some files could not be loaded:") + "\n\n" + "\n".join(os.path.basename(path) for path, error in result.errors),
            detailedText="\n\n".join(f"{path}\n{error}" for path, error in result.errors))
    return result


def _afterLoad(result, shell):
    """Show what was loaded: scans in the slice views; in Studio, the lesion in Draw and a toast."""
    scans = [node for node in result.volumes if _isScan(node)]
    if scans:
        slicer.util.setSliceViewerLayers(background=scans[0], fit=True)
    if shell is None:
        return
    sourceVolume = scans[0] if scans else _backgroundVolume()
    lesions = result.segmentations
    if sourceVolume is not None and (scans or lesions):
        # Draw edits the loaded lesion, else a "Lesions" segmentation on the geometry of the scan (never the
        # segmentation of a previous scan or template, which may have another geometry)
        if segmentEditorWidget() is not None:
            selectInSegmentEditor(lesions[0] if lesions else _lesionSegmentationFor(sourceVolume), sourceVolume)
        shell.switchTo("draw")
    loaded = result.nodes()
    if loaded:
        name = loaded[0].GetName()
        if len(loaded) == 1:
            shell.toast(_("Loaded {name}").format(name=name))
        else:
            shell.toast(_("Loaded {name} and {count} more").format(name=name, count=len(loaded) - 1))
    elif result.scenes:
        shell.toast(_("Loaded {name}").format(name=os.path.basename(result.scenes[0])))


def loadRecentFile(properties, action=None, shell=None):
    """Load an entry of the recently loaded files (``fileParameters`` of a "RecentlyLoadedMenu" action).

    The file is loaded with its recorded properties (by triggering ``action`` if given). In Studio, a
    loaded scan is shown in Draw, as for ``loadFiles()``. A scene keeps its own views.
    """
    shell = _currentShell(shell)
    fileName = str((properties or {}).get("fileName", ""))
    isScene = (properties or {}).get("fileType") == "SceneFile" or _extension(fileName) in SCENE_EXTENSIONS
    scene = slicer.mrmlScene
    nodeIdsBefore = {scene.GetNthNode(index).GetID() for index in range(scene.GetNumberOfNodes())}
    if action is not None:
        action.trigger()
    else:
        properties = dict(properties)
        fileType = properties.get("fileType") or slicer.app.coreIOManager().fileType(properties.get("fileName", ""))
        with slicer.util.WaitCursor():
            if not slicer.app.coreIOManager().loadNodes(fileType, properties):
                slicer.util.errorDisplay(_("Failed to load {path}").format(path=properties.get("fileName", "")))
    result = LoadResult()
    if isScene:
        result.scenes.append(fileName)
        _afterLoad(result, shell)
        return result
    for index in range(scene.GetNumberOfNodes()):
        node = scene.GetNthNode(index)
        if node.GetID() in nodeIdsBefore or node.GetHideFromEditors():
            continue
        if node.IsA("vtkMRMLScalarVolumeNode"):
            result.volumes.append(node)
        elif node.IsA("vtkMRMLSegmentationNode"):
            if fileName and isLesionName(fileName):
                _applyLesionNaming(node, fileName)
            result.segmentations.append(node)
    _afterLoad(result, shell)
    return result


def loadFolder(path, shell=None):
    """Load a folder: DICOM to the DICOM module, several NIfTI files as a card, else the Add Data dialog.

    Return "dicom", "nifti" or "addData".
    """
    shell = _currentShell(shell)
    niftiFiles, isDicom = scanFolder(path)
    if isDicom:
        importDicomFolder(path)
        return "dicom"
    if len(niftiFiles) >= 2:
        from . import library

        if library.showFolderCard(path, niftiFiles, shell):
            return "nifti"
    openAddDataDialog([path])
    return "addData"


def scanFolder(path):
    """Return ``(niftiFiles, isDicom)`` for a folder: its NIfTI files (sorted), and whether it has DICOM files.

    Subfolders are examined (``FOLDER_SCAN_MAX_DEPTH`` levels, ``FOLDER_SCAN_MAX_FILES`` files at most);
    only the header of up to ``FOLDER_SCAN_MAX_DICOM_PROBES`` files is read.
    """
    path = os.path.abspath(path)
    niftiFiles = []
    isDicom = False
    examined = 0
    probes = 0
    rootDepth = path.rstrip(os.sep).count(os.sep)
    for directory, subdirectories, fileNames in os.walk(path):
        subdirectories[:] = sorted(name for name in subdirectories if not name.startswith("."))
        if directory.rstrip(os.sep).count(os.sep) - rootDepth >= FOLDER_SCAN_MAX_DEPTH:
            subdirectories[:] = []
        for fileName in sorted(fileNames):
            if fileName.startswith("."):
                continue
            examined += 1
            filePath = os.path.join(directory, fileName)
            if isNiftiFile(filePath):
                niftiFiles.append(filePath)
            elif not isDicom and probes < FOLDER_SCAN_MAX_DICOM_PROBES:
                probes += 1
                isDicom = isDicomFile(filePath)
            if examined >= FOLDER_SCAN_MAX_FILES:
                return niftiFiles, isDicom
    return niftiFiles, isDicom


def importDicomFolder(path):
    """Import a DICOM folder into the DICOM database and show the DICOM browser.

    Without the DICOM module, the Add Data dialog opens with the folder.
    """
    if not hasattr(slicer.modules, "dicom"):
        openAddDataDialog([path])
        return False
    slicer.util.selectModule("DICOM")
    from DICOMLib import DICOMUtils

    database = getattr(slicer, "dicomDatabase", None)
    if database is None or not database.isOpen:
        slicer.util.errorDisplay(_("The DICOM database is not set up. Choose a database folder in the DICOM module, then import the folder again."))
        return False
    with slicer.util.WaitCursor():
        return DICOMUtils.importDicom(path)


def handleDrop(paths, addData=False, shell=None):
    """Load dropped paths: Shift+drop (``addData``) or several folders open the Add Data dialog."""
    paths = [str(path) for path in paths]
    if not paths:
        return None
    if addData or (len(paths) > 1 and any(os.path.isdir(path) for path in paths)):
        openAddDataDialog(paths)
        return "addData"
    if len(paths) == 1 and os.path.isdir(paths[0]):
        return loadFolder(paths[0], shell)
    return loadFiles(paths, shell)


#
# Dialogs
#

FILE_DIALOG_FILTERS = ";;".join((
    _("Scans and lesions") + " (*.nii *.nii.gz *.nrrd *.nhdr *.mha *.mhd *.seg.vtm)",
    _("Scenes") + " (*.mrb *.mrml)",
    _("All files") + " (*)",
))


def _lastDirectory():
    directory = str(slicer.util.settingsValue("Studio/LastOpenDirectory", "") or "")
    return directory if directory and os.path.isdir(directory) else qt.QDir.homePath()


def _rememberDirectory(path):
    directory = path if os.path.isdir(path) else os.path.dirname(path)
    if directory != slicer.util.settingsValue("Studio/LastOpenDirectory", ""):
        qt.QSettings().setValue("Studio/LastOpenDirectory", directory)


def openFilesDialog(shell=None):
    """Open… (Ctrl+O in Studio): choose files and load them with ``loadFiles()``."""
    paths = qt.QFileDialog.getOpenFileNames(slicer.util.mainWindow(), _("Open"), _lastDirectory(), FILE_DIALOG_FILTERS)
    if not paths:
        return None
    _rememberDirectory(paths[0])
    return loadFiles(paths, shell)


def openFolderDialog(shell=None):
    """Open folder…: choose a folder and load it with ``loadFolder()``."""
    path = qt.QFileDialog.getExistingDirectory(slicer.util.mainWindow(), _("Open folder"), _lastDirectory())
    if not path:
        return None
    _rememberDirectory(path)
    return loadFolder(path, shell)


#
# Start on MNI template
#

_templateBanner = None
#: Nodes created by the last "Start on MNI template": {"volume": node ID, "segmentation": node ID}
_templateNodeIds = {}
#: SampleData sources of the templates, by sample name
_templateSources = {}


def templateSource(sampleName):
    """The SampleData source of a template, or None if the SampleData module is not available."""
    source = _templateSources.get(sampleName)
    if source is None:
        try:
            import SampleData
        except ImportError:
            return None
        source = SampleData.SampleDataLogic().sourceForSampleName(sampleName)
        if source is not None:
            _templateSources[sampleName] = source
    return source


def cachedTemplatePath(sampleName=TEMPLATE_SAMPLE_1MM):
    """Path of a downloaded template (Studio download cache or SampleData cache), or None."""
    from . import downloads

    source = templateSource(sampleName)
    if source is None:
        return None
    fileName = source.fileNames[0]
    for directory in (downloads.cacheDirectory(), slicer.mrmlScene.GetCacheManager().GetRemoteCacheDirectory()):
        path = os.path.join(directory, fileName)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


def _templateVolume(sampleName, path=None):
    """The template volume already in the scene, else load it (from ``path``, else from SampleData)."""
    source = templateSource(sampleName)
    if source is None:
        raise RuntimeError(_("The SampleData module is not available"))
    nodeName = source.nodeNames[0]
    for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        if node.GetName() == nodeName and not node.IsA("vtkMRMLLabelMapVolumeNode"):
            return node
    if path is not None:
        return slicer.util.loadVolume(path, {"name": nodeName, "singleFile": True})
    import SampleData

    volumeNode = SampleData.SampleDataLogic().downloadSample(sampleName)
    if volumeNode is None:
        raise RuntimeError(_("Failed to load {sampleName}").format(sampleName=sampleName))
    return volumeNode


def startOnTemplate(res=2, shell=None):
    """Load the MNI152NLin2009cAsym template and open Draw on a "Lesions" segmentation of its geometry.

    The 2 mm brain is bundled, so this works offline. The 1 mm T1w is used instead when it was downloaded
    before. Otherwise a banner offers to download it in the background (``res=1`` starts that download
    right away). The template volume and its "Lesions" segmentation are reused if they are in the scene.
    Return ``(volumeNode, segmentationNode)``.
    """
    shell = _currentShell(shell)
    cached1mm = cachedTemplatePath(TEMPLATE_SAMPLE_1MM)
    with slicer.util.WaitCursor():
        if cached1mm is not None:
            volumeNode = _templateVolume(TEMPLATE_SAMPLE_1MM, cached1mm)
        else:
            volumeNode = _templateVolume(TEMPLATE_SAMPLE_2MM)
        slicer.util.setSliceViewerLayers(background=volumeNode, fit=True)
        segmentationNode = _lesionSegmentationFor(volumeNode)
        _templateNodeIds["volume"] = volumeNode.GetID()
        _templateNodeIds["segmentation"] = segmentationNode.GetID()
        selectInSegmentEditor(segmentationNode, volumeNode)
        if shell is not None:
            shell.switchTo("draw")
        elif hasattr(slicer.modules, "segmenteditor") and slicer.util.mainWindow() is not None:
            slicer.util.selectModule("SegmentEditor")
    if cached1mm is None:
        banner = showTemplateDownloadBanner()
        if banner is not None and res == 1 and banner.state == "offer":
            banner.startDownload()
    return volumeNode, segmentationNode


def showTemplateDownloadBanner(url=None):
    """Show the card that offers the 1 mm template download (``url`` replaces the TemplateFlow URL, for tests)."""
    global _templateBanner
    from . import downloads

    mainWindow = slicer.util.mainWindow()
    source = templateSource(TEMPLATE_SAMPLE_1MM)
    if mainWindow is None or source is None:
        return None
    if _templateBanner is not None and _templateBanner.isVisible():
        return _templateBanner
    checksum = source.checksums[0] if source.checksums else None
    _templateBanner = downloads.DownloadBanner(
        mainWindow.centralWidget(),
        offerText=_("Download the 1 mm template ({size}) for finer tracing").format(size="13.7 MB"),
        url=url or source.uris[0],
        sha256=checksum,
        dest=os.path.join(downloads.cacheDirectory(), source.fileNames[0]),
        readyText=_("The 1 mm template is ready."),
        actionText=_("Switch to 1 mm"),
        onAction=switchTemplateTo1mm)
    _templateBanner.show()
    return _templateBanner


def templateBanner():
    """The 1 mm template banner, if it is shown."""
    return _templateBanner if _templateBanner is not None and _templateBanner.isVisible() else None


def switchTemplateTo1mm(path, shell=None):
    """Use the downloaded 1 mm template instead of the 2 mm one, if no lesion was drawn yet.

    The background volume of the slice views and the source volume of the Segment Editor are replaced, and
    the "Lesions" segmentation is bound to the 1 mm geometry. A lesion already drawn keeps the 2 mm grid.
    Return True if the template was switched.
    """
    shell = _currentShell(shell)
    segmentationId = _templateNodeIds.get("segmentation")
    segmentationNode = slicer.mrmlScene.GetNodeByID(segmentationId) if segmentationId else None
    if segmentationNode is not None and segmentationNode.GetName() != LESION_SEGMENTATION_NAME:
        # The scene was closed since: the ID belongs to another segmentation
        segmentationNode = None
    if segmentationNode is not None and not isSegmentationEmpty(segmentationNode):
        message = _("The lesion is already drawn on the 2 mm template: it stays on the 2 mm grid.")
        if shell is not None:
            shell.toast(message, 4000)
        else:
            slicer.util.infoDisplay(message)
        return False
    with slicer.util.WaitCursor():
        volumeNode = _templateVolume(TEMPLATE_SAMPLE_1MM, path)
        slicer.util.setSliceViewerLayers(background=volumeNode, fit=True)
        _templateNodeIds["volume"] = volumeNode.GetID()
        if segmentationNode is not None:
            segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
            widget = getattr(slicer.modules, "SegmentEditorWidget", None)
            if widget is not None and widget.editor is not None and widget.editor.segmentationNode() == segmentationNode:
                widget.editor.setSourceVolumeNode(volumeNode)
    if shell is not None:
        shell.toast(_("Switched to the 1 mm template"))
    return True


#
# Drop handling (plug-in)
#


def localPaths(mimeData):
    """Local file paths of the URLs of a drag, or an empty list if any URL is not a local file."""
    if mimeData is None or not mimeData.hasUrls():
        return []
    paths = [url.toLocalFile() for url in mimeData.urls()]
    if not paths or not all(paths):
        return []
    return paths


class _DropHandler(qt.QObject):
    """Event filter of the main window: drops of local files are loaded directly (Shift: Add Data)."""

    def __init__(self, shell):
        qt.QObject.__init__(self)
        self.shell = shell

    def eventFilter(self, obj, event):
        try:
            return self._handleEvent(event)
        except Exception:
            logging.error(f"Studio drop handler failed:\n{traceback.format_exc()}")
        return False

    def _handleEvent(self, event):
        eventType = event.type()
        if eventType in (qt.QEvent.DragEnter, qt.QEvent.DragMove):
            if not localPaths(event.mimeData()):
                # Not files: classic handling
                return False
            event.setDropAction(qt.Qt.CopyAction)
            event.accept()
            if eventType == qt.QEvent.DragEnter:
                _setDropHighlight(True)
            return True
        if eventType == qt.QEvent.DragLeave:
            _setDropHighlight(False)
            return False
        if eventType == qt.QEvent.Drop:
            _setDropHighlight(False)
            paths = localPaths(event.mimeData())
            if not paths:
                return False
            event.setDropAction(qt.Qt.CopyAction)
            event.accept()
            addData = bool(event.keyboardModifiers() & qt.Qt.ShiftModifier)
            # Loaded after the drop returns, so that the application that started the drag is not blocked
            qt.QTimer.singleShot(0, lambda: self._load(paths, addData))
            return True
        return False

    def _load(self, paths, addData):
        try:
            handleDrop(paths, addData, self.shell)
        except Exception:
            logging.error(f"Studio: cannot load the dropped files:\n{traceback.format_exc()}")


def _setDropHighlight(active):
    import sys

    library = sys.modules.get("StudioLib.library")
    if library is not None:
        library.setDropHighlight(active)


_dropHandler = None
_uninstallOnQuit = False


def install(shell):
    """Handle the files dropped on the main window while Studio is shown."""
    global _dropHandler, _uninstallOnQuit
    uninstall(shell)
    _dropHandler = _DropHandler(shell)
    shell.mainWindow.installEventFilter(_dropHandler)
    if not _uninstallOnQuit:
        # Python event filters must not run while the interpreter is finalized (after the application quits)
        slicer.app.connect("aboutToQuit()", lambda: uninstall(None))
        _uninstallOnQuit = True


def uninstall(shell):
    """Restore the classic drop handling (Add Data dialog)."""
    global _dropHandler
    if _dropHandler is None:
        return
    mainWindow = _dropHandler.shell.mainWindow
    if _isAlive(mainWindow):
        mainWindow.removeEventFilter(_dropHandler)
    _dropHandler = None
    _setDropHighlight(False)


def _isAlive(qobject):
    """False if the C++ object was deleted."""
    try:
        qobject.objectName
    except Exception:
        return False
    return True

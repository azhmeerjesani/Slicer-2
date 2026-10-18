"""Direct loading of files in Slicer Studio, and "Start on MNI template" (SPEC 3.1, "Import rules").

Unambiguous files load directly, without the Add Data dialog:

- a name that looks like a lesion (``isLesionName()``: lesion, mask, seg, label or roi as a whole word)
  is loaded as a segmentation, whose first segment becomes "Lesion" (#F2545B). An empty mask gets an
  empty "Lesion" segment to draw into. In a multi-label mask, only the first label becomes "Lesion": the
  other labels keep the names given by the reader ("Segment_2"...). ``.seg.nrrd`` and ``.seg.vtm`` files
  with several segments keep their names and colors;
- other NIfTI, NRRD and MetaImage files are loaded as scalar volumes and shown in all slice views;
- ``.mrb`` and ``.mrml`` files are loaded as scenes.

In Studio, a scan or lesion file that is already loaded (same real path as the file of a node in the scene) is
not loaded a second time: it becomes current again, with the toast "<name> is already open". Classic keeps the
official behavior (the file is loaded again).

Anything else (other file types, several folders, Shift+drop) opens the classic Add Data dialog with the
dropped paths. A DICOM folder goes to the DICOM module; a folder with several NIfTI files is offered as
a card in the Library. In Studio, loading a scan switches to Draw, on the lesion loaded with it or on a new
"Lesions (<scan name>)" segmentation bound to that scan (never on the segmentation of another scan, even if
it has the same geometry, as scans normalized to MNI space do).

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
from slicer.util import VTKObservationMixin

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
    """Nodes and files of a load. ``addDataPaths`` were handed to the Add Data dialog instead.

    ``alreadyOpen`` are nodes of files that were already loaded (Studio only): they were not loaded again.
    """

    def __init__(self):
        self.volumes = []
        self.segmentations = []
        self.scenes = []
        self.addDataPaths = []
        self.errors = []
        self.alreadyOpen = []

    def nodes(self):
        return self.volumes + self.segmentations

    def __repr__(self):
        return (f"LoadResult(volumes={[node.GetName() for node in self.volumes]}, "
                f"segmentations={[node.GetName() for node in self.segmentations]}, scenes={self.scenes}, "
                f"addDataPaths={self.addDataPaths}, errors={self.errors}, "
                f"alreadyOpen={[node.GetName() for node in self.alreadyOpen]})")


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
    An empty mask (no segment) gets an empty "Lesion" segment, so that Draw has a segment to draw into.
    """
    segmentation = segmentationNode.GetSegmentation()
    if segmentation.GetNumberOfSegments() == 0:
        segmentId = segmentation.AddEmptySegment("", LESION_SEGMENT_NAME, LESION_COLOR)
        styleLesionSegment(segmentationNode, segmentId)
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
        # The geometry of the mask is kept even if it is empty (no node reference: the labelmap is removed below)
        segmentationNode.GetSegmentation().SetConversionParameter(
            slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName(), _volumeGeometry(labelmapNode))
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


def _isBoundTo(segmentationNode, volumeNode):
    """True if the reference geometry of the segmentation is ``volumeNode`` itself (node reference and geometry).

    The node reference is set by ``SetReferenceImageGeometryParameterFromVolumeNode()``. A geometry match alone
    is not enough: scans of different subjects normalized to MNI space share their geometry.
    """
    role = slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole()
    return segmentationNode.GetNodeReferenceID(role) == volumeNode.GetID() and _referenceGeometryMatches(segmentationNode, volumeNode)


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


def scanLesionSegmentationName(volumeNode):
    """Name of the segmentation created for the lesion of a loaded scan: "Lesions (<scan name>)"."""
    return f"{LESION_SEGMENTATION_NAME} ({volumeNode.GetName()})"


def _lesionSegmentationFor(volumeNode, name):
    """The segmentation ``name`` bound to ``volumeNode`` (see ``_isBoundTo()``) if it is in the scene, else a new one.

    The segmentation of another volume is never reused, even if it has the same geometry: it may hold the lesion
    of another subject.
    """
    for segmentationNode in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
        if segmentationNode.GetName() == name and _isBoundTo(segmentationNode, volumeNode):
            if segmentationNode.GetSegmentation().GetNumberOfSegments() == 0:
                segmentId = segmentationNode.GetSegmentation().AddEmptySegment("", LESION_SEGMENT_NAME, LESION_COLOR)
                styleLesionSegment(segmentationNode, segmentId)
            return segmentationNode
    return createLesionSegmentation(volumeNode, name)


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


def _normalizedPath(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def openNodeForFile(path):
    """The scan or segmentation of the scene that was loaded from ``path`` (same real path), or None.

    Only nodes shown to the user are considered: scans (not label maps) and segmentations whose storage node
    reads that file.
    """
    if not path:
        return None
    target = _normalizedPath(path)
    for className in ("vtkMRMLScalarVolumeNode", "vtkMRMLSegmentationNode"):
        for node in slicer.util.getNodesByClass(className):
            if node.GetHideFromEditors() or (className == "vtkMRMLScalarVolumeNode" and not _isScan(node)):
                continue
            storageNode = node.GetStorageNode()
            fileName = storageNode.GetFileName() if storageNode is not None else None
            if fileName and _normalizedPath(fileName) == target:
                return node
    return None


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
                openNode = openNodeForFile(path) if shell is not None and kind != "scene" else None
                if openNode is not None:
                    # In Studio, a file is not loaded twice: its node becomes current again
                    result.alreadyOpen.append(openNode)
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


def _editedNodes():
    """``(segmentationNode, sourceVolumeNode)`` of the Segment Editor, Nones if its widget was not created."""
    widget = getattr(slicer.modules, "SegmentEditorWidget", None)
    editor = widget.editor if widget is not None else None
    if editor is None:
        return None, None
    return editor.segmentationNode(), editor.sourceVolumeNode()


def _sameNode(node, other):
    return node is not None and other is not None and node.GetID() == other.GetID()


def _afterLoad(result, shell):
    """Show what was loaded: scans in the slice views; in Studio, the lesion in Draw and a toast.

    Nodes of files that were already open (``result.alreadyOpen``) become current again: the scan is shown,
    and Draw keeps editing its lesion if it already does.
    """
    scans = [node for node in result.volumes if _isScan(node)]
    openScans = [node for node in result.alreadyOpen if _isScan(node)]
    openLesions = [node for node in result.alreadyOpen if node.IsA("vtkMRMLSegmentationNode")]
    if scans:
        slicer.util.setSliceViewerLayers(background=scans[0], fit=True)
    elif openScans and not _sameNode(_backgroundVolume(), openScans[0]):
        slicer.util.setSliceViewerLayers(background=openScans[0], fit=True)
    if shell is None:
        return
    editedSegmentation, editedSource = _editedNodes()
    lesions = result.segmentations or openLesions
    if scans:
        sourceVolume = scans[0]
    elif openScans:
        sourceVolume = openScans[0]
    elif lesions and _sameNode(lesions[0], editedSegmentation) and editedSource is not None:
        sourceVolume = editedSource
    else:
        sourceVolume = _backgroundVolume()
    if sourceVolume is not None and (scans or lesions or openScans):
        # Draw edits the loaded lesion, else a segmentation bound to the scan (never the segmentation of a previous
        # scan or of the template, which may hold another subject's lesion)
        if segmentEditorWidget() is not None:
            if lesions:
                lesion = lesions[0]
            elif not scans and editedSegmentation is not None and _sameNode(editedSource, sourceVolume):
                # The scan was already open and Draw edits its lesion: keep it (and the selected segment)
                lesion = editedSegmentation
            else:
                lesion = _lesionSegmentationFor(sourceVolume, scanLesionSegmentationName(sourceVolume))
            if not (_sameNode(lesion, editedSegmentation) and _sameNode(sourceVolume, editedSource)):
                selectInSegmentEditor(lesion, sourceVolume)
        shell.switchTo("draw")
    loaded = result.nodes()
    if loaded:
        name = loaded[0].GetName()
        if len(loaded) == 1:
            shell.toast(_("Loaded {name}").format(name=name))
        else:
            shell.toast(_("Loaded {name} and {count} more").format(name=name, count=len(loaded) - 1))
    elif result.alreadyOpen:
        shell.toast(_("{name} is already open").format(name=result.alreadyOpen[0].GetName()))
    elif result.scenes:
        shell.toast(_("Loaded {name}").format(name=os.path.basename(result.scenes[0])))


def loadRecentFile(properties, action=None, shell=None):
    """Load an entry of the recently loaded files (``fileParameters`` of a "RecentlyLoadedMenu" action).

    The file is loaded with its recorded properties (by triggering ``action`` if given). In Studio, a
    loaded scan is shown in Draw, as for ``loadFiles()``, and a scan or lesion that is already open is not
    loaded again (it becomes current). A scene keeps its own views.
    """
    shell = _currentShell(shell)
    fileName = str((properties or {}).get("fileName", ""))
    isScene = (properties or {}).get("fileType") == "SceneFile" or _extension(fileName) in SCENE_EXTENSIONS
    openNode = openNodeForFile(fileName) if shell is not None and not isScene else None
    if openNode is not None:
        result = LoadResult()
        result.alreadyOpen.append(openNode)
        _afterLoad(result, shell)
        return result
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
#: Nodes of the last "Start on MNI template": {"volume": node ID, "segmentation": node ID}. Forgotten when the
#: scene is closed (node IDs start again from 1 then) and always checked by ``_templateNodes()``.
_templateNodeIds = {}
#: SampleData sources of the templates, by sample name
_templateSources = {}
_templateWatcher = None


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


def _templateNodeName(sampleName):
    source = templateSource(sampleName)
    return source.nodeNames[0] if source is not None else None


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


def _templateVolume(sampleName, path=None, show=True):
    """The template volume already in the scene, else load it (from ``path``, else from SampleData).

    With ``show=False``, a volume loaded from ``path`` does not replace the background of the slice views.
    """
    source = templateSource(sampleName)
    if source is None:
        raise RuntimeError(_("The SampleData module is not available"))
    nodeName = source.nodeNames[0]
    for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        if node.GetName() == nodeName and not node.IsA("vtkMRMLLabelMapVolumeNode"):
            return node
    if path is not None:
        return slicer.util.loadVolume(path, {"name": nodeName, "singleFile": True, "show": show})
    import SampleData

    volumeNode = SampleData.SampleDataLogic().downloadSample(sampleName)
    if volumeNode is None:
        raise RuntimeError(_("Failed to load {sampleName}").format(sampleName=sampleName))
    return volumeNode


def _trackTemplate(volumeNode, segmentationNode):
    _templateNodeIds["volume"] = volumeNode.GetID()
    _templateNodeIds["segmentation"] = segmentationNode.GetID() if segmentationNode is not None else None
    _watchTemplate()


def _templateNodes():
    """The template volume and the "Lesions" segmentation of the last "Start on MNI template", if they are still
    in the scene; ``(None, None)`` otherwise (the segmentation alone is None if it was removed or re-bound).

    The volume must still be a template volume and the segmentation must still be bound to it: a node ID alone
    does not identify a node once the scene was closed.
    """
    scene = slicer.mrmlScene
    volumeId = _templateNodeIds.get("volume")
    volumeNode = scene.GetNodeByID(volumeId) if volumeId else None
    templateNames = (_templateNodeName(TEMPLATE_SAMPLE_2MM), _templateNodeName(TEMPLATE_SAMPLE_1MM))
    if volumeNode is None or not _isScan(volumeNode) or volumeNode.GetName() not in templateNames:
        return None, None
    segmentationId = _templateNodeIds.get("segmentation")
    segmentationNode = scene.GetNodeByID(segmentationId) if segmentationId else None
    if segmentationNode is not None and (not segmentationNode.IsA("vtkMRMLSegmentationNode") or not _isBoundTo(segmentationNode, volumeNode)):
        segmentationNode = None
    return volumeNode, segmentationNode


def _isTemplateShown(volumeNode):
    """True if ``volumeNode`` is the background volume of the slice views."""
    background = _backgroundVolume()
    return volumeNode is not None and background is not None and background.GetID() == volumeNode.GetID()


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
        segmentationNode = _lesionSegmentationFor(volumeNode, LESION_SEGMENTATION_NAME)
        _trackTemplate(volumeNode, segmentationNode)
        selectInSegmentEditor(segmentationNode, volumeNode)
        if shell is not None:
            shell.switchTo("draw")
        elif hasattr(slicer.modules, "segmenteditor") and slicer.util.mainWindow() is not None:
            slicer.util.selectModule("SegmentEditor")
    if cached1mm is None:
        banner = showTemplateDownloadBanner()
        if banner is not None and res == 1 and banner.state == "offer":
            banner.startDownload()
    else:
        # A banner whose download completed while it was hidden: the 1 mm template is used now
        _closeTemplateBanner()
    return volumeNode, segmentationNode


def showTemplateDownloadBanner(url=None):
    """Show the card that offers the 1 mm template download (``url`` replaces the TemplateFlow URL, for tests).

    A banner hidden while the template was not shown is shown again, with its download state.
    """
    global _templateBanner
    from . import downloads

    mainWindow = slicer.util.mainWindow()
    source = templateSource(TEMPLATE_SAMPLE_1MM)
    if mainWindow is None or source is None:
        return None
    if _templateBanner is not None and _templateBanner.isAlive():
        if url is None or url == _templateBanner.download.url:
            _templateBanner.show()
            return _templateBanner
        _closeTemplateBanner()
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
    _watchTemplate()
    return _templateBanner


def templateBanner():
    """The 1 mm template banner, if it is shown."""
    return _templateBanner if _templateBanner is not None and _templateBanner.isVisible() else None


def _closeTemplateBanner():
    global _templateBanner
    if _templateBanner is not None:
        _templateBanner.close()
        _templateBanner = None


def _updateTemplateBanner():
    """Show the 1 mm banner only while the 2 mm template is the background of the slice views.

    Once the template is no longer in the scene (for example, the scene was closed), the banner closes; a running
    download goes on with the banner hidden, so that the next "Start on MNI template" uses the 1 mm file.
    """
    global _templateBanner
    banner = _templateBanner
    if banner is None:
        return
    if not banner.isAlive():
        _templateBanner = None
        return
    volumeNode = _templateNodes()[0]
    is1mm = volumeNode is not None and volumeNode.GetName() == _templateNodeName(TEMPLATE_SAMPLE_1MM)
    if (volumeNode is None or is1mm) and not banner.download.isRunning():
        _closeTemplateBanner()
        return
    banner.setVisible(_isTemplateShown(volumeNode) and not is1mm)


class _TemplateWatcher(VTKObservationMixin):
    """Forgets the template nodes when the scene is closed, and updates the 1 mm banner when the scene or the
    background volume of the slice views changes.
    """

    def __init__(self):
        VTKObservationMixin.__init__(self)
        self.timer = qt.QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(0)
        self.timer.connect("timeout()", _updateTemplateBanner)
        scene = slicer.mrmlScene
        self.addObserver(scene, scene.EndCloseEvent, self._onSceneClosed)
        self.addObserver(scene, scene.NodeAddedEvent, self._onNodeAdded)
        self.addObserver(scene, scene.NodeRemovedEvent, self._onChanged)
        for compositeNode in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
            self._observeCompositeNode(compositeNode)
        slicer.app.connect("aboutToQuit()", self.dispose)

    def _observeCompositeNode(self, compositeNode):
        if not self.hasObserver(compositeNode, vtk.vtkCommand.ModifiedEvent, self._onChanged):
            self.addObserver(compositeNode, vtk.vtkCommand.ModifiedEvent, self._onChanged)

    def _onSceneClosed(self, caller, event):
        _templateNodeIds.clear()
        self._onChanged(caller, event)

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def _onNodeAdded(self, caller, event, node):
        if node is not None and node.IsA("vtkMRMLSliceCompositeNode"):
            self._observeCompositeNode(node)

    def _onChanged(self, caller, event):
        if _templateBanner is not None:
            self.timer.start()

    def dispose(self):
        slicer.app.disconnect("aboutToQuit()", self.dispose)
        self.timer.stop()
        self.removeObservers()


def _watchTemplate():
    global _templateWatcher
    if _templateWatcher is None:
        _templateWatcher = _TemplateWatcher()


def switchTemplateTo1mm(path, shell=None):
    """Use the downloaded 1 mm template instead of the 2 mm one, if no lesion was drawn yet.

    Only the nodes of the last "Start on MNI template" are changed, if they are still in the scene: the "Lesions"
    segmentation is bound to the 1 mm geometry, and the 1 mm volume replaces the 2 mm one as background of the
    slice views and as source volume of the Segment Editor (where the 2 mm template was shown or edited).
    A lesion already drawn keeps the 2 mm grid. Return True if the template was switched.
    """
    shell = _currentShell(shell)
    volumeNode, segmentationNode = _templateNodes()
    if volumeNode is None or volumeNode.GetName() == _templateNodeName(TEMPLATE_SAMPLE_1MM):
        # The template is not in the scene anymore (the next "Start on MNI template" uses the 1 mm file),
        # or it is the 1 mm one already
        return False
    if segmentationNode is not None and not isSegmentationEmpty(segmentationNode):
        message = _("The lesion is already drawn on the 2 mm template: it stays on the 2 mm grid.")
        if shell is not None:
            shell.toast(message, 4000)
        else:
            slicer.util.infoDisplay(message)
        return False
    widget = getattr(slicer.modules, "SegmentEditorWidget", None)
    editor = widget.editor if widget is not None else None
    editedSegmentation = editor.segmentationNode() if editor is not None else None
    editsTemplate = segmentationNode is not None and editedSegmentation is not None and editedSegmentation.GetID() == segmentationNode.GetID()
    shown = _isTemplateShown(volumeNode)
    with slicer.util.WaitCursor():
        # The 1 mm volume is not shown when the user's own scan is the background
        volume1mm = _templateVolume(TEMPLATE_SAMPLE_1MM, path, show=shown)
        if shown:
            slicer.util.setSliceViewerLayers(background=volume1mm, fit=True)
        if segmentationNode is not None:
            segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volume1mm)
        _trackTemplate(volume1mm, segmentationNode)
        if editsTemplate:
            editor.setSourceVolumeNode(volume1mm)
    # The background is unchanged if the template was not shown, so the watcher is not notified: close the banner now
    _updateTemplateBanner()
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

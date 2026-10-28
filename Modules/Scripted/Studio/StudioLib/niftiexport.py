"""Export as NIfTI (A-11, decision D-N1): scans, label maps, segmentations, maps, DICOM series and whole projects.

Every image that Studio shows can be written as a standard single-file NIfTI-1 image (``.nii.gz`` by default, or
``.nii``) that FSL, SPM, AFNI, nibabel and official 3D Slicer read with left and right exactly as Studio shows them:

- **Geometry.** The affine of the file is the image's IJK to RAS matrix, as 3D Slicer's own NIfTI writer stores it
  (sform = qform = IJK to RAS; the qform has qfac = -1 for a left-handed grid). Oblique and DICOM-derived volumes keep
  their full direction matrix: nothing is resampled unless the user asks for a reference image. Voxels are written in
  the volume's own (i, j, k) order, so a voxel at negative RAS x (the subject's left) stays at negative x in every reader.
- **Transforms.** A linear transform applied to a node is folded into the affine of the exported copy (no resampling);
  a non-linear one is hardened into a temporary copy (resampled). The scene is never changed; the sidecar says so.
- **Codes.** sform and qform codes are 4 (MNI152) on a recognised MNI grid or one of the user's templates (A-10),
  2 (aligned) for an image whose transform was applied, 1 (scanner) otherwise; units are mm and seconds.
- **Values.** The data type is kept (uint8, int16, float32...) and window/level is never applied to the data. Maps are
  written as float32 with an optional thresholded binary mask.
- **Segmentations** are written as one label map (values 1..N in segment order, with ``<name>.tsv`` and an ITK-SNAP
  ``<name>.txt`` label table) or as one binary mask (uint8 0/1) per segment, on the segmentation's own grid or resampled
  (nearest neighbour) onto a chosen reference image. Empty segments are skipped, with a note.
- **Sidecars.** A BIDS-style ``<name>.json`` next to each image: the Studio version, the source node, the space and the
  template id and checksum when known, segment names, the map threshold, units and the creation date. Sidecars never
  hold an absolute path (only file names, or for lesions the scan relative to the sidecar, as Save lesion writes it).

The product does not depend on nibabel; images are written with ``LesionNetworkMappingLib.nifti`` (numpy only), or with
3D Slicer's own writer when that library is not part of the build.

Use :func:`planExport` (or :func:`planProject`, :func:`planDicom`) to get the jobs and the files they write, then
:func:`runJobs` (synchronous) or ``niftiexport_ui.ExportRunner`` (progress, writing in a background thread).
"""

import contextlib
import datetime
import json
import logging
import os
import re

import numpy as np
import slicer
import vtk
from slicer.i18n import tr as _

from . import space

#: Extensions of the exported images (the first one is the default)
EXTENSIONS = (".nii.gz", ".nii")
#: How a segmentation is written
SEGMENTATION_LABELMAP = "labelmap"
SEGMENTATION_MASKS = "masks"
SEGMENTATION_MODES = (SEGMENTATION_LABELMAP, SEGMENTATION_MASKS)

#: Kinds of exported images
KIND_SCAN = "scan"
KIND_TEMPLATE = "template"
KIND_LABELMAP = "labelmap"
KIND_SEGMENTATION = "segmentation"
KIND_MAP = "map"

#: NIfTI xform codes (nifti1.h)
XFORM_UNKNOWN = 0
XFORM_SCANNER = 1
XFORM_ALIGNED = 2
XFORM_MNI = 4
XFORM_NAMES = {XFORM_UNKNOWN: "unknown", XFORM_SCANNER: "scanner_anat", XFORM_ALIGNED: "aligned_anat", 3: "talairach", XFORM_MNI: "mni_152"}
#: xyzt_units: mm (2) and seconds (8), as 3D Slicer writes them
XYZT_UNITS_MM_SEC = 10

#: Default folder of exports inside a project (A-09): <project>/Exports/nifti
PROJECT_EXPORT_FOLDER = "nifti"
#: Name of the list of files written by a project export
README_NAME = "README.txt"

#: Fork-only settings (Studio/*): the last folder, format and segmentation mode of the export dialog
LAST_FOLDER_KEY = "Studio/NiftiExport/LastFolder"
FORMAT_KEY = "Studio/NiftiExport/Format"
SEGMENTATION_MODE_KEY = "Studio/NiftiExport/SegmentationMode"

#: Node attribute written by the DICOM scalar volume plug-in
DICOM_UIDS_ATTRIBUTE = "DICOM.instanceUIDs"
#: A BIDS subject entity in a name: "sub-01"
_BIDS_SUBJECT_PATTERN = re.compile(r"(?:^|[_\-/\s.(])(sub-[A-Za-z0-9]+)")
#: Modality suffixes recognised in scan names (BIDS spelling)
_MODALITIES = (("flair", "FLAIR"), ("t1w", "T1w"), ("t1", "T1w"), ("mprage", "T1w"), ("t2w", "T2w"), ("t2", "T2w"), ("dwi", "dwi"), ("bold", "bold"),
               ("ct", "CT"))
#: The description written into the header of every file (80 characters at most)
DESCRIPTION = "Slicer Studio NIfTI export"


class ExportError(RuntimeError):
    """An export that cannot be done (the message is shown to the user)."""


#
# Helpers
#


def _niftiLib():
    """``LesionNetworkMappingLib.nifti`` (numpy-only NIfTI-1 writer), or None when it is not part of this build."""
    try:
        from LesionNetworkMappingLib import nifti

        return nifti
    except ImportError:
        return None


def _templatesModule():
    try:
        from . import templates

        return templates
    except Exception:
        return None


def _projectsModule():
    try:
        from . import projects

        return projects
    except Exception:
        return None


def _matrixToArray(matrix):
    return np.array([[matrix.GetElement(row, column) for column in range(4)] for row in range(4)])


def _arrayToMatrix(array):
    matrix = vtk.vtkMatrix4x4()
    for row in range(4):
        for column in range(4):
            matrix.SetElement(row, column, float(array[row, column]))
    return matrix


def splitExtension(path):
    """``("/a/b/sub-01_T1w", ".nii.gz")``: the base and the NIfTI extension of ``path`` ("" when it has none)."""
    lower = str(path).lower()
    for extension in EXTENSIONS:
        if lower.endswith(extension):
            return str(path)[:-len(extension)], str(path)[-len(extension):]
    return str(path), ""


def withExtension(path, extension=".nii.gz"):
    """``path`` with a NIfTI extension (``extension`` is added when it has none)."""
    base, current = splitExtension(path)
    return path if current else base + extension


def sidecarPath(path):
    """``sub-01_T1w.json`` next to ``sub-01_T1w.nii.gz``."""
    return splitExtension(path)[0] + ".json"


def safeName(text, fallback="image"):
    """A file name made of ASCII letters, digits, ".", "_" and "-" only (spaces become "_")."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text or "")).strip("._-")
    name = re.sub(r"_+", "_", name)
    return name or fallback


def entityValue(text, fallback="segment"):
    """A BIDS entity value (letters and digits only): "Left lesion 2" -> "Leftlesion2"."""
    value = re.sub(r"[^A-Za-z0-9]+", "", str(text or ""))
    return value or fallback


def _storageFile(node):
    storageNode = node.GetStorageNode() if node is not None and hasattr(node, "GetStorageNode") else None
    fileName = storageNode.GetFileName() if storageNode is not None else None
    return str(fileName) if fileName else None


def _nowIso():
    return datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def studioVersion():
    return f"Slicer Studio {slicer.app.applicationVersion}"


@contextlib.contextmanager
def _temporaryNodes():
    """Remove every node added to the scene inside the block (temporary label maps, their color tables and displays)."""
    scene = slicer.mrmlScene
    before = {scene.GetNthNode(index).GetID() for index in range(scene.GetNumberOfNodes())}
    try:
        yield
    finally:
        for index in reversed(range(scene.GetNumberOfNodes())):
            node = scene.GetNthNode(index)
            if node is not None and node.GetID() not in before:
                scene.RemoveNode(node)


#
# What can be exported
#


def isFigure(node):
    return node is not None and node.GetAttribute(space.ROLE_ATTRIBUTE) == space.ROLE_FIGURE


def isMapNode(node):
    """A network or statistical map (Map results, LNM outputs, statistical maps)."""
    projects = _projectsModule()
    if projects is not None:
        return projects.isMapNode(node)
    return node is not None and (node.GetAttribute("LNM.Result") == "1" or bool(node.GetAttribute("LNM.Statistic"))
                                 or node.GetAttribute(space.ROLE_ATTRIBUTE) == space.ROLE_STATMAP)


def isTemplateNode(node):
    if node is None or not node.IsA("vtkMRMLScalarVolumeNode"):
        return False
    return node.GetAttribute(space.ROLE_ATTRIBUTE) == space.ROLE_TEMPLATE or (node.GetName() or "").startswith(space.TEMPLATE_SPACE)


def isDicomNode(node):
    return node is not None and node.IsA("vtkMRMLVolumeNode") and bool(node.GetAttribute(DICOM_UIDS_ATTRIBUTE))


def kindOf(node):
    """"scan", "template", "labelmap", "segmentation" or "map" (None when ``node`` cannot be exported as NIfTI)."""
    if node is None:
        return None
    if node.IsA("vtkMRMLSegmentationNode"):
        return KIND_SEGMENTATION
    if node.IsA("vtkMRMLLabelMapVolumeNode"):
        return KIND_LABELMAP
    if node.IsA("vtkMRMLScalarVolumeNode"):
        if isMapNode(node):
            return KIND_MAP
        if isTemplateNode(node):
            return KIND_TEMPLATE
        return KIND_SCAN
    return None


def kindText(kind):
    return {KIND_SCAN: _("Scan"), KIND_TEMPLATE: _("Template"), KIND_LABELMAP: _("Label map"), KIND_SEGMENTATION: _("Segmentation"),
            KIND_MAP: _("Map")}.get(kind, _("Image"))


def segmentIds(segmentationNode, nonEmpty=False):
    """The segment IDs of a segmentation in order (only those with voxels when ``nonEmpty``)."""
    segmentation = segmentationNode.GetSegmentation()
    ids = [segmentation.GetNthSegmentID(index) for index in range(segmentation.GetNumberOfSegments())]
    if nonEmpty:
        ids = [segmentId for segmentId in ids if not isSegmentEmpty(segmentationNode, segmentId)]
    return ids


def isSegmentEmpty(segmentationNode, segmentId):
    """True if a segment has no voxel (closed surface segments without a labelmap count as not empty)."""
    try:
        from . import draw

        return draw.isLesionEmpty(segmentationNode, [segmentId])
    except ImportError:
        labelmap = segmentationNode.GetBinaryLabelmapInternalRepresentation(segmentId)
        if labelmap is None or labelmap.IsEmpty():
            return True
        segment = segmentationNode.GetSegmentation().GetSegment(segmentId)
        array = slicer.util.arrayFromSegmentInternalBinaryLabelmap(segmentationNode, segmentId)
        return not np.any(array == segment.GetLabelValue())


def unavailableReason(node):
    """Why ``node`` cannot be written as NIfTI ("" when it can)."""
    if node is None:
        return _("Nothing to export")
    if isFigure(node):
        return _("A figure is a picture, not an image in space: it is not exported as NIfTI")
    if node.IsA("vtkMRMLSegmentationNode"):
        if node.GetSegmentation().GetNumberOfSegments() == 0:
            return _("The segmentation has no segment")
        if not segmentIds(node, nonEmpty=True):
            return _("Every segment is empty")
        return ""
    if node.IsA("vtkMRMLVectorVolumeNode") or node.IsA("vtkMRMLTensorVolumeNode"):
        return _("Colour and tensor volumes are not exported as NIfTI (use File > Save Data)")
    if not node.IsA("vtkMRMLScalarVolumeNode"):
        return _("Only images and segmentations are exported as NIfTI")
    imageData = node.GetImageData()
    if imageData is None or imageData.GetNumberOfPoints() == 0:
        return _("The image has no voxels")
    if imageData.GetNumberOfScalarComponents() != 1:
        return _("Images with several components per voxel are not exported as NIfTI (use File > Save Data)")
    return ""


def exportableNodes(includeTemplates=True):
    """The images and segmentations of the scene that can be exported (not hidden from the editors), scans first."""
    nodes = []
    for className in ("vtkMRMLScalarVolumeNode", "vtkMRMLSegmentationNode"):
        for node in slicer.util.getNodesByClass(className):
            if node.GetHideFromEditors() or isFigure(node):
                continue
            if not includeTemplates and kindOf(node) == KIND_TEMPLATE:
                continue
            if unavailableReason(node):
                continue
            nodes.append(node)
    order = {KIND_SCAN: 0, KIND_TEMPLATE: 1, KIND_LABELMAP: 2, KIND_MAP: 3, KIND_SEGMENTATION: 4}
    return sorted(nodes, key=lambda node: order.get(kindOf(node), 5))


def hasExportableNodes():
    """True when the scene has an image or a segmentation with segments (cheap: no voxel is read; for Search)."""
    for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        if not node.GetHideFromEditors() and not isFigure(node) and node.GetImageData() is not None:
            return True
    return any(not node.GetHideFromEditors() and node.GetSegmentation().GetNumberOfSegments() > 0
               for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"))


def dicomNodes():
    """The loaded volumes that came from DICOM (the DICOM scalar volume plug-in tags them)."""
    return [node for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode") if isDicomNode(node) and not node.GetHideFromEditors()]


#
# Geometry, codes and spaces
#


def templateInfo(node, grid=None):
    """``{"id", "name", "space", "sha256"}`` of the template that ``node`` is (or whose grid it is on), or None. Never a path."""
    templates = _templatesModule()
    if templates is None:
        return None
    template = None
    try:
        template = templates.templateOfNode(node) if node is not None else None
        if template is None and grid is not None:
            dims, affine = grid
            template = templates.templateForGrid(tuple(int(n) for n in dims), np.asarray(affine).tolist())
    except Exception:
        logging.debug("Slicer Studio: template lookup failed", exc_info=True)
        template = None
    if template is None:
        return None
    sha256 = template.imageSha256 if template.isUser else (template.sha256.get("image") or None)
    info = {"id": template.id, "name": template.name, "space": template.space}
    if sha256:
        info["sha256"] = sha256
    return info


def spaceInfo(node, grid, transformed=False):
    """``(space name or None, resolution, template info or None)`` of an exported image.

    The node's ``Studio.Space`` attribute wins; else the grid is compared with the known MNI grids and the user's
    templates. With ``transformed`` (a transform was applied to the exported copy) the image is no longer on the grid
    or in the space it is tagged with, so there is no space (code 2, aligned).
    """
    if transformed:
        return None, None, None
    named = space.spaceOf(node) if node is not None and node.GetAttribute(space.SPACE_ATTRIBUTE) else None
    if named is None and grid is not None:
        named = space.spaceOf(grid)
        if named is None and node is not None and isTemplateNode(node):
            named = space.spaceOf(node)
    template = templateInfo(node, grid)
    if named is None and template is not None:
        named = (template["space"], None)
    if named is None:
        return None, None, None
    return named[0], named[1], template


def xformCode(spaceName, transformed=False):
    """4 (MNI152) on an MNI grid or a template, 2 (aligned) when a transform was applied, else 1 (scanner)."""
    if spaceName:
        return XFORM_MNI
    if transformed:
        return XFORM_ALIGNED
    return XFORM_SCANNER


def _cloneVolume(volumeNode):
    """A temporary copy of a volume with its own image data and the same parent transform (removed by ``_temporaryNodes``)."""
    scene = slicer.mrmlScene
    clone = scene.AddNewNodeByClass(volumeNode.GetClassName(), scene.GenerateUniqueName("StudioNiftiExport"))
    clone.SetHideFromEditors(True)
    clone.SetSaveWithScene(False)
    imageData = vtk.vtkImageData()
    imageData.DeepCopy(volumeNode.GetImageData())
    clone.SetAndObserveImageData(imageData)
    matrix = vtk.vtkMatrix4x4()
    volumeNode.GetIJKToRASMatrix(matrix)
    clone.SetIJKToRASMatrix(matrix)
    transformNode = volumeNode.GetParentTransformNode()
    clone.SetAndObserveTransformNodeID(transformNode.GetID() if transformNode is not None else None)
    return clone


def volumeData(volumeNode):
    """``(data_ijk, affine, transformNote)`` of a scalar volume as it is shown in the views.

    ``data_ijk`` is a copy of the voxels indexed [i, j, k] (file order), ``affine`` the 4x4 IJK to RAS matrix in world
    coordinates. A linear parent transform is folded into the affine; a non-linear one is hardened into a temporary copy
    (resampled). ``transformNote`` describes what was applied (None without a transform). Call it inside
    ``_temporaryNodes()``. The scene is never changed.
    """
    transformNode = volumeNode.GetParentTransformNode()
    note = None
    if transformNode is not None and not transformNode.IsTransformToWorldLinear():
        clone = _cloneVolume(volumeNode)
        slicer.vtkSlicerTransformLogic().hardenTransform(clone)
        # Label maps (also the temporary ones of segmentations) are resampled nearest neighbour, other volumes linearly
        interpolation = "nearest neighbour" if volumeNode.IsA("vtkMRMLLabelMapVolumeNode") else "linear interpolation"
        note = {"Name": transformNode.GetName(), "Linear": False,
                "Description": f"A non-linear transform was applied to the exported copy (resampled, {interpolation})"}
        volumeNode = clone
        transformNode = None
    matrix = vtk.vtkMatrix4x4()
    volumeNode.GetIJKToRASMatrix(matrix)
    affine = _matrixToArray(matrix)
    if transformNode is not None:
        toWorld = vtk.vtkMatrix4x4()
        transformNode.GetMatrixTransformToWorld(toWorld)
        affine = _matrixToArray(toWorld) @ affine
        note = {"Name": transformNode.GetName(), "Linear": True,
                "Description": "The linear transform was applied to the exported copy's affine (no resampling)"}
    array = slicer.util.arrayFromVolume(volumeNode)
    if array is None or array.ndim != 3:
        raise ExportError(_("{name} is not a 3D image").format(name=volumeNode.GetName()))
    # [k, j, i] (VTK memory order) -> [i, j, k] (file order); the copy keeps the data after temporary nodes are removed
    return np.array(array, copy=True).transpose(2, 1, 0), affine, note


#
# Write tasks
#


class WriteTask:
    """One NIfTI image and its side files, ready to be written (all arrays are owned copies: safe in another thread)."""

    def __init__(self, path, data, affine, code, sidecar=None, textFiles=None, dtype=None, node=None):
        self.path = path
        self.data = data
        self.affine = np.asarray(affine, dtype=np.float64)
        self.code = int(code)
        self.sidecar = sidecar
        self.textFiles = dict(textFiles or {})
        self.dtype = dtype
        #: Fallback without LesionNetworkMappingLib: the node written by 3D Slicer's writer on the main thread
        self.node = node

    def files(self):
        return [self.path] + ([sidecarPath(self.path)] if self.sidecar is not None else []) + list(self.textFiles)

    def write(self):
        """Write the image (atomically), its sidecar and text files. Return the written paths."""
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        nifti = _niftiLib()
        if nifti is None or self.data is None:
            raise ExportError(_("This build cannot write NIfTI files without 3D Slicer's writer"))
        data = self.data
        if self.dtype is not None and data.dtype != np.dtype(self.dtype):
            data = data.astype(self.dtype)
        nifti.write_nifti(self.path, data, self.affine, sform_code=self.code, qform_code=self.code, descrip=DESCRIPTION,
                          xyzt_units=XYZT_UNITS_MM_SEC)
        self._writeSideFiles()
        return self.files()

    def _writeSideFiles(self):
        if self.sidecar is not None:
            writeJson(sidecarPath(self.path), self.sidecar)
        for path, text in self.textFiles.items():
            _writeText(path, text)


def writeJson(path, content):
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(content, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(temporary, path)


def _writeText(path, text):
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(temporary, path)


def _writeWithSlicer(task):
    """Fallback without LesionNetworkMappingLib: 3D Slicer's own writer (codes stay 1), then the side files.

    A storage node outside the scene writes the file, so the node keeps its own storage node and file name.
    """
    folder = os.path.dirname(task.path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    storageNode = task.node.CreateDefaultStorageNode()
    if storageNode is None:
        raise ExportError(_("Failed to write {path}").format(path=task.path))
    storageNode.SetFileName(task.path)
    storageNode.SetUseCompression(task.path.lower().endswith(".gz"))
    if not storageNode.WriteData(task.node):
        raise ExportError(_("Failed to write {path}").format(path=task.path))
    task._writeSideFiles()
    return task.files()


#
# Sidecars
#


def baseSidecar(node, kind, spaceName, resolution, template, code, data, affine, transformNote=None):
    """The fields every sidecar has (BIDS style; never an absolute path)."""
    content = {
        "GeneratedBy": studioVersion(),
        "CreationDate": _nowIso(),
        "Kind": kind,
        "SourceNode": node.GetName() if node is not None else "",
    }
    sourceFile = _storageFile(node)
    if sourceFile:
        content["SourceFile"] = os.path.basename(sourceFile)
    content["Space"] = spaceName or ("aligned" if transformNote else "scanner")
    if resolution is not None:
        content["SpaceResolutionMm"] = resolution
    if template is not None:
        content["Template"] = template
    content["SformCode"] = code
    content["QformCode"] = code
    content["XformName"] = XFORM_NAMES.get(code, "")
    content["SpatialUnits"] = "mm"
    content["Orientation"] = ("The affine maps (i, j, k) to RAS+ world coordinates in mm (x increases to the subject's right), as 3D Slicer "
                              "writes it; no voxel was reoriented")
    if data is not None:
        content["Shape"] = [int(n) for n in data.shape]
        content["VoxelSizeMm"] = [round(float(np.linalg.norm(affine[:3, axis])), 6) for axis in range(3)]
        content["DataType"] = str(data.dtype)
    if transformNote is not None:
        content["TransformApplied"] = transformNote
    return content


def _hexColor(rgb):
    return "#" + "".join(f"{max(0, min(255, round(float(component) * 255))):02x}" for component in rgb[:3])


#
# Jobs
#


class Job:
    """The export of one node: ``prepare()`` reads the scene (main thread) and returns ``WriteTask`` objects."""

    def __init__(self, node, kind, path, options=None, title=None):
        self.nodeId = node.GetID()
        self.nodeName = node.GetName()
        self.kind = kind
        self.path = path
        self.options = dict(options or {})
        self.title = title or node.GetName()
        self.notes = []
        self._files = None

    def node(self):
        return slicer.mrmlScene.GetNodeByID(self.nodeId)

    def files(self):
        """The paths this job writes (images, sidecars, label tables), known before it runs."""
        if self._files is None:
            self._files = self._plannedFiles()
        return list(self._files)

    def images(self):
        return [path for path in self.files() if splitExtension(path)[1]]

    def _plannedFiles(self):
        node = self.node()
        if self.kind == KIND_SEGMENTATION:
            if self.options.get("mode") == SEGMENTATION_MASKS:
                files = []
                for _segmentId, path in self._maskPaths(node):
                    files += [path, sidecarPath(path)]
                return files
            base = splitExtension(self.path)[0]
            return [self.path, sidecarPath(self.path), base + ".tsv", base + ".txt"]
        files = [self.path, sidecarPath(self.path)]
        if self.kind == KIND_MAP and self.options.get("threshold") is not None:
            maskPath = mapMaskPath(self.path)
            files += [maskPath, sidecarPath(maskPath)]
        return files

    def _maskPaths(self, node):
        """``[(segment ID, path)]`` of the masks of a segmentation (non-empty segments, unique names).

        The first segment of a name keeps it. A later segment whose name gives the same entity value (the names differ
        only by case, spaces or punctuation) gets ``dup<N>`` after it, and N skips every value another segment has. So
        no file name equals the name of another segment: "Lesion 1", "Lesion-1" and "Lesion 12" give ``Lesion1``,
        ``Lesion1dup2`` and ``Lesion12``.
        """
        base, extension = splitExtension(self.path)
        segmentation = node.GetSegmentation()
        entries = [(segmentId, entityValue(segmentation.GetSegment(segmentId).GetName()))
                   for segmentId in segmentIds(node, nonEmpty=True)]
        reserved = {value.lower() for _segmentId, value in entries}
        taken = set()
        result = []
        for segmentId, value in entries:
            candidate, index = value, 2
            while candidate.lower() in taken:
                candidate = f"{value}dup{index}"
                index += 1
                while candidate.lower() in reserved:
                    candidate = f"{value}dup{index}"
                    index += 1
            taken.add(candidate.lower())
            result.append((segmentId, maskBaseName(base, candidate) + extension))
        return result

    def prepare(self):
        node = self.node()
        if node is None:
            raise ExportError(_("{name} is no longer open").format(name=self.nodeName))
        reason = unavailableReason(node)
        if reason:
            raise ExportError(f"{self.nodeName}: {reason}")
        if self.kind == KIND_SEGMENTATION:
            return self._prepareSegmentation(node)
        return self._prepareVolume(node)

    def _prepareVolume(self, node):
        with _temporaryNodes():
            data, affine, transformNote = volumeData(node)
        grid = (data.shape, affine)
        spaceName, resolution, template = spaceInfo(node, grid, transformed=transformNote is not None)
        code = xformCode(spaceName, transformNote is not None)
        dtype = None
        if self.kind == KIND_MAP and data.dtype != np.float32:
            dtype = np.float32
        sidecar = baseSidecar(node, self.kind, spaceName, resolution, template, code, data, affine, transformNote)
        if dtype is not None:
            sidecar["DataType"] = "float32"
        sidecar["Units"] = {KIND_LABELMAP: "label", KIND_MAP: "statistic"}.get(self.kind, "arbitrary")
        if self.kind == KIND_LABELMAP:
            sidecar["Labels"] = _labelmapLabels(node, data)
        if isDicomNode(node):
            sidecar.update(dicomInfo(node))
        tasks = []
        if self.kind == KIND_MAP:
            statistic = node.GetAttribute("LNM.Statistic") or ""
            if statistic:
                sidecar["Statistic"] = statistic
            threshold = self.options.get("threshold")
            if threshold is not None:
                sign = self.options.get("sign") or "both"
                sidecar["Threshold"] = {"Value": float(threshold), "Sign": sign, "Rule": self.options.get("rule") or "",
                                        "Mask": os.path.basename(mapMaskPath(self.path))}
                mask = thresholdMask(data, threshold, sign)
                maskPath = mapMaskPath(self.path)
                maskSidecar = dict(sidecar)
                maskSidecar.update({"Kind": "mask", "Units": "binary", "DataType": "uint8", "ThresholdedFrom": os.path.basename(self.path)})
                maskSidecar.pop("Statistic", None)
                tasks.append(WriteTask(maskPath, mask, affine, code, maskSidecar))
                if not mask.any():
                    self.notes.append(_("No voxel of {name} passes the threshold: the mask is empty").format(name=self.nodeName))
            else:
                sidecar["Threshold"] = None
        if _niftiLib() is None:
            task = WriteTask(self.path, None, affine, XFORM_SCANNER, sidecar, node=node)
        else:
            task = WriteTask(self.path, data, affine, code, sidecar, dtype=dtype)
        if transformNote is not None:
            self.notes.append(_("{name}: its transform was applied to the exported copy (the scene is unchanged)").format(name=self.nodeName))
        return [task] + tasks

    def _referenceVolume(self, node):
        """The reference image of the export: the chosen one, else the lesion's own grid (draw's recorded grid volume or the
        segmentation's reference image), else None (the segmentation's reference geometry).
        """
        reference = self.options.get("reference")
        referenceNode = slicer.mrmlScene.GetNodeByID(reference) if reference else None
        if referenceNode is not None:
            return referenceNode, True
        try:
            from . import draw

            recorded = draw._recordedGridVolume(node)
        except Exception:
            recorded = None
        if recorded is not None:
            return recorded, False
        geometry = node.GetSegmentation().GetConversionParameter(slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName())
        if not geometry:
            role = slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole()
            return node.GetNodeReference(role), False
        return None, False

    def _exportLabelmap(self, node, ids, referenceNode):
        """A temporary label map node of ``ids`` (values 1..N in this order) on the reference grid."""
        scene = slicer.mrmlScene
        labelmapNode = scene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", scene.GenerateUniqueName("StudioNiftiExport"))
        labelmapNode.SetHideFromEditors(True)
        labelmapNode.SetSaveWithScene(False)
        idArray = vtk.vtkStringArray()
        for segmentId in ids:
            idArray.InsertNextValue(segmentId)
        exported = slicer.modules.segmentations.logic().ExportSegmentsToLabelmapNode(
            node, idArray, labelmapNode, referenceNode, slicer.vtkSegmentation.EXTENT_REFERENCE_GEOMETRY)
        if not exported or labelmapNode.GetImageData() is None:
            raise ExportError(_("Failed to export the segments of {name}").format(name=node.GetName()))
        return labelmapNode

    def _segmentationSidecar(self, node, data, affine, transformNote, referenceNode, resampled):
        grid = (data.shape, affine)
        spaceName, resolution, template = spaceInfo(node, grid, transformed=transformNote is not None)
        if spaceName is None and referenceNode is not None and not transformNote:
            spaceName, resolution, template = spaceInfo(referenceNode, grid)
        code = xformCode(spaceName, transformNote is not None)
        sidecar = baseSidecar(node, KIND_SEGMENTATION, spaceName, resolution, template, code, data, affine, transformNote)
        # The fields of a saved lesion (Save lesion's sidecar, D-U3): the scan relative to the sidecar, notes, source reference
        lesion = _lesionFields(node, self.path)
        sidecar.update(lesion)
        if resampled:
            sidecar["Resampled"] = {"Reference": referenceNode.GetName(), "Interpolation": "nearest neighbour"}
        return sidecar, code

    def _prepareSegmentation(self, node):
        mode = self.options.get("mode") or SEGMENTATION_LABELMAP
        segmentation = node.GetSegmentation()
        allIds = segmentIds(node)
        ids = segmentIds(node, nonEmpty=True)
        empty = [segmentation.GetSegment(segmentId).GetName() for segmentId in allIds if segmentId not in ids]
        if empty:
            self.notes.append(_("{name}: empty segments were skipped: {segments}").format(name=self.nodeName, segments=", ".join(empty)))
        tasks = []
        with _temporaryNodes():
            referenceNode, resampled = self._referenceVolume(node)
            if mode == SEGMENTATION_MASKS:
                for segmentId, path in self._maskPaths(node):
                    labelmapNode = self._exportLabelmap(node, [segmentId], referenceNode)
                    data, affine, transformNote = volumeData(labelmapNode)
                    data = (data != 0).astype(np.uint8)
                    sidecar, code = self._segmentationSidecar(node, data, affine, transformNote, referenceNode, resampled)
                    segment = segmentation.GetSegment(segmentId)
                    sidecar.update({"Kind": "mask", "Units": "binary", "Segment": segment.GetName(), "SegmentColor": _hexColor(segment.GetColor()),
                                    "VoxelCount": int(np.count_nonzero(data))})
                    tasks.append(WriteTask(path, data, affine, code, sidecar))
            else:
                labelmapNode = self._exportLabelmap(node, ids, referenceNode)
                data, affine, transformNote = volumeData(labelmapNode)
                if data.max(initial=0) <= 255 and data.min(initial=0) >= 0:
                    data = data.astype(np.uint8)
                sidecar, code = self._segmentationSidecar(node, data, affine, transformNote, referenceNode, resampled)
                labels = []
                counts = np.bincount(data.ravel().astype(np.int64), minlength=len(ids) + 1) if data.size else np.zeros(len(ids) + 1, int)
                for value, segmentId in enumerate(ids, start=1):
                    segment = segmentation.GetSegment(segmentId)
                    labels.append({"Value": value, "Name": segment.GetName(), "Color": _hexColor(segment.GetColor()), "VoxelCount": int(counts[value])})
                sidecar.update({"Kind": "labelmap", "Units": "label", "Labels": labels})
                if len(ids) > 1:
                    sidecar["Overlap"] = "Where segments overlap, a voxel has the value of the later segment"
                base = splitExtension(self.path)[0]
                tasks.append(WriteTask(self.path, data, affine, code, sidecar, textFiles={base + ".tsv": labelTableTsv(labels),
                                                                                         base + ".txt": labelTableItkSnap(labels)}))
            if transformNote is not None:
                self.notes.append(_("{name}: its transform was applied to the exported copy (the scene is unchanged)").format(name=self.nodeName))
        return tasks


def _labelmapLabels(node, data):
    """``[{"Value", "Name", "Color"}]`` of the values present in a label map volume (names from its color table)."""
    values = [int(value) for value in np.unique(data) if value != 0][:1000]
    displayNode = node.GetDisplayNode()
    colorNode = displayNode.GetColorNode() if displayNode is not None else None
    labels = []
    for value in values:
        entry = {"Value": value}
        if colorNode is not None and 0 <= value < colorNode.GetNumberOfColors():
            entry["Name"] = colorNode.GetColorName(value) or str(value)
            rgba = [0.0, 0.0, 0.0, 0.0]
            colorNode.GetColor(value, rgba)
            entry["Color"] = _hexColor(rgba)
        labels.append(entry)
    return labels


def _lesionFields(segmentationNode, path):
    """The fields of Save lesion's sidecar (``draw.writeSidecar``): Source (the scan, relative to the sidecar), Notes and
    SourceReference; empty when Draw is not available.
    """
    try:
        from . import draw
    except ImportError:
        return {}
    fields = {"Notes": segmentationNode.GetAttribute(draw.NOTES_ATTRIBUTE) or "",
              "SourceReference": segmentationNode.GetAttribute(draw.SOURCE_REFERENCE_ATTRIBUTE) or ""}
    role = slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole()
    sourceNode = segmentationNode.GetNodeReference(role)
    if sourceNode is None:
        editor = draw.segmentEditor() if hasattr(draw, "segmentEditor") else None
        if editor is not None and editor.segmentationNode() is segmentationNode:
            sourceNode = editor.sourceVolumeNode()
    if sourceNode is not None:
        source = draw._sidecarSource(sidecarPath(path), sourceNode)
        if source and not os.path.isabs(source):
            fields["Source"] = source
    return fields


def labelTableTsv(labels):
    """A BIDS look-up table (``dseg.tsv``): index, name, color."""
    lines = ["index\tname\tcolor"]
    for label in labels:
        lines.append(f"{label['Value']}\t{_tsvText(label['Name'])}\t{label.get('Color', '')}")
    return "\n".join(lines) + "\n"


def _tsvText(text):
    return str(text).replace("\t", " ").replace("\n", " ")


def labelTableItkSnap(labels):
    """An ITK-SNAP label description file (ITK-SNAP: Segmentation > Import Label Descriptions)."""
    lines = [
        "################################################",
        "# ITK-SnAP Label Description File (Slicer Studio NIfTI export)",
        "# Columns: IDX -R- -G- -B- -A-- VIS MSH LABEL",
        "################################################",
        '    0     0    0    0        0  0  0    "Clear Label"',
    ]
    for label in labels:
        color = label.get("Color") or "#ffffff"
        red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
        name = str(label["Name"]).replace('"', "'")
        lines.append(f'{label["Value"]:5d} {red:5d} {green:4d} {blue:4d}        1  1  1    "{name}"')
    return "\n".join(lines) + "\n"


def mapMaskPath(path):
    """``T_map_desc-thresholded_mask.nii.gz`` next to ``T_map.nii.gz``."""
    base, extension = splitExtension(path)
    return f"{base}_desc-thresholded_mask{extension or '.nii.gz'}"


def thresholdMask(data, threshold, sign="both"):
    """uint8 mask of the voxels that pass ``threshold``: |x| >= t ("both"), x >= t ("pos") or x <= -t ("neg")."""
    values = np.asarray(data, dtype=np.float64)
    finite = np.isfinite(values)
    threshold = abs(float(threshold))
    if sign == "pos":
        passed = values >= threshold
    elif sign == "neg":
        passed = values <= -threshold
    else:
        passed = np.abs(values) >= threshold
    return (passed & finite).astype(np.uint8)


#
# DICOM
#


def _dicomValue(uid, tag):
    database = getattr(slicer, "dicomDatabase", None)
    if database is None or not uid:
        return ""
    isOpen = getattr(database, "isOpen", False)
    if not (isOpen() if callable(isOpen) else isOpen):
        # Without a database (a volume loaded from a scene), Qt would log an SQL error per query
        return ""
    try:
        return str(database.instanceValue(uid, tag) or "").strip()
    except Exception:
        return ""


def dicomInfo(node):
    """Series fields of a DICOM-derived volume for its sidecar (never a patient name, identifier or date)."""
    uids = (node.GetAttribute(DICOM_UIDS_ATTRIBUTE) or "").split()
    uid = uids[0] if uids else ""
    info = {"DicomDerived": True}
    for key, tag in (("SeriesDescription", "0008,103e"), ("SeriesNumber", "0020,0011"), ("Modality", "0008,0060")):
        value = _dicomValue(uid, tag)
        if value:
            info[key] = int(value) if key == "SeriesNumber" and value.isdigit() else value
    return info


def dicomFileName(node):
    """``"003_T1_MPRAGE"``: the series number and description of a DICOM-derived volume, else its node name."""
    info = dicomInfo(node)
    parts = []
    if info.get("SeriesNumber") is not None:
        number = info["SeriesNumber"]
        parts.append(f"{number:03d}" if isinstance(number, int) else safeName(number))
    if info.get("SeriesDescription"):
        parts.append(safeName(info["SeriesDescription"]))
    return "_".join(parts) if parts else safeName(node.GetName())


#
# Names
#


def bidsSubject(node):
    """The BIDS subject ("sub-01") in the node's name or file name, else None."""
    for text in (node.GetName(), os.path.basename(_storageFile(node) or "")):
        match = _BIDS_SUBJECT_PATTERN.search(text or "")
        if match:
            return match.group(1)
    return None


def _modality(text):
    words = [word.lower() for word in re.split(r"[^A-Za-z0-9]+", text or "") if word]
    for token, suffix in _MODALITIES:
        if token in words:
            return suffix
    return None


def defaultBaseName(node, kind=None, bids=False):
    """The base file name (without extension) proposed for ``node``.

    With ``bids`` (project exports), names are BIDS-like when the node names a subject: ``sub-01_T1w``,
    ``sub-01_desc-lesion`` (masks are ``sub-01_label-<segment>_desc-lesion_mask``), ``sub-01_stat-T_statmap``; otherwise the
    node name.
    """
    kind = kind or kindOf(node)
    name = node.GetName()
    if isDicomNode(node):
        return dicomFileName(node)
    if not bids:
        return safeName(name)
    subject = bidsSubject(node)
    if subject is None:
        return safeName(name)
    if kind == KIND_SEGMENTATION:
        return f"{subject}_desc-lesion"
    if kind == KIND_MAP:
        statistic = entityValue(node.GetAttribute("LNM.Statistic") or "", fallback="")
        return f"{subject}_stat-{statistic}_statmap" if statistic else f"{subject}_statmap"
    if kind == KIND_LABELMAP:
        return f"{subject}_dseg"
    modality = _modality(name) or _modality(os.path.basename(_storageFile(node) or ""))
    return f"{subject}_{modality}" if modality else f"{subject}_{safeName(name.replace(subject, ''), 'scan')}"


def maskBaseName(base, segment):
    """The base name of the mask of ``segment``: ``<base>_label-<segment>_mask``, with BIDS entity order kept.

    BIDS puts ``desc`` last of the entities, so ``sub-01_desc-lesion`` gives ``sub-01_label-<segment>_desc-lesion_mask``.
    """
    match = re.match(r"^(.+)_(desc-[A-Za-z0-9]+)$", base)
    if match:
        return f"{match.group(1)}_label-{segment}_{match.group(2)}_mask"
    return f"{base}_label-{segment}_mask"


def uniquePath(path, reserved):
    """``path``, or ``<base>_2<ext>``... when another job of the same export already writes it (case-insensitive)."""
    base, extension = splitExtension(path)
    candidate, index = path, 2
    while os.path.normcase(candidate).lower() in reserved:
        candidate = f"{base}_{index}{extension}"
        index += 1
    reserved.add(os.path.normcase(candidate).lower())
    return candidate


#
# Plans
#


def planExport(nodes, folder, extension=".nii.gz", segmentationMode=SEGMENTATION_LABELMAP, reference=None, mapOptions=None, bids=False,
               names=None):
    """The jobs that export ``nodes`` into ``folder``.

    :param extension: ".nii.gz" (default) or ".nii".
    :param segmentationMode: "labelmap" (one label map per segmentation) or "masks" (one binary mask per segment).
    :param reference: ID of a volume onto which segmentations are resampled (nearest neighbour), or None (their own grid).
    :param mapOptions: ``{node ID: {"threshold", "sign", "rule"}}``: maps written with a thresholded mask.
    :param bids: BIDS-like names when the nodes name a subject (project exports).
    :param names: ``{node ID: base name}`` to override the proposed names.
    """
    if extension not in EXTENSIONS:
        raise ValueError(f"Unknown NIfTI extension {extension!r}")
    if segmentationMode not in SEGMENTATION_MODES:
        raise ValueError(f"Unknown segmentation mode {segmentationMode!r}")
    jobs = []
    reserved = set()
    for node in nodes:
        if node is None or unavailableReason(node):
            continue
        kind = kindOf(node)
        base = (names or {}).get(node.GetID()) or defaultBaseName(node, kind, bids=bids)
        if kind == KIND_SEGMENTATION and segmentationMode == SEGMENTATION_LABELMAP:
            base = f"{base}_dseg"
        path = uniquePath(os.path.join(folder, base + extension), reserved)
        options = {}
        if kind == KIND_SEGMENTATION:
            options = {"mode": segmentationMode, "reference": reference}
        elif kind == KIND_MAP and mapOptions and node.GetID() in mapOptions:
            options = dict(mapOptions[node.GetID()])
        job = Job(node, kind, path, options)
        for other in job.files():
            reserved.add(os.path.normcase(other).lower())
        jobs.append(job)
    return jobs


def planDicom(folder, extension=".nii.gz"):
    """Jobs that convert every loaded DICOM series to NIfTI (named "<series number>_<description>")."""
    return planExport(dicomNodes(), folder, extension)


def projectExportFolder(create=False):
    """``<project>/Exports/nifti`` of the open project (A-09), or None without a project."""
    projects = _projectsModule()
    exports = projects.exportsFolder(create=create) if projects is not None else None
    if not exports:
        return None
    folder = os.path.join(exports, PROJECT_EXPORT_FOLDER)
    if create:
        os.makedirs(folder, exist_ok=True)
    return folder


def projectNodes():
    """What a project export writes: every image (templates excluded: they are known by name), lesions and maps."""
    return [node for node in exportableNodes(includeTemplates=False) if node.GetSaveWithScene()]


def mapOptionsFromMapModule():
    """``{node ID: {"threshold", "sign", "rule"}}`` of the map that Map shows with a threshold (empty otherwise)."""
    widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
    if widget is None or not hasattr(widget, "legendState"):
        return {}
    try:
        # A reopened project has not restored its map until Map is first entered: the widget does that quietly
        if hasattr(widget, "restoreSceneResult"):
            widget.restoreSceneResult()
        # The threshold of the map itself, also while the views show a descriptive QA map (whose legend has none)
        state = widget.resultLegendState() if hasattr(widget, "resultLegendState") else widget.legendState()
        node = widget._parameterNode.resultVolume if widget._parameterNode is not None else None
    except Exception:
        logging.debug("Slicer Studio: the Map threshold could not be read", exc_info=True)
        return {}
    if state is None or node is None or state.get("threshold") is None:
        return {}
    return {node.GetID(): {"threshold": float(state["threshold"]), "sign": state.get("sign") or "both", "rule": str(state.get("thresholdText") or "")}}


def planProject(folder=None, extension=".nii.gz"):
    """Jobs of "Export project as NIfTI": images, lesions (one mask per segment) and maps into ``folder`` (default
    ``<project>/Exports/nifti``), with BIDS-like names when the names allow it.
    """
    folder = folder or projectExportFolder(create=False)
    if not folder:
        raise ExportError(_("No project is open"))
    return planExport(projectNodes(), folder, extension, segmentationMode=SEGMENTATION_MASKS, mapOptions=mapOptionsFromMapModule(), bids=True)


def plannedFiles(jobs):
    files = []
    for job in jobs:
        files += job.files()
    return files


def existingFiles(jobs, extra=()):
    """The files of ``jobs`` (and ``extra``) that already exist: the user is asked before they are replaced."""
    return [path for path in [*plannedFiles(jobs), *extra] if os.path.exists(path)]


#
# Running
#


class Result:
    """What an export wrote: ``files`` (all paths), ``images`` (NIfTI paths), ``notes`` and ``errors`` (texts)."""

    def __init__(self, folder=None):
        self.folder = folder
        self.files = []
        self.images = []
        self.notes = []
        self.errors = []

    def add(self, written):
        self.files += written
        self.images += [path for path in written if splitExtension(path)[1]]


def runJobs(jobs, overwrite=False, readme=None, progress=None):
    """Run ``jobs`` now (main thread). Return a ``Result``.

    :param overwrite: replace existing files; else ``FileExistsError`` is raised before anything is written.
    :param readme: ``(path, title)``: also write a README listing the files (project exports).
    :param progress: ``progress(done, total, title)``, called before each job.
    """
    extra = [readme[0]] if readme else []
    if not overwrite:
        existing = existingFiles(jobs, extra)
        if existing:
            raise FileExistsError(existing[0])
    result = Result(os.path.dirname(jobs[0].path) if jobs else None)
    for index, job in enumerate(jobs):
        if progress is not None:
            progress(index, len(jobs), job.title)
        try:
            tasks = job.prepare()
            for task in tasks:
                result.add(_writeWithSlicer(task) if task.data is None else task.write())
        except Exception as e:
            logging.error(f"Slicer Studio: NIfTI export of {job.nodeName} failed: {e}")
            result.errors.append(f"{job.nodeName}: {e}")
        result.notes += job.notes
    if readme:
        writeReadme(readme[0], jobs, result, readme[1])
        result.files.append(readme[0])
    if progress is not None:
        progress(len(jobs), len(jobs), "")
    return result


def exportNodes(nodes, folder, extension=".nii.gz", segmentationMode=SEGMENTATION_LABELMAP, reference=None, overwrite=False, mapOptions=None):
    """Export ``nodes`` into ``folder`` now; return the ``Result``. A script-friendly shortcut of ``planExport`` + ``runJobs``."""
    return runJobs(planExport(nodes, folder, extension, segmentationMode, reference, mapOptions), overwrite=overwrite)


def exportVolume(node, path, overwrite=False, threshold=None, sign="both"):
    """Export one scalar or label map volume (or map, optionally with a thresholded mask) to ``path``; return the ``Result``."""
    path = withExtension(os.path.abspath(str(path)))
    kind = kindOf(node)
    if kind is None or kind == KIND_SEGMENTATION:
        raise ExportError(_("{name} is not an image").format(name=node.GetName() if node is not None else ""))
    options = {"threshold": threshold, "sign": sign} if kind == KIND_MAP and threshold is not None else {}
    return runJobs([Job(node, kind, path, options)], overwrite=overwrite)


def exportSegmentation(node, path, mode=SEGMENTATION_LABELMAP, reference=None, overwrite=False):
    """Export a segmentation as one label map (``path``) or as one mask per segment (``<base>_label-<name>_mask``, see ``maskBaseName``)."""
    path = withExtension(os.path.abspath(str(path)))
    if kindOf(node) != KIND_SEGMENTATION:
        raise ExportError(_("{name} is not a segmentation").format(name=node.GetName() if node is not None else ""))
    referenceId = reference.GetID() if hasattr(reference, "GetID") else reference
    return runJobs([Job(node, KIND_SEGMENTATION, path, {"mode": mode, "reference": referenceId})], overwrite=overwrite)


def writeReadme(path, jobs, result, title):
    """README.txt of a project export: what each file is, its space and codes, and how to use the files."""
    lines = [title, "=" * len(title), "", _("Written by {version} on {date}.").format(version=studioVersion(), date=_nowIso()), "",
             _("Every image is a NIfTI-1 file whose affine (sform = qform) is 3D Slicer's IJK to RAS matrix: FSL, SPM, AFNI, nibabel"),
             _("and official 3D Slicer show left and right as Slicer Studio shows them. Each image has a .json sidecar."),
             _("sform/qform codes: 4 = MNI152 (a template grid), 2 = aligned (a transform was applied), 1 = scanner."), "", _("Files:")]
    written = set(result.images)
    for job in jobs:
        for image in job.images():
            if image not in written:
                continue
            name = os.path.basename(image)
            sidecar = sidecarPath(image)
            description = ""
            try:
                with open(sidecar, encoding="utf-8") as f:
                    content = json.load(f)
                description = f"{content.get('Kind', '')}, {content.get('Space', '')}, code {content.get('SformCode', '')}"
                if content.get("Segment"):
                    description += f", segment {content['Segment']}"
            except (OSError, ValueError):
                pass
            lines.append(f"- {name}: {job.nodeName} ({description})")
    if result.notes:
        lines += ["", _("Notes:")] + [f"- {note}" for note in result.notes]
    if result.errors:
        lines += ["", _("Not exported:")] + [f"- {error}" for error in result.errors]
    templates = [node.GetName() for node in exportableNodes() if kindOf(node) == KIND_TEMPLATE]
    if templates:
        lines += ["", _("Templates are not copied (they are public files known by name): {names}.").format(names=", ".join(templates))]
    _writeText(path, "\n".join(lines) + "\n")
    return path

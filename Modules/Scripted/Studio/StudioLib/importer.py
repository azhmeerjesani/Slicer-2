"""Direct loading of files in Slicer Studio, and "Start on MNI template" (SPEC 3.1, "Import rules").

Unambiguous files load directly, without the Add Data dialog:

- a name that looks like a lesion (``isLesionName()``: lesion, mask, seg, label or roi as a whole word)
  is loaded as a segmentation. In a label map image (a NIfTI, NRRD or MetaImage mask), the first segment
  becomes "Lesion" (#F2545B); the other labels keep the names given by the reader ("Segment_2"...). An empty
  mask gets an empty "Lesion" segment to draw into. Files that store segments (``.seg.nrrd``, ``.seg.nhdr``,
  ``.seg.vtm``, or a NRRD file with segment fields, as Slicer writes them) are never changed: their segments
  keep their names, colors and terminology, whatever their number (SPEC 9.7: a file saved again after it was
  opened in Studio has the same content);
- other NIfTI, NRRD and MetaImage files are loaded as scalar volumes and shown in all slice views;
- a 4D NIfTI image (fMRI, a 4D mask: ``isNifti4D()``), which the volume reader refuses, is loaded as a sequence (as
  3D Slicer's Add Data does), whatever its name; the volume that shows its current time point is the scan;
- ``.mrb`` and ``.mrml`` files are loaded as scenes. They keep their own layout and views; a scene opened in
  Library is shown in Draw (the Library page covers the views). In Studio with a project open, a scene opens as its
  own project instead of being added to the open one (A-09: the open project is saved and closed first).

In Studio, a scan or lesion file that is already loaded (same real path as the file of a node in the scene) is
not loaded a second time: it becomes current again, with the toast "<name> is already open". Classic keeps the
official behavior (the file is loaded again).

Anything else (other file types, several folders, Shift+drop) opens the classic Add Data dialog with the
dropped paths. A DICOM folder goes to the DICOM module; a folder with several NIfTI files is offered as
a card in the Library, whose primary action is "Create study" (W3-07, ``StudioLib.study_ui``); a study folder
(with a ``study.json``) opens the study. While a study is open, "Start on MNI template" adds a case to the study on
its template, and ``loadedSet()`` is None (the study replaces the loaded set). In Studio, loading a scan switches to Draw, on the lesion loaded with it or on a new
"Lesions (<scan name>)" segmentation bound to that scan (never on the segmentation of another scan, even if
it has the same geometry, as scans normalized to MNI space do).

Studio import routes (USABILITY 2, 6.4, 7, 8, 13; U-07). Nothing here hides a follow-up action or changes data:

- a **figure** (PNG, JPEG, TIFF, BMP: a case-report picture) is shown large in the Yellow view of the Compare
  layout (7005), next to the template (Red, where you draw). It is tagged ``Studio.Role`` = "figure", never becomes
  the Segment Editor source, cannot be painted into (interactor guard) and never follows linked views or the
  wheel off its single slice. A notice asks on which side of the figure the patient's right is; "Right" mirrors the
  figure pane. Removing the figure restores the previous layout and the Yellow view. On a template the figure also
  opens for tracing (``StudioLib.figure``, L-29): the Figure card of Draw crops it, fits it to a cut plane of the
  template with landmarks and shows it over the template; on a scan in scan space it stays this reference only;
- a **statistical map** (``*_statmap``, ``*_desc-T_*``... or a NIfTI intent code TTEST, ZSCORE, CORREL) is shown as a
  thresholded two-sided overlay with a colour legend on the anatomy, with one threshold control in a notice;
- a **lesion without a scan** is shown on the template only when it is on an MNI grid (``space.spaceOf()``), on its
  own grid (the template is only the source); a lesion in scan space offers its scan instead (the ``Source`` of its
  BIDS-style sidecar). Several lesions loaded at once are a **loaded set** (``loadedSet()``), shown one at a time;
- a scan loaded from the Sample Data or DICOM module opens in Draw like a scan opened from Library;
- a **new lesion** is only created on request (``newLesionSegmentation()``), with Undo;
- the 1 mm template is offered in the inspector notice slot after the first stroke, never over the views;
- ``participants.tsv`` never opens the Add Data dialog; opened files and folders are recorded for Recent.

This module is an optional plug-in of the Studio shell: ``install(shell)`` handles the files dropped on
the main window while Studio is shown, ``uninstall(shell)`` restores the classic drop handling.
The loading functions also work in Classic (``shell=None``): they only load the files there (figures and
tables go to the Add Data dialog, as in 3D Slicer).
"""

import gzip
import json
import logging
import os
import re
import struct
import sys
import time
import traceback
import zlib

import qt
import slicer
import vtk
from slicer.i18n import tr as _
from slicer.util import VTKObservationMixin

from . import space

#: Whole-word lesion names (SPEC 3.1): "sub-01_lesion.nii.gz", "mask.nrrd", "brain.seg.nrrd"...
LESION_NAME_PATTERN = re.compile(r"(?i)(^|[_\-.])(lesion|mask|seg|label|roi)([_\-.]|$)")

#: Extensions of scalar volumes that load without options (lowercase)
VOLUME_EXTENSIONS = (".nii", ".nii.gz", ".nrrd", ".nhdr", ".mha", ".mhd")
NIFTI_EXTENSIONS = (".nii", ".nii.gz")
SCENE_EXTENSIONS = (".mrb", ".mrml")
#: Pictures (case-report figures): shown as a reference next to the template, never edited
FIGURE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")
#: Tables (participants.tsv)
TABLE_EXTENSIONS = (".tsv", ".csv")
#: Names of statistical maps (BIDS-derivative and LNM outputs): "sub-05_desc-T_funcmap.nii.gz", "x_statmap.nii"...
STATMAP_NAME_PATTERN = re.compile(r"(?i)(_statmap|_funcmap|(^|_)desc-(T|Z|AvgR|AvgRFz|Fz|tmap|zmap)(_|\.))")
#: NIfTI-1 intent codes of statistical maps: CORREL, TTEST, ZSCORE
STATMAP_INTENT_CODES = (2, 3, 5)
#: BIDS subject entity of a file name ("sub-05")
BIDS_SUBJECT_PATTERN = re.compile(r"(?i)(?:^|[_\-./\\])(sub-[a-z0-9]+)")
#: Segmentation files that store segment names and colors: they are kept as they are
SEGMENTATION_FILE_EXTENSIONS = (".seg.nrrd", ".seg.nhdr", ".seg.vtm")
#: Field of a segment in the header of a NRRD file written by Slicer ("Segment0_Name:=Tumor")
SEGMENT_FIELD_PATTERN = re.compile(rb"^Segment\d+_(ID|Name|Color|Tags|LabelValue):=", re.MULTILINE)
#: At most this many bytes of a NRRD file are read to find segment fields
NRRD_HEADER_MAX_BYTES = 1 << 20

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
#: A loaded set holds 2 to this many lesions loaded at once (USABILITY 2, "Library, loaded set")
MAX_LOADED_SET_LESIONS = 60

#: Rows of a dropped table that are counted at most
MAX_TABLE_ROWS_COUNTED = 100000

#: Compare layout (USABILITY 6.6, 13): the template axial in Red (55 %, where you draw) and a figure in Yellow (45 %).
#: Inside the fork range 7001-7099 that the layout node writes portably (SPEC 9.7 rule 3); 7004 is reserved (L-29).
COMPARE_LAYOUT_ID = 7005
COMPARE_LAYOUT_TITLE = "Studio Compare"
COMPARE_LAYOUT_XML = (
    '<layout type="horizontal" split="true">'
    '<item splitSize="550">'
    '<view class="vtkMRMLSliceNode" singletontag="Red">'
    '<property name="orientation" action="default">Axial</property>'
    '<property name="viewlabel" action="default">R</property>'
    '<property name="viewcolor" action="default">#F34A33</property>'
    "</view>"
    "</item>"
    '<item splitSize="450">'
    '<view class="vtkMRMLSliceNode" singletontag="Yellow">'
    '<property name="orientation" action="default">Axial</property>'
    '<property name="viewlabel" action="default">Y</property>'
    '<property name="viewcolor" action="default">#EDD54C</property>'
    "</view>"
    "</item>"
    "</layout>")
#: The view that shows a figure, and the view group that keeps it out of linked view changes while it does (scenes
#: store any view group above 0 as 1)
FIGURE_VIEW_NAME = "Yellow"
FIGURE_VIEW_GROUP = 1
#: Width of the figure pane (D-U4, until L-29): at least 460 px, 700 px on a screen 1920 px wide or more. The template
#: view where you draw keeps at least ``TEMPLATE_PANE_MIN_WIDTH``.
FIGURE_PANE_MIN_WIDTH = 460
FIGURE_PANE_MIN_WIDTH_LARGE = 700
FIGURE_PANE_LARGE_SCREEN_WIDTH = 1920
TEMPLATE_PANE_MIN_WIDTH = 240

#: Settings (fork-only keys, USABILITY 17)
TEMPLATE_1MM_OFFER_KEY = "Studio/Template1mmOffer"
RECENT_OPEN_TIMES_KEY = "Studio/RecentOpenTimes"
RECENT_FOLDERS_KEY = "Studio/RecentFolders"
MAX_RECENT_OPEN_TIMES = 50
MAX_RECENT_FOLDERS = 10

#: Node attribute: the reference image geometry of a lesion when it was loaded (W2-05 saves on it)
LESION_GRID_ATTRIBUTE = "Studio.LesionGrid"
#: Node attribute: the lesion was read from a file with fractional values (a probabilistic mask), as "<min> <max>"
LESION_FRACTIONAL_ATTRIBUTE = "Studio.LesionFractional"
#: Keys of a lesion's sidecar that Draw's Lesion card edits, and their node attributes (``draw.NOTES_ATTRIBUTE``,
#: ``draw.SOURCE_REFERENCE_ATTRIBUTE``)
LESION_SIDECAR_TEXT_ATTRIBUTES = (("Notes", "Studio.LesionNotes"), ("SourceReference", "Studio.LesionSourceReference"))

#: Statistical map overlay: name of its colour table, opacity, and the default threshold (fraction of the window)
STATMAP_COLOR_NODE_NAME = "StudioStatMapDiverging"
STATMAP_OPACITY = 0.6
STATMAP_THRESHOLD_FRACTION = 0.25
STATMAP_WINDOW_PERCENTILE = 99.5

#: Inspector notices of the importer, by priority: a notice of higher priority is never replaced by a lower one
NOTICE_PRIORITIES = {"figure-convention": 50, "statmap": 40, "native-lesion": 30, "lesion-space": 20, "template-missing": 15, "template1mm": 10}

#: Modules that load data by themselves (a scan loaded there opens in Draw)
TRANSIENT_LOADER_MODULES = ("SampleData", "DICOM")
#: Volumes added within this time are one load (multi-volume samples are left where they are)
TRANSIENT_LOAD_WINDOW_MS = 500


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
    """How a dropped or opened path is loaded: "folder", "scene", "sequence", "lesion", "statmap", "volume", "figure", "table"
    or "other".

    A 4D NIfTI image (fMRI, a 4D mask) is a "sequence" whatever its name: the volume reader cannot read it. Lesion names
    have priority over statistical map names.
    """
    if os.path.isdir(path):
        return "folder"
    extension = _extension(path)
    if extension in SCENE_EXTENSIONS:
        return "scene"
    if extension in SEGMENTATION_FILE_EXTENSIONS:
        return "lesion"
    if extension in NIFTI_EXTENSIONS and isNifti4D(path):
        return "sequence"
    if extension in VOLUME_EXTENSIONS:
        if isLesionName(path):
            return "lesion"
        if extension in NIFTI_EXTENSIONS and isStatMap(path):
            return "statmap"
        return "volume"
    if extension in FIGURE_EXTENSIONS:
        return "figure"
    if extension in TABLE_EXTENSIONS:
        return "table"
    return "other"


def isNiftiFile(path):
    return _extension(path) in NIFTI_EXTENSIONS


def _niftiHeader(path, size):
    """The first ``size`` bytes of a NIfTI file (gzip or not), None if unreadable.

    A damaged file is unreadable, never an exception: gzip raises ``OSError`` (``BadGzipFile``) for a file that is not
    gzip, ``EOFError`` for a truncated one and ``zlib.error`` for a corrupt deflate stream.
    """
    try:
        opener = gzip.open if _extension(path) == ".nii.gz" or str(path).lower().endswith(".gz") else open
        with opener(path, "rb") as f:
            return f.read(size)
    except (OSError, EOFError, zlib.error):
        return None


def niftiDimensions(path):
    """The ``dim`` field of a NIfTI-1 or NIfTI-2 header (``dim[0]`` is the number of dimensions), None if unreadable."""
    header = _niftiHeader(path, 540)
    if header is None or len(header) < 348:
        return None
    for byteOrder in ("<", ">"):
        sizeOfHeader = struct.unpack(byteOrder + "i", header[0:4])[0]
        if sizeOfHeader == 348:
            return list(struct.unpack(byteOrder + "8h", header[40:56]))
        if sizeOfHeader == 540 and len(header) >= 540:
            return list(struct.unpack(byteOrder + "8q", header[16:80]))
    return None


def isNifti4D(path):
    """True for a NIfTI image with several time points (``dim[4] > 1``): a time series such as fMRI, read as a sequence."""
    if _extension(path) not in NIFTI_EXTENSIONS or not os.path.isfile(path):
        return False
    dims = niftiDimensions(path)
    return dims is not None and 4 <= dims[0] <= 7 and dims[4] > 1


def niftiIntentCode(path):
    """The intent code of a NIfTI-1 file (header bytes 68-69, in the byte order given by ``sizeof_hdr``), None if unreadable."""
    header = _niftiHeader(path, 348)
    if header is None or len(header) < 348:
        return None
    for byteOrder in ("<", ">"):
        if struct.unpack(byteOrder + "i", header[0:4])[0] == 348:
            return struct.unpack(byteOrder + "h", header[68:70])[0]
    return None


def isStatMap(path):
    """True for a NIfTI statistical map: a name like ``*_statmap``, ``*_funcmap`` or ``*_desc-T_*``, or a NIfTI intent code
    TTEST (3), ZSCORE (5) or CORREL (2).
    """
    if not isNiftiFile(path):
        return False
    if STATMAP_NAME_PATTERN.search(os.path.basename(path)):
        return True
    return niftiIntentCode(path) in STATMAP_INTENT_CODES


def bidsSubject(text):
    """The BIDS subject entity in a file or node name ("sub-05"), None if there is none."""
    match = BIDS_SUBJECT_PATTERN.search(str(text or ""))
    return match.group(1) if match else None


def readTableHeader(path, countRows=True):
    """``(columns, rowCount)`` of a TSV or CSV file (its first non-empty line is the header), None if it cannot be read.

    Rows are counted up to ``MAX_TABLE_ROWS_COUNTED`` (a large file dropped by mistake is not read to its end); without
    ``countRows`` only the header is read and ``rowCount`` is None.
    """
    header = None
    rowCount = 0
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                if header is None:
                    header = line.rstrip("\r\n")
                    if not countRows:
                        break
                    continue
                rowCount += 1
                if rowCount >= MAX_TABLE_ROWS_COUNTED:
                    break
    except OSError:
        return None
    if header is None:
        return None
    delimiter = "\t" if "\t" in header or _extension(path) == ".tsv" else ","
    columns = [column.strip().strip('"') for column in header.split(delimiter)]
    return columns, (rowCount if countRows else None)


def isParticipantsTable(path):
    """True for a TSV or CSV file whose header has a ``participant_id`` column (a BIDS participants table)."""
    if _extension(path) not in TABLE_EXTENSIONS:
        return False
    header = readTableHeader(path, countRows=False)
    return header is not None and "participant_id" in header[0]


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
    ``figures`` and ``statmaps`` are the reference images of the load (Studio only), ``tables`` the participants
    tables that were recognized.
    """

    def __init__(self):
        self.volumes = []
        self.segmentations = []
        self.scenes = []
        self.addDataPaths = []
        self.errors = []
        self.alreadyOpen = []
        self.figures = []
        self.statmaps = []
        self.tables = []
        #: A lesion that the scan of this load was opened for ("Open with patient_T1.nii.gz")
        self.pairedLesion = None

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


def storesSegments(path):
    """True if the file stores segments with their names, colors and terminology.

    These are the segmentation files (``.seg.nrrd``, ``.seg.nhdr``, ``.seg.vtm``, ``.vtm``) and the NRRD files
    whose header has segment fields (a segmentation that Slicer saved as ``.nrrd``). A label map image (NIfTI,
    MetaImage, NRRD without segment fields) does not: its segments are named by the reader ("Segment_1"...).
    """
    extension = _extension(path)
    if extension in SEGMENTATION_FILE_EXTENSIONS or extension == ".vtm":
        return True
    if extension not in (".nrrd", ".nhdr"):
        return False
    try:
        with open(path, "rb") as f:
            header = f.read(NRRD_HEADER_MAX_BYTES)
    except OSError:
        return False
    # The header ends at the first empty line (the data follows it in a .nrrd file)
    header = re.split(rb"\r?\n\r?\n", header, maxsplit=1)[0]
    return SEGMENT_FIELD_PATTERN.search(header) is not None


def _applyLesionNaming(segmentationNode, path):
    """Name the first segment of a label map image "Lesion", with the lesion color.

    Files that store segments (``storesSegments()``) are never changed, whatever their number of segments:
    their names, colors and terminology were chosen by whoever saved them, and a scene saved after the file was
    opened must keep them (SPEC 9.7). An empty mask (no segment) gets an empty "Lesion" segment, so that Draw has
    a segment to draw into.
    """
    if storesSegments(path):
        return
    segmentation = segmentationNode.GetSegmentation()
    if segmentation.GetNumberOfSegments() == 0:
        segmentId = segmentation.AddEmptySegment("", LESION_SEGMENT_NAME, LESION_COLOR)
        styleLesionSegment(segmentationNode, segmentId)
        return
    segmentId = segmentation.GetNthSegmentID(0)
    segmentation.GetSegment(segmentId).SetName(LESION_SEGMENT_NAME)
    styleLesionSegment(segmentationNode, segmentId)


#: Lesions whose voxels ``loadLesion()`` set after the reader (a fractional file): {node ID: (weak reference, voxel stamp)}.
#: The storage node's read time is older than these voxels, which are still the file as read (``unchangedSinceLoad()``).
_convertedOnLoad = {}


def _voxelStamp(segmentationNode):
    """(segment count, newest binary labelmap MTime): changes when any segment's voxels change (``draw._contentStamp``)."""
    segmentation = segmentationNode.GetSegmentation()
    binaryName = slicer.vtkSegmentationConverter.GetBinaryLabelmapRepresentationName()
    newest = 0
    for index in range(segmentation.GetNumberOfSegments()):
        representation = segmentation.GetNthSegment(index).GetRepresentation(binaryName)
        if representation is not None:
            newest = max(newest, representation.GetMTime())
    return segmentation.GetNumberOfSegments(), newest


def unchangedSinceLoad(segmentationNode):
    """True when ``loadLesion()`` set the voxels of this lesion from its fractional file and they were not changed since."""
    if segmentationNode is None:
        return False
    record = _convertedOnLoad.get(segmentationNode.GetID())
    if record is None or record[0].Get() is not segmentationNode:
        return False
    return record[1] == _voxelStamp(segmentationNode)


def _fractionalMask(path):
    """``(mask, (low, high))`` for a NIfTI lesion file with fractional values (a probabilistic mask), else None.

    ``mask`` is the bool array [i, j, k] of the voxels above 0, in file voxel order; ``low``/``high`` the value range.
    Slicer's readers cast an image to an integer type by truncation, so such a file would open empty (all values below
    1) or with only its voxels equal to 1. Only float (or scaled) images are read here: an integer mask costs a header.
    """
    if not str(path).lower().endswith(NIFTI_EXTENSIONS):
        return None
    try:
        import importlib

        import numpy as np

        nifti = importlib.import_module("LesionNetworkMappingLib.nifti")
        header = nifti.read_nifti_header(path)
        slope, inter = header["scl_slope"], header["scl_inter"]
        scaled = np.isfinite(slope) and slope != 0 and (slope != 1 or (np.isfinite(inter) and inter != 0))
        if header["dtype"].kind != "f" and not scaled:
            return None
        data, _affine, _header = nifti.read_nifti(path)
    except Exception:
        logging.warning(f"Studio: the values of {path} could not be checked:\n{traceback.format_exc()}")
        return None
    if data.ndim > 3 and all(n == 1 for n in data.shape[3:]):
        data = data.reshape(data.shape[:3])
    if data.ndim != 3:
        return None
    values = data[np.isfinite(data) & (data != 0)]
    if not values.size or np.array_equal(values, np.round(values)):
        return None
    return np.isfinite(data) & (data > 0), (float(values.min()), float(values.max()))


def _setFractionalMask(segmentationNode, mask):
    """Make the segmentation one segment (Segment_1, label 1) of the voxels of ``mask`` on its reference geometry.

    Return False (nothing changed) when the reference geometry of the segmentation is not the grid of ``mask``.
    """
    import vtk.util.numpy_support

    segmentation = segmentationNode.GetSegmentation()
    geometry = segmentation.GetConversionParameter(slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName())
    labelmap = slicer.vtkOrientedImageData()
    if not geometry or not slicer.vtkSegmentationConverter.DeserializeImageGeometry(geometry, labelmap, True, vtk.VTK_UNSIGNED_CHAR, 1):
        return False
    if tuple(labelmap.GetDimensions()) != tuple(mask.shape):
        return False
    scalars = vtk.util.numpy_support.vtk_to_numpy(labelmap.GetPointData().GetScalars())
    scalars[:] = mask.transpose(2, 1, 0).ravel()
    labelmap.Modified()
    segmentation.RemoveAllSegments()
    if mask.any():
        segment = slicer.vtkSegment()
        segment.SetName(LESION_SEGMENT_NAME)
        segment.SetColor(*LESION_COLOR)
        segment.SetLabelValue(1)
        segment.AddRepresentation(slicer.vtkSegmentationConverter.GetBinaryLabelmapRepresentationName(), labelmap)
        segmentation.AddSegment(segment, "Segment_1")
    return True


def _nameSegmentationAfterFile(segmentationNode, path):
    """Name a segmentation node that was read from a volume file (``x.nii.gz``) after the file without its extension.

    The segmentation reader keeps ".nii" in the node name of "x.nii.gz". Used by ``loadLesion()`` and by
    ``loadRecentFile()``, so that a lesion has the same name (and project file name) however it was opened.
    """
    baseName = _baseName(path)
    if baseName and segmentationNode.GetName() != baseName and _extension(path) in VOLUME_EXTENSIONS:
        segmentationNode.SetName(slicer.mrmlScene.GenerateUniqueName(baseName))


def loadLesion(path):
    """Load a lesion file as a segmentation (see ``_applyLesionNaming()`` for the "Lesion" segment).

    Image files that the segmentation reader cannot read are loaded as a labelmap volume and converted. A NIfTI file
    with fractional values (a probabilistic mask, ``desc-lesionProb``) is shown with every voxel above 0 as the lesion,
    not truncated to integers by the reader; the node gets ``LESION_FRACTIONAL_ATTRIBUTE`` and the file is unchanged.
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
    fractional = None if storesSegments(path) else _fractionalMask(path)
    if fractional is not None:
        mask, (low, high) = fractional
        if _setFractionalMask(segmentationNode, mask):
            segmentationNode.SetAttribute(LESION_FRACTIONAL_ATTRIBUTE, f"{low:g} {high:g}")
            logging.info(f"Studio: {path} has fractional values ({low:g} to {high:g}): the voxels above 0 are the lesion")
        else:
            logging.warning(f"Studio: {path} has fractional values but its grid is not the segmentation's: values below 1 are lost")
    _nameSegmentationAfterFile(segmentationNode, path)
    _applyLesionNaming(segmentationNode, path)
    if segmentationNode.GetAttribute(LESION_FRACTIONAL_ATTRIBUTE):
        # The mask is how the file reads as a lesion, not an edit: Draw says "Saved · <file>" as for a 0/1 file
        reference = vtk.vtkWeakReference()
        reference.Set(segmentationNode)
        _convertedOnLoad[segmentationNode.GetID()] = (reference, _voxelStamp(segmentationNode))
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


# Result of the last voxel scans of isSegmentationEmpty(), keyed by the modification times of the labelmap and of its
# scalars (vtkTimeStamp values are unique in the process): the scan is O(voxels), the answer is asked on every refresh.
_labelValueScanCache = {}
_LABEL_VALUE_SCAN_CACHE_SIZE = 64
_LABEL_VALUE_SCAN_BLOCK_VOXELS = 1 << 22


def _labelmapHasLabelValue(labelmap, labelValue):
    """True if ``labelmap`` has a voxel with ``labelValue``.

    The scan runs in blocks and stops at the first hit, so it allocates no full-size temporary array and a lesion
    costs a fraction of the image. The answer is cached until the labelmap or its scalars are modified.
    """
    import vtk.util.numpy_support

    scalars = labelmap.GetPointData().GetScalars()
    key = (labelmap.GetMTime(), scalars.GetMTime(), labelValue)
    cached = _labelValueScanCache.get(key)
    if cached is not None:
        return cached
    array = vtk.util.numpy_support.vtk_to_numpy(scalars)
    found = False
    blockSize = max(1, _LABEL_VALUE_SCAN_BLOCK_VOXELS)
    for start in range(0, array.shape[0], blockSize):
        if (array[start:start + blockSize] == labelValue).any():
            found = True
            break
    if len(_labelValueScanCache) >= _LABEL_VALUE_SCAN_CACHE_SIZE:
        _labelValueScanCache.clear()
    _labelValueScanCache[key] = found
    return found


def isSegmentationEmpty(segmentationNode):
    """True if no segment of the segmentation has any voxel (or the segmentation has no segment)."""
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
        if _labelmapHasLabelValue(labelmap, segmentation.GetSegment(segmentId).GetLabelValue()):
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
    if sourceVolumeNode is not None and _referenceKind(sourceVolumeNode) is None:
        # A figure or a statistical map is never the source (USABILITY P12)
        editor.setSourceVolumeNode(sourceVolumeNode)
    segmentation = segmentationNode.GetSegmentation()
    if segmentation.GetNumberOfSegments() > 0:
        editor.setCurrentSegmentID(segmentation.GetNthSegmentID(0))


def _isReference(node):
    """True for a figure or a statistical map: shown for comparison, never edited and never a Segment Editor source."""
    return node is not None and node.GetAttribute(space.ROLE_ATTRIBUTE) in (space.ROLE_FIGURE, space.ROLE_STATMAP)


def _referenceKind(node):
    """"figure" or "statmap" for a volume that is a reference image, None otherwise.

    Tagged volumes (``Studio.Role``), and untagged ones that a route of 3D Slicer loaded (the "Recently loaded" menu, the
    Add Data dialog): the file is a picture or a statistical map, or the volume is a colour picture with a single slice.
    """
    if node is None or not node.IsA("vtkMRMLVolumeNode") or node.IsA("vtkMRMLLabelMapVolumeNode"):
        return None
    role = node.GetAttribute(space.ROLE_ATTRIBUTE)
    if role in (space.ROLE_FIGURE, space.ROLE_STATMAP):
        return role
    storageNode = node.GetStorageNode()
    fileName = storageNode.GetFileName() if storageNode is not None else None
    if fileName:
        kind = fileKind(fileName)
        if kind in ("figure", "statmap"):
            return kind
    image = node.GetImageData()
    if node.IsA("vtkMRMLVectorVolumeNode") and image is not None and 1 in image.GetDimensions():
        return "figure"
    return None


def _backgroundVolume():
    """The background volume of the first slice view that has one (figure views are skipped), or None."""
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None:
        return None
    for viewName in layoutManager.sliceViewNames():
        compositeNode = layoutManager.sliceWidget(viewName).mrmlSliceCompositeNode()
        volumeNode = slicer.mrmlScene.GetNodeByID(compositeNode.GetBackgroundVolumeID()) if compositeNode.GetBackgroundVolumeID() else None
        if volumeNode is not None and not _isReference(volumeNode):
            return volumeNode
    return None


def _isScan(node):
    return node.IsA("vtkMRMLScalarVolumeNode") and not node.IsA("vtkMRMLLabelMapVolumeNode")


def _isTemplate(node):
    """True for a template volume of "Start on MNI template" (tagged, or named like one in a scene saved before the tag)."""
    if node is None:
        return False
    if node.GetAttribute(space.ROLE_ATTRIBUTE) == space.ROLE_TEMPLATE:
        return True
    return node.GetName() in (_templateNodeName(TEMPLATE_SAMPLE_2MM), _templateNodeName(TEMPLATE_SAMPLE_1MM))


def _userScanShown():
    """The scan shown in the slice views when it is the user's own (not a template, a figure or a map), else None."""
    background = _backgroundVolume()
    return background if background is not None and not _isTemplate(background) else None


def _figureCompositeNodes():
    return [node for node in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode")
            if node.GetBackgroundVolumeID() and space.isFigure(slicer.mrmlScene.GetNodeByID(node.GetBackgroundVolumeID()))]


def setViewLayers(background=None, foreground=None, foregroundOpacity=None, fit=False):
    """``slicer.util.setSliceViewerLayers()`` for every slice view except figure views: a figure stays where it is shown.

    Only the given layers change. With ``fit``, the slice views of the layout (not the figure views) are fitted.
    """
    figureIds = {node.GetID() for node in _figureCompositeNodes()}
    for compositeNode in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
        if compositeNode.GetID() in figureIds:
            continue
        if background is not None:
            compositeNode.SetBackgroundVolumeID(background.GetID())
        if foreground is not None:
            compositeNode.SetForegroundVolumeID(foreground.GetID())
        if foregroundOpacity is not None:
            compositeNode.SetForegroundOpacity(foregroundOpacity)
    if fit:
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None:
            for name in layoutManager.sliceViewNames():
                sliceWidget = layoutManager.sliceWidget(name)
                if sliceWidget is not None and sliceWidget.mrmlSliceCompositeNode().GetID() not in figureIds:
                    sliceWidget.sliceLogic().FitSliceToAll()


def _normalizedPath(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def openNodeForFile(path):
    """The scan or segmentation of the scene that was loaded from ``path`` (same real path), or None.

    Only nodes shown to the user are considered: scans (not label maps) and segmentations whose storage node
    reads that file. A node whose file the open project took over (its storage node now names the copy in the project) is
    found by the file it was read from (``_projectSourceNodes()``).
    """
    if not path:
        return None
    target = _normalizedPath(path)
    for className in ("vtkMRMLScalarVolumeNode", "vtkMRMLSegmentationNode"):
        for node in slicer.util.getNodesByClass(className):
            if _shownNodeForFile(node) is None:
                continue
            storageNode = node.GetStorageNode()
            fileName = storageNode.GetFileName() if storageNode is not None else None
            if fileName and _normalizedPath(fileName) == target:
                return node
    for sequenceNode in slicer.util.getNodesByClass("vtkMRMLSequenceNode"):
        # A 4D image (``loadSequence()``): the volume that shows its current item
        storageNode = sequenceNode.GetStorageNode()
        fileName = storageNode.GetFileName() if storageNode is not None else None
        if fileName and _normalizedPath(fileName) == target:
            proxyNode = _shownNodeForFile(sequenceNode)
            if proxyNode is not None:
                return proxyNode
    for node in _projectSourceNodes(path):
        shown = _shownNodeForFile(node)
        if shown is not None:
            return shown
    return None


def _shownNodeForFile(node):
    """The node shown to the user for ``node`` read from a file: a scan (not a label map) or a segmentation, the proxy scan
    of a sequence; None for anything else.
    """
    if node is None:
        return None
    if node.IsA("vtkMRMLSequenceNode"):
        proxyNode = _sequenceProxy(node)
        return proxyNode if proxyNode is not None and _isScan(proxyNode) else None
    if node.GetHideFromEditors():
        return None
    if node.IsA("vtkMRMLScalarVolumeNode"):
        return node if _isScan(node) else None
    return node if node.IsA("vtkMRMLSegmentationNode") else None


def _projectSourceNodes(path):
    """Nodes of the scene read from ``path``, a file outside the open project that the project took over (A-09: the project
    copies or writes it into its own folders and its storage node names that file from then on).
    """
    try:
        from . import autosave

        service = autosave.instance()
    except ImportError:
        return []
    writer = getattr(service, "projectWriter", None) if service is not None else None
    if writer is None or not hasattr(writer, "nodeIdsForSource"):
        return []
    nodes = (slicer.mrmlScene.GetNodeByID(nodeId) for nodeId in writer.nodeIdsForSource(path))
    return [node for node in nodes if node is not None]


def _sequenceProxy(sequenceNode):
    """The node that shows the current item of ``sequenceNode`` (its first sequence browser's proxy), None if none."""
    browserNode = slicer.modules.sequences.logic().GetFirstBrowserNodeForSequenceNode(sequenceNode)
    return browserNode.GetProxyNode(sequenceNode) if browserNode is not None else None


def loadSequence(path):
    """Load a 4D image (fMRI, a 4D mask) as a sequence, as 3D Slicer's Add Data does, and return the volume that shows its
    current time point (the sequence browser's proxy node).
    """
    sequenceNode = slicer.util.loadNodeFromFile(path, "SequenceFile")
    proxyNode = _sequenceProxy(sequenceNode)
    if proxyNode is None or not proxyNode.IsA("vtkMRMLVolumeNode"):
        raise RuntimeError(_("{path} was loaded as a sequence but has no volume to show").format(path=path))
    return proxyNode


def _openReference(path, role):
    """The figure or statistical map (``role``) of the scene that was loaded from ``path`` (same real path), or None."""
    if not path:
        return None
    target = _normalizedPath(path)
    for node in slicer.util.getNodesByClass("vtkMRMLVolumeNode"):
        if node.GetAttribute(space.ROLE_ATTRIBUTE) != role:
            continue
        storageNode = node.GetStorageNode()
        fileName = storageNode.GetFileName() if storageNode is not None else None
        if fileName and _normalizedPath(fileName) == target:
            return node
    for node in _projectSourceNodes(path):
        if node.IsA("vtkMRMLVolumeNode") and node.GetAttribute(space.ROLE_ATTRIBUTE) == role:
            return node
    return None


#
# Inspector notices
#

#: Notices of the importer waiting for the inspector notice slot: key -> widget (see ``NOTICE_PRIORITIES``)
_pendingNotices = {}
_noticeRetryTimer = None


def _widgetAlive(widget):
    try:
        widget.objectName
    except Exception:
        return False
    return True


def _postNotice(key, widget, shell=None):
    """Show ``widget`` in the inspector notice slot (Draw) when no notice of a higher priority is waiting.

    A notice line built here that ``widget`` replaces (the same key) is released: nothing else holds it, and a widget without a
    parent that is left to the interpreter's exit can call Python event filters while Python is finalized (a crash on quit). The
    widgets of ``_OWNED_NOTICE_KEYS`` belong to their owner (the download banner, the map overlay) and are never released here.
    """
    previous = _pendingNotices.get(key)
    _pendingNotices[key] = widget
    _refreshNotices(shell)
    if previous is not None and previous is not widget and key not in _OWNED_NOTICE_KEYS:
        releaseWidget(previous)


#: Notice keys whose widgets have another owner (``DownloadBanner.widget``, ``_StatMapOverlay.widget``)
_OWNED_NOTICE_KEYS = ("template1mm", "statmap")


#: Signals of notice controls that hold Python callbacks
_RELEASED_SIGNALS = (("QAbstractButton", "clicked()"), ("QDoubleSpinBox", "valueChanged(double)"), ("QSlider", "valueChanged(int)"))


def releaseWidget(widget, owner=None):
    """Hide a notice widget and delete it once the current event is processed.

    The Python callbacks of its controls are disconnected first. PythonQt would otherwise release them while the C++
    widget is destroyed, and a Python object freed then (``owner``, kept alive until the release) could delete wrappers
    of widgets that are being destroyed. The release is deferred because a notice is often closed from one of its own
    buttons.
    """
    if widget is None or not _widgetAlive(widget):
        return
    widget.hide()
    holder = [widget, owner]

    def release():
        target = holder[0]
        if _widgetAlive(target):
            for className, signal in _RELEASED_SIGNALS:
                for child in target.findChildren(className):
                    child.disconnect(signal)
            target.deleteLater()
        holder.clear()

    qt.QTimer.singleShot(0, release)


def _deleteNow(widget):
    """Delete a widget now (application exit), its controls' Python callbacks disconnected first."""
    if widget is None or not _widgetAlive(widget):
        return
    for className, signal in _RELEASED_SIGNALS:
        for child in widget.findChildren(className):
            child.disconnect(signal)
    if widget.parent() is None:
        # Python owns a widget without a parent: delete it while the application exists (not at interpreter exit), without the
        # Python event filters that its destruction would call
        from . import ui

        ui.removeStudioEventFilters(widget)
        widget.delete()


def _disposeOnQuit():
    """Release what the importer holds before the application quits: notice widgets, overlays, observers, the offer."""
    global _figureController, _transientWatcher, _loadedSet
    for widget in list(_pendingNotices.values()):
        _deleteNow(widget)
    _pendingNotices.clear()
    for overlay in list(_statMaps.values()):
        _deleteNow(overlay.widget)
    _statMaps.clear()
    if _loadedSet is not None:
        _loadedSet._unwatch()
    _loadedSet = None
    if _templateOfferWatcher is not None:
        _templateOfferWatcher.dispose()
    if _templateBanner is not None:
        widget = _templateBanner.widget
        _templateBanner.dispose()
        _deleteNow(widget)
    if _transientWatcher is not None:
        _transientWatcher.dispose()
        _transientWatcher = None
    if _figureController is not None:
        _figureController.dispose()
        _figureController = None


def _withdrawNotice(key, shell=None, delete=True):
    """Remove a notice of the importer (the slot then shows the next waiting one). ``delete``: delete its widget."""
    widget = _pendingNotices.pop(key, None)
    shell = _currentShell(shell)
    if shell is not None:
        shell.clearInspectorNotice(key)
    if delete and widget is not None:
        releaseWidget(widget)
    _refreshNotices(shell)


def noticeKey(shell=None):
    """Key of the importer's notice shown in the slot ("figure-convention", "statmap"...), None otherwise."""
    shell = _currentShell(shell)
    if shell is None:
        return None
    key = shell.inspectorNoticeKey()
    return key if key in _pendingNotices else None


def noticeWidget(key):
    """The widget of a waiting or shown notice of the importer, None if there is none."""
    widget = _pendingNotices.get(key)
    return widget if widget is not None and _widgetAlive(widget) else None


def _refreshNotices(shell=None):
    global _noticeRetryTimer
    for key in [key for key, widget in _pendingNotices.items() if not _widgetAlive(widget)]:
        _pendingNotices.pop(key)
    shell = _currentShell(shell)
    if shell is None:
        return
    currentKey = shell.inspectorNoticeKey()
    current = shell.inspectorNotice()
    if current is not None and currentKey not in NOTICE_PRIORITIES:
        # A notice of another owner (crash recovery...) is never replaced: try again once it is gone
        if _noticeRetryTimer is None:
            _noticeRetryTimer = qt.QTimer()
            _noticeRetryTimer.setSingleShot(True)
            _noticeRetryTimer.setInterval(1000)
            _noticeRetryTimer.connect("timeout()", _refreshNotices)
        if _pendingNotices:
            _noticeRetryTimer.start()
        return
    if not _pendingNotices:
        if currentKey in NOTICE_PRIORITIES:
            shell.setInspectorNotice(None)
        return
    key = max(_pendingNotices, key=lambda name: NOTICE_PRIORITIES.get(name, 0))
    widget = _pendingNotices[key]
    if currentKey != key or current is not widget:
        shell.setInspectorNotice(widget, key=key, workspace="draw")


def _clearNotices(shell=None):
    """Hide the importer's notice (Classic); the waiting notices are kept and shown again by ``_refreshNotices()``."""
    shell = _currentShell(shell) if shell is None else shell
    if shell is not None and shell.inspectorNoticeKey() in _pendingNotices:
        shell.setInspectorNotice(None)


def _noticeFrame(objectName, text, actions=(), warning=False):
    """``ui.noticeLine()`` with an object name; ``warning``: amber text (a space line)."""
    from . import ui

    frame = ui.noticeLine(text, actions)
    frame.setObjectName(objectName)
    label = frame.findChild("QLabel", "StudioNoticeText")
    if label is not None:
        label.setObjectName(objectName + "Text")
        label.setTextFormat(qt.Qt.PlainText)
    color = "warning" if warning else "text"
    ui.bindStyleSheet(frame, lambda: (
        f"QFrame#{objectName} {{ background-color: {ui.token('accent-subtle').name()}; border: none; border-bottom: 1px solid {ui.token('hairline').name()}; }}"
        f"QLabel#{objectName}Text {{ color: {ui.token(color).name()}; background: transparent; }}"))
    return frame


def _noticeStack(objectName, text, actions):
    """A notice whose actions do not fit next to its text: the text on one line, the buttons under it (wrapped rows).

    The container is height-for-width with an Ignored horizontal policy, like ``ui.noticeLine()``.
    """
    from . import ui

    container = qt.QFrame()
    container.setObjectName(objectName)
    layout = qt.QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 4)
    layout.setSpacing(0)
    line = ui.noticeLine(text)
    label = line.findChild("QLabel", "StudioNoticeText")
    if label is not None:
        label.setObjectName(objectName + "Text")
        label.setTextFormat(qt.Qt.PlainText)
    layout.addWidget(line)
    buttons = []
    for buttonText, callback in actions:
        button = qt.QToolButton()
        button.setObjectName("StudioNoticeButton")
        button.setText(buttonText)
        button.setAutoRaise(True)
        button.setFocusPolicy(qt.Qt.TabFocus)
        button.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
        button.connect("clicked()", callback)
        ui.setRole(button, "ghost")
        ui.installButtonKeys(button)
        buttons.append(button)
    if buttons:
        row = ui.wrapRow(buttons, width=2 * ui.WRAP_ROW_WIDTH, spacing=2)
        row.setContentsMargins(6, 0, 8, 0)
        layout.addWidget(row)
    policy = qt.QSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    policy.setHeightForWidth(True)
    container.setSizePolicy(policy)
    ui.bindStyleSheet(line, lambda: f"QFrame#StudioNoticeLine {{ background: transparent; border: none; }} QLabel#{objectName}Text {{ color: {ui.token('text').name()}; background: transparent; }}")
    ui.bindStyleSheet(container, lambda: (
        f"QFrame#{objectName} {{ background-color: {ui.token('accent-subtle').name()}; border: none; border-bottom: 1px solid {ui.token('hairline').name()}; }}"))
    return container


def _noticeButton(frame, text):
    for button in frame.findChildren("QToolButton"):
        if str(button.text) == str(text):
            return button
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


def loadFiles(paths, shell=None, pairedLesion=None):
    """Load files directly (see the module documentation) and return a ``LoadResult``.

    If a path is a folder or a file of another type, nothing is loaded and the Add Data dialog opens with
    all the paths. In Studio, the first scan becomes the background of the slice views, the Segment Editor
    is prepared on it (with the lesion file of the same load, if any) and Draw is shown. Figures, statistical maps
    and participants tables take their own routes (module documentation). ``pairedLesion``: a lesion that the scan
    of this load belongs to ("Open with patient_T1.nii.gz"): Draw edits it on the scan.
    """
    shell = _currentShell(shell)
    result = LoadResult()
    result.pairedLesion = pairedLesion
    paths = [os.path.abspath(str(path)) for path in paths]
    if not paths:
        return result
    kinds = [fileKind(path) for path in paths]
    if shell is None:
        # Classic: 3D Slicer's routes (a picture or a table goes to Add Data, a statistical map is a volume)
        kinds = ["volume" if kind == "statmap" else kind for kind in kinds]
        if any(kind not in ("scene", "volume", "sequence", "lesion") for kind in kinds):
            result.addDataPaths = paths
            openAddDataDialog(paths)
            return result
    else:
        tables = [path for path, kind in zip(paths, kinds, strict=True) if kind == "table" and isParticipantsTable(path)]
        if tables:
            # A participants table never opens the Add Data dialog (its "Color" reader would read it as a colour table)
            for path in tables:
                _openParticipantsTable(path, shell)
            result.tables = tables
            kept = [(path, kind) for path, kind in zip(paths, kinds, strict=True) if path not in tables]
            paths, kinds = [path for path, _kind in kept], [kind for _path, kind in kept]
            if not paths:
                return result
        if any(kind not in ("scene", "volume", "sequence", "lesion", "figure", "statmap") for kind in kinds):
            result.addDataPaths = paths
            openAddDataDialog(paths)
            return result

    loadedPaths = []
    with slicer.util.WaitCursor():
        # Scenes first (they may replace nodes), then scans (a 4D image is a sequence), then lesions (shown over the scans)
        for wantedKind in ("scene", "volume", "sequence", "lesion"):
            for path, kind in zip(paths, kinds, strict=True):
                if kind != wantedKind:
                    continue
                openNode = openNodeForFile(path) if shell is not None and kind != "scene" else None
                if openNode is not None:
                    # In Studio, a file is not loaded twice: its node becomes current again
                    result.alreadyOpen.append(openNode)
                    loadedPaths.append(path)
                    continue
                try:
                    if kind == "scene":
                        if not _loadScene(path, shell):
                            continue  # not opened (for example a project locked by another computer): already announced
                        result.scenes.append(path)
                    elif kind == "volume":
                        result.volumes.append(slicer.util.loadVolume(path, {"singleFile": True}))
                    elif kind == "sequence":
                        result.volumes.append(loadSequence(path))
                    else:
                        segmentationNode = loadLesion(path)
                        restoreSidecarText(segmentationNode, path)
                        result.segmentations.append(segmentationNode)
                    loadedPaths.append(path)
                except Exception as e:
                    logging.error(f"Studio: cannot load {path}:\n{traceback.format_exc()}")
                    result.errors.append((path, str(e)))
    _afterLoad(result, shell)
    if shell is not None:
        statmapPaths = [path for path, kind in zip(paths, kinds, strict=True) if kind == "statmap"]
        figurePaths = [path for path, kind in zip(paths, kinds, strict=True) if kind == "figure"]
        for path in statmapPaths:
            if _routeStatMap(path, shell, result):
                loadedPaths.append(path)
        if figurePaths:
            if _openFigure(figurePaths[0], shell, result):
                loadedPaths.append(figurePaths[0])
            if len(figurePaths) > 1:
                logging.info(f"Studio: only the first figure is shown ({len(figurePaths)} were opened)")
    if loadedPaths:
        recordOpenTimes(loadedPaths)
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


def _setLesionVisible(segmentationNode, visible):
    displayNode = segmentationNode.GetDisplayNode() if segmentationNode is not None else None
    if displayNode is not None and bool(displayNode.GetVisibility()) != bool(visible):
        displayNode.SetVisibility(bool(visible))


def _afterLoad(result, shell):
    """Show what was loaded: scans in the slice views; in Studio, the lesion in Draw and a toast.

    Nodes of files that were already open (``result.alreadyOpen``) become current again: the scan is shown,
    and Draw keeps editing its lesion if it already does. A scene opened in Library is shown in Draw with its own
    layout and views (the Library page covers the views). A lesion loaded without a scan is routed by its space
    (``_showLesionWithoutScan()``): never bound to the template unless it is on an MNI grid.
    """
    scans = [node for node in result.volumes if _isScan(node) and not _isReference(node)]
    openScans = [node for node in result.alreadyOpen if _isScan(node)]
    openLesions = [node for node in result.alreadyOpen if node.IsA("vtkMRMLSegmentationNode")]
    if shell is None:
        if scans:
            slicer.util.setSliceViewerLayers(background=scans[0], fit=True)
        elif openScans and not _sameNode(_backgroundVolume(), openScans[0]):
            slicer.util.setSliceViewerLayers(background=openScans[0], fit=True)
        return
    # A figure or a statistical map that a route of 3D Slicer loaded (the "Recently loaded" menu) is never shown as a
    # scan: it takes its own route once the scans are shown
    references = [node for node in result.volumes if not _isReference(node) and _referenceKind(node) is not None]
    referenceIds = {node.GetID() for node in references}
    if references:
        result.volumes = [node for node in result.volumes if node.GetID() not in referenceIds]
    scans = [node for node in scans if node.GetID() not in referenceIds]
    openScans = [node for node in openScans if _referenceKind(node) is None]
    _showLoaded(result, shell, scans, openScans, openLesions)
    for node in references:
        _routeReferenceNode(node, shell, result)


def _figureModule():
    """``StudioLib.figure`` (trace from a figure, L-29), None if this build does not have it."""
    try:
        from . import figure
    except ImportError:
        return None
    return figure


def _openFigure(path, shell, result):
    """A figure on a template opens for tracing (``figure.openFigure``: Compare layout first, with the Figure card); on a
    scan in scan space it stays a reference in the Compare layout (U-07's route, the non-Trace fallback).
    """
    figure = _figureModule()
    if figure is not None:
        try:
            if figure.canTrace():
                return figure.openFigure(path, shell, result) is not None
        except Exception:
            logging.error(f"Studio: cannot open {path} for figure tracing:\n{traceback.format_exc()}")
    return _routeFigure(path, shell, result)


def _routeReferenceNode(volumeNode, shell, result):
    """Route a figure or a statistical map that is already loaded (not tagged yet)."""
    storageNode = volumeNode.GetStorageNode()
    path = storageNode.GetFileName() if storageNode is not None else None
    if _referenceKind(volumeNode) == "statmap":
        return _routeStatMap(path, shell, result, mapNode=volumeNode)
    return _routeFigure(path, shell, result, figureNode=volumeNode)


def _forgetLoadedSetFor(segmentationNode):
    """A lesion outside the current loaded set is edited: the set is no longer current and its lesions are hidden."""
    if _loadedSet is not None and segmentationNode is not None and not _loadedSet.contains(segmentationNode):
        _loadedSet.forget(hideLesions=True)


def _showLoaded(result, shell, scans, openScans, openLesions):
    """Studio part of ``_afterLoad()``: the scans and lesions of a load in Draw."""
    if scans:
        setViewLayers(background=scans[0], fit=True)
    elif openScans and not _sameNode(_backgroundVolume(), openScans[0]):
        setViewLayers(background=openScans[0], fit=True)
    for lesion in result.segmentations:
        recordLesionGrid(lesion)
    editedSegmentation, editedSource = _editedNodes()
    lesions = result.segmentations or openLesions
    if result.pairedLesion is not None and not lesions and slicer.mrmlScene.IsNodePresent(result.pairedLesion):
        lesions = [result.pairedLesion]
    if not scans and not openScans and lesions and _userScanShown() is None and not (
            _sameNode(lesions[0], editedSegmentation) and editedSource is not None and not _isTemplate(editedSource)):
        # A lesion without a scan of its own: shown on the template only when it is on an MNI grid
        _showLesionsWithoutScan(lesions, shell, newlyLoaded=bool(result.segmentations))
        _loadToast(result, shell)
        return
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
            if scans or openScans:
                # A scan of the user: the lesions of a loaded set (MNI masks) are not shown over it
                _forgetLoadedSetFor(lesion)
            if lesions:
                # A lesion opened with its scan is shown (it may have been hidden while it had no scan), and bound to it
                # when it is on the scan's grid (its own grid is never changed)
                _setLesionVisible(lesion, True)
                _bindToScanGrid(lesion, sourceVolume)
                _withdrawNotice("native-lesion", shell)
                _updateLesionSpaceNotice(lesion, sourceVolume, shell)
            if not (_sameNode(lesion, editedSegmentation) and _sameNode(sourceVolume, editedSource)):
                selectInSegmentEditor(lesion, sourceVolume)
        shell.switchTo("draw", keepLayout=_figureController is not None and _figureController.isShown())
    elif lesions:
        # A segmentation without a scan to draw on: Draw shows it (the Library page would cover the views)
        if segmentEditorWidget() is not None and not _sameNode(lesions[0], editedSegmentation):
            selectInSegmentEditor(lesions[0], None)
        shell.switchTo("draw")
    elif result.scenes and shell.currentWorkspace() == "library":
        # The Library page covers the views: Draw shows the scene, in the scene's own layout
        shell.switchTo("draw", keepLayout=True)
    _loadToast(result, shell)


def _bindToScanGrid(segmentationNode, volumeNode, tolerance=1e-3):
    """Bind a lesion to a scan (reference geometry node reference) when the lesion is on the scan's grid.

    A lesion saved on the grid of its scan and opened again with it has the same grid, up to the float precision of the
    NIfTI header: it is bound to the scan, as if it had been drawn on it. A lesion on another grid keeps its own grid
    and stays unbound. Return True if the lesion is bound to the scan.
    """
    if segmentationNode is None or volumeNode is None or _isReference(volumeNode) or _isTemplate(volumeNode):
        return False
    if _isBoundTo(segmentationNode, volumeNode):
        return True
    lesionGrid, scanGrid = space.gridOf(segmentationNode), space.gridOf(volumeNode)
    if lesionGrid is None or scanGrid is None or tuple(lesionGrid[0]) != tuple(scanGrid[0]):
        return False
    import numpy as np

    if not np.allclose(lesionGrid[1], scanGrid[1], atol=tolerance):
        return False
    segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
    return True


def _loadToast(result, shell):
    fractional = [node for node in result.segmentations if node.GetAttribute(LESION_FRACTIONAL_ATTRIBUTE)]
    if fractional:
        # A probabilistic mask is not an ordinary 0/1 lesion: say how it is shown (a study offers Binarize at 0.5)
        shell.toast(_("{name} has fractional values (a probabilistic mask): every voxel above 0 is shown as the lesion. "
                      "The file is unchanged.").format(name=fractional[0].GetName()), ms=6000, level="warning")
        return
    held = getattr(shell, "consequenceHeld", None)
    if callable(held) and held():
        # A consequence of the load ("Opened a copy in Projects/x. The original is not changed.") is on screen: a
        # plain "Loaded x" would replace it at once, and the user would never read it
        return
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


def _recordedFileType(properties):
    """The file type of a recent file entry: the recorded ``fileType``, else the type 3D Slicer gives to its file name."""
    properties = properties or {}
    fileType = properties.get("fileType")
    if not fileType and properties.get("fileName"):
        fileType = slicer.app.coreIOManager().fileType(str(properties["fileName"]))
    return str(fileType or "")


def loadRecentFile(properties, action=None, shell=None):
    """Load an entry of the recently loaded files (``fileParameters`` of a "RecentlyLoadedMenu" action).

    The file is loaded with its recorded properties (by triggering ``action`` if given). In Studio, a
    loaded scan is shown in Draw, as for ``loadFiles()``, and a scan or lesion that is already open is not
    loaded again (it becomes current). A scene keeps its own views; with a project open, it opens as its own project as
    with Open... (A-09: the open project is saved and closed first; a scene from outside the folder opens as a copy). A
    lesion recorded as a segmentation, a figure and a statistical map are opened as with ``loadFiles()`` (Open...).
    """
    shell = _currentShell(shell)
    fileName = str((properties or {}).get("fileName", ""))
    isScene = (properties or {}).get("fileType") == "SceneFile" or _extension(fileName) in SCENE_EXTENSIONS
    if shell is not None and not isScene and os.path.isfile(fileName) and fileKind(fileName) in ("figure", "statmap"):
        # A figure or a statistical map takes its own route (never a scan of Draw); an open one is shown again
        return loadFiles([fileName], shell)
    if shell is not None and not isScene and os.path.isfile(fileName) and fileKind(fileName) == "lesion" and _recordedFileType(properties) == "SegmentationFile":
        # A lesion opens as with Open...: a probabilistic mask is not truncated to integers by the reader (it would
        # open empty), and the notes of its sidecar are restored (loadLesion, restoreSidecarText)
        return loadFiles([fileName], shell)
    openNode = openNodeForFile(fileName) if shell is not None and not isScene else None
    if openNode is not None:
        result = LoadResult()
        result.alreadyOpen.append(openNode)
        _afterLoad(result, shell)
        recordOpenTimes([fileName])
        return result
    scene = slicer.mrmlScene
    nodeIdsBefore = {scene.GetNthNode(index).GetID() for index in range(scene.GetNumberOfNodes())}
    loaded = True
    if isScene:
        service = _sceneProjectService(shell)
        if service is not None and os.path.isfile(fileName):
            # As Open... and drop (_loadScene): the scene opens as its own project (A-09), the open project is saved and
            # closed first; triggering the recent file action would add the scene's data to the open project instead
            result = LoadResult()
            try:
                opened = bool(service.openSceneFile(fileName))
            except Exception as e:
                logging.error(f"Studio: cannot load {fileName}:\n{traceback.format_exc()}")
                result.errors.append((fileName, str(e)))
                slicer.util.errorDisplay(_("Failed to load {path}").format(path=fileName), detailedText=str(e))
                opened = False
            if opened:
                result.scenes.append(fileName)
                recordOpenTimes([fileName])
            _afterLoad(result, shell)
            return result
        _announceSceneSource(fileName)
    if action is not None:
        action.trigger()
    else:
        properties = dict(properties)
        fileType = properties.get("fileType") or slicer.app.coreIOManager().fileType(properties.get("fileName", ""))
        with slicer.util.WaitCursor():
            loaded = bool(slicer.app.coreIOManager().loadNodes(fileType, properties))
            if not loaded:
                slicer.util.errorDisplay(_("Failed to load {path}").format(path=properties.get("fileName", "")))
    result = LoadResult()
    if isScene:
        result.scenes.append(fileName)
        _afterLoad(result, shell)
        if loaded:
            recordOpenTimes([fileName])
        return result
    for index in range(scene.GetNumberOfNodes()):
        node = scene.GetNthNode(index)
        if node.GetID() in nodeIdsBefore or node.GetHideFromEditors():
            continue
        if node.IsA("vtkMRMLScalarVolumeNode"):
            result.volumes.append(node)
        elif node.IsA("vtkMRMLSegmentationNode"):
            if fileName:
                _nameSegmentationAfterFile(node, fileName)
            if fileName and isLesionName(fileName):
                _applyLesionNaming(node, fileName)
            result.segmentations.append(node)
    _afterLoad(result, shell)
    if result.nodes():
        recordOpenTimes([fileName])
    return result


def loadFolder(path, shell=None):
    """Load a folder: a study folder opens the study, DICOM goes to the DICOM module, several NIfTI files are offered in a
    card (with Create study), else the Add Data dialog.

    Return "study", "dicom", "nifti" or "addData".
    """
    shell = _currentShell(shell)
    studyUi = _studyUiModule()
    if studyUi is not None and os.path.isfile(os.path.join(path, "study.json")):
        studyUi.openStudy(path, shell)
        return "study"
    sceneFile = _folderScene(path)
    if sceneFile is not None:
        # A scene folder (a Slicer Studio project, or a scene that 3D Slicer saved with its data): open its scene
        loadFiles([sceneFile], shell)
        return "scene"
    niftiFiles, isDicom = scanFolder(path)
    if isDicom:
        if importDicomFolder(path, shell):
            recordFolder(path, "dicom", 0)
        return "dicom"
    if len(niftiFiles) >= 2:
        from . import library

        if library.showFolderCard(path, niftiFiles, shell):
            recordFolder(path, "nifti", len(niftiFiles))
            return "nifti"
    openAddDataDialog([path])
    return "addData"


def _loadScene(path, shell):
    """Load a scene file; return True if it is open.

    In Studio with a project open, the scene opens as its own project (A-09: the open project is saved and closed first; a
    scene from outside the Slicer Studio folder becomes a new project that starts as a copy). Otherwise its data is added
    to the open scene, as 3D Slicer's Add Data does.
    """
    service = _sceneProjectService(shell)
    if service is not None:
        return bool(service.openSceneFile(path))
    _announceSceneSource(path)
    slicer.util.loadScene(path, {})
    return True


def _sceneProjectService(shell):
    """The autosave service if a scene opened now in Studio opens as its own project (``opensSceneAsProject()``), else None
    (Classic, a study or no open project: the scene's data is added to the open scene, as in 3D Slicer).
    """
    if shell is None:
        return None
    try:
        from . import autosave

        service = autosave.instance()
    except ImportError:
        return None
    if service is not None and hasattr(service, "opensSceneAsProject") and service.opensSceneAsProject():
        return service
    return None


def _announceSceneSource(path):
    """Tell the autosave service which file a scene comes from (a .mrb is unpacked first): it names the project of a copy."""
    try:
        from . import autosave

        service = autosave.instance()
        if service is not None and hasattr(service, "setPendingSceneSource"):
            service.setPendingSceneSource(path)
    except Exception:
        logging.debug(f"Studio: {traceback.format_exc()}")


def _folderScene(path):
    """The scene file of a scene folder: a Slicer Studio project, or a folder with exactly one .mrml (3D Slicer's "Save" with
    data); None otherwise.
    """
    try:
        from . import projects

        if projects.isProjectFolder(path):
            return projects.sceneFile(path)
        scenes = [name for name in os.listdir(path) if name.lower().endswith(".mrml") and not name.startswith(".")]
    except (ImportError, OSError):
        return None
    return os.path.join(path, scenes[0]) if len(scenes) == 1 else None


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


def _folderFiles(path, limit=FOLDER_SCAN_MAX_FILES):
    files = []
    for directory, subdirectories, fileNames in os.walk(path):
        subdirectories[:] = sorted(name for name in subdirectories if not name.startswith("."))
        for fileName in sorted(fileNames):
            if not fileName.startswith("."):
                files.append(os.path.join(directory, fileName))
                if len(files) >= limit:
                    return files
    return files


def importDicomFolder(path, shell=None):
    """Import a DICOM folder into the DICOM database; in Studio, a folder of one series opens in Draw.

    Without a database, the default one is created as 3D Slicer does when a DICOM folder is dropped on it. A folder of
    several series is shown in the DICOM browser (a series loaded there opens in Draw too). Without the DICOM module,
    the Add Data dialog opens with the folder.
    """
    shell = _currentShell(shell)
    if not hasattr(slicer.modules, "dicom"):
        openAddDataDialog([path])
        return False
    from DICOMLib import DICOMUtils

    database = getattr(slicer, "dicomDatabase", None)
    if database is None or not database.isOpen:
        try:
            import DICOM

            DICOM.DICOMFileDialog.createDefaultDatabase()
        except Exception:
            logging.error(f"Studio: cannot create the DICOM database:\n{traceback.format_exc()}")
        database = getattr(slicer, "dicomDatabase", None)
    if database is None or not database.isOpen:
        slicer.util.selectModule("DICOM")
        slicer.util.errorDisplay(_("The DICOM database is not set up. Choose a database folder in the DICOM module, then import the folder again."))
        return False
    with slicer.util.WaitCursor():
        if not DICOMUtils.importDicom(path):
            slicer.util.selectModule("DICOM")
            return False
        seriesUIDs = []
        if shell is not None:
            try:
                seriesUIDs = sorted(DICOMUtils.seriesUIDsForFiles(_folderFiles(path)))
            except Exception:
                logging.error(f"Studio: cannot list the DICOM series of {path}:\n{traceback.format_exc()}")
    if shell is not None and len(seriesUIDs) == 1:
        try:
            with _ownLoad(), slicer.util.WaitCursor():
                nodeIds = DICOMUtils.loadSeriesByUID(seriesUIDs)
        except Exception:
            logging.error(f"Studio: cannot load the DICOM series of {path}:\n{traceback.format_exc()}")
            nodeIds = []
        result = LoadResult()
        for nodeId in nodeIds or []:
            node = slicer.mrmlScene.GetNodeByID(nodeId)
            if node is not None and node.IsA("vtkMRMLScalarVolumeNode") and _isScan(node):
                result.volumes.append(node)
        if result.volumes:
            _afterLoad(result, shell)
            return True
    # Several series (or none loadable): the DICOM browser lists them
    slicer.util.selectModule("DICOM")
    return True


def _studyUiModule():
    """``StudioLib.study_ui`` when studies are available in this build, else None."""
    try:
        from . import study_ui
    except ImportError:
        return None
    return study_ui if study_ui.isAvailable() else None


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
# Recent files and folders (Studio/RecentOpenTimes, Studio/RecentFolders)
#


def _jsonSetting(key, default):
    value = slicer.util.settingsValue(key, "")
    if not value:
        return default
    try:
        parsed = json.loads(str(value))
    except ValueError:
        return default
    return parsed if isinstance(parsed, type(default)) else default


def recentOpenTimes():
    """``{path: time.time()}`` of the files opened in Studio, at most ``MAX_RECENT_OPEN_TIMES`` (the newest)."""
    return _jsonSetting(RECENT_OPEN_TIMES_KEY, {})


def recordOpenTimes(paths, when=None):
    """Record that ``paths`` were opened now (``Studio/RecentOpenTimes``, a JSON object)."""
    paths = [os.path.abspath(str(path)) for path in paths if path]
    if not paths:
        return
    when = time.time() if when is None else when
    times = recentOpenTimes()
    for path in paths:
        times[path] = when
    newest = sorted(times.items(), key=lambda item: item[1], reverse=True)[:MAX_RECENT_OPEN_TIMES]
    qt.QSettings().setValue(RECENT_OPEN_TIMES_KEY, json.dumps(dict(newest)))


def recentFolders():
    """The folders opened in Studio, newest first: ``[{"path", "kind" ("nifti", "dicom"), "count", "time"}]``."""
    return [entry for entry in _jsonSetting(RECENT_FOLDERS_KEY, []) if isinstance(entry, dict) and entry.get("path")]


def recordFolder(path, kind, count):
    """Record an opened folder in ``Studio/RecentFolders`` (at most ``MAX_RECENT_FOLDERS``, newest first)."""
    path = os.path.abspath(str(path))
    folders = [entry for entry in recentFolders() if entry.get("path") != path]
    folders.insert(0, {"path": path, "kind": kind, "count": int(count), "time": time.time()})
    qt.QSettings().setValue(RECENT_FOLDERS_KEY, json.dumps(folders[:MAX_RECENT_FOLDERS]))


#
# Tables
#


def _openParticipantsTable(path, shell):
    """A participants table: imported into the open study, else offered as a new study (``study_ui``); without studies in
    this build, a toast that says what it holds.
    """
    study_ui = _studyUiModule()
    handler = getattr(study_ui, "openParticipantsTable", None) if study_ui is not None else None
    if handler is not None:
        try:
            handler(path, shell)
            return
        except Exception:
            logging.error(f"Studio: cannot open {path} as a study table:\n{traceback.format_exc()}")
    columns, rowCount = readTableHeader(path) or ([], 0)
    shown = [column for column in columns if column != "participant_id"][:4]
    columnText = ", ".join(shown) + ("…" if len(columns) - 1 > len(shown) else "")
    shell.toast(_("{name}: {n} participants ({columns}). Studies are not available in this build.").format(
        name=os.path.basename(path), n=rowCount, columns=columnText or "participant_id"), 6000)


#
# Lesions without a scan (USABILITY 2, "Draw, native-space lesion alone"; 8 rule 1)
#


def recordLesionGrid(segmentationNode):
    """Record the reference image geometry of a lesion as it was loaded (``Studio.LesionGrid``), once."""
    if segmentationNode is None or segmentationNode.GetAttribute(LESION_GRID_ATTRIBUTE):
        return
    geometry = space._referenceGeometry(segmentationNode)
    if geometry:
        segmentationNode.SetAttribute(LESION_GRID_ATTRIBUTE, geometry)


def _lesionFilePath(segmentationNode):
    storageNode = segmentationNode.GetStorageNode() if segmentationNode is not None else None
    fileName = storageNode.GetFileName() if storageNode is not None else None
    return fileName or None


def sidecarPath(lesionPath):
    """The BIDS-style sidecar of a lesion file: ``sub-01_lesion.json`` next to ``sub-01_lesion.nii.gz``."""
    return os.path.join(os.path.dirname(lesionPath), _baseName(lesionPath) + ".json")


def readSidecar(lesionPath):
    """The content of the sidecar of a lesion file (a dict), or None when there is none or it is not a JSON object."""
    if not lesionPath:
        return None
    try:
        with open(sidecarPath(lesionPath), encoding="utf-8") as f:
            sidecar = json.load(f)
    except (OSError, ValueError):
        return None
    return sidecar if isinstance(sidecar, dict) else None


def restoreSidecarText(segmentationNode, lesionPath):
    """The ``Notes`` and ``SourceReference`` of a lesion file's sidecar (written by Draw's Save lesion) on the lesion
    loaded from it: the Lesion card shows them and the next Save lesion keeps them. A value already on the node wins.
    """
    sidecar = readSidecar(lesionPath)
    if segmentationNode is None or sidecar is None:
        return
    for key, attribute in LESION_SIDECAR_TEXT_ATTRIBUTES:
        value = sidecar.get(key)
        if isinstance(value, str) and segmentationNode.GetAttribute(attribute) is None:
            segmentationNode.SetAttribute(attribute, value)


def sidecarSource(lesionPath):
    """The scan that the sidecar of a lesion file names, if that file exists: ``Source`` (D-U3), else the BIDS keys
    ``Sources`` (a list) and ``SpatialReference`` of study lesions. Paths are relative to the sidecar; BIDS URIs and
    URLs are not resolved.
    """
    sidecar = readSidecar(lesionPath)
    if sidecar is None:
        return None
    path = sidecarPath(lesionPath)
    candidates = [sidecar.get("Source"), sidecar.get("source")]
    sources = sidecar.get("Sources")
    candidates.extend(sources if isinstance(sources, list) else [sources])
    candidates.append(sidecar.get("SpatialReference"))
    for source in candidates:
        if not source or not isinstance(source, str) or source.startswith("bids:") or "://" in source:
            continue
        resolved = source if os.path.isabs(source) else os.path.normpath(os.path.join(os.path.dirname(path), source))
        if os.path.isfile(resolved):
            return resolved
    return None


def lesionSpaceLine(segmentationNode, sourceVolumeNode=None):
    """The amber space line of a lesion shown on a template (USABILITY 7.7), None when there is nothing to say.

    "Lesion grid MNI152NLin6Asym 2 mm, shown on MNI152NLin2009cAsym (a few mm apart)" for another MNI space than the
    template's; "Shown on the MNI template; the lesion is in scan space" for a lesion in scan space shown on request.
    """
    if segmentationNode is None:
        return None
    if sourceVolumeNode is None:
        editedSegmentation, editedSource = _editedNodes()
        sourceVolumeNode = editedSource if _sameNode(editedSegmentation, segmentationNode) else None
    if sourceVolumeNode is None or not _isTemplate(sourceVolumeNode):
        return None
    lesionSpace = space.spaceOf(segmentationNode)
    if lesionSpace is None:
        return _("Shown on the MNI template; the lesion is in scan space")
    templateSpace = space.spaceOf(sourceVolumeNode) or (space.TEMPLATE_SPACE, None)
    if lesionSpace[0] != templateSpace[0]:
        templates = _templatesModule() if sourceVolumeNode.GetAttribute(_TEMPLATE_ID_ATTRIBUTE) else None
        userTemplate = templates.userTemplateOfNode(sourceVolumeNode) if templates is not None else None
        templateText = f"{userTemplate.name} ({space.spaceLabel(templateSpace)})" if userTemplate is not None else templateSpace[0]
        return _("Lesion grid {lesion}, shown on {template} (a few mm apart)").format(lesion=space.spaceLabel(lesionSpace), template=templateText)
    return None


def _drawModule():
    import sys

    return sys.modules.get("StudioLib.draw")


def _updateLesionSpaceNotice(segmentationNode, sourceVolumeNode, shell):
    """Show the space line of the lesion (interim notice until the Draw Lesion card shows it, W2-05)."""
    draw = _drawModule()
    line = lesionSpaceLine(segmentationNode, sourceVolumeNode)
    if draw is not None and getattr(draw, "showsLesionSpaceLine", False):
        _withdrawNotice("lesion-space", shell)
        return
    if not line:
        _withdrawNotice("lesion-space", shell)
        return
    widget = noticeWidget("lesion-space")
    if widget is not None:
        label = widget.findChild("QLabel", "StudioLesionSpaceNoticeText")
        if label is not None and str(label.text) == line:
            _refreshNotices(shell)
            return
        _withdrawNotice("lesion-space", shell)
    frame = _noticeFrame("StudioLesionSpaceNotice", line, [(_("OK"), lambda: _withdrawNotice("lesion-space"))], warning=True)
    _postNotice("lesion-space", frame, shell)


def _ensureTemplateShown(shell=None):
    """The template volume (the 1 mm one when it was downloaded), loaded if needed and shown in the slice views."""
    cached1mm = cachedTemplatePath(TEMPLATE_SAMPLE_1MM)
    with slicer.util.WaitCursor():
        volumeNode = _templateVolume(TEMPLATE_SAMPLE_1MM, cached1mm) if cached1mm is not None else _templateVolume(TEMPLATE_SAMPLE_2MM)
        if not _isTemplateShown(volumeNode):
            setViewLayers(background=volumeNode, fit=True)
        trackedVolume = _templateNodes()[0]
        if not _sameNode(trackedVolume, volumeNode):
            _trackTemplate(volumeNode, None)
    return volumeNode


def nativeLesionOffer(segmentationNode):
    """What Draw offers for a lesion in scan space shown alone (W2-05's empty-state variant reads it):
    ``{"text", "sourcePath" (or None), "openWith()", "openScan()", "showOnTemplate()"}``.
    """
    sourcePath = sidecarSource(_lesionFilePath(segmentationNode))
    return {
        "text": _("This lesion is in scan space. Open the scan it was drawn on."),
        "sourcePath": sourcePath,
        "openWith": (lambda: openScanForLesion(segmentationNode, sourcePath)) if sourcePath else None,
        "openScan": lambda: openScanForLesion(segmentationNode),
        "showOnTemplate": lambda: showLesionOnTemplate(segmentationNode),
    }


def openScanForLesion(segmentationNode, path=None, shell=None):
    """Open the scan of a lesion in scan space (``path``, else a file dialog): Draw edits the lesion on it."""
    shell = _currentShell(shell)
    if path is None:
        directory = os.path.dirname(_lesionFilePath(segmentationNode) or "") or _lastDirectory()
        path = qt.QFileDialog.getOpenFileName(slicer.util.mainWindow(), _("Open the scan of the lesion"), directory,
                                              _("Scans") + " (*.nii *.nii.gz *.nrrd *.nhdr *.mha *.mhd)")
        if not path:
            return None
        _rememberDirectory(path)
    return loadFiles([path], shell, pairedLesion=segmentationNode)


def showLesionOnTemplate(segmentationNode, shell=None):
    """Show a lesion on the MNI template on request ("Show on the MNI template anyway"): it keeps its own grid."""
    shell = _currentShell(shell)
    template = _ensureTemplateShown(shell)
    _withdrawNotice("native-lesion", shell)
    _setLesionVisible(segmentationNode, True)
    selectInSegmentEditor(segmentationNode, template)
    if shell is not None:
        _updateLesionSpaceNotice(segmentationNode, template, shell)
        shell.switchTo("draw", keepLayout=_figureController is not None and _figureController.isShown())
    return template


def _showNativeLesion(segmentationNode, shell):
    """A lesion in scan space without its scan: never bound to the template; Draw offers its scan."""
    if _backgroundVolume() is not None:
        # A template (or nothing of the lesion's space) is shown: the lesion would be drawn at a wrong place
        _setLesionVisible(segmentationNode, False)
    widget = segmentEditorWidget()
    if widget is not None and widget.editor is not None:
        editor = widget.editor
        editor.setSegmentationNode(segmentationNode)
        editor.setSourceVolumeNode(None)
        segmentation = segmentationNode.GetSegmentation()
        if segmentation.GetNumberOfSegments() > 0:
            editor.setCurrentSegmentID(segmentation.GetNthSegmentID(0))
    _withdrawNotice("lesion-space", shell)
    offer = nativeLesionOffer(segmentationNode)
    draw = _drawModule()
    if draw is not None and callable(getattr(draw, "showNativeLesionState", None)):
        try:
            draw.showNativeLesionState(shell, offer)
            _withdrawNotice("native-lesion", shell)
            return
        except Exception:
            logging.error(f"Studio: the Draw native lesion state failed:\n{traceback.format_exc()}")
    actions = []
    if offer["openWith"] is not None:
        actions.append((_("Open with {name}").format(name=os.path.basename(offer["sourcePath"])), offer["openWith"]))
    actions.append((_("Open scan…"), offer["openScan"]))
    actions.append((_("Show on the MNI template anyway"), offer["showOnTemplate"]))
    _withdrawNotice("native-lesion", shell)
    _postNotice("native-lesion", _noticeStack("StudioNativeLesionNotice", offer["text"], actions), shell)


def _showLesion(segmentationNode, shell):
    """Show one lesion that has no scan: on the template (as its source) when it is on an MNI grid, else the offer of its scan."""
    if space.isMni(segmentationNode):
        template = _ensureTemplateShown(shell)
        _withdrawNotice("native-lesion", shell)
        _setLesionVisible(segmentationNode, True)
        selectInSegmentEditor(segmentationNode, template)
        _updateLesionSpaceNotice(segmentationNode, template, shell)
    else:
        _showNativeLesion(segmentationNode, shell)


def _nameAfterSubject(segmentationNode):
    """Name a lesion of a loaded set after the BIDS subject of its file ("sub-05 lesion"): node and single segment."""
    subject = bidsSubject(os.path.basename(_lesionFilePath(segmentationNode) or "")) or bidsSubject(segmentationNode.GetName())
    if not subject:
        return
    name = _("{subject} lesion").format(subject=subject)
    if segmentationNode.GetName() != name:
        segmentationNode.SetName(slicer.mrmlScene.GenerateUniqueName(name) if slicer.util.getFirstNodeByName(name) is not None else name)
    segmentation = segmentationNode.GetSegmentation()
    if segmentation.GetNumberOfSegments() == 1 and not storesSegments(_lesionFilePath(segmentationNode) or ""):
        segmentation.GetNthSegment(0).SetName(segmentationNode.GetName())


def _showLesionsWithoutScan(lesions, shell, newlyLoaded=True):
    global _loadedSet
    if newlyLoaded and 2 <= len(lesions) <= MAX_LOADED_SET_LESIONS:
        if _loadedSet is not None:
            _loadedSet.forget(hideLesions=True)
        for lesion in lesions:
            _nameAfterSubject(lesion)
        _loadedSet = LoadedSet(lesions, shell)
        _loadedSet.select(0)
    else:
        _forgetLoadedSetFor(lesions[0])
        _showLesion(lesions[0], shell)
    shell.switchTo("draw", keepLayout=_figureController is not None and _figureController.isShown())


#
# Loaded set (USABILITY 2, "Library, loaded set"): lesions loaded at once, shown one at a time
#

_loadedSet = None


class LoadedSet:
    """Lesions loaded by one "Load all" (or one drop), in file order, shown one at a time in Draw.

    ``select(i)`` shows lesion ``i`` (the others are hidden) and edits it; ``next()`` / ``previous()`` step (no
    wrap-around: a toast at the ends); ``close()`` removes exactly the nodes of the set.
    """

    def __init__(self, segmentationNodes, shell=None):
        self._ids = [node.GetID() for node in segmentationNodes]
        self.index = -1
        self._shell = shell
        self._editor = None
        self._closeTag = None
        self._watch()

    def nodes(self):
        """The lesions of the set that are still in the scene, in file order."""
        nodes = [slicer.mrmlScene.GetNodeByID(nodeId) for nodeId in self._ids]
        return [node for node in nodes if node is not None and node.IsA("vtkMRMLSegmentationNode")]

    def contains(self, segmentationNode):
        return segmentationNode is not None and segmentationNode.GetID() in self._ids and any(
            _sameNode(node, segmentationNode) for node in self.nodes())

    def __len__(self):
        return len(self.nodes())

    def _watch(self):
        """Follow the lesion that Draw edits: another lesion of the set becomes current, a lesion outside the set (or a
        closed scene) ends the set, so the breadcrumb never names a lesion that is not edited.
        """
        widget = segmentEditorWidget()
        editor = widget.editor if widget is not None else None
        if editor is not None:
            editor.connect("segmentationNodeChanged(vtkMRMLSegmentationNode*)", self._onEditedChanged)
            self._editor = editor
        self._closeTag = slicer.mrmlScene.AddObserver(slicer.mrmlScene.EndCloseEvent, self._onSceneClosed)

    def _unwatch(self):
        if self._editor is not None:
            try:
                self._editor.disconnect("segmentationNodeChanged(vtkMRMLSegmentationNode*)", self._onEditedChanged)
            except Exception:
                pass
            self._editor = None
        if self._closeTag is not None:
            slicer.mrmlScene.RemoveObserver(self._closeTag)
            self._closeTag = None

    def _onSceneClosed(self, caller=None, event=None):
        self.forget()

    def _onEditedChanged(self, segmentationNode=None):
        try:
            if _loadedSet is not self or segmentationNode is None:
                return
            nodes = self.nodes()
            index = next((i for i, node in enumerate(nodes) if _sameNode(node, segmentationNode)), None)
            if index is None:
                self.forget()
            elif index != self.index:
                # Another lesion of the set was chosen in the Segment Editor: it is the one shown
                self.index = index
                for other in nodes:
                    _setLesionVisible(other, _sameNode(other, segmentationNode))
                self._showBreadcrumb(nodes)
        except Exception:
            logging.error(f"Studio: loaded set update failed:\n{traceback.format_exc()}")

    def _showBreadcrumb(self, nodes):
        shell = _currentShell(self._shell)
        if shell is not None and 0 <= self.index < len(nodes):
            shell.setBreadcrumb(self.subject(nodes[self.index]), _("{i} of {n}").format(i=self.index + 1, n=len(nodes)),
                                onClick=lambda: shell.switchTo("library"))

    def current(self):
        nodes = self.nodes()
        return nodes[self.index] if 0 <= self.index < len(nodes) else None

    def subject(self, node):
        return bidsSubject(os.path.basename(_lesionFilePath(node) or "")) or bidsSubject(node.GetName()) or node.GetName()

    def select(self, index):
        """Show lesion ``index`` only, edit it (on the template when it is on an MNI grid), and name it in the breadcrumb."""
        nodes = self.nodes()
        if not nodes:
            return None
        index = max(0, min(int(index), len(nodes) - 1))
        self.index = index
        lesion = nodes[index]
        for other in nodes:
            if other is not lesion:
                _setLesionVisible(other, False)
        shell = _currentShell(self._shell)
        _setLesionVisible(lesion, True)
        if shell is not None:
            _showLesion(lesion, shell)
            self._showBreadcrumb(nodes)
        else:
            selectInSegmentEditor(lesion, _templateNodes()[0] if space.isMni(lesion) else None)
        return lesion

    def next(self):
        return self._step(1)

    def previous(self):
        return self._step(-1)

    def _step(self, delta):
        nodes = self.nodes()
        target = self.index + delta
        if not nodes:
            return None
        if target < 0 or target >= len(nodes):
            shell = _currentShell(self._shell)
            if shell is not None:
                shell.toast(_("First lesion of the set") if target < 0 else _("Last lesion of the set"))
            return None
        return self.select(target)

    def forget(self, hideLesions=False):
        """Stop being the current set (the nodes stay in the scene; ``hideLesions``: hidden in the views)."""
        global _loadedSet
        self._unwatch()
        if hideLesions:
            for node in self.nodes():
                _setLesionVisible(node, False)
        if _loadedSet is self:
            _loadedSet = None
            shell = _currentShell(self._shell)
            if shell is not None:
                shell.setBreadcrumb(None)

    def close(self):
        """Remove exactly the nodes of the set (with their display and storage nodes)."""
        nodes = self.nodes()
        editedSegmentation, _editedSource = _editedNodes()
        widget = segmentEditorWidget()
        if widget is not None and widget.editor is not None and any(_sameNode(node, editedSegmentation) for node in nodes):
            widget.editor.setSegmentationNode(None)
        self.forget()
        _withdrawNotice("native-lesion")
        _withdrawNotice("lesion-space")
        for node in nodes:
            related = [node.GetNthDisplayNode(i) for i in range(node.GetNumberOfDisplayNodes())]
            related.append(node.GetStorageNode())
            slicer.mrmlScene.RemoveNode(node)
            for other in related:
                if other is not None and slicer.mrmlScene.IsNodePresent(other):
                    slicer.mrmlScene.RemoveNode(other)


def _studySession():
    """The open study (``study_ui.session()``, W3-07), else None."""
    try:
        from . import study_ui
    except ImportError:
        return None
    return study_ui.session()


def loadedSet():
    """The current loaded set (``LoadedSet``), None when there is none or while a study is open (a study replaces it)."""
    global _loadedSet
    if _loadedSet is not None and not _loadedSet.nodes():
        _loadedSet.forget()
    if _loadedSet is not None and _studySession() is not None:
        return None
    return _loadedSet


#
# New lesion (only on request)
#


def _nextLesionName():
    used = {node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")}
    number = 2
    while f"{LESION_SEGMENT_NAME} {number}" in used:
        number += 1
    return f"{LESION_SEGMENT_NAME} {number}"


def newLesionSegmentation(volumeNode=None, name=None, shell=None):
    """Create a new lesion on the geometry of ``volumeNode`` (the Draw source, else the shown scan) and edit it.

    It is named by the next free "Lesion 2", "Lesion 3"... (the node and its single segment), the previous lesion is
    hidden in the slice views, and a consequence toast offers Undo (removes the new lesion and shows the previous one).
    Return the new segmentation node, None without a volume.
    """
    shell = _currentShell(shell)
    previousSegmentation, previousSource = _editedNodes()
    if volumeNode is None:
        volumeNode = previousSource if previousSource is not None else _backgroundVolume()
    if volumeNode is None or _isReference(volumeNode):
        return None
    name = name or _nextLesionName()
    segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", name)
    segmentationNode.CreateDefaultDisplayNodes()
    segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
    segmentId = segmentationNode.GetSegmentation().AddEmptySegment("", name, LESION_COLOR)
    styleLesionSegment(segmentationNode, segmentId)
    hidden = None
    if previousSegmentation is not None and not _sameNode(previousSegmentation, segmentationNode):
        displayNode = previousSegmentation.GetDisplayNode()
        if displayNode is not None and displayNode.GetVisibility2D():
            displayNode.SetVisibility2D(False)
            hidden = previousSegmentation.GetID()
    selectInSegmentEditor(segmentationNode, volumeNode)
    if shell is not None:
        previousId = previousSegmentation.GetID() if previousSegmentation is not None else None
        previousSourceId = previousSource.GetID() if previousSource is not None else None

        def undo():
            undoNewLesion(segmentationNode.GetID(), previousId, previousSourceId, hidden)

        shell.toast(_("New lesion {name}; the previous one is hidden").format(name=name), action=undo, actionText=_("Undo"), level="consequence")
    return segmentationNode


def undoNewLesion(newId, previousId=None, previousSourceId=None, hiddenId=None):
    """Undo ``newLesionSegmentation()``: remove the new lesion and edit (and show) the previous one again."""
    scene = slicer.mrmlScene
    node = scene.GetNodeByID(newId) if newId else None
    if node is not None:
        scene.RemoveNode(node)
    hidden = scene.GetNodeByID(hiddenId) if hiddenId else None
    if hidden is not None and hidden.GetDisplayNode() is not None:
        hidden.GetDisplayNode().SetVisibility2D(True)
    previous = scene.GetNodeByID(previousId) if previousId else None
    if previous is not None:
        selectInSegmentEditor(previous, scene.GetNodeByID(previousSourceId) if previousSourceId else None)


#
# Dialogs
#

FILE_DIALOG_FILTERS = ";;".join((
    _("Scans, lesions and figures") + " (*.nii *.nii.gz *.nrrd *.nhdr *.mha *.mhd *.seg.vtm *.png *.jpg *.jpeg *.tif *.tiff *.bmp)",
    _("Figures") + " (*.png *.jpg *.jpeg *.tif *.tiff *.bmp)",
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
#: Watches the template lesion until its first stroke, then offers the 1 mm template (``_TemplateOfferWatcher``)
_templateOfferWatcher = None


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


def _tagTemplate(volumeNode, sampleName):
    """Tag a template volume: ``Studio.Role`` = "template" and its space (``Studio.Space``)."""
    if volumeNode.GetAttribute(space.ROLE_ATTRIBUTE) != space.ROLE_TEMPLATE:
        volumeNode.SetAttribute(space.ROLE_ATTRIBUTE, space.ROLE_TEMPLATE)
    resolution = 1 if sampleName == TEMPLATE_SAMPLE_1MM else 2
    spaceValue = f"{space.TEMPLATE_SPACE} {resolution}"
    if volumeNode.GetAttribute(space.SPACE_ATTRIBUTE) != spaceValue:
        volumeNode.SetAttribute(space.SPACE_ATTRIBUTE, spaceValue)
    return volumeNode


def _templateVolume(sampleName, path=None, show=True):
    """The template volume already in the scene, else load it (from ``path``, else from SampleData).

    The volume is tagged as a template (``Studio.Role``, ``Studio.Space``). A volume loaded from ``path`` is never shown
    here (``show`` is kept for compatibility): the callers show it in the views that are not figure views.
    """
    source = templateSource(sampleName)
    if source is None:
        raise RuntimeError(_("The SampleData module is not available"))
    nodeName = source.nodeNames[0]
    for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        if node.GetName() == nodeName and not node.IsA("vtkMRMLLabelMapVolumeNode"):
            return _tagTemplate(node, sampleName)
    with _ownLoad():
        if path is not None:
            return _tagTemplate(slicer.util.loadVolume(path, {"name": nodeName, "singleFile": True, "show": False}), sampleName)
        import SampleData

        volumeNode = SampleData.SampleDataLogic().downloadSample(sampleName)
    if volumeNode is None:
        raise RuntimeError(_("Failed to load {sampleName}").format(sampleName=sampleName))
    return _tagTemplate(volumeNode, sampleName)


def studyTemplateVolume(sampleName=TEMPLATE_SAMPLE_2MM):
    """The template volume of a study (a SampleData template: shared with "Start on MNI template", loaded once)."""
    with _ownLoad():
        return _templateVolume(sampleName)


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
    if volumeNode is None or not _isScan(volumeNode):
        return None, None
    if volumeNode.GetName() not in templateNames and not volumeNode.GetAttribute(_TEMPLATE_ID_ATTRIBUTE):
        # A template of the registry (yours, A-10) is tracked by its id attribute, whatever its name
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


def _isSavedAndUnchanged(segmentationNode):
    """True if the lesion has voxels and is saved to a file with no change since (Draw's "Saved" state)."""
    draw = _drawModule()
    lesionSaveState = getattr(draw, "lesionSaveState", None) if draw is not None else None
    if segmentationNode is None or lesionSaveState is None:
        return False
    try:
        state = lesionSaveState(segmentationNode)[0]
    except Exception:
        logging.error(f"Studio: the lesion save state failed:\n{traceback.format_exc()}")
        return False
    return state == "saved" and not isSegmentationEmpty(segmentationNode)


def _templateLesionFor(volumeNode, shell=None):
    """The lesion to draw on the template ``volumeNode`` for "Start on MNI template".

    The lesion of the last "Start on MNI template" is kept while it is bound to the template (a "Lesion 2" that it
    started too), else the template's "Lesions" segmentation (created if needed). A lesion that is saved and unchanged
    belongs to a finished case: a new lesion is started instead (``newLesionSegmentation()``: the saved one keeps its
    file and is hidden, Undo in the toast), so that the Save call to action, which re-saves silently to the file of
    the session's first save, can never overwrite the previous case. A lesion with unsaved changes (or never saved)
    is reused as it is: its strokes are never hidden and it is never re-targeted to another file.
    """
    trackedVolume, segmentationNode = _templateNodes()
    if segmentationNode is None or not _sameNode(trackedVolume, volumeNode):
        segmentationNode = _lesionSegmentationFor(volumeNode, LESION_SEGMENTATION_NAME)
    elif segmentationNode.GetSegmentation().GetNumberOfSegments() == 0:
        segmentId = segmentationNode.GetSegmentation().AddEmptySegment("", LESION_SEGMENT_NAME, LESION_COLOR)
        styleLesionSegment(segmentationNode, segmentId)
    if not _isSavedAndUnchanged(segmentationNode):
        return segmentationNode
    # Edit the saved lesion first, so that the new lesion hides it and its Undo edits it again
    selectInSegmentEditor(segmentationNode, volumeNode)
    newSegmentationNode = newLesionSegmentation(volumeNode, shell=shell)
    return newSegmentationNode if newSegmentationNode is not None else segmentationNode

#: ``Studio.TemplateId`` of template volumes (``templates.TEMPLATE_ID_ATTRIBUTE``; a portable id, never a path)
_TEMPLATE_ID_ATTRIBUTE = "Studio.TemplateId"


def _templatesModule():
    try:
        from . import templates

        return templates
    except Exception:
        logging.error(f"Studio: the template registry is not available:\n{traceback.format_exc()}")
        return None


def _chosenTemplate(templateId):
    """The registry template of "Start on template": ``templateId``, else the default; None for the plain built-in path
    (the bundled 2 mm brain, or the 1 mm T1w when it was downloaded, as before A-10).
    """
    templates = _templatesModule()
    if templates is None:
        return None
    if templateId is None:
        templateId = templates.defaultTemplateId()
        if templateId == templates.BUILTIN_DEFAULT:
            return None
    template = templates.templateById(templateId)
    if template is None or (template.id != templates.BUILTIN_DEFAULT and not template.isAvailable()):
        # Never the SampleData download of a built-in whose cached file is gone: templates are downloaded only on request
        raise RuntimeError(_("The template {id} is not on this computer").format(id=templateId))
    return template


def _tagRegistryTemplate(volumeNode, template):
    for attribute, value in ((space.ROLE_ATTRIBUTE, space.ROLE_TEMPLATE), (space.SPACE_ATTRIBUTE, template.studioSpaceValue()),
                             (_TEMPLATE_ID_ATTRIBUTE, template.id)):
        if volumeNode.GetAttribute(attribute) != value:
            volumeNode.SetAttribute(attribute, value)
    return volumeNode


def loadTemplateVolume(template):
    """The volume of a registry template (yours or built-in), loaded once and tagged ``Studio.Role`` = template, its
    ``Studio.Space`` and ``Studio.TemplateId``; the 3D brain surfaces of your template are loaded with it.
    """
    templates = _templatesModule()
    for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        if node.GetAttribute(_TEMPLATE_ID_ATTRIBUTE) == template.id and not node.IsA("vtkMRMLLabelMapVolumeNode"):
            _loadTemplateSurfaces(template)
            return node
    sampleName = templates.sampleNameOf(template) if templates is not None else None
    if sampleName in (TEMPLATE_SAMPLE_2MM, TEMPLATE_SAMPLE_1MM):
        volumeNode = _templateVolume(sampleName, (cachedTemplatePath(sampleName) or template.imagePath) if sampleName == TEMPLATE_SAMPLE_1MM else None)
        volumeNode.SetAttribute(_TEMPLATE_ID_ATTRIBUTE, template.id)
        return volumeNode
    path = template.imagePath
    if not path or not os.path.isfile(path):
        raise RuntimeError(_("The image of the template {name} is missing").format(name=template.name))
    with _ownLoad():
        # Tagged before it enters the scene (the Draw cards and space checks that react to the new node see a template), and
        # read by its storage node: a template is not a recently loaded file of the user (no RecentlyLoadedFiles entry)
        volumeNode = slicer.vtkMRMLScalarVolumeNode()
        volumeNode.SetName(template.name)
        _tagRegistryTemplate(volumeNode, template)
        storageNode = slicer.vtkMRMLVolumeArchetypeStorageNode()
        storageNode.SetFileName(path)
        storageNode.SetSingleFile(True)
        if not storageNode.ReadData(volumeNode):
            raise RuntimeError(_("Failed to load the template {name}").format(name=template.name))
        slicer.mrmlScene.AddNode(storageNode)
        volumeNode.SetAndObserveStorageNodeID(storageNode.GetID())
        slicer.mrmlScene.AddNode(volumeNode)
        volumeNode.CreateDefaultDisplayNodes()
        displayNode = volumeNode.GetDisplayNode()
        if displayNode is not None and displayNode.GetColorNodeID() is None:
            displayNode.SetAndObserveColorNodeID("vtkMRMLColorTableNodeGrey")
    _loadTemplateSurfaces(template)
    return volumeNode


def _loadTemplateSurfaces(template):
    """The 3D brain surfaces of your template as models: light gray, 30% opacity, in the 3D view only."""
    templates = _templatesModule()
    if templates is None or template.builtin:
        return []
    loaded = {node.GetAttribute("Studio.TemplateSurface") for node in slicer.util.getNodesByClass("vtkMRMLModelNode")
              if node.GetAttribute(_TEMPLATE_ID_ATTRIBUTE) == template.id}
    models = []
    for path in template.surfacePaths:
        name = os.path.basename(path)
        if name in loaded or not os.path.isfile(path):
            continue
        try:
            with _ownLoad():
                modelNode = slicer.modules.models.logic().AddModel(path, templates.SURFACE_COORDINATE_SYSTEM)
            if modelNode is None:
                raise RuntimeError(name)
        except Exception:
            logging.warning(f"Studio: the surface {name} of the template {template.name} could not be loaded")
            continue
        modelNode.SetName(f"{template.name} {templates.stemOf(name)}")
        modelNode.SetAttribute(space.ROLE_ATTRIBUTE, templates.ROLE_SURFACE)
        modelNode.SetAttribute(_TEMPLATE_ID_ATTRIBUTE, template.id)
        modelNode.SetAttribute("Studio.TemplateSurface", name)
        displayNode = modelNode.GetDisplayNode()
        if displayNode is not None:
            displayNode.SetColor(*templates.SURFACE_COLOR)
            displayNode.SetOpacity(templates.SURFACE_OPACITY)
            displayNode.SetVisibility2D(False)
            displayNode.SetVisibility3D(True)
        models.append(modelNode)
    return models


def _startOnRegistryTemplate(template, shell):
    """Start on a template of the registry that is not the plain built-in path (yours, the 1 mm or NLin6 built-in)."""
    with slicer.util.WaitCursor():
        volumeNode = loadTemplateVolume(template)
        setViewLayers(background=volumeNode, fit=True)
        segmentationNode = _templateLesionFor(volumeNode, shell)
        _trackTemplate(volumeNode, segmentationNode)
        selectInSegmentEditor(segmentationNode, volumeNode)
        if shell is not None:
            for key in ("native-lesion", "lesion-space", "template1mm", "template-missing"):
                _withdrawNotice(key, shell)
            shell.switchTo("draw", keepLayout=_figureController is not None and _figureController.isShown())
        elif hasattr(slicer.modules, "segmenteditor") and slicer.util.mainWindow() is not None:
            slicer.util.selectModule("SegmentEditor")
    _closeTemplateBanner()
    return volumeNode, segmentationNode


def _isStudyTemplateChoice(studySession, templateId):
    """True when ``templateId`` (a "Start on template ▾" menu entry) is the default template or the open study's template:
    it then adds a case to the study, like the main button. Another template starts a case outside the study.
    """
    templates = _templatesModule()
    if templates is None:
        return False
    template = templates.templateById(templateId)
    if template is None or template.id == templates.defaultTemplateId():
        return template is not None
    try:
        record = studySession.templateRecord() or {}
    except Exception:
        return False
    source = str(record.get("source") or "")
    if templates.isUserStudyRecord(record):
        studyTemplate = templates.templateForStudyRecord(record)
        return studyTemplate is not None and studyTemplate.id == template.id
    sampleName = source[len("sampledata:"):] if source.startswith("sampledata:") else None
    return bool(sampleName) and template.builtin and templates.sampleNameOf(template) == sampleName


def startOnTemplate(res=2, shell=None, volumeNode=None, templateId=None):
    """Load the MNI152NLin2009cAsym template and open Draw on a "Lesions" segmentation of its geometry.

    ``templateId`` (A-10): a template of the registry (``templates``: "user:<slug>" for your own, "builtin:<name>");
    without it, the default template (``Studio/Templates/Default``) when it is not the bundled one. Your template draws on
    its own grid with its space and name; the 1 mm offer below is only for the built-in 2009c template. With a study open,
    no ``templateId``, the default one or the study's own adds a case to the study (``_isStudyTemplateChoice``).

    The 2 mm brain is bundled, so this works offline. The 1 mm T1w is used instead when it was downloaded
    before. Otherwise the 1 mm template is offered in the inspector notice slot after the first stroke on the 2 mm
    template (``res=1`` offers it now and starts the download). The template volume and its lesion are reused if they
    are in the scene, except a lesion that is saved and unchanged: the next case then starts on a new lesion, so that
    Save never re-saves it silently to the previous case's file (``_templateLesionFor()``). ``volumeNode``: a template
    volume that is already loaded (from the Sample Data module) is used. Return ``(volumeNode, segmentationNode)``.
    """
    shell = _currentShell(shell)
    studySession = _studySession()
    if studySession is not None and volumeNode is None and (templateId is None or _isStudyTemplateChoice(studySession, templateId)):
        # With a study: a new case on the study template (one template node per session; W3-07). A saved lesion is never
        # re-targeted: the case is a new subject whose lesion is written by Done -> next.
        studySession.newCase()
        return studySession.volumeNode(), studySession.segmentationNode()
    forced2mm = False
    if volumeNode is None:
        template = _chosenTemplate(templateId)
        templates = _templatesModule()
        if template is not None and templates is not None and template.id != templates.BUILTIN_DEFAULT:
            return _startOnRegistryTemplate(template, shell)
        forced2mm = template is not None
    cached1mm = cachedTemplatePath(TEMPLATE_SAMPLE_1MM)
    with slicer.util.WaitCursor():
        if forced2mm:
            # "MNI152 2009c" chosen in the menu: the bundled 2 mm brain, even when the 1 mm T1w was downloaded
            volumeNode = _templateVolume(TEMPLATE_SAMPLE_2MM)
        elif volumeNode is not None:
            is1mm = volumeNode.GetName() == _templateNodeName(TEMPLATE_SAMPLE_1MM)
            _tagTemplate(volumeNode, TEMPLATE_SAMPLE_1MM if is1mm else TEMPLATE_SAMPLE_2MM)
            if is1mm and cached1mm is None:
                # The 1 mm template is in the scene: nothing to offer
                cached1mm = volumeNode.GetStorageNode().GetFileName() if volumeNode.GetStorageNode() is not None else ""
        elif cached1mm is not None:
            volumeNode = _templateVolume(TEMPLATE_SAMPLE_1MM, cached1mm)
        else:
            volumeNode = _templateVolume(TEMPLATE_SAMPLE_2MM)
        setViewLayers(background=volumeNode, fit=True)
        segmentationNode = _templateLesionFor(volumeNode, shell)
        _trackTemplate(volumeNode, segmentationNode)
        selectInSegmentEditor(segmentationNode, volumeNode)
        if shell is not None:
            _withdrawNotice("native-lesion", shell)
            _withdrawNotice("lesion-space", shell)
            shell.switchTo("draw", keepLayout=_figureController is not None and _figureController.isShown())
        elif hasattr(slicer.modules, "segmenteditor") and slicer.util.mainWindow() is not None:
            slicer.util.selectModule("SegmentEditor")
    if cached1mm is None:
        if res == 1:
            banner = showTemplateDownloadBanner()
            if banner is not None and banner.state == "offer":
                banner.startDownload()
        else:
            _watchTemplateOffer(segmentationNode)
    else:
        # An offer whose download completed while it was hidden: the 1 mm template is used now
        _closeTemplateBanner()
    return volumeNode, segmentationNode


#
# The 1 mm template offer (USABILITY 12 rule 3): a notice line after the first stroke, never over the views
#


def isTemplateOfferDismissed():
    return str(slicer.util.settingsValue(TEMPLATE_1MM_OFFER_KEY, "") or "") == "dismissed"


def _dismissTemplateOffer():
    """Remember "Not now" across restarts (``Studio/Template1mmOffer`` = "dismissed")."""
    qt.QSettings().setValue(TEMPLATE_1MM_OFFER_KEY, "dismissed")


class _TemplateOfferWatcher(VTKObservationMixin):
    """Observes the template lesion until its first non-empty change, then offers the 1 mm template (once)."""

    def __init__(self, segmentationNode):
        VTKObservationMixin.__init__(self)
        self.segmentationId = segmentationNode.GetID()
        self.timer = qt.QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(300)
        self.timer.connect("timeout()", self._check)
        self.addObserver(segmentationNode, vtk.vtkCommand.ModifiedEvent, self._onModified)
        self.addObserver(segmentationNode, slicer.vtkSegmentation.SegmentModified, self._onModified)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self._onClosing)

    def _onModified(self, caller, event):
        self.timer.start()

    def _onClosing(self, caller, event):
        self.dispose()

    def _check(self):
        global _templateOfferWatcher
        segmentationNode = slicer.mrmlScene.GetNodeByID(self.segmentationId)
        if segmentationNode is None:
            self.dispose()
            return
        if isSegmentationEmpty(segmentationNode):
            return
        self.dispose()
        templateVolume = _templateNodes()[0]
        _editedSegmentation, editedSource = _editedNodes()
        if (isTemplateOfferDismissed() or cachedTemplatePath(TEMPLATE_SAMPLE_1MM) is not None or templateVolume is None
                or templateVolume.GetName() != _templateNodeName(TEMPLATE_SAMPLE_2MM) or not _sameNode(editedSource, templateVolume)):
            return
        showTemplateDownloadBanner()

    def dispose(self):
        global _templateOfferWatcher
        self.timer.stop()
        self.removeObservers()
        if _templateOfferWatcher is self:
            _templateOfferWatcher = None


def _watchTemplateOffer(segmentationNode):
    """Offer the 1 mm template after the first stroke on the 2 mm template (unless "Not now" was chosen)."""
    global _templateOfferWatcher
    if (segmentationNode is None or isTemplateOfferDismissed() or cachedTemplatePath(TEMPLATE_SAMPLE_1MM) is not None
            or templateSource(TEMPLATE_SAMPLE_1MM) is None):
        return
    if _templateBanner is not None and _templateBanner.isAlive():
        # Offered already: shown again (with its download state)
        _updateTemplateBanner()
        return
    if not isSegmentationEmpty(segmentationNode):
        showTemplateDownloadBanner()
        return
    if _templateOfferWatcher is not None:
        if _templateOfferWatcher.segmentationId == segmentationNode.GetID():
            return
        _templateOfferWatcher.dispose()
    _templateOfferWatcher = _TemplateOfferWatcher(segmentationNode)


def _onTemplateDownloadProgress(bytesReceived, bytesTotal):
    shell = _currentShell()
    if shell is None or bytesTotal <= 0:
        return
    percent = int(100 * bytesReceived / bytesTotal)
    if _statusChipFree(shell):
        shell.setStatusChip(_("1 mm template {p}%").format(p=percent), "busy")


_TEMPLATE_CHIP_PATTERN = re.compile(r"^1 mm template \d+%$")


def _statusChipFree(shell):
    """True when the top bar shows no status chip, or the 1 mm template progress."""
    chip = getattr(getattr(shell, "topBar", None), "statusChip", None)
    if chip is None:
        return False
    try:
        return not chip.isVisible() or _TEMPLATE_CHIP_PATTERN.match(str(chip.text)) is not None
    except Exception:
        return False


def _clearTemplateChip(*args):
    shell = _currentShell()
    chip = getattr(getattr(shell, "topBar", None), "statusChip", None) if shell is not None else None
    if chip is not None and chip.isVisible() and _TEMPLATE_CHIP_PATTERN.match(str(chip.text)):
        shell.setStatusChip(None)


def _templateOfferDetails():
    message = _("The MNI152NLin2009cAsym 1 mm T1w template ({size}, TemplateFlow) has finer voxels for tracing. "
                "A lesion already drawn stays on the 2 mm grid.").format(size="13.7 MB")
    shell = _currentShell()
    if shell is not None:
        shell.toast(message, 8000)
    else:
        slicer.util.infoDisplay(message)


def showTemplateDownloadBanner(url=None):
    """Offer the 1 mm template download as a line in the inspector notice slot (``url`` replaces the TemplateFlow URL,
    for tests). An offer that exists already is shown again, with its download state. Return the ``DownloadBanner``.
    """
    global _templateBanner
    from . import downloads

    mainWindow = slicer.util.mainWindow()
    source = templateSource(TEMPLATE_SAMPLE_1MM)
    if mainWindow is None or source is None:
        return None
    if _templateBanner is not None and _templateBanner.isAlive():
        if url is None or url == _templateBanner.download.url:
            _postNotice("template1mm", _templateBanner.widget)
            return _templateBanner
        _closeTemplateBanner()
    checksum = source.checksums[0] if source.checksums else None
    _templateBanner = downloads.DownloadBanner(
        None,
        offerText=_("Sharper 1 mm template available"),
        url=url or source.uris[0],
        sha256=checksum,
        dest=os.path.join(downloads.cacheDirectory(), source.fileNames[0]),
        readyText=_("The 1 mm template is ready."),
        actionText=_("Switch to 1 mm"),
        onAction=switchTemplateTo1mm,
        embedded=True,
        onDismiss=_dismissTemplateOffer,
        onDetails=_templateOfferDetails,
        onClosed=lambda: _withdrawNotice("template1mm", delete=False),
        progressText=_("Downloading the 1 mm template… {percent}%"))
    _templateBanner.download.progress.connect(_onTemplateDownloadProgress)
    _templateBanner.download.finished.connect(_clearTemplateChip)
    _templateBanner.download.failed.connect(_clearTemplateChip)
    _postNotice("template1mm", _templateBanner.widget)
    _watchTemplate()
    return _templateBanner


def templateBanner():
    """The 1 mm template offer (a ``DownloadBanner`` embedded in the inspector notice slot), if it is offered."""
    return _templateBanner if _templateBanner is not None and _templateBanner.isAlive() and noticeWidget("template1mm") is not None else None


def templateOfferWidget():
    """The widget of the 1 mm template offer (a notice line), None when it is not offered (U-02's Current work card)."""
    banner = templateBanner()
    return banner.widget if banner is not None else None


def _closeTemplateBanner():
    global _templateBanner
    if _templateBanner is not None:
        banner = _templateBanner
        _templateBanner = None
        banner.close()
    _withdrawNotice("template1mm", delete=False)
    _clearTemplateChip()


def _updateTemplateBanner():
    """Offer the 1 mm template only while the 2 mm template is the background of the slice views.

    Once the template is no longer in the scene (for example, the scene was closed), the offer closes; a running
    download goes on with the offer withdrawn, so that the next "Start on MNI template" uses the 1 mm file.
    """
    global _templateBanner
    banner = _templateBanner
    if banner is None:
        return
    if not banner.isAlive():
        _templateBanner = None
        _withdrawNotice("template1mm", delete=False)
        return
    volumeNode = _templateNodes()[0]
    is1mm = volumeNode is not None and volumeNode.GetName() == _templateNodeName(TEMPLATE_SAMPLE_1MM)
    if (volumeNode is None or is1mm) and not banner.download.isRunning():
        _closeTemplateBanner()
        return
    if _isTemplateShown(volumeNode) and not is1mm:
        if noticeWidget("template1mm") is None:
            _postNotice("template1mm", banner.widget)
    else:
        _withdrawNotice("template1mm", delete=False)


class _TemplateWatcher(VTKObservationMixin):
    """Forgets the template nodes when the scene is closed, and updates the 1 mm offer when the scene or the
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
        global _loadedSet
        _templateNodeIds.clear()
        _loadedSet = None
        for key in ("native-lesion", "lesion-space", "statmap", "figure-convention"):
            _withdrawNotice(key)
        _statMaps.clear()
        shell = _currentShell()
        if shell is not None:
            shell.setBreadcrumb(None)
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
            setViewLayers(background=volume1mm, fit=True)
        if segmentationNode is not None:
            segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volume1mm)
        _trackTemplate(volume1mm, segmentationNode)
        if editsTemplate:
            editor.setSourceVolumeNode(volume1mm)
    # The background is unchanged if the template was not shown, so the watcher is not notified: close the offer now
    _updateTemplateBanner()
    if shell is not None:
        shell.toast(_("Switched to the 1 mm template"))
    return True


#
# Figures (USABILITY 13, interim until L-29): a reference shown next to the template, never edited
#

_figureController = None
#: The "draw on the template" toast is shown once per session
_figureGuardToastShown = False


def ensureCompareLayout():
    """Register the Compare layout (7005) when the running build does not define it. Return True if it was added."""
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None:
        return False
    node = layoutManager.layoutLogic().GetLayoutNode()
    if node is None or node.IsLayoutDescription(COMPARE_LAYOUT_ID):
        return False
    node.AddLayoutDescription(COMPARE_LAYOUT_ID, COMPARE_LAYOUT_XML)
    return True


def figureController():
    """The controller of the figure shown in the Yellow view (``_FigureReference``), None when no figure is shown."""
    return _figureController if _figureController is not None and _figureController.isShown() else None


class _FigureReference(VTKObservationMixin):
    """A figure shown in the Yellow view of the Compare layout, and everything that keeps it a reference.

    - Interactor guard (priority 2.0, above the Segment Editor's 1.0): left press, release and double-click are aborted
      while an editor effect is active (a stroke over the figure changes no voxel); a plain wheel and the slice keys are
      aborted (the figure has one slice); Ctrl+wheel and right-drag zoom and middle-drag pan still work.
    - Yellow is put in its own view group and its link flag is reset, so linked views never drag it off the figure's
      slice.
    - When the figure is removed, or Yellow shows something else, the remembered layout and Yellow view (orientation,
      field of view, offset, link flag, view group, layers) are restored.
    """

    GUARDED_KEYS = ("Up", "Down", "Prior", "Next", "Page_Up", "Page_Down", "Home", "End", "Left", "Right", "f", "b")

    def __init__(self, shell):
        VTKObservationMixin.__init__(self)
        self._shell = shell
        self.figureId = None
        self.saved = None
        self._guardTags = []
        self._guardInteractor = None
        self._suspended = False
        self._observedComposite = None
        #: The Segment Editor shows its source volume in every slice view on layout and source changes: such a change of
        #: Yellow is undone (the figure stays), a change made by the user restores the view
        self._layoutChanged = False
        self._lastSourceId = None
        self._checkTimer = qt.QTimer()
        self._checkTimer.setSingleShot(True)
        self._checkTimer.setInterval(0)
        self._checkTimer.connect("timeout()", self._check)
        scene = slicer.mrmlScene
        self.addObserver(scene, scene.NodeRemovedEvent, self._onSceneChanged)
        self.addObserver(scene, scene.StartCloseEvent, self._onSceneClosing)
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None:
            layoutManager.connect("layoutChanged(int)", self._onLayoutChanged)

    #
    # State
    #

    def figure(self):
        node = slicer.mrmlScene.GetNodeByID(self.figureId) if self.figureId else None
        return node if node is not None and space.isFigure(node) else None

    def isShown(self):
        figure = self.figure()
        compositeNode = self._compositeNode()
        return figure is not None and compositeNode is not None and compositeNode.GetBackgroundVolumeID() == figure.GetID()

    @staticmethod
    def _sliceWidget():
        layoutManager = slicer.app.layoutManager()
        return layoutManager.sliceWidget(FIGURE_VIEW_NAME) if layoutManager is not None else None

    @staticmethod
    def _sliceNode():
        return space._sliceNode(FIGURE_VIEW_NAME)

    @staticmethod
    def _compositeNode():
        return space._compositeNode(FIGURE_VIEW_NAME)

    def _remember(self):
        """Remember the layout and the Yellow view before the first figure is shown."""
        layoutManager = slicer.app.layoutManager()
        sliceNode, compositeNode = self._sliceNode(), self._compositeNode()
        if sliceNode is None or compositeNode is None:
            return
        sliceToRas = vtk.vtkMatrix4x4()
        sliceToRas.DeepCopy(sliceNode.GetSliceToRAS())
        self.saved = {
            "layout": layoutManager.layout if layoutManager is not None else None,
            "sliceToRAS": sliceToRas,
            "orientation": sliceNode.GetOrientation(),
            "fieldOfView": list(sliceNode.GetFieldOfView()),
            "xyzOrigin": list(sliceNode.GetXYZOrigin()),
            "viewGroup": sliceNode.GetViewGroup(),
            "linked": compositeNode.GetLinkedControl(),
            "background": compositeNode.GetBackgroundVolumeID(),
            "foreground": compositeNode.GetForegroundVolumeID(),
            "label": compositeNode.GetLabelVolumeID(),
        }

    #
    # Show
    #

    def show(self, figureNode):
        """Show ``figureNode`` in Yellow (Compare layout), unlinked, axial, fitted, at its slice; install the guard."""
        if not self.isShown():
            # The first figure: the view it replaces is restored when the figure leaves (a next figure keeps it)
            self._remember()
        self.figureId = figureNode.GetID()
        ensureCompareLayout()
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None and layoutManager.layout != COMPARE_LAYOUT_ID:
            layoutManager.setLayout(COMPARE_LAYOUT_ID)
        compositeNode, sliceNode = self._compositeNode(), self._sliceNode()
        if compositeNode is None or sliceNode is None:
            raise RuntimeError("The Yellow slice view is not available")
        # Unlinked first: a linked composite node would send the figure to every view
        compositeNode.SetLinkedControl(False)
        sliceNode.SetViewGroup(FIGURE_VIEW_GROUP)
        compositeNode.SetBackgroundVolumeID(figureNode.GetID())
        compositeNode.SetForegroundVolumeID(None)
        compositeNode.SetLabelVolumeID(None)
        sliceNode.SetOrientationToAxial()
        if space.figureConvention(figureNode) == "neurological":
            # A figure shown again keeps its answer (the axial orientation above is not mirrored)
            self.mirror(True)
        self._sizePane()
        self._fit()
        # The views of the new layout get their size when the application processes events: size the pane and fit the
        # figure again then
        for delay in (0, 250):
            qt.QTimer.singleShot(delay, self._refit)
        self._observeComposite(compositeNode)
        self._installGuard()
        self._layoutChanged = False
        self._lastSourceId = self._editorSourceId()

    @staticmethod
    def _editorSourceId():
        widget = getattr(slicer.modules, "SegmentEditorWidget", None)
        editor = getattr(widget, "editor", None) if widget is not None else None
        source = editor.sourceVolumeNode() if editor is not None else None
        return source.GetID() if source is not None else None

    def _refit(self):
        if self.isShown():
            if self._sizePane():
                setViewLayers(fit=True)
            self._fit()

    @staticmethod
    def figurePaneMinimumWidth(widget=None):
        """D-U4: the figure pane is at least 460 px wide, 700 px on a screen 1920 px wide or more."""
        from . import ui

        screen = ui.screenOf(widget)
        geometry = screen.geometry if screen is not None else None
        geometry = geometry() if callable(geometry) else geometry
        screenWidth = geometry.width() if geometry is not None else 0
        return FIGURE_PANE_MIN_WIDTH_LARGE if screenWidth >= FIGURE_PANE_LARGE_SCREEN_WIDTH else FIGURE_PANE_MIN_WIDTH

    def _sizePane(self):
        """Give the figure pane its width (D-U4): at least ``figurePaneMinimumWidth()`` and its share of the layout, the
        template view keeping ``TEMPLATE_PANE_MIN_WIDTH``. Return True if the split changed.
        """
        sliceWidget = self._sliceWidget()
        layoutManager = slicer.app.layoutManager()
        if sliceWidget is None or layoutManager is None or layoutManager.layout != COMPARE_LAYOUT_ID:
            return False
        child, splitter = sliceWidget, sliceWidget.parent()
        while splitter is not None and not splitter.inherits("QSplitter"):
            child, splitter = splitter, splitter.parent()
        if splitter is None:
            return False
        sizes = [int(size) for size in splitter.sizes()]
        index = splitter.indexOf(child)
        total = sum(sizes)
        if len(sizes) != 2 or index not in (0, 1) or total < 2 * TEMPLATE_PANE_MIN_WIDTH:
            return False
        other = splitter.widget(1 - index)
        otherHint = other.minimumSizeHint if other is not None else None
        otherHint = otherHint() if callable(otherHint) else otherHint
        # The template view is never narrower than its view bar needs (the figure pane is then narrower than D-U4 asks)
        otherMinimum = max(TEMPLATE_PANE_MIN_WIDTH, otherHint.width() if otherHint is not None else 0, other.minimumWidth if other is not None else 0)
        width = max(round(total * 0.45), self.figurePaneMinimumWidth(sliceWidget))
        width = max(min(width, total - otherMinimum), 0)
        if abs(sizes[index] - width) <= 1:
            return False
        sizes[index], sizes[1 - index] = width, total - width
        splitter.setSizes(sizes)
        return True

    def _fit(self):
        sliceWidget = self._sliceWidget()
        figure = self.figure()
        if sliceWidget is None or figure is None:
            return
        sliceWidget.sliceLogic().FitSliceToAll()
        bounds = [0.0] * 6
        figure.GetRASBounds(bounds)
        self._sliceNode().SetSliceOffset(0.5 * (bounds[4] + bounds[5]))

    def mirror(self, mirrored):
        """Mirror the figure pane (a neurological figure is shown with the patient's right on the left, like the template)."""
        sliceNode = self._sliceNode()
        if sliceNode is None:
            return
        sliceToRas = sliceNode.GetSliceToRAS()
        # Column 0 is the screen's +x direction: radiological axial is -R (patient left on screen right)
        isMirrored = sliceToRas.GetElement(0, 0) > 0
        if isMirrored != bool(mirrored):
            for row in range(3):
                sliceToRas.SetElement(row, 0, -sliceToRas.GetElement(row, 0))
            sliceNode.UpdateMatrices()
            self._fit()

    #
    # Guard
    #

    def _installGuard(self):
        sliceWidget = self._sliceWidget()
        interactor = sliceWidget.sliceView().interactor() if sliceWidget is not None else None
        if interactor is None or self._suspended:
            return
        if self._guardInteractor is not None and self._guardInteractor == interactor and self._guardTags:
            return
        self._removeGuard()
        self._guardInteractor = interactor
        for event, handler in ((vtk.vtkCommand.LeftButtonPressEvent, self._onLeftButton),
                               (vtk.vtkCommand.LeftButtonReleaseEvent, self._onLeftButton),
                               (vtk.vtkCommand.LeftButtonDoubleClickEvent, self._onLeftButton),
                               (vtk.vtkCommand.MouseWheelForwardEvent, self._onWheel),
                               (vtk.vtkCommand.MouseWheelBackwardEvent, self._onWheel),
                               (vtk.vtkCommand.KeyPressEvent, self._onKey),
                               (vtk.vtkCommand.CharEvent, self._onKey)):
            self._addGuardObserver(interactor, event, handler)

    def _addGuardObserver(self, interactor, event, handler):
        command = []

        def callback(caller, eventName):
            try:
                if handler(caller) and command:
                    command[0].SetAbortFlag(1)
            except Exception:
                logging.error(f"Studio figure guard failed:\n{traceback.format_exc()}")

        tag = interactor.AddObserver(event, callback, 2.0)
        command.append(interactor.GetCommand(tag))
        self._guardTags.append(tag)

    def _removeGuard(self):
        interactor = self._guardInteractor
        if interactor is not None:
            for tag in self._guardTags:
                interactor.RemoveObserver(tag)
        self._guardTags = []
        self._guardInteractor = None

    def _guarding(self):
        return not self._suspended and self.isShown()

    @staticmethod
    def _effectActive():
        widget = getattr(slicer.modules, "SegmentEditorWidget", None)
        editor = getattr(widget, "editor", None) if widget is not None else None
        return editor is not None and editor.activeEffect() is not None

    def _onLeftButton(self, interactor):
        global _figureGuardToastShown
        if not self._guarding() or not self._effectActive():
            return False
        if not _figureGuardToastShown:
            _figureGuardToastShown = True
            shell = _currentShell(self._shell)
            if shell is not None:
                shell.toast(_("The figure is a reference: draw on the template views"), 4000)
        return True

    def _onWheel(self, interactor):
        if not self._guarding():
            return False
        return not (interactor.GetControlKey() or interactor.GetShiftKey() or interactor.GetAltKey())

    def _onKey(self, interactor):
        if not self._guarding() or interactor.GetControlKey() or interactor.GetAltKey():
            return False
        return str(interactor.GetKeySym() or "") in self.GUARDED_KEYS

    #
    # Watch
    #

    def _observeComposite(self, compositeNode):
        if self._observedComposite is not None and self._observedComposite != compositeNode:
            self.removeObserver(self._observedComposite, vtk.vtkCommand.ModifiedEvent, self._onCompositeModified)
            self._observedComposite = None
        if self._observedComposite is None:
            self.addObserver(compositeNode, vtk.vtkCommand.ModifiedEvent, self._onCompositeModified)
            self._observedComposite = compositeNode

    def _onCompositeModified(self, caller, event):
        self._checkTimer.start()

    def _onSceneChanged(self, caller, event):
        if self.figureId is not None:
            self._checkTimer.start()

    def _onLayoutChanged(self, layoutId=None):
        if self.figureId is not None:
            # The Segment Editor puts its source volume in every view of the new layout: Yellow keeps the figure
            self._layoutChanged = True
            self._checkTimer.start()
        if self.isShown():
            # The view widget of Yellow may be another one (and the splitter of the Compare layout a new one)
            self._installGuard()
            if layoutId == COMPARE_LAYOUT_ID:
                for delay in (0, 250):
                    qt.QTimer.singleShot(delay, self._refit)

    def _onSceneClosing(self, caller, event):
        if self.figureId is not None:
            self.restore()

    def _check(self):
        """Reset the link flag of Yellow while it shows the figure; restore the view when the figure is gone."""
        try:
            if self.figureId is None or self._suspended:
                # Classic: the views are the user's (no link reset, no restore)
                return
            compositeNode = self._compositeNode()
            figure = self.figure()
            if figure is not None and compositeNode is not None and compositeNode.GetBackgroundVolumeID() == figure.GetID():
                if compositeNode.GetLinkedControl():
                    compositeNode.SetLinkedControl(False)
                sliceNode = self._sliceNode()
                if sliceNode is not None and sliceNode.GetViewGroup() != FIGURE_VIEW_GROUP:
                    sliceNode.SetViewGroup(FIGURE_VIEW_GROUP)
                return
            background = slicer.mrmlScene.GetNodeByID(compositeNode.GetBackgroundVolumeID()) if compositeNode is not None and compositeNode.GetBackgroundVolumeID() else None
            if space.isFigure(background):
                # Another figure replaced it (a figure routed by the user's view controller)
                self.figureId = background.GetID()
                return
            sourceId = self._editorSourceId()
            automatic = self._layoutChanged or (sourceId != self._lastSourceId)
            self._layoutChanged = False
            self._lastSourceId = sourceId
            if figure is not None and compositeNode is not None and automatic and (background is None or background.GetID() == sourceId):
                # The Segment Editor showed its source volume in Yellow (new layout or new source): the figure stays
                compositeNode.SetBackgroundVolumeID(figure.GetID())
                compositeNode.SetForegroundVolumeID(None)
                compositeNode.SetLabelVolumeID(None)
                compositeNode.SetLinkedControl(False)
                return
            self.restore(figureRemoved=figure is None)
        except Exception:
            logging.error(f"Studio: figure view update failed:\n{traceback.format_exc()}")

    #
    # Restore
    #

    def restore(self, figureRemoved=True):
        """The figure left Yellow: restore the remembered layout and Yellow view, remove the guard and the question."""
        self._removeGuard()
        self._checkTimer.stop()
        saved, self.saved = self.saved, None
        self.figureId = None
        _withdrawNotice("figure-convention", self._shell)
        sliceNode, compositeNode = self._sliceNode(), self._compositeNode()
        if saved is None or sliceNode is None or compositeNode is None:
            return
        background = slicer.mrmlScene.GetNodeByID(compositeNode.GetBackgroundVolumeID()) if compositeNode.GetBackgroundVolumeID() else None
        if background is None or space.isFigure(background):
            # Yellow is empty (the figure was removed): it shows again what it showed before the figure
            for setter, nodeId in ((compositeNode.SetBackgroundVolumeID, saved["background"]),
                                   (compositeNode.SetForegroundVolumeID, saved["foreground"]),
                                   (compositeNode.SetLabelVolumeID, saved["label"])):
                setter(nodeId if nodeId and slicer.mrmlScene.GetNodeByID(nodeId) is not None and not space.isFigure(slicer.mrmlScene.GetNodeByID(nodeId)) else None)
        sliceNode.SetViewGroup(saved["viewGroup"])
        if saved["sliceToRAS"] is not None:
            sliceNode.GetSliceToRAS().DeepCopy(saved["sliceToRAS"])
            sliceNode.SetFieldOfView(*saved["fieldOfView"])
            sliceNode.SetXYZOrigin(*saved["xyzOrigin"])
            sliceNode.UpdateMatrices()
        else:
            # An adopted figure (the view before it is unknown): Yellow becomes the default sagittal view again
            if not compositeNode.GetBackgroundVolumeID() and _backgroundVolume() is not None:
                compositeNode.SetBackgroundVolumeID(_backgroundVolume().GetID())
            sliceNode.SetOrientation(saved["orientation"])
            sliceWidget = self._sliceWidget()
            if sliceWidget is not None:
                sliceWidget.sliceLogic().FitSliceToAll()
        compositeNode.SetLinkedControl(saved["linked"])
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None and layoutManager.layout == COMPARE_LAYOUT_ID and saved["layout"] is not None and saved["layout"] != COMPARE_LAYOUT_ID:
            layoutManager.setLayout(saved["layout"])

    def adopt(self, figureNode):
        """Guard a figure that is already shown in Yellow (a scene saved with one). When it leaves, Yellow becomes the
        sagittal view of the Focus layout again.
        """
        from . import layouts

        compositeNode, sliceNode = self._compositeNode(), self._sliceNode()
        if compositeNode is None or sliceNode is None:
            return
        self.figureId = figureNode.GetID()
        self.saved = {"layout": layouts.FOCUS_LAYOUT_ID, "sliceToRAS": None, "orientation": "Sagittal", "fieldOfView": None,
                      "xyzOrigin": None, "viewGroup": 0, "linked": False, "background": None, "foreground": None, "label": None}
        compositeNode.SetLinkedControl(False)
        sliceNode.SetViewGroup(FIGURE_VIEW_GROUP)
        self._observeComposite(compositeNode)
        self._installGuard()
        self._layoutChanged = False
        self._lastSourceId = self._editorSourceId()

    #
    # Classic
    #

    def suspend(self):
        """Classic: no guard (the figure and its view stay as they are)."""
        self._suspended = True
        self._removeGuard()

    def resume(self, shell):
        self._shell = shell
        self._suspended = False
        if self.isShown():
            self._installGuard()
        elif self.figureId is not None:
            # The figure left Yellow in Classic: the user arranged the views there, they stay as they are (only the view
            # group that kept Yellow out of linked changes is given back)
            sliceNode = self._sliceNode()
            if self.saved is not None and sliceNode is not None and sliceNode.GetViewGroup() == FIGURE_VIEW_GROUP:
                sliceNode.SetViewGroup(self.saved["viewGroup"])
            self.figureId = None
            self.saved = None
            _withdrawNotice("figure-convention", shell)

    def dispose(self):
        self._removeGuard()
        self._checkTimer.stop()
        self.removeObservers()
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None:
            try:
                layoutManager.disconnect("layoutChanged(int)", self._onLayoutChanged)
            except Exception:
                pass


def _figureConventionNotice(figureNode, shell):
    """The question "In this figure, the patient's right is on the [Left] [Right] [Not sure]" (stays until answered)."""
    figureId = figureNode.GetID()
    frame = _noticeFrame("StudioFigureConventionNotice", _("In this figure, the patient's right is on the"), [
        (_("Left"), lambda: answerFigureConvention("radiological", figureId)),
        (_("Right"), lambda: answerFigureConvention("neurological", figureId)),
        (_("Not sure"), lambda: answerFigureConvention("unknown", figureId)),
    ])
    frame.setAccessibleName(_("Figure convention question"))
    return frame


def answerFigureConvention(convention, figureId=None):
    """Store the answer to the convention question (``Studio.FigureConvention``); "neurological" mirrors the figure pane.

    "radiological": the patient's right is on the left of the figure, as on the template: nothing to flip.
    """
    if convention not in space.CONVENTIONS:
        raise ValueError(f"Unknown figure convention: {convention}")
    controller = _figureController
    figure = slicer.mrmlScene.GetNodeByID(figureId) if figureId else (controller.figure() if controller is not None else None)
    if figure is None:
        _withdrawNotice("figure-convention")
        return False
    figure.SetAttribute(space.FIGURE_CONVENTION_ATTRIBUTE, convention)
    if controller is not None and controller.figureId == figure.GetID():
        controller.mirror(convention == "neurological")
    _withdrawNotice("figure-convention")
    return True


def adoptShownFigure(shell=None):
    """Guard the figure that a loaded scene shows in Yellow, and ask its convention when it is not known. Return it."""
    global _figureController
    shell = _currentShell(shell)
    if shell is None:
        return None
    if _figureController is not None and _figureController.isShown():
        return _figureController.figure()
    background = space.viewBackground(FIGURE_VIEW_NAME)
    layoutManager = slicer.app.layoutManager()
    if not space.isFigure(background) and layoutManager is not None and layoutManager.layout == COMPARE_LAYOUT_ID:
        # A scene saved in the Compare layout: the Segment Editor may have put its source volume in Yellow while the
        # scene was loaded; the figure (the last one) goes back there
        figures = [node for node in slicer.util.getNodesByClass("vtkMRMLVolumeNode") if space.isFigure(node)]
        compositeNode = space._compositeNode(FIGURE_VIEW_NAME)
        if figures and compositeNode is not None:
            compositeNode.SetLinkedControl(False)
            compositeNode.SetBackgroundVolumeID(figures[-1].GetID())
            compositeNode.SetForegroundVolumeID(None)
            compositeNode.SetLabelVolumeID(None)
            background = figures[-1]
    if not space.isFigure(background):
        return None
    if _figureController is None:
        _figureController = _FigureReference(shell)
    _figureController.adopt(background)
    if space.figureConvention(background) == "unknown":
        _postNotice("figure-convention", _figureConventionNotice(background, shell), shell)
    return background


def _routeFigure(path, shell, result, figureNode=None):
    """Show a figure next to the template (module documentation). On error, the Add Data dialog opens with it (3D Slicer's route).

    ``figureNode``: the figure is already loaded (by a route of 3D Slicer). A figure whose file is already open is not
    loaded again: it is shown again (its convention is kept).
    """
    global _figureController
    reopened = None if figureNode is not None else _openReference(path, space.ROLE_FIGURE)
    if reopened is not None:
        figureNode = reopened
        result.alreadyOpen.append(figureNode)
    loaded = figureNode is None
    try:
        if _backgroundVolume() is None:
            # Something to draw on: the template (an existing template lesion is reused)
            startOnTemplate(shell=shell)
        if figureNode is None:
            with slicer.util.WaitCursor():
                figureNode = slicer.util.loadVolume(path, {"singleFile": True, "show": False})
        figureNode.SetAttribute(space.ROLE_ATTRIBUTE, space.ROLE_FIGURE)
        if figureNode.GetAttribute(space.FIGURE_CONVENTION_ATTRIBUTE) not in space.CONVENTIONS:
            figureNode.SetAttribute(space.FIGURE_CONVENTION_ATTRIBUTE, "unknown")
        if shell.currentWorkspace() != "draw":
            shell.switchTo("draw")
        if _figureController is None:
            _figureController = _FigureReference(shell)
        alreadyShown = _figureController.isShown() and _figureController.figureId == figureNode.GetID()
        if not alreadyShown:
            _figureController.show(figureNode)
        if reopened is None:
            result.figures.append(figureNode)
    except Exception:
        logging.error(f"Studio: cannot show {path} as a figure:\n{traceback.format_exc()}")
        if loaded and figureNode is not None and slicer.mrmlScene.IsNodePresent(figureNode):
            slicer.mrmlScene.RemoveNode(figureNode)
        if path:
            result.addDataPaths.append(path)
            openAddDataDialog([path])
        return False
    if reopened is not None:
        if not alreadyShown and space.figureConvention(figureNode) == "unknown":
            # Shown again in place of another figure: its question replaces the other figure's
            _withdrawNotice("figure-convention", shell)
            _postNotice("figure-convention", _figureConventionNotice(figureNode, shell), shell)
        shell.toast(_("{name} is already open").format(name=figureNode.GetName()))
        return True
    _withdrawNotice("figure-convention", shell)
    _postNotice("figure-convention", _figureConventionNotice(figureNode, shell), shell)
    shell.toast(_("Figure shown on the right; the template on the left is where you draw"), level="consequence")
    return True


#
# Statistical maps (LNM-09, LNM-10; interim until the Map page loads them, W3-08)
#

_statMaps = {}


def statMapColorTable(window, threshold, name=STATMAP_COLOR_NODE_NAME, colorNode=None):
    """A 256-entry cold-to-hot colour table for the window [-``window``, ``window``] (window 2W, level 0) whose entries
    with |value| < ``threshold`` are transparent: a two-sided threshold through the table's alpha.
    """
    if colorNode is None:
        colorNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLColorTableNode", slicer.mrmlScene.GenerateUniqueName(name) if slicer.util.getFirstNodeByName(name) else name)
        colorNode.SetTypeToUser()
        colorNode.SetNumberOfColors(256)
        colorNode.SetAttribute(space.ROLE_ATTRIBUTE, space.ROLE_STATMAP)
    window = float(window) if window > 0 else 1.0
    lookupTable = colorNode.GetLookupTable()
    lookupTable.SetTableRange(0, 255)
    for index in range(256):
        value = -window + 2.0 * window * index / 255.0
        fraction = min(1.0, abs(value) / window)
        if value < 0:
            # Cold: blue (near 0) to cyan (-W)
            color = (0.0, fraction, 1.0)
        else:
            # Hot: red (near 0) to yellow (+W)
            color = (1.0, fraction, 0.0)
        alpha = 0.0 if abs(value) < threshold else 1.0
        colorNode.SetColor(index, f"{value:.3g}", color[0], color[1], color[2], alpha)
    colorNode.Modified()
    return colorNode


def _roundSignificant(value, digits=2):
    if value == 0:
        return 0.0
    import math

    return round(value, -math.floor(math.log10(abs(value))) + digits - 1)


def statMapWindow(volumeNode):
    """W of a statistical map: the 99.5th percentile of |value| (the max |value| when that is 0)."""
    import numpy as np

    array = np.abs(slicer.util.arrayFromVolume(volumeNode).astype(float))
    finite = array[np.isfinite(array)]
    if finite.size == 0:
        return 1.0
    window = float(np.percentile(finite, STATMAP_WINDOW_PERCENTILE))
    if window <= 0:
        window = float(finite.max())
    return window if window > 0 else 1.0


class _StatMapOverlay:
    """A statistical map shown as a thresholded overlay: colour table, colour legend and the notice with its controls."""

    def __init__(self, mapNode, shell):
        self.mapId = mapNode.GetID()
        self._shell = shell
        self.window = statMapWindow(mapNode)
        self.threshold = _roundSignificant(STATMAP_THRESHOLD_FRACTION * self.window)
        self.colorNode = statMapColorTable(self.window, self.threshold)
        displayNode = mapNode.GetDisplayNode()
        if displayNode is None:
            mapNode.CreateDefaultDisplayNodes()
            displayNode = mapNode.GetDisplayNode()
        displayNode.SetAutoWindowLevel(False)
        displayNode.SetWindowLevel(2.0 * self.window, 0.0)
        displayNode.SetAndObserveColorNodeID(self.colorNode.GetID())
        displayNode.SetApplyThreshold(False)
        self.legend = None
        self.widget = self._createNotice(mapNode)

    def addLegend(self):
        """Show the colour legend of the map (after the map is shown: the legend follows the views that show it)."""
        mapNode = self.mapNode()
        try:
            self.legend = slicer.modules.colors.logic().AddDefaultColorLegendDisplayNode(mapNode)
        except Exception:
            logging.error(f"Studio: cannot add the colour legend of {mapNode.GetName()}:\n{traceback.format_exc()}")

    def mapNode(self):
        return slicer.mrmlScene.GetNodeByID(self.mapId)

    def _createNotice(self, mapNode):
        from . import ui

        frame = _noticeFrame("StudioStatMapNotice", _("{name} · show |value| ≥").format(name=mapNode.GetName()), [(_("Remove overlay"), self.remove)])
        row = []
        self.thresholdSpinBox = qt.QDoubleSpinBox()
        self.thresholdSpinBox.setObjectName("StudioStatMapThresholdSpinBox")
        self.thresholdSpinBox.setDecimals(3 if self.window < 1 else 2)
        self.thresholdSpinBox.setRange(0.0, self.window)
        self.thresholdSpinBox.setSingleStep(_roundSignificant(self.window / 50.0) or 0.01)
        self.thresholdSpinBox.setValue(self.threshold)
        self.thresholdSpinBox.setToolTip(_("Values with |value| below this are hidden"))
        self.thresholdSpinBox.connect("valueChanged(double)", self.setThreshold)
        row.append(self.thresholdSpinBox)
        opacityLabel = qt.QLabel(_("Opacity"))
        row.append(opacityLabel)
        self.opacitySlider = qt.QSlider(qt.Qt.Horizontal)
        self.opacitySlider.setObjectName("StudioStatMapOpacitySlider")
        self.opacitySlider.setRange(0, 100)
        self.opacitySlider.setValue(round(STATMAP_OPACITY * 100))
        self.opacitySlider.setToolTip(_("Opacity of the map"))
        self.opacitySlider.connect("valueChanged(int)", lambda value: self.setOpacity(value / 100.0))
        row.append(self.opacitySlider)
        controls = ui.wrapRow(row)
        controls.setContentsMargins(12, 0, 8, 4)
        container = qt.QFrame()
        container.setObjectName("StudioStatMapNoticeContainer")
        layout = qt.QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(frame)
        layout.addWidget(controls)
        policy = qt.QSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        container.setSizePolicy(policy)
        ui.bindStyleSheet(container, lambda: f"QFrame#StudioStatMapNoticeContainer {{ background-color: {ui.token('accent-subtle').name()}; }}")
        return container

    def setThreshold(self, threshold):
        """Hide the values with |value| < ``threshold`` (the alpha of the colour table is rebuilt)."""
        self.threshold = float(threshold)
        if slicer.mrmlScene.IsNodePresent(self.colorNode):
            statMapColorTable(self.window, self.threshold, colorNode=self.colorNode)

    def setOpacity(self, opacity):
        figureIds = {node.GetID() for node in _figureCompositeNodes()}
        for compositeNode in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
            if compositeNode.GetID() not in figureIds and compositeNode.GetForegroundVolumeID() == self.mapId:
                compositeNode.SetForegroundOpacity(float(opacity))

    def remove(self):
        """Remove the overlay: the map, its legend and its colour table leave the scene."""
        _statMaps.pop(self.mapId, None)
        if noticeWidget("statmap") is self.widget:
            _withdrawNotice("statmap", self._shell, delete=False)
        releaseWidget(self.widget, owner=self)
        mapNode = self.mapNode()
        for compositeNode in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
            if compositeNode.GetForegroundVolumeID() == self.mapId:
                compositeNode.SetForegroundVolumeID(None)
        for node in (self.legend, mapNode, self.colorNode):
            if node is not None and slicer.mrmlScene.IsNodePresent(node):
                slicer.mrmlScene.RemoveNode(node)


def statMapOverlay(mapNode=None):
    """The overlay of a statistical map (the last one shown when ``mapNode`` is None), None if there is none."""
    if mapNode is not None:
        return _statMaps.get(mapNode.GetID())
    return list(_statMaps.values())[-1] if _statMaps else None


def _routeStatMap(path, shell, result, mapNode=None):
    """Show a statistical map as a thresholded overlay on the anatomy (module documentation).

    ``mapNode``: the map is already loaded (by a route of 3D Slicer). A map whose file is already open is not loaded
    again: its overlay is shown again. If the overlay cannot be made, the map is shown as a plain overlay: it never
    becomes a scan of Draw (no segmentation is bound to it).
    """
    reopened = None if mapNode is not None else _openReference(path, space.ROLE_STATMAP)
    if mapNode is None:
        # Since W3-08 a statistical map file opens in Map (``mappage.loadMap``); one that Map already shows is shown again
        try:
            from . import mappage
        except ImportError:
            mappage = None
        shown = mappage.showOpenMap(path, shell) if mappage is not None and callable(getattr(mappage, "showOpenMap", None)) else None
        if shown is not None:
            result.alreadyOpen.append(shown)
            if shell is not None:
                shell.toast(_("{name} is already open").format(name=shown.GetName()))
            return True
        if reopened is None and mappage is not None and callable(getattr(mappage, "loadMap", None)):
            volume = mappage.loadMap(path, shell)
            if volume is not None:
                result.statmaps.append(volume)
            return volume is not None
    if reopened is not None:
        mapNode = reopened
        result.alreadyOpen.append(mapNode)
    overlay = _statMaps.get(mapNode.GetID()) if mapNode is not None else None
    try:
        if mapNode is None:
            with slicer.util.WaitCursor():
                mapNode = slicer.util.loadVolume(path, {"singleFile": True, "show": False})
        mapNode.SetAttribute(space.ROLE_ATTRIBUTE, space.ROLE_STATMAP)
        if _backgroundVolume() is None:
            startOnTemplate(shell=shell)
        if overlay is None:
            overlay = _StatMapOverlay(mapNode, shell)
            _statMaps[mapNode.GetID()] = overlay
            setViewLayers(foreground=mapNode, foregroundOpacity=STATMAP_OPACITY)
            overlay.addLegend()
        else:
            setViewLayers(foreground=mapNode, foregroundOpacity=overlay.opacitySlider.value / 100.0)
        if reopened is None:
            result.statmaps.append(mapNode)
    except Exception:
        logging.error(f"Studio: cannot show {path} as a statistical map:\n{traceback.format_exc()}")
        if mapNode is None:
            result.errors.append((path, _("Cannot load the file")))
            return False
        _statMaps.pop(mapNode.GetID(), None)
        setViewLayers(foreground=mapNode, foregroundOpacity=STATMAP_OPACITY)
        shell.toast(_("{name} is shown as a plain overlay (no threshold)").format(name=mapNode.GetName()), level="consequence")
        return True
    if shell.currentWorkspace() == "library":
        shell.switchTo("draw", keepLayout=_figureController is not None and _figureController.isShown())
    _withdrawNotice("statmap", shell, delete=False)
    _postNotice("statmap", overlay.widget, shell)
    if reopened is not None:
        shell.toast(_("{name} is already open").format(name=mapNode.GetName()))
    else:
        shell.toast(_("Map shown as an overlay; values with |value| < {t} are hidden").format(t=f"{overlay.threshold:g}"), level="consequence")
    return True


#
# Scans loaded by the Sample Data and DICOM modules open in Draw
#

_ownLoads = 0


class _OwnLoad:
    """Context of a load made by the importer itself: the transient module watcher ignores its nodes."""

    def __enter__(self):
        global _ownLoads
        _ownLoads += 1
        return self

    def __exit__(self, *args):
        global _ownLoads
        _ownLoads -= 1
        return False


def _ownLoad():
    return _OwnLoad()


class _TransientLoadWatcher(VTKObservationMixin):
    """A scan added while the Sample Data or DICOM module is shown opens in Draw (with a "Lesion" segment), as if it was
    opened from Library. Scene imports and multi-volume loads (more than one volume within 500 ms) are left alone.
    """

    def __init__(self, shell):
        VTKObservationMixin.__init__(self)
        self.shell = shell
        self.added = []
        self.timer = qt.QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(TRANSIENT_LOAD_WINDOW_MS)
        self.timer.connect("timeout()", self._onTimeout)
        scene = slicer.mrmlScene
        self.addObserver(scene, scene.NodeAddedEvent, self._onNodeAdded)
        self.addObserver(scene, scene.EndImportEvent, self._onSceneImported)

    def _onSceneImported(self, caller, event):
        # A scene saved with a figure in Yellow: the figure is guarded again
        qt.QTimer.singleShot(0, lambda: adoptShownFigure(self.shell))

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def _onNodeAdded(self, caller, event, node):
        if node is None or _ownLoads or not node.IsA("vtkMRMLScalarVolumeNode") or node.IsA("vtkMRMLLabelMapVolumeNode"):
            return
        scene = slicer.mrmlScene
        if scene.IsImporting() or scene.IsClosing() or scene.IsRestoring() or node.GetHideFromEditors():
            return
        if not self.shell.isActive() or self.shell.currentModuleName() not in TRANSIENT_LOADER_MODULES:
            return
        self.added.append(node.GetID())
        self.timer.start()

    def _onTimeout(self):
        added, self.added = self.added, []
        try:
            nodes = [slicer.mrmlScene.GetNodeByID(nodeId) for nodeId in added]
            nodes = [node for node in nodes if node is not None and _referenceKind(node) is None]
            if len(nodes) != 1 or not self.shell.isActive() or self.shell.currentModuleName() not in TRANSIENT_LOADER_MODULES:
                return
            if _isTemplate(nodes[0]):
                # The MNI template of the Sample Data module: "Start on MNI template" on it (its "Lesions" is reused)
                startOnTemplate(shell=self.shell, volumeNode=nodes[0])
                return
            result = LoadResult()
            result.volumes.append(nodes[0])
            _afterLoad(result, self.shell)
        except Exception:
            logging.error(f"Studio: cannot open the loaded scan in Draw:\n{traceback.format_exc()}")

    def dispose(self):
        self.timer.stop()
        self.removeObservers()


_transientWatcher = None


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
    """Handle the files dropped on the main window while Studio is shown; open scans loaded by the Sample Data and DICOM
    modules in Draw; guard a figure view; show the importer's notices.
    """
    global _dropHandler, _uninstallOnQuit, _transientWatcher
    uninstall(shell)
    _dropHandler = _DropHandler(shell)
    shell.mainWindow.installEventFilter(_dropHandler)
    if not _uninstallOnQuit:
        # Python event filters must not run while the interpreter is finalized (after the application quits)
        slicer.app.connect("aboutToQuit()", lambda: uninstall(None))
        slicer.app.connect("aboutToQuit()", _disposeOnQuit)
        _uninstallOnQuit = True
    # Scenes saved with the Compare layout open with it (SPEC 9.7 rule 3)
    ensureCompareLayout()
    _forgetCompareAsDrawLayout()
    _transientWatcher = _TransientLoadWatcher(shell)
    _watchTemplate()
    if _figureController is not None:
        _figureController.resume(shell)
    figure = _figureModule()
    if figure is not None:
        try:
            figure.install(shell)
        except Exception:
            logging.error(f"Studio: figure tracing could not be installed:\n{traceback.format_exc()}")
    # The notices that waited in Classic are shown again once the shell is active, and a figure shown in Yellow is guarded
    qt.QTimer.singleShot(0, _refreshNotices)
    qt.QTimer.singleShot(0, lambda: adoptShownFigure(shell))


def _forgetCompareAsDrawLayout():
    """Draw remembers its last layout: Compare only makes sense while a figure is shown."""
    from . import layouts

    key = "Studio/Draw/Layout"
    try:
        remembered = int(slicer.util.settingsValue(key, 0) or 0)
    except (TypeError, ValueError):
        return
    if remembered == COMPARE_LAYOUT_ID and figureController() is None:
        qt.QSettings().setValue(key, layouts.FOCUS_LAYOUT_ID)


def uninstall(shell):
    """Restore the classic drop handling (Add Data dialog) and remove what Studio shows (Classic stays classic)."""
    global _dropHandler, _transientWatcher
    if _transientWatcher is not None:
        _transientWatcher.dispose()
        _transientWatcher = None
    if _figureController is not None:
        _figureController.suspend()
    figure = sys.modules.get("StudioLib.figure")
    if figure is not None:
        try:
            figure.uninstall(shell)
        except Exception:
            logging.error(f"Studio: figure tracing could not be uninstalled:\n{traceback.format_exc()}")
    if shell is not None:
        _clearNotices(shell)
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

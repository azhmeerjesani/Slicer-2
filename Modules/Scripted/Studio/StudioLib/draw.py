"""Draw workspace of Slicer Studio (SPEC 2.6, 3.2, 5.1-5.5, 5.9, 5.10): hooks ``enter(shell)`` and ``leave(shell)``.

Draw is the Segment Editor module in its studio presentation (``SegmentEditorWidget.setPresentation("studio",
extension())``; SPEC D2/D3). This module is that extension and the hooks around it:

- **Panel**: the Lesion card (``StudioLib.lesioncard``) with the brush size chips and the source chip, the tool
  tiles (Brush, Outline, Smart fill, Erase, Fill gaps; "More tools ▾"), the options of the active tool, the
  Display card (collapsed) and a sticky call-to-action footer: "Save lesion · Ctrl+Enter" without a study,
  "Done → next · Ctrl+Enter" with one, and the secondary "Map network →" / "Normalize to MNI…".
- **Tool state** (``_DrawSession``, from QW-A): the user's last tool survives every round trip. Arming is intent
  based (USABILITY 5.2, D-U2): Brush is armed on entry for Start on MNI template, New lesion, a loaded lesion
  file, a loaded set, an explicit request (``requestArm()``, "Continue in Draw") or a return with a recorded
  tool; a plain scan lands unarmed with the Brush tile suggested. Place and window/level modes suspend the tool
  and re-arm it when the mode ends (``suspendedEffect()``).
- **Save model** (USABILITY 8): ``saveLesion()`` writes the lesion on its own grid (the segmentation's reference
  geometry, never the source volume's), a BIDS-style sidecar, and nothing for an empty lesion. After the first save
  of a session the CTA and ``Ctrl+Shift+S`` re-save silently; "Save lesion as…" always asks.
  ``lesionSaveState()`` tells Saved / Unsaved changes / Last saved as / Not saved yet.
- **Metrics** (``lesionMetrics()``): cached by the labelmap content, used by the card (300 ms after an edit).
- **Keys** (Draw only): N / Shift+N gap slices, C centre on the lesion, Alt+←/→ loaded set or study subjects,
  Ctrl+Shift+S save, Ctrl+Y redo; the mnemonics P D M E L Shift+F O come with the presentation.
- **Views**: follow strokes (the other views move to each stroke, ``Studio/FollowStrokes``), a double-click while a
  drawing tool is active does not maximize the view, network maps (``LNM.Result``) are never shown in Draw.
- **Extension points**: ``addInspectorCard()`` / ``removeInspectorCard()``, ``drawcard_*`` modules
  (``DRAW_CARD``), ``LesionCard.addChipRow()`` / ``addMenuAction()``, ``notifyAssist()`` / ``assistCounts()``.

Everything is undone by ``leave()`` and by the classic presentation, which the shell sets in Classic.
Settings: ``Studio/BrushDiameterMm``, ``Studio/FollowStrokes``, ``Studio/LastSaveFolder``,
``Studio/Draw/ShowAllControls``. Node attributes: ``Studio.LesionSavedFile``, ``Studio.LesionSavedTime``,
``Studio.LesionNotes``, ``Studio.LesionSourceReference`` (no absolute path, no process-local value).
"""

import datetime
import html
import importlib
import json
import logging
import math
import os
import pkgutil
import re
import tempfile
import traceback
import weakref

from StudioLib import lazyNumpy as np
import qt
import slicer
import vtk
from slicer.i18n import tr as _
from slicer.util import VTKObservationMixin

from . import lesioncard, space, ui

#: Effect armed when the entry expresses the intent to trace
DEFAULT_EFFECT = "Paint"
#: Draw shortcuts (SPEC 9.6: not used by the views or the Segment Editor)
SAVE_LESION_SHORTCUT = "Ctrl+Shift+S"
REDO_SHORTCUT = "Ctrl+Y"
PRIMARY_SHORTCUT = "Ctrl+Enter"
NEXT_GAP_SHORTCUT = "N"
PREVIOUS_GAP_SHORTCUT = "Shift+N"
CENTER_SHORTCUT = "C"
PREVIOUS_SUBJECT_SHORTCUT = "Alt+Left"
NEXT_SUBJECT_SHORTCUT = "Alt+Right"
#: File dialog filter of "Save lesion…"
NIFTI_FILTER = "NIfTI (*.nii.gz *.nii)"
NIFTI_EXTENSIONS = (".nii.gz", ".nii")
#: Folder of the last saved lesion (a Studio key: official Slicer keys are never written)
LAST_SAVE_FOLDER_KEY = "Studio/LastSaveFolder"
#: Brush diameter (mm) of Paint and Erase, kept across sessions
BRUSH_DIAMETER_KEY = "Studio/BrushDiameterMm"
DEFAULT_BRUSH_DIAMETER_MM = 3.0
BRUSH_CHIPS_MM = (1.0, 3.0, 6.0, 12.0)
#: Follow strokes: the other views move to each stroke
FOLLOW_STROKES_KEY = "Studio/FollowStrokes"
#: "Show all controls" (the classic Segment Editor controls inside Draw)
SHOW_ALL_CONTROLS_KEY = "Studio/Draw/ShowAllControls"

#: Node attributes of the saved state and of the notes (USABILITY 8.9; SPEC 9.7 rule 10)
SAVED_FILE_ATTRIBUTE = "Studio.LesionSavedFile"
SAVED_TIME_ATTRIBUTE = "Studio.LesionSavedTime"
NOTES_ATTRIBUTE = "Studio.LesionNotes"
SOURCE_REFERENCE_ATTRIBUTE = "Studio.LesionSourceReference"
#: Attribute of a network map volume (never shown in Draw)
NETWORK_MAP_ATTRIBUTE = "LNM.Result"

#: Metrics debounce after an edit (SPEC 5.2)
METRICS_DEBOUNCE_MS = 300
#: "Fill gaps and specks": closing kernel (mm) and smallest kept island (mm³)
CLEANUP_CLOSING_MM = 2.0
CLEANUP_MIN_ISLAND_MM3 = 10.0
#: Smooth that removes more than this part of the lesion shows a consequence toast with Undo
SMOOTH_LOSS_FRACTION = 0.5
#: Effects during which a double-click in a view does not maximize it (it closes an outline or is a click)
DOUBLE_CLICK_GUARDED_EFFECTS = ("Paint", "Draw", "Erase", "Level tracing", "Scissors", "Fill between slices", "Smart fill")
#: Effects whose strokes the other views follow
FOLLOWED_EFFECTS = ("Paint", "Erase", "Draw")
#: Priorities of the view interactor observers: the stroke observer sees presses before the Segment Editor (1.0),
#: the double-click guard runs after the editor (so an Outline is still closed by a double-click) and before the
#: interactor style (0.0), which would maximize the view
STROKE_OBSERVER_PRIORITY = 1.5
DOUBLE_CLICK_GUARD_PRIORITY = 0.5
#: Order threshold of inspector cards: below it between the Lesion card and the tools, else after Display
CARD_ORDER_AFTER_DISPLAY = 100

#: BIDS subject entity in a file or node name ("sub-01_T1w.nii.gz" -> "sub-01")
_BIDS_SUBJECT_PATTERN = re.compile(r"(?:^|[_\-/\s])(sub-[A-Za-z0-9]+)")
#: "Lesions (<scan name>)", the segmentation that the importer creates for a scan
_SCAN_LESIONS_PATTERN = re.compile(r"^Lesions? \((.+)\)$")
#: Generic lesion names: "Lesions", "Lesions_1", "Lesion 2", "Segmentation_3"
_GENERIC_NAME_PATTERN = re.compile(r"(?i)^(lesions?|segmentation|mask)([ _-]?\d+)?$")
#: "Lesion 2", "Lesion 3"... created by "New lesion" (importer.newLesionSegmentation)
_NEW_LESION_PATTERN = re.compile(r"^Lesion \d+$")
#: A saved lesion file whose name ends with a number: "case-001_lesion.nii.gz"
_NUMBERED_FILE_PATTERN = re.compile(r"^(.*?)(\d+)(\D*?)(\.nii(?:\.gz)?)$", re.IGNORECASE)
#: Names that say nothing about the case: the file is named "lesion.nii.gz"
_GENERIC_NAMES = ("lesion", "lesions", "segmentation", "mask", "")

#: Studio names and Lucide icons of the tools (D27, SPEC 4.6); effect names and titles are unchanged
EFFECT_APPEARANCE = {
    "Paint": (_("Brush"), "brush"),
    "Draw": (_("Outline"), "lasso"),
    "Smart fill": (_("Smart fill"), "wand-sparkles"),
    "Erase": (_("Erase"), "eraser"),
    "Fill between slices": (_("Fill gaps"), "between-horizontal-start"),
    "Level tracing": (_("Auto outline"), "spline"),
    "Threshold": (_("Threshold"), "contrast"),
    "Grow from seeds": (_("Grow from seeds"), "sprout"),
    "Smoothing": (_("Smooth"), "waves"),
    "Islands": (_("Islands"), "split"),
    "Margin": (_("Margin"), "scaling"),
    "Scissors": (_("Scissors"), "scissors"),
    "Logical operators": (_("Combine"), "combine"),
    "Hollow": (_("Hollow"), "circle-dashed"),
    "Mask volume": (_("Mask volume"), "square-dashed"),
}
#: Mnemonics of the tools (installed by the studio presentation of SegmentEditor.py)
EFFECT_MNEMONICS = {"Paint": "P", "Draw": "D", "Smart fill": "M", "Erase": "E", "Level tracing": "L", "Fill between slices": "Shift+F"}


def _effectDescriptions():
    control = qt.QKeySequence("Ctrl+A").toString(qt.QKeySequence.NativeText)
    control = control[:-1] if control.endswith("A") else control
    control = control[:-1] if control.endswith("+") else control
    return {
        "Paint": _("Paint with a round brush (Shift+wheel or [ ] to resize; Space pauses)"),
        "Draw": _("Trace an outline: drag for a lasso, click for points, Enter or right-click closes (Space pauses)"),
        "Smart fill": _("Click inside a lesion to fill it automatically. Shift+click adds, {control}+click removes, drag or Shift+wheel: "
                        "tolerance, Enter applies").format(control=control),
        "Erase": _("Erase with the brush (E again: the previous tool; Space pauses)"),
        "Fill between slices": _("Fill the slices between traced slices (Space pauses)"),
    }


def _toolHints():
    control = _effectDescriptions()["Smart fill"].split("Shift+click adds, ")[1].split("+click")[0]
    return {
        "Paint": _("Shift + wheel or [ ] to resize · Space pauses"),
        "Erase": _("Shift + wheel or [ ] to resize · E: previous tool"),
        "Draw": _("Drag: lasso · click: points · Enter closes · X removes last"),
        "Smart fill": _("Click inside · Shift+click adds · {control}+click removes · Enter applies").format(control=control),
        "Fill between slices": _("Trace a few slices, then Fill · selected lesion only"),
        "Smoothing": _("Smooth the lesion; Closing fills small holes"),
    }


#: Not recorded yet (first entry)
_UNSET = object()


#
# Segment Editor access
#


def _moduleWidget():
    """The Python Segment Editor module widget, or None if it was not created."""
    return getattr(slicer.modules, "SegmentEditorWidget", None)


def segmentEditor():
    """The ``qMRMLSegmentEditorWidget`` of the Segment Editor module, or None if it was not created."""
    widget = _moduleWidget()
    return getattr(widget, "editor", None) if widget is not None else None


def _isModuleEntered():
    widget = _moduleWidget()
    if widget is None:
        return False
    try:
        return bool(widget.parent.isEntered)
    except Exception:
        return False


def _canEdit(editor):
    """True if an effect can be used: a segmentation with a selected segment and a source volume."""
    segmentationNode = editor.segmentationNode()
    segmentId = editor.currentSegmentID()
    if segmentationNode is None or not segmentId or editor.sourceVolumeNode() is None:
        return False
    return segmentationNode.GetSegmentation().GetSegment(segmentId) is not None


def _canDraw(editor):
    """True if a tool can be picked: a segmentation and a source volume (the first tool adds the "Lesion" segment)."""
    return editor.segmentationNode() is not None and editor.sourceVolumeNode() is not None


#: Name that the Segment Editor gives a segment it adds ("Segment_1"): Draw renames the first one to its lesion name
_DEFAULT_SEGMENT_NAME_PATTERN = re.compile(r"^Segment_\d+$")


def _effectName(editor):
    effect = editor.activeEffect()
    return effect.name if effect is not None else None


def _effectHelpText(effect):
    """The help text of a Segment Editor effect ("" if none)."""
    if effect is None:
        return ""
    helpText = getattr(effect, "helpText", "")
    try:
        helpText = helpText() if callable(helpText) else helpText
    except Exception:
        helpText = ""
    return str(helpText or "")


def _importer():
    try:
        from . import importer
    except Exception:
        return None
    return importer


#: Optional modules looked up by Draw: {name: module or None} (a failed import is not tried again on every refresh)
_optionalModules = {}


def _optionalModule(name):
    if name not in _optionalModules:
        try:
            _optionalModules[name] = importlib.import_module(name)
        except Exception:
            _optionalModules[name] = None
    return _optionalModules[name]


def _isEditableSource(node):
    """A volume that may be the source of Draw: not a figure, a map, a colour image or a single slice."""
    widgetModule = _optionalModule("SegmentEditor")
    check = getattr(widgetModule, "isEditableSourceVolume", None)
    if check is not None:
        return check(node)
    return node is not None and node.IsA("vtkMRMLScalarVolumeNode") and not node.IsA("vtkMRMLLabelMapVolumeNode")


def _editableVolumes():
    return [node for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode") if _isEditableSource(node)]


def _isTemplateVolume(node):
    if node is None:
        return False
    importer = _importer()
    if importer is not None and hasattr(importer, "_isTemplate"):
        try:
            return bool(importer._isTemplate(node))
        except Exception:
            pass
    return node.GetAttribute(space.ROLE_ATTRIBUTE) == space.ROLE_TEMPLATE


def _settingsBool(key, default):
    value = slicer.util.settingsValue(key, default)
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "on")
    return bool(value)


def _settingsFloat(key, default):
    try:
        return float(slicer.util.settingsValue(key, default))
    except (TypeError, ValueError):
        return default


def _sameNode(node, other):
    return node is not None and other is not None and node.GetID() == other.GetID()


def _selectedSegmentId(segmentationNode, editor=None):
    """The segment edited in Draw if it belongs to ``segmentationNode``, else its first segment (or None)."""
    if segmentationNode is None:
        return None
    editor = editor or segmentEditor()
    segmentation = segmentationNode.GetSegmentation()
    if editor is not None and _sameNode(editor.segmentationNode(), segmentationNode):
        segmentId = editor.currentSegmentID()
        if segmentId and segmentation.GetSegment(segmentId) is not None:
            return segmentId
    return segmentation.GetNthSegmentID(0) if segmentation.GetNumberOfSegments() > 0 else None


#
# Lesion metrics (cached by the labelmap content)
#

_metricsCache = {}


def _labelmapArray(segmentationNode, segmentId):
    """``(mask_kji, ijkToRAS 4x4 numpy, labelmap MTime)`` of a segment's internal binary labelmap, or None when empty.

    The array is the shared layer (other segments may use other label values): the mask compares with this
    segment's label value. ``ijkToRAS`` maps the array's (i, j, k) index to RAS.
    """
    try:
        labelmap = segmentationNode.GetBinaryLabelmapInternalRepresentation(segmentId)
    except Exception:
        labelmap = None
    if labelmap is None or labelmap.IsEmpty() or labelmap.GetPointData().GetScalars() is None:
        return None
    segment = segmentationNode.GetSegmentation().GetSegment(segmentId)
    labelValue = segment.GetLabelValue() if segment is not None else 1
    array = slicer.util.arrayFromSegmentInternalBinaryLabelmap(segmentationNode, segmentId)
    imageToWorld = vtk.vtkMatrix4x4()
    labelmap.GetImageToWorldMatrix(imageToWorld)
    matrix = np.array([[imageToWorld.GetElement(row, column) for column in range(4)] for row in range(4)])
    extent = labelmap.GetExtent()
    offset = np.eye(4)
    offset[:3, 3] = (extent[0], extent[2], extent[4])
    return array == labelValue, matrix @ offset, labelmap.GetMTime()


def _contentStamp(segmentationNode):
    """A value that changes when any segment's voxels change: (segment count, newest labelmap MTime)."""
    if segmentationNode is None:
        return None
    segmentation = segmentationNode.GetSegmentation()
    newest = 0
    binaryName = slicer.vtkSegmentationConverter.GetBinaryLabelmapRepresentationName()
    for index in range(segmentation.GetNumberOfSegments()):
        representation = segmentation.GetNthSegment(index).GetRepresentation(binaryName)
        if representation is not None:
            newest = max(newest, representation.GetMTime())
    return segmentation.GetNumberOfSegments(), newest


def lesionMetrics(segmentationNode, compute=False, segmentId=None, axis=0):
    """Metrics of the lesion (``lesioncard.computeLesionMetrics()``) plus ``mL``, ``centroidRas`` and ``sides``.

    ``{voxels, mL, ml, parts, gaps, centroidRas, centroid_ras, sides, outside_pct, bbox, x_min, x_max, spans_both}``
    of the edited segment (else the first). The result is cached by the labelmap content; with ``compute=False`` the
    cache is returned (or None when it is out of date). ``sides`` is the hemisphere word (MNI grids only) or None.
    """
    if segmentationNode is None:
        return None
    segmentId = segmentId or _selectedSegmentId(segmentationNode)
    if not segmentId:
        return None
    key = (segmentationNode.GetID(), segmentId, axis)
    stamp = _contentStamp(segmentationNode)
    cached = _metricsCache.get(key)
    if cached is not None and cached[0] == stamp and cached[1] is segmentationNode.GetSegmentation():
        return cached[2]
    if not compute:
        return None
    data = _labelmapArray(segmentationNode, segmentId)
    if data is None:
        metrics = lesioncard.computeLesionMetrics(np.zeros((1, 1, 1), bool), (1, 1, 1), np.eye(4), axis=axis)
        matrix = None
    else:
        mask, matrix, _mtime = data
        metrics = lesioncard.computeLesionMetrics(mask, None, matrix, axis=axis)
    metrics = dict(metrics)
    metrics["mL"] = metrics["ml"]
    metrics["centroidRas"] = metrics["centroid_ras"]
    metrics["ijkToRAS"] = matrix
    sides = None
    if metrics["voxels"]:
        center = metrics["centroid_ras"][0]
        sides = space.hemisphereWord(center, segmentationNode, partsSpanBoth=metrics["spans_both"])
    metrics["sides"] = sides
    _metricsCache[key] = (stamp, segmentationNode.GetSegmentation(), metrics)
    if len(_metricsCache) > 64:
        _metricsCache.pop(next(iter(_metricsCache)))
    return metrics


#: space.isMni() of segmentations: {node ID: (reference geometry, Studio.Space, result)}
_mniCache = {}


def _isMniLesion(segmentationNode):
    """``space.isMni(segmentationNode)``, cached by the reference geometry (the panel asks on every refresh)."""
    if segmentationNode is None:
        return False
    geometry = segmentationNode.GetSegmentation().GetConversionParameter(
        slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName())
    key = (geometry, segmentationNode.GetAttribute(space.SPACE_ATTRIBUTE))
    cached = _mniCache.get(segmentationNode.GetID())
    if cached is not None and cached[0] == key:
        return cached[1]
    result = bool(space.isMni(segmentationNode))
    _mniCache[segmentationNode.GetID()] = (key, result)
    return result


def isLesionEmpty(segmentationNode, segmentIds=None):
    """True if no voxel of the given segments (default: all) is set."""
    if segmentationNode is None:
        return True
    segmentation = segmentationNode.GetSegmentation()
    if segmentIds is None:
        segmentIds = [segmentation.GetNthSegmentID(index) for index in range(segmentation.GetNumberOfSegments())]
    for segmentId in segmentIds:
        data = _labelmapArray(segmentationNode, segmentId)
        if data is not None and data[0].any():
            return False
        if data is None and segmentationNode.GetBinaryLabelmapInternalRepresentation(segmentId) is None:
            # Not a binary labelmap (closed surface source): empty only when its source representation is
            if segmentHasNonLabelmapContent(segmentationNode, segmentId):
                return False
    return True


def _sourceRepresentation(segmentationNode, segmentId):
    """The data of a segment's source representation (for example its closed surface ``vtkPolyData``), or None."""
    if segmentationNode is None or not segmentId:
        return None
    segmentation = segmentationNode.GetSegmentation()
    segment = segmentation.GetSegment(segmentId)
    if segment is None:
        return None
    # vtkSegment has no Python-usable list of its representations: ask for the source one by name
    representation = segment.GetRepresentation(segmentation.GetSourceRepresentationName())
    if representation is None:
        representation = segment.GetRepresentation(slicer.vtkSegmentationConverter.GetSegmentationClosedSurfaceRepresentationName())
    return representation


def segmentHasNonLabelmapContent(segmentationNode, segmentId):
    """True when a segment without binary labelmap holds data in its source representation (a closed surface with
    points, or a non-empty image such as a fractional labelmap).
    """
    representation = _sourceRepresentation(segmentationNode, segmentId)
    if representation is None:
        return False
    if representation.IsA("vtkPolyData"):
        return representation.GetNumberOfPoints() > 0
    if representation.IsA("vtkImageData"):
        return not representation.IsEmpty() if hasattr(representation, "IsEmpty") else representation.GetNumberOfPoints() > 0
    return True


#: Surface metrics of segments with a closed surface source: {(node ID, segment ID): (polydata, MTime, metrics)}
_surfaceMetricsCache = {}


def surfaceMetrics(segmentationNode, segmentId=None):
    """``{"mL", "parts", "centroidRas", "spans_both"}`` of the closed surface of a segment that has no binary labelmap
    (a segmentation whose source representation is "Closed surface", as official 3D Slicer writes it), else None.
    """
    if segmentationNode is None:
        return None
    segmentId = segmentId or _selectedSegmentId(segmentationNode)
    if not segmentId or segmentationNode.GetBinaryLabelmapInternalRepresentation(segmentId) is not None:
        return None
    polyData = _sourceRepresentation(segmentationNode, segmentId)
    if polyData is None or not polyData.IsA("vtkPolyData") or polyData.GetNumberOfPoints() == 0:
        return None
    key = (segmentationNode.GetID(), segmentId)
    cached = _surfaceMetricsCache.get(key)
    if cached is not None and cached[0] is polyData and cached[1] == polyData.GetMTime():
        return cached[2]
    triangles = vtk.vtkTriangleFilter()
    triangles.SetInputData(polyData)
    connectivity = vtk.vtkPolyDataConnectivityFilter()
    connectivity.SetInputConnection(triangles.GetOutputPort())
    connectivity.SetExtractionModeToAllRegions()
    connectivity.Update()
    parts = connectivity.GetNumberOfExtractedRegions()
    mass = vtk.vtkMassProperties()
    mass.SetInputConnection(triangles.GetOutputPort())
    mass.Update()
    volume = abs(mass.GetVolume()) / 1000.0 if polyData.GetNumberOfPolys() else 0.0
    bounds = polyData.GetBounds()
    center = vtk.vtkCenterOfMass()
    center.SetInputData(polyData)
    center.SetUseScalarsAsWeights(False)
    center.Update()
    metrics = {"mL": volume, "parts": max(parts, 1), "centroidRas": tuple(center.GetCenter()),
               "spans_both": bounds[0] < 0 < bounds[1]}
    _surfaceMetricsCache[key] = (polyData, polyData.GetMTime(), metrics)
    if len(_surfaceMetricsCache) > 64:
        _surfaceMetricsCache.pop(next(iter(_surfaceMetricsCache)))
    return metrics


#
# State kept across workspace switches
#


class DrawState:
    """What ``leave()`` records and ``enter()`` restores.

    ``effect`` is the name of the active effect, None if the user deselected it, ``_UNSET`` before the first
    entry. The node and segment IDs identify what was edited. The segmentation node is also kept as a weak
    reference: node IDs are reused after the scene is closed, so the ID alone does not tell a new case apart.
    """

    def __init__(self):
        self.effect = _UNSET
        self.segmentationNodeId = None
        self.segmentId = None
        self.sourceVolumeNodeId = None
        self._segmentationNodeRef = vtk.vtkWeakReference()

    def isRecorded(self):
        return self.effect is not _UNSET

    def segmentationNode(self):
        """The recorded segmentation node, None if there is none or it was deleted."""
        return self._segmentationNodeRef.Get()

    def setSegmentationNode(self, segmentationNode):
        self._segmentationNodeRef.Set(segmentationNode)
        self.segmentationNodeId = segmentationNode.GetID() if segmentationNode is not None else None

    def isDeselectedOn(self, segmentationNode):
        """True if the user deselected the tool (Esc) while editing ``segmentationNode``."""
        return self.effect is None and segmentationNode is not None and self.segmentationNode() is segmentationNode

    def __repr__(self):
        effect = "<unset>" if self.effect is _UNSET else self.effect
        return (f"DrawState(effect={effect!r}, segmentation={self.segmentationNodeId!r}, segment={self.segmentId!r}, "
                f"source={self.sourceVolumeNodeId!r})")


class _Snapshot:
    def __init__(self, editor):
        self.effect = _effectName(editor)
        self.segmentationNodeId = editor.segmentationNodeID() or None
        self.segmentId = editor.currentSegmentID() or None
        self.sourceVolumeNodeId = editor.sourceVolumeNodeID() or None
        self.canEdit = _canEdit(editor)
        # Node IDs are reused after the scene is closed: the node itself tells a new case apart
        self._segmentationNodeRef = vtk.vtkWeakReference()
        self._segmentationNodeRef.Set(editor.segmentationNode())

    def segmentationNode(self):
        return self._segmentationNodeRef.Get()

    def sameTarget(self, other):
        return (self._segmentationNodeRef.Get() is other._segmentationNodeRef.Get()
                and (self.segmentationNodeId, self.segmentId, self.sourceVolumeNodeId) == (
                    other.segmentationNodeId, other.segmentId, other.sourceVolumeNodeId))


class _DrawSession(VTKObservationMixin):
    """Draw state of one shell: the recorded tool, the footer, the shortcuts and the observers while Draw is shown."""

    def __init__(self, shell):
        VTKObservationMixin.__init__(self)
        self.shellRef = weakref.ref(shell)
        self.state = DrawState()
        #: Effect to activate as soon as the editor can edit (None: nothing to arm)
        self.pendingEffect = None
        self.active = False
        self.footer = None
        self.redoShortcut = None
        self.shortcutsRegistered = False
        self._redoKeyFree = None
        #: Effect deselected by a Place or window/level mode, armed again when the mode ends
        self._suspendedEffect = None
        #: The next entry (or the next lesion) arms Brush: an explicit intent ("Continue in Draw", New lesion)
        self._armRequested = False
        self._snapshot = None
        self._observedNode = None
        self._observedSegmentation = None
        self._observedInteractionNode = None
        self._viewObservations = []
        self._strokePress = None
        self._lastStroke = None
        self._smoothBaseline = None
        self._voxelViewDefaulted = set()
        self._layoutManagerConnected = False
        #: Shortcuts of the module navigation actions while Draw takes Alt+Left/Right from them: {objectName: [keys]}
        self._takenActionShortcuts = {}
        # Parameter node changes are coalesced: one evaluation per event loop iteration
        self._timer = qt.QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.connect("timeout()", self._evaluate)
        self._metricsTimer = qt.QTimer()
        self._metricsTimer.setSingleShot(True)
        self._metricsTimer.setInterval(METRICS_DEBOUNCE_MS)
        self._metricsTimer.connect("timeout()", self._onMetricsTimeout)
        self._refreshTimer = qt.QTimer()
        self._refreshTimer.setSingleShot(True)
        self._refreshTimer.setInterval(0)
        self._refreshTimer.connect("timeout()", self.refreshPanel)
        self._adoptTimer = qt.QTimer()
        self._adoptTimer.setSingleShot(True)
        self._adoptTimer.setInterval(0)
        self._adoptTimer.connect("timeout()", self._adoptNewSegment)
        self._followTimer = qt.QTimer()
        self._followTimer.setSingleShot(True)
        self._followTimer.setInterval(30)
        self._followTimer.connect("timeout()", self._followStroke)

    @property
    def shell(self):
        return self.shellRef()

    #
    # enter / leave
    #

    def enter(self):
        shell = self.shell
        self.active = True
        ext = extension()
        ext.attach(self)
        widget = _moduleWidget()
        if widget is not None and hasattr(widget, "setPresentation"):
            ext.showAllControls = _settingsBool(SHOW_ALL_CONTROLS_KEY, False)
            widget.setPresentation("studio", ext)
        self._installFooter(shell)
        self._installShortcuts(shell)
        self._takeActionShortcuts(shell)
        editor = segmentEditor()
        if editor is None:
            return
        ext.ensureSegmentation(editor)
        self._applyBrushDefault(editor)
        _removeNetworkMaps()
        self._applyVoxelViewDefault(editor)
        self._restore(editor)
        self._observe(editor)
        self._observeScene()
        self._observeInteractionNode()
        self._observeSegmentation(editor.segmentationNode())
        self._installViewObservers()
        _registerPaletteProvider()
        _registerAssistObserver()
        self._snapshot = _Snapshot(editor)
        # The effect is armed on the next event loop iteration (_evaluate()), after the views that the switch shows
        # (Library page hidden, Draw layout) got their Enter events. Armed now, Paint would get the Enter event of
        # the view under the mouse pointer and draw its brush before the brush has a shape (VTK error
        # "vtkTransformPolyDataFilter has 0 connections"), until the first mouse move.
        self._timer.start()
        self.refreshPanel()
        self._metricsTimer.start()

    def leave(self):
        shell = self.shell
        self._timer.stop()
        self._metricsTimer.stop()
        self._refreshTimer.stop()
        self._followTimer.stop()
        if self._adoptTimer.isActive():
            self._adoptTimer.stop()
            self._adoptNewSegment()
        editor = segmentEditor()
        if editor is not None:
            self._record(editor)
            # The classic look of the tool options (Draw gives them back on enter, see DrawExtension.refresh())
            setOptionsStudioLook(editor, False)
        self.removeObservers()
        self._observedNode = None
        self._observedSegmentation = None
        self._observedInteractionNode = None
        self._removeViewObservers()
        self._snapshot = None
        self.pendingEffect = None
        self._strokePress = None
        self._lastStroke = None
        self._removeShortcuts()
        self._restoreActionShortcuts()
        _unregisterAssistObserver()
        if self.footer is not None:
            if shell is not None and shell.inspectorFooter() == self.footer.widget:
                shell.setInspectorFooter(None)
            self.footer.dispose()
            self.footer = None
        # The shell clears the primary action (Ctrl+Enter) when the workspace is left
        extension().detach(self)
        self.active = False

    def _record(self, editor):
        state = self.state
        if self._timer.isActive():
            # A change of the last event loop iteration that was not followed yet
            self._timer.stop()
            self._evaluate()
        if self._suspendedEffect is not None:
            # Left while a Place or window/level mode suspends the tool: the tool is still the user's choice
            state.effect = self._suspendedEffect
            self._suspendedEffect = None
        elif _isModuleEntered():
            # Left before the module (workspace switch, Classic): the editor still has its effect. Without one,
            # the state already holds the user's choice: None after Esc, or the tool that is waiting to be armed.
            effect = _effectName(editor)
            if effect is not None:
                state.effect = effect
        # Else the module was left first (transient module): its exit() deactivated the effect, and the state
        # holds the effect that was active before (see _evaluate()).
        segmentationNode = editor.segmentationNode()
        if state.effect is None and segmentationNode is not None and not state.isDeselectedOn(segmentationNode):
            # Deselected on another lesion than the one now edited: the next entry is a new landing
            state.effect = _UNSET
        state.setSegmentationNode(segmentationNode)
        state.segmentId = editor.currentSegmentID() or None
        state.sourceVolumeNodeId = editor.sourceVolumeNodeID() or None

    def _restore(self, editor):
        """Restore the recorded segment and source volume, and choose the effect to arm."""
        state = self.state
        scene = slicer.mrmlScene
        segmentationNode = editor.segmentationNode()
        if (state.segmentId and segmentationNode is not None and state.segmentationNode() is segmentationNode
                and segmentationNode.GetSegmentation().GetSegment(state.segmentId) is not None
                and editor.currentSegmentID() != state.segmentId):
            editor.setCurrentSegmentID(state.segmentId)
        if editor.sourceVolumeNode() is None and state.sourceVolumeNodeId:
            sourceVolumeNode = scene.GetNodeByID(state.sourceVolumeNodeId)
            if sourceVolumeNode is not None and _isEditableSource(sourceVolumeNode):
                editor.setSourceVolumeNode(sourceVolumeNode)
        if state.effect is None and segmentationNode is not None and not state.isDeselectedOn(segmentationNode):
            # Deselected with Esc on another lesion (another case was opened meanwhile): a new landing
            state.effect = _UNSET
        if state.isRecorded() and state.effect is not None and not _sameNode(state.segmentationNode(), segmentationNode) and not self._intendsToTrace(editor):
            # Another lesion than the one the tool was used on, opened without the intent to trace (a dropped scan)
            state.effect = _UNSET
        self.pendingEffect = self._landingEffect(editor) if not state.isRecorded() else state.effect

    #
    # Arming policy (USABILITY 5.2, D-U2)
    #

    def requestArm(self):
        """The next entry (or the next lesion shown in Draw) arms Brush: the user asked to trace."""
        self._armRequested = True
        if self.active:
            self.pendingEffect = DEFAULT_EFFECT
            self._timer.start()

    def _intendsToTrace(self, editor):
        """True if what Draw shows was opened to be traced (not a plain scan opened to be looked at)."""
        if self._armRequested:
            return True
        segmentationNode, sourceVolumeNode = editor.segmentationNode(), editor.sourceVolumeNode()
        if segmentationNode is None or sourceVolumeNode is None:
            return False
        if _isTemplateVolume(sourceVolumeNode):
            return True
        importer = _importer()
        loadedSet = importer.loadedSet() if importer is not None and hasattr(importer, "loadedSet") else None
        if loadedSet is not None and hasattr(loadedSet, "contains") and loadedSet.contains(segmentationNode):
            return True
        storageNode = segmentationNode.GetStorageNode()
        if storageNode is not None and storageNode.GetFileName():
            # A lesion file that was opened
            return True
        if _NEW_LESION_PATTERN.match(segmentationNode.GetName() or ""):
            return True
        return not isLesionEmpty(segmentationNode)

    def _landingEffect(self, editor):
        """Brush when the entry expresses the intent to trace, else nothing (the Brush tile is suggested)."""
        if self._intendsToTrace(editor):
            self._armRequested = False
            return DEFAULT_EFFECT
        return None

    def suggestedEffect(self):
        """The tool shown as suggested (outlined, not checked) while none is active, or None."""
        editor = segmentEditor()
        if editor is None or editor.activeEffect() is not None or not _canDraw(editor):
            return None
        if self.state.isRecorded() and self.state.effect is None and self._snapshot is not None and self._snapshot.effect is None:
            # Deselected (Esc or Space): Space resumes the last tool
            return None
        return DEFAULT_EFFECT

    def pausedEffect(self):
        """The tool that Space resumes after it was deselected (Esc or Space), or None."""
        editor = segmentEditor()
        if editor is None or editor.activeEffect() is not None:
            return None
        last = self._lastActiveEffect
        return last if last and self.state.effect is None else None

    _lastActiveEffect = None

    #
    # Tracking of the active effect while Draw is shown
    #

    def _observe(self, editor):
        node = editor.mrmlSegmentEditorNode()
        if node is self._observedNode:
            return
        if self._observedNode is not None:
            self.removeObserver(self._observedNode, vtk.vtkCommand.ModifiedEvent, self._onParameterNodeModified)
        self._observedNode = node
        if node is not None:
            self.addObserver(node, vtk.vtkCommand.ModifiedEvent, self._onParameterNodeModified)

    def _onParameterNodeModified(self, caller, event):
        self._timer.start()

    def _evaluate(self):
        """Follow the effect chosen by the user, and arm the pending effect when the editor can edit."""
        if not self.active:
            return
        editor = segmentEditor()
        if editor is None or not _isModuleEntered():
            # The module is being left (its exit() deactivates the effect): not a choice of the user
            return
        self._observe(editor)
        snapshot = _Snapshot(editor)
        previous = self._snapshot
        self._snapshot = snapshot
        segmentationNode = editor.segmentationNode()
        if previous is not None:
            if snapshot.effect is not None and snapshot.effect != previous.effect:
                # The user picked a tool
                self.state.effect = snapshot.effect
                self.pendingEffect = None
                self._onEffectChanged(editor, snapshot.effect)
            elif snapshot.effect is None and previous.effect is not None:
                if self._suspendedEffect is not None:
                    # Deselected by a Place or window/level mode: not the user's choice, armed again when it ends
                    pass
                elif snapshot.sameTarget(previous) and snapshot.canEdit:
                    # Deselected with Esc, Space or the active tile: do not fight it (on this lesion)
                    self.state.effect = None
                    self.state.setSegmentationNode(segmentationNode)
                    self.pendingEffect = None
                elif previous.segmentationNode() is not segmentationNode and segmentationNode is not None and not self._intendsToTrace(editor):
                    # Another lesion that was not opened to be traced (a dropped scan): it lands unarmed (D-U2)
                    self.state.effect = _UNSET
                    self.pendingEffect = None
                else:
                    # Deactivated by the editor (another segmentation or segment, scene closed): arm it again
                    self.pendingEffect = previous.effect
                self._onEffectChanged(editor, None)
            if snapshot.effect is None and previous.segmentationNode() is not segmentationNode and segmentationNode is not None:
                # Another lesion is shown without an active tool: a landing
                if not self.state.isRecorded() and self.pendingEffect is None:
                    self.pendingEffect = self._landingEffect(editor)
            if previous.segmentationNode() is not segmentationNode:
                self._observeSegmentation(segmentationNode)
            if previous.sourceVolumeNodeId != snapshot.sourceVolumeNodeId:
                self._applyVoxelViewDefault(editor)
                extension().refreshCards()
        if snapshot.effect is not None:
            self._lastActiveEffect = snapshot.effect
        if (snapshot.effect is None and self.state.effect is None and segmentationNode is not None
                and not self.state.isDeselectedOn(segmentationNode) and self._suspendedEffect is None):
            # Deselected with Esc on another lesion, and now a new case or lesion is edited: a new landing
            self.state.effect = _UNSET
            self.pendingEffect = self._landingEffect(editor)
        self._tryArm(editor)
        self._persistBrushDiameter(editor)
        self.refreshPanel()

    def _tryArm(self, editor):
        effectName = self.pendingEffect
        if not effectName:
            return
        if editor.activeEffect() is not None:
            self.pendingEffect = None
            return
        if not _isModuleEntered() or not _canDraw(editor):
            return
        if not _canEdit(editor) and extension().ensureLesionSegment(editor) is None:
            return
        if editor.effectByName(effectName) is None:
            self.pendingEffect = None
            return
        self.pendingEffect = None
        if effectName in ("Paint", "Erase"):
            self._applyBrushDefault(editor)
        editor.setActiveEffectByName(effectName)

    def _onEffectChanged(self, editor, effectName):
        """A tool was chosen (or deselected): Smooth safety. The brush size is not set here: Brush and Erase share it (a
        common parameter), and the size the user (or a script) just set is kept and remembered (_persistBrushDiameter()).
        """
        self._smoothBaseline = None
        if effectName == "Smoothing":
            self._prepareSmoothing(editor)

    #
    # Place and window/level modes suspend the tool (U14)
    #

    def _observeInteractionNode(self):
        node = slicer.app.applicationLogic().GetInteractionNode() if slicer.app.applicationLogic() is not None else None
        if node is None or node is self._observedInteractionNode:
            return
        self._observedInteractionNode = node
        self.addObserver(node, vtk.vtkCommand.ModifiedEvent, self._onInteractionNodeModified)

    def _onInteractionNodeModified(self, caller, event):
        if not self.active:
            return
        mode = caller.GetCurrentInteractionMode()
        if mode != slicer.vtkMRMLInteractionNode.ViewTransform:
            if self._suspendedEffect is None:
                editor = segmentEditor()
                name = _effectName(editor) if editor is not None else None
                if name is None and self._snapshot is not None:
                    name = self._snapshot.effect
                if name is not None:
                    self._suspendedEffect = name
                    self.refreshPanel()
            return
        if self._suspendedEffect is not None:
            self.pendingEffect = self._suspendedEffect
            self.state.effect = self._suspendedEffect
            self._suspendedEffect = None
            self._timer.start()

    def suspendedEffect(self):
        """The tool deselected by a Place or window/level mode, armed again when the mode ends (None if none)."""
        return self._suspendedEffect

    #
    # Brush size, voxel view, network maps
    #

    def _applyBrushDefault(self, editor, onlyWhenAbsolute=False):
        """Paint and Erase: absolute diameter ``Studio/BrushDiameterMm`` (default 3 mm)."""
        effect = editor.effectByName("Paint")
        if effect is None or editor.mrmlSegmentEditorNode() is None:
            return
        diameter = _settingsFloat(BRUSH_DIAMETER_KEY, DEFAULT_BRUSH_DIAMETER_MM)
        try:
            if onlyWhenAbsolute and effect.integerParameter("BrushDiameterIsRelative"):
                return
            if effect.integerParameter("BrushDiameterIsRelative"):
                effect.setCommonParameter("BrushDiameterIsRelative", 0)
            if abs(effect.doubleParameter("BrushAbsoluteDiameter") - diameter) > 1e-6:
                effect.setCommonParameter("BrushAbsoluteDiameter", diameter)
        except Exception:
            logging.debug(f"Studio: cannot set the brush size:\n{traceback.format_exc()}")

    def _persistBrushDiameter(self, editor):
        name = _effectName(editor)
        if name not in ("Paint", "Erase"):
            return
        effect = editor.activeEffect()
        try:
            if effect.integerParameter("BrushDiameterIsRelative"):
                return
            diameter = round(float(effect.doubleParameter("BrushAbsoluteDiameter")), 4)
        except Exception:
            return
        if diameter > 0 and abs(diameter - _settingsFloat(BRUSH_DIAMETER_KEY, DEFAULT_BRUSH_DIAMETER_MM)) > 1e-6:
            qt.QSettings().setValue(BRUSH_DIAMETER_KEY, diameter)

    def setBrushDiameter(self, diameterMm):
        """Brush size chips: Paint and Erase draw with ``diameterMm`` (absolute), kept in ``Studio/BrushDiameterMm``."""
        qt.QSettings().setValue(BRUSH_DIAMETER_KEY, float(diameterMm))
        editor = segmentEditor()
        if editor is not None:
            self._applyBrushDefault(editor)
        self.refreshPanel()

    def _applyVoxelViewDefault(self, editor):
        """Voxel view (nearest interpolation) is on by default for a source on an MNI grid (SPEC 5.3), once per volume."""
        sourceVolumeNode = editor.sourceVolumeNode()
        if sourceVolumeNode is None or sourceVolumeNode.GetID() in self._voxelViewDefaulted:
            return
        self._voxelViewDefaulted.add(sourceVolumeNode.GetID())
        if not (_isTemplateVolume(sourceVolumeNode) or space.isMni(sourceVolumeNode)):
            return
        displayNode = sourceVolumeNode.GetDisplayNode()
        if displayNode is not None and hasattr(displayNode, "SetInterpolate") and displayNode.GetInterpolate():
            displayNode.SetInterpolate(0)

    #
    # Observers of the scene, the edited segmentation and the views
    #

    def _observeScene(self):
        scene = slicer.mrmlScene
        for event in (scene.NodeAddedEvent, scene.NodeRemovedEvent, scene.EndImportEvent):
            if not self.hasObserver(scene, event, self._onSceneChanged):
                self.addObserver(scene, event, self._onSceneChanged)
        # A handler of its own: the event argument of a shared handler does not tell the MRML scene events apart
        if not self.hasObserver(scene, scene.EndCloseEvent, self._onSceneEndClose):
            self.addObserver(scene, scene.EndCloseEvent, self._onSceneEndClose)

    def _onSceneChanged(self, caller, event):
        self._refreshTimer.start()

    def _onSceneEndClose(self, caller, event):
        self._voxelViewDefaulted = set()
        _onSceneClosed()
        self._onSceneClosed()
        self._refreshTimer.start()

    def _onSceneClosed(self):
        """A closed scene ends the case: the next lesion is a new landing (a dropped scan lands unarmed, D-U2), and the
        tool that the closing deactivated is not armed again. An explicit intent (``requestArm()``) is kept.
        """
        self.state.effect = _UNSET
        self.state.setSegmentationNode(None)
        self.state.segmentId = None
        self.state.sourceVolumeNodeId = None
        self._suspendedEffect = None
        self._lastActiveEffect = None
        self.pendingEffect = DEFAULT_EFFECT if self._armRequested else None
        editor = segmentEditor()
        if editor is not None:
            if editor.activeEffect() is not None:
                # The editor keeps its effect without a segmentation: the next case would start with it
                editor.setActiveEffect(None)
            # The deactivation by the closing is not compared with the tool of the closed case
            self._snapshot = _Snapshot(editor)

    def _observeSegmentation(self, segmentationNode):
        if self._observedSegmentation is not None:
            node, segmentation = self._observedSegmentation
            for event in (slicer.vtkSegmentation.SourceRepresentationModified, slicer.vtkSegmentation.SegmentModified,
                          slicer.vtkSegmentation.SegmentAdded, slicer.vtkSegmentation.SegmentRemoved):
                self.removeObserver(segmentation, event, self._onSegmentationModified)
            self.removeObserver(segmentation, slicer.vtkSegmentation.SegmentAdded, self._onSegmentAdded)
            self.removeObserver(node, vtk.vtkCommand.ModifiedEvent, self._onSegmentationModified)
            self.removeObserver(node, slicer.vtkMRMLDisplayableNode.DisplayModifiedEvent, self._onSegmentationNodeModified)
        self._observedSegmentation = None
        if segmentationNode is None or segmentationNode.GetSegmentation() is None:
            return
        segmentation = segmentationNode.GetSegmentation()
        self._observedSegmentation = (segmentationNode, segmentation)
        for event in (slicer.vtkSegmentation.SourceRepresentationModified, slicer.vtkSegmentation.SegmentModified,
                      slicer.vtkSegmentation.SegmentAdded, slicer.vtkSegmentation.SegmentRemoved):
            self.addObserver(segmentation, event, self._onSegmentationModified)
        self.addObserver(segmentation, slicer.vtkSegmentation.SegmentAdded, self._onSegmentAdded)
        # Display changes (eye, opacity, fill) show at once; other changes of the node (each stroke modifies it) with the
        # 300 ms metrics debounce, so that a stroke does not wait for the panel
        self.addObserver(segmentationNode, vtk.vtkCommand.ModifiedEvent, self._onSegmentationModified)
        self.addObserver(segmentationNode, slicer.vtkMRMLDisplayableNode.DisplayModifiedEvent, self._onSegmentationNodeModified)

    def _onSegmentationModified(self, caller, event):
        self._metricsTimer.start()

    def _onSegmentAdded(self, caller, event):
        # After the editor selected it and activated the tool (renaming inside the event would interleave with them)
        self._adoptTimer.start()

    def _adoptNewSegment(self):
        if self.active and self._observedSegmentation is not None:
            extension().adoptNewSegment(self._observedSegmentation[0])

    def _onSegmentationNodeModified(self, caller, event):
        self._refreshTimer.start()

    def _onMetricsTimeout(self):
        if not self.active:
            return
        editor = segmentEditor()
        if editor is not None and _effectName(editor) == "Smoothing":
            self._checkSmoothingLoss(editor)
        self.refreshPanel(computeMetrics=True)

    def _installViewObservers(self):
        self._removeViewObservers()
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        if not self._layoutManagerConnected:
            layoutManager.connect("layoutChanged(int)", self._onLayoutChanged)
            self._layoutManagerConnected = True
        for viewName in layoutManager.sliceViewNames():
            sliceWidget = layoutManager.sliceWidget(viewName)
            if sliceWidget is None:
                continue
            interactor = sliceWidget.sliceView().interactor()
            if interactor is None:
                continue
            observation = [interactor, viewName, []]
            for event, handler, priority in ((vtk.vtkCommand.LeftButtonPressEvent, self._onStrokePress, STROKE_OBSERVER_PRIORITY),
                                             (vtk.vtkCommand.LeftButtonReleaseEvent, self._onStrokeRelease, STROKE_OBSERVER_PRIORITY),
                                             (vtk.vtkCommand.LeftButtonDoubleClickEvent, self._onDoubleClick, DOUBLE_CLICK_GUARD_PRIORITY)):
                self._addViewObserver(observation, event, handler, priority, viewName)
            self._viewObservations.append(observation)

    def _addViewObserver(self, observation, event, handler, priority, viewName):
        interactor = observation[0]
        command = []

        def callback(caller, eventName):
            try:
                if handler(caller, viewName) and command:
                    command[0].SetAbortFlag(1)
            except Exception:
                logging.error(f"Studio Draw view observer failed:\n{traceback.format_exc()}")

        tag = interactor.AddObserver(event, callback, priority)
        command.append(interactor.GetCommand(tag))
        observation[2].append(tag)

    def _removeViewObservers(self):
        for interactor, _viewName, tags in self._viewObservations:
            for tag in tags:
                try:
                    interactor.RemoveObserver(tag)
                except Exception:
                    pass
        self._viewObservations = []
        if self._layoutManagerConnected:
            layoutManager = slicer.app.layoutManager()
            if layoutManager is not None:
                try:
                    layoutManager.disconnect("layoutChanged(int)", self._onLayoutChanged)
                except Exception:
                    pass
            self._layoutManagerConnected = False

    def _onLayoutChanged(self, layout=None):
        if self.active:
            qt.QTimer.singleShot(0, self._reinstallViewObservers)

    def _reinstallViewObservers(self):
        if self.active:
            self._installViewObservers()

    def _onDoubleClick(self, interactor, viewName):
        """A double-click with a drawing tool does not maximize the view (the effect still gets it first)."""
        editor = segmentEditor()
        return editor is not None and _effectName(editor) in DOUBLE_CLICK_GUARDED_EFFECTS

    def _onStrokePress(self, interactor, viewName):
        editor = segmentEditor()
        if editor is None or _effectName(editor) not in FOLLOWED_EFFECTS or interactor.GetShiftKey() or interactor.GetControlKey() or interactor.GetAltKey():
            self._strokePress = None
            return False
        self._strokePress = (viewName, _contentStamp(editor.segmentationNode()))
        return False

    def _onStrokeRelease(self, interactor, viewName):
        press = self._strokePress
        self._strokePress = None
        if press is None or press[0] != viewName:
            return False
        sliceWidget = slicer.app.layoutManager().sliceWidget(viewName)
        if sliceWidget is None:
            return False
        x, y = interactor.GetEventPosition()
        xyToRas = sliceWidget.mrmlSliceNode().GetXYToRAS()
        ras = xyToRas.MultiplyPoint((x, y, 0, 1))[:3]
        self._lastStroke = (viewName, tuple(ras), press[1])
        # After the effect applied the stroke (on release, or a few events later)
        self._followTimer.start()
        return False

    def _followStroke(self):
        """Move the other non-figure views to the last stroke (``Studio/FollowStrokes``), if it changed the lesion."""
        stroke = self._lastStroke
        self._lastStroke = None
        editor = segmentEditor()
        if stroke is None or editor is None or not self.active or not _settingsBool(FOLLOW_STROKES_KEY, True):
            return
        viewName, ras, stampBefore = stroke
        if _contentStamp(editor.segmentationNode()) == stampBefore:
            # The press did not change the lesion (a click outside, Space paused...)
            return
        followStroke(viewName, ras)

    #
    # Smooth safety (U15)
    #

    def _prepareSmoothing(self, editor):
        segmentationNode = editor.segmentationNode()
        metrics = lesionMetrics(segmentationNode, compute=True)
        if metrics is None or not metrics["voxels"]:
            return
        self._smoothBaseline = metrics["voxels"]
        k0, k1, j0, j1, i0, i1 = metrics["bbox"]
        if min(k1 - k0, j1 - j0, i1 - i0) + 1 < 3:
            effect = editor.activeEffect()
            try:
                if effect.parameter("SmoothingMethod") != "MORPHOLOGICAL_CLOSING":
                    effect.setParameter("SmoothingMethod", "MORPHOLOGICAL_CLOSING")
            except Exception:
                return
            extension().setTransientHint(_("Thin lesion: Closing keeps it"))

    def _checkSmoothingLoss(self, editor):
        segmentationNode = editor.segmentationNode()
        metrics = lesionMetrics(segmentationNode, compute=True)
        if metrics is None:
            return
        baseline = self._smoothBaseline
        self._smoothBaseline = metrics["voxels"]
        if baseline and metrics["voxels"] < baseline * (1.0 - SMOOTH_LOSS_FRACTION):
            percent = round(100.0 * (baseline - metrics["voxels"]) / baseline)
            shell = self.shell
            if shell is not None:
                shell.toast(_("Smooth removed {pct}% of {lesion}").format(pct=percent, lesion=segmentationNode.GetName()),
                            action=_undo, actionText=_("Undo"), level="consequence")

    #
    # Panel
    #

    def refreshPanel(self, computeMetrics=False):
        if not self.active:
            return
        try:
            extension().refresh(self, computeMetrics=computeMetrics)
        except Exception:
            logging.error(f"Studio: the Draw panel could not be updated:\n{traceback.format_exc()}")
        self._updateFooter()

    #
    # Footer and shortcuts
    #

    def _installFooter(self, shell):
        if self.footer is None:
            # Created on every entry and disposed by leave(): nothing is kept across Classic
            self.footer = _CtaFooter(self)
        shell.setInspectorFooter(self.footer.widget)
        self._updateFooter()

    def _updateFooter(self):
        shell = self.shell
        if self.footer is None or shell is None:
            return
        label, callback = self.footer.update()
        if callback is not None and (shell.primaryActionLabel() != label or not shell.primaryActionLabel()):
            shell.setPrimaryAction(callback, label)

    def _installShortcuts(self, shell):
        if not self.shortcutsRegistered:
            # Installed now (Draw is the current workspace) and every time Draw is entered again
            for keySequence, callback, description in (
                    (SAVE_LESION_SHORTCUT, lambda: saveLesionNow(self.shell), _("Save lesion")),
                    (NEXT_GAP_SHORTCUT, lambda: jumpToGap(1, self.shell), _("Next gap slice of the lesion")),
                    (PREVIOUS_GAP_SHORTCUT, lambda: jumpToGap(-1, self.shell), _("Previous gap slice of the lesion")),
                    (CENTER_SHORTCUT, lambda: centerOnLesion(self.shell), _("Center on lesion")),
                    (PREVIOUS_SUBJECT_SHORTCUT, lambda: stepSubject(-1, self.shell), _("Previous lesion or subject")),
                    (NEXT_SUBJECT_SHORTCUT, lambda: stepSubject(1, self.shell), _("Next lesion or subject"))):
                shell.addWorkspaceShortcut("draw", keySequence, callback, description)
            self.shortcutsRegistered = True
        self._removeShortcuts()
        if self._redoKeyFree is None:
            # Checked once: the other shortcuts of the main window do not change while Studio is shown
            self._redoKeyFree = _keyIsFree(shell.mainWindow, REDO_SHORTCUT)
        if self._redoKeyFree:
            # Not a Studio shortcut: it is the Segment Editor's redo, under the key that other applications use
            shortcut = qt.QShortcut(qt.QKeySequence(REDO_SHORTCUT), shell.mainWindow)
            shortcut.setObjectName("StudioDrawRedoShortcut")
            shortcut.setContext(qt.Qt.WindowShortcut)
            shortcut.connect("activated()", _redo)
            self.redoShortcut = shortcut

    def _takeActionShortcuts(self, shell):
        """Alt+Left/Right step through a loaded set or a study in Draw: the module navigation actions (whose
        QKeySequence::Back/Forward are Alt+Left/Right on Windows and Linux) keep only their other keys meanwhile.
        """
        taken = {qt.QKeySequence(key).toString(qt.QKeySequence.PortableText) for key in (PREVIOUS_SUBJECT_SHORTCUT, NEXT_SUBJECT_SHORTCUT)}
        for objectName in ("PreviousModuleAction", "NextModuleAction"):
            if objectName in self._takenActionShortcuts:
                continue
            action = shell.mainWindow.findChild("QAction", objectName)
            if action is None:
                continue
            shortcuts = list(action.shortcuts())
            kept = [key for key in shortcuts if key.toString(qt.QKeySequence.PortableText) not in taken]
            if len(kept) != len(shortcuts):
                self._takenActionShortcuts[objectName] = (action, shortcuts)
                action.setShortcuts(kept)

    def _restoreActionShortcuts(self):
        for objectName, (action, shortcuts) in list(self._takenActionShortcuts.items()):
            try:
                action.setShortcuts(shortcuts)
            except Exception:
                # Deleted with the main window
                pass
        self._takenActionShortcuts = {}

    def _removeShortcuts(self):
        if self.redoShortcut is None:
            return
        try:
            self.redoShortcut.setEnabled(False)
            self.redoShortcut.setObjectName("")
            self.redoShortcut.deleteLater()
        except Exception:
            # Deleted with the main window
            pass
        self.redoShortcut = None


def _keyIsFree(mainWindow, keySequence):
    """True if no enabled shortcut or action of the main window already uses ``keySequence``.

    A second shortcut would make the key ambiguous, and Qt would trigger neither (on Windows the Segment Editor
    already binds Ctrl+Y as the standard redo key).
    """
    portable = qt.QKeySequence(keySequence).toString(qt.QKeySequence.PortableText)
    for shortcut in mainWindow.findChildren("QShortcut"):
        if shortcut.enabled and shortcut.key.toString(qt.QKeySequence.PortableText) == portable:
            return False
    for action in mainWindow.actions():
        if action.enabled and any(key.toString(qt.QKeySequence.PortableText) == portable for key in action.shortcuts()):
            return False
    return True


#: Widgets that take typed text: Ctrl+Y there must not change the lesion (the user is typing, e.g. a segment name)
_TEXT_INPUT_CLASSES = ("QLineEdit", "QAbstractSpinBox", "QTextEdit", "QPlainTextEdit", "ctkConsole")


def _redo():
    focusWidget = slicer.app.focusWidget()
    if focusWidget is not None and any(focusWidget.inherits(className) for className in _TEXT_INPUT_CLASSES):
        return
    editor = segmentEditor()
    if editor is not None and _isModuleEntered():
        editor.redo()


def _undo():
    editor = segmentEditor()
    if editor is not None:
        editor.undo()


#: Draw sessions of the shells (one shell per main window)
_sessions = weakref.WeakKeyDictionary()


def session(shell):
    """The Draw session of ``shell`` (created on first use)."""
    drawSession = _sessions.get(shell)
    if drawSession is None:
        drawSession = _DrawSession(shell)
        _sessions[shell] = drawSession
    return drawSession


def _activeSession():
    for drawSession in list(_sessions.values()):
        if drawSession.active:
            return drawSession
    return None


def _currentShell(shell=None):
    if shell is not None:
        return shell
    try:
        from . import shell as shellModule
    except Exception:
        return None
    return shellModule.instance()


def drawState(shell):
    """What Draw recorded when it was last left (``DrawState``)."""
    return session(shell).state


def suspendedEffect(shell=None):
    """The Draw tool that a Place or window/level mode suspended ("Paint" -> "Brush resumes after"), else None."""
    shell = _currentShell(shell)
    drawSession = _sessions.get(shell) if shell is not None else None
    return drawSession.suspendedEffect() if drawSession is not None else None


def requestArm(shell=None):
    """Arm Brush on the next entry into Draw (or now, if Draw is shown): "Continue in Draw", "Open in Draw"."""
    shell = _currentShell(shell)
    if shell is not None:
        session(shell).requestArm()


#
# Views: follow strokes, center, gaps
#


def _layoutSliceViews(skipFigures=True):
    """(view name, slice widget) of the slice views of the layout, figure views left out."""
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None:
        return []
    figures = set(space.figureViewNames()) if skipFigures else set()
    views = []
    for viewName in layoutManager.sliceViewNames():
        sliceWidget = layoutManager.sliceWidget(viewName)
        if sliceWidget is None or viewName in figures or not sliceWidget.isVisible():
            continue
        views.append((viewName, sliceWidget))
    return views


def _showsPoint(sliceWidget, ras, margin=0.1):
    """True if the slice view already shows ``ras``: on its slice (within half a slice) and inside its field of view."""
    sliceNode = sliceWidget.mrmlSliceNode()
    rasToSlice = vtk.vtkMatrix4x4()
    vtk.vtkMatrix4x4.Invert(sliceNode.GetSliceToRAS(), rasToSlice)
    x, y, z, _w = rasToSlice.MultiplyPoint((*ras, 1.0))
    spacing = sliceWidget.sliceLogic().GetLowestVolumeSliceSpacing()[2] or 1.0
    fieldOfView = sliceNode.GetFieldOfView()
    return (abs(z) <= 0.5 * spacing and abs(x) <= (0.5 - margin) * fieldOfView[0] and abs(y) <= (0.5 - margin) * fieldOfView[1])


def followStroke(paintedView, ras):
    """Center the slice views other than ``paintedView`` (and never a figure view) on ``ras``, unless a view already
    shows it (the views do not jump while the strokes stay in sight, and nothing is rendered again).
    """
    for viewName, sliceWidget in _layoutSliceViews():
        if viewName == paintedView or _showsPoint(sliceWidget, ras):
            continue
        sliceWidget.mrmlSliceNode().JumpSliceByCentering(*ras)


def centerOnLesion(shell=None):
    """Bring every non-figure slice view to the centroid of the lesion (C). Return the RAS point, or None."""
    shell = _currentShell(shell)
    editor = segmentEditor()
    segmentationNode = editor.segmentationNode() if editor is not None else None
    metrics = lesionMetrics(segmentationNode, compute=True)
    if metrics is None or not metrics["voxels"]:
        if shell is not None:
            shell.toast(_("The lesion is empty: draw first"))
        return None
    ras = metrics["centroid_ras"]
    for _viewName, sliceWidget in _layoutSliceViews():
        sliceWidget.mrmlSliceNode().JumpSliceByCentering(*ras)
    return ras


def _gapAxis(sliceNode, ijkToRAS):
    """``(array axis, aligned)`` of the labelmap axis closest to the slice normal of ``sliceNode``."""
    sliceToRas = sliceNode.GetSliceToRAS()
    normal = np.array([sliceToRas.GetElement(row, 2) for row in range(3)])
    normal /= np.linalg.norm(normal) or 1.0
    best, bestCos = 0, -1.0
    # Array axis 0 is k (matrix column 2), 1 is j (column 1), 2 is i (column 0)
    for axis, column in ((0, 2), (1, 1), (2, 0)):
        direction = ijkToRAS[:3, column]
        cos = abs(float(np.dot(direction, normal))) / (float(np.linalg.norm(direction)) or 1.0)
        if cos > bestCos:
            best, bestCos = axis, cos
    return best, bestCos > 0.999


def jumpToGap(step=1, shell=None, viewName="Red"):
    """N / Shift+N: move the view to the next (``step=1``) or previous gap slice of the lesion, wrapping; to the
    lesion's first slice when it has no gap. On an oblique view the nearest labelmap axis is used ("approx.").
    Return the slice index jumped to, or None.
    """
    shell = _currentShell(shell)
    editor = segmentEditor()
    layoutManager = slicer.app.layoutManager()
    sliceWidget = layoutManager.sliceWidget(viewName) if layoutManager is not None else None
    segmentationNode = editor.segmentationNode() if editor is not None else None
    if sliceWidget is None or segmentationNode is None:
        return None
    sliceNode = sliceWidget.mrmlSliceNode()
    reference = lesionMetrics(segmentationNode, compute=True)
    if reference is None or not reference["voxels"] or reference.get("ijkToRAS") is None:
        if shell is not None:
            shell.toast(_("The lesion is empty: draw first"))
        return None
    axis, aligned = _gapAxis(sliceNode, reference["ijkToRAS"])
    metrics = lesionMetrics(segmentationNode, compute=True, axis=axis)
    matrix = metrics["ijkToRAS"]
    rasToIjk = np.linalg.inv(matrix)
    sliceToRas = sliceNode.GetSliceToRAS()
    center = np.array([sliceToRas.GetElement(row, 3) for row in range(3)] + [1.0])
    ijk = rasToIjk @ center
    current = ijk[(2, 1, 0)[axis]]
    gaps = sorted(metrics["gaps"])
    k0, _k1, j0, _j1, i0, _i1 = metrics["bbox"]
    first = (k0, j0, i0)[axis]
    if gaps:
        if step > 0:
            later = [gap for gap in gaps if gap > current + 0.5]
            target = later[0] if later else gaps[0]
        else:
            earlier = [gap for gap in gaps if gap < current - 0.5]
            target = earlier[-1] if earlier else gaps[-1]
    else:
        target = first
    centroidIjk = rasToIjk @ np.array([*metrics["centroid_ras"], 1.0])
    centroidIjk[(2, 1, 0)[axis]] = target
    ras = matrix @ centroidIjk
    sliceNode.JumpSliceByOffsetting(float(ras[0]), float(ras[1]), float(ras[2]))
    if shell is not None:
        if not gaps:
            shell.toast(_("No gap: first slice of the lesion") + ("" if aligned else " " + _("(approx.)")))
        elif not aligned:
            shell.toast(_("Gap slice (approx.)"))
    return int(target)


def stepSubject(step, shell=None):
    """Alt+←/→: previous/next subject of the study, else lesion of the loaded set, else a toast."""
    shell = _currentShell(shell)
    study = getattr(shell, "studySession", None) if shell is not None else None
    if study is not None and hasattr(study, "next"):
        return study.next() if step > 0 else study.previous()
    importer = _importer()
    loadedSet = importer.loadedSet() if importer is not None and hasattr(importer, "loadedSet") else None
    if loadedSet is not None:
        if shell is not None:
            session(shell).requestArm()
        return loadedSet.next() if step > 0 else loadedSet.previous()
    if shell is not None:
        shell.toast(_("Open a folder of lesions or a study to step through subjects"))
    return None


def _removeNetworkMaps():
    """Network maps are never shown in Draw: they leave the foreground of the slice views (Map shows them again)."""
    scene = slicer.mrmlScene
    for compositeNode in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
        foregroundId = compositeNode.GetForegroundVolumeID()
        node = scene.GetNodeByID(foregroundId) if foregroundId else None
        if node is not None and node.GetAttribute(NETWORK_MAP_ATTRIBUTE) == "1":
            compositeNode.SetForegroundVolumeID(None)


#
# Lesion editing helpers (one undo step each)
#


def _writeMask(segmentationNode, segmentId, mask, imageToWorldArray, extent):
    """Replace the segment's voxels in ``extent`` (absolute IJK of the labelmap) by ``mask`` (k, j, i)."""
    import vtk.util.numpy_support

    image = slicer.vtkOrientedImageData()
    image.SetExtent(*extent)
    image.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
    imageToWorld = vtk.vtkMatrix4x4()
    for row in range(4):
        for column in range(4):
            imageToWorld.SetElement(row, column, float(imageToWorldArray[row, column]))
    image.SetImageToWorldMatrix(imageToWorld)
    array = vtk.util.numpy_support.vtk_to_numpy(image.GetPointData().GetScalars())
    array[:] = mask.astype(np.uint8).ravel()
    slicer.vtkSlicerSegmentationsModuleLogic.SetBinaryLabelmapToSegment(
        image, segmentationNode, segmentId, slicer.vtkSlicerSegmentationsModuleLogic.MODE_REPLACE, image.GetExtent())


def _editLesion(transform, title, shell=None):
    """Apply ``transform(maskCrop, spacing) -> newCrop`` to the edited segment around its extent (2 voxel margin
    plus the kernel), as one undo step. Return (voxels before, voxels after), or None when there is nothing to edit.
    """
    editor = segmentEditor()
    segmentationNode = editor.segmentationNode() if editor is not None else None
    segmentId = editor.currentSegmentID() if editor is not None else None
    if segmentationNode is None or not segmentId:
        return None
    data = _labelmapArray(segmentationNode, segmentId)
    if data is None or not data[0].any():
        if shell is not None:
            shell.toast(_("The lesion is empty: nothing to do"))
        return None
    mask, matrix, _mtime = data
    spacing = np.linalg.norm(matrix[:3, :3], axis=0)  # i, j, k
    bbox = lesioncard._boundingBox(mask)
    margin = 2 + math.ceil(CLEANUP_CLOSING_MM / max(1e-6, float(spacing.min())))
    k0, k1, j0, j1, i0, i1 = bbox
    lows = [max(0, k0 - margin), max(0, j0 - margin), max(0, i0 - margin)]
    highs = [min(mask.shape[0] - 1, k1 + margin), min(mask.shape[1] - 1, j1 + margin), min(mask.shape[2] - 1, i1 + margin)]
    crop = mask[lows[0]:highs[0] + 1, lows[1]:highs[1] + 1, lows[2]:highs[2] + 1]
    result = transform(crop.copy(), spacing)
    before, after = int(np.count_nonzero(crop)), int(np.count_nonzero(result))
    if before == after and np.array_equal(crop, result):
        return before, after
    editor.saveStateForUndo()
    labelmap = segmentationNode.GetBinaryLabelmapInternalRepresentation(segmentId)
    extent = labelmap.GetExtent()
    cropExtent = (extent[0] + lows[2], extent[0] + highs[2], extent[2] + lows[1], extent[2] + highs[1], extent[4] + lows[0], extent[4] + highs[0])
    # The matrix maps the array index to RAS; the image to world matrix maps the absolute IJK index
    offset = np.eye(4)
    offset[:3, 3] = (-extent[0], -extent[2], -extent[4])
    _writeMask(segmentationNode, segmentId, result, matrix @ offset, cropExtent)
    return before, after


def _ellipsoid(spacing, radiusMm):
    """Ellipsoid structuring element (k, j, i) of ``radiusMm`` on a grid of ``spacing`` (i, j, k)."""
    radii = [max(0, round(radiusMm / max(1e-6, float(s)))) for s in (spacing[2], spacing[1], spacing[0])]
    grids = np.ogrid[tuple(slice(-r, r + 1) for r in radii)]
    distance = sum((g / max(r, 1)) ** 2 if r > 0 else g * 0.0 for g, r in zip(grids, radii, strict=True))
    return distance <= 1.0


def _keepLargest(mask, spacing):
    from scipy import ndimage

    labels, count = ndimage.label(mask, structure=lesioncard.PARTS_STRUCTURE)
    if count <= 1:
        return mask
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    return labels == int(np.argmax(sizes))


def _fillGapsAndSpecks(mask, spacing):
    """Closing with a 2 mm ellipsoid, then removal of islands under 10 mm³ (SPEC 5.2 Clean up)."""
    from scipy import ndimage

    structure = _ellipsoid(spacing, CLEANUP_CLOSING_MM / 2.0 if CLEANUP_CLOSING_MM > 0 else 0)
    closed = ndimage.binary_closing(mask, structure=structure) if structure.size > 1 else mask.copy()
    closed |= mask
    voxelVolume = float(np.prod(spacing))
    minimumVoxels = math.ceil(CLEANUP_MIN_ISLAND_MM3 / max(voxelVolume, 1e-9))
    labels, count = ndimage.label(closed, structure=lesioncard.PARTS_STRUCTURE)
    if count == 0:
        return closed
    sizes = np.bincount(labels.ravel())
    keep = sizes >= minimumVoxels
    keep[0] = False
    return keep[labels]


def keepLargestPart(shell=None):
    """Keep the largest part of the lesion (26-connectivity), one undo step."""
    shell = _currentShell(shell)
    result = _editLesion(_keepLargest, _("Keep largest"), shell)
    if result is not None and shell is not None:
        shell.toast(_("Kept the largest part: {removed} voxels removed").format(removed=result[0] - result[1]),
                    action=_undo, actionText=_("Undo"))
    return result


def fillGapsAndSpecks(shell=None):
    """"Fill gaps and specks": closes gaps up to 2 mm and removes islands under 10 mm³, one undo step."""
    shell = _currentShell(shell)
    result = _editLesion(_fillGapsAndSpecks, _("Fill gaps and specks"), shell)
    if result is not None and shell is not None:
        before, after = result
        shell.toast(_("Fill gaps and specks: {before} → {after} voxels").format(before=before, after=after), action=_undo, actionText=_("Undo"))
    return result


def fillGaps(shell=None):
    """The gaps link of the card: Fill gaps one-click when the effect offers it, else the Fill gaps tool."""
    editor = segmentEditor()
    if editor is None or editor.effectByName("Fill between slices") is None:
        return
    widget = _moduleWidget()
    if widget is not None and hasattr(widget, "activateEffect"):
        widget.activateEffect("Fill between slices")
    else:
        editor.setActiveEffectByName("Fill between slices")
    effect = editor.activeEffect()
    scripted = effect.self() if effect is not None and hasattr(effect, "self") else None
    for name in ("fillOneClick", "onFill", "fill"):
        method = getattr(scripted, name, None)
        if callable(method):
            method()
            return


#
# Save lesion (USABILITY 8, SPEC 5.10)
#

#: Saved lesions of this session: {segmentation node ID: (weak reference, absolute path, content stamp, time)}. A record
#: is valid only for the node it was made for (node IDs are reused after the scene is closed), and Draw clears them
#: when the scene is closed.
_savedRecords = {}


def _onSceneClosed():
    _savedRecords.clear()
    _metricsCache.clear()
    _mniCache.clear()


#: The scene observed by ``_installImportGuard()`` and its observer tag
_importGuard = {"scene": None, "tag": None}


def _installImportGuard():
    """Observe the scene loads for ``_onSceneStartImport()`` (once per scene; Draw's own observers end when Draw is left)."""
    scene = slicer.mrmlScene
    if scene is None or _importGuard["scene"] is scene:
        return
    _importGuard["tag"] = scene.AddObserver(scene.StartImportEvent, _onSceneStartImport)
    _importGuard["scene"] = scene


def _onSceneStartImport(caller=None, event=None):
    """A scene is loaded in Studio while Draw is not shown: the editor stops following its parameter node until Draw is
    entered again (``SegmentEditor.enter()`` selects it). The loaded Segment Editor node names the tool that was armed when
    the scene was saved before its segmentation is in the scene, and the editor would try (and fail) to activate it outside
    Draw ("setSourceRepresentationToBinaryLabelmap failed: No segmentation node"). Classic is unchanged.
    """
    try:
        from . import shell as shellModule

        studioShell = shellModule.instance()
        if studioShell is None or not studioShell.isActive() or _isModuleEntered():
            return
        widget, editor = _moduleWidget(), segmentEditor()
        if widget is None or editor is None or editor.mrmlSegmentEditorNode() is None:
            return
        editor.setMRMLSegmentEditorNode(None)
        if hasattr(widget, "parameterSetNode"):
            widget.parameterSetNode = None
    except Exception:
        logging.error("Studio Draw: preparing the scene load failed:\n" + traceback.format_exc())


def _savedRecord(segmentationNode):
    if segmentationNode is None:
        return None
    record = _savedRecords.get(segmentationNode.GetID())
    if record is None or record[0].Get() is not segmentationNode:
        return None
    return record


def _unchangedSinceRead(segmentationNode, storageNode):
    """True when the voxels of the lesion did not change since its file was read (or written).

    The importer names and styles the segment of a loaded mask, so the node itself is modified since read; its
    labelmaps are not.
    """
    if not segmentationNode.GetModifiedSinceRead():
        return True
    importer = _importer()
    if importer is not None and hasattr(importer, "unchangedSinceLoad") and importer.unchangedSinceLoad(segmentationNode):
        return True  # a fractional file whose voxels the importer set after the reader (as the file reads as a lesion)
    try:
        storedTime = storageNode.GetStoredTime().GetMTime()
    except (AttributeError, TypeError):
        return False
    count, newest = _contentStamp(segmentationNode)
    return count > 0 and 0 < newest <= storedTime


def lesionSaveState(segmentationNode):
    """``(state, path or file name, time)`` of a lesion, ``state`` in:

    - "saved": saved in this session and not changed since (or loaded from a NIfTI file and not modified);
    - "unsaved": saved in this session, changed since (or loaded from a NIfTI file and painted since: the path is the
      file it was loaded from, which the Save call to action still never overwrites without asking);
    - "previous": only the attributes of an earlier save are known (after the scene was saved and reopened):
      the file name and the ISO 8601 time;
    - "never": never saved.
    """
    if segmentationNode is None:
        return "never", None, None
    record = _savedRecord(segmentationNode)
    if record is not None:
        _ref, path, stamp, savedTime = record
        return ("saved" if stamp == _contentStamp(segmentationNode) else "unsaved"), path, savedTime
    storageNode = segmentationNode.GetStorageNode()
    fileName = storageNode.GetFileName() if storageNode is not None else None
    if fileName and str(fileName).lower().endswith(NIFTI_EXTENSIONS):
        return ("saved" if _unchangedSinceRead(segmentationNode, storageNode) else "unsaved"), str(fileName), None
    savedFile = segmentationNode.GetAttribute(SAVED_FILE_ATTRIBUTE)
    if savedFile:
        return "previous", savedFile, segmentationNode.GetAttribute(SAVED_TIME_ATTRIBUTE)
    return "never", None, None


def _relativeTime(isoTime):
    try:
        moment = datetime.datetime.fromisoformat(str(isoTime).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    now = datetime.datetime.now(datetime.UTC)
    seconds = (now - moment).total_seconds()
    if seconds < 60:
        return _("just now")
    if seconds < 3600:
        return _("{n} min ago").format(n=int(seconds // 60))
    if seconds < 86400 and moment.astimezone().date() == now.astimezone().date():
        return moment.astimezone().strftime("%H:%M")
    return moment.astimezone().strftime("%d %b").lstrip("0")


def saveStateText(segmentationNode):
    """The save line of the Lesion card and its tooltip: ("Saved · sub-01_lesion.nii.gz · 2 min ago", full path)."""
    state, path, savedTime = lesionSaveState(segmentationNode)
    name = os.path.basename(path) if path else ""
    if state == "saved":
        parts = [_("Saved"), name] + ([_relativeTime(savedTime)] if savedTime else [])
        return " · ".join(part for part in parts if part), path
    # In a project (A-09) the lesion itself is saved with the project; the line then speaks of the NIfTI mask
    inProject = _projectLesionsFolder() is not None
    if state == "unsaved":
        if inProject:
            return _("Saved in the project · {name} is older").format(name=name), _("{path}\nSave lesion updates it.").format(path=path)
        return _("Unsaved changes · {name}").format(name=name), path
    if state == "previous":
        when = _relativeTime(savedTime) if savedTime else ""
        return (_("Last saved as {name}").format(name=name) + (f" · {when}" if when else "")), None
    if inProject:
        # One line, as "Not saved yet": the card does not grow when the project starts
        return _("Saved in the project"), _("The lesion is saved with the project. Save lesion writes it as a NIfTI mask (.nii.gz).")
    return _("Not saved yet"), None


def _sanitizeFileName(name):
    """A file name made of letters, digits, ".", "_" and "-" only."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("._-")
    return re.sub(r"_+", "_", name)


def _storageFileName(node):
    storageNode = node.GetStorageNode() if node is not None and hasattr(node, "GetStorageNode") else None
    fileName = storageNode.GetFileName() if storageNode is not None else None
    return str(fileName) if fileName else None


def _isInside(path, directory):
    if not directory:
        return False
    try:
        path = os.path.normcase(os.path.realpath(path))
        directory = os.path.normcase(os.path.realpath(directory))
        return os.path.commonpath([path, directory]) == directory
    except ValueError:
        # Different drives
        return False


def _applicationDirectories():
    """Folders that never hold the user's files: the application, the temporary and the cache folders."""
    directories = [slicer.app.slicerHome, slicer.app.applicationDirPath(), slicer.app.temporaryPath, tempfile.gettempdir()]
    for attribute in ("cachePath", "defaultCachePath"):
        value = getattr(slicer.app, attribute, None)
        if isinstance(value, str):
            directories.append(value)
    try:
        directories.append(slicer.mrmlScene.GetCacheManager().GetRemoteCacheDirectory())
    except Exception:
        pass
    try:
        from . import downloads

        directories.append(downloads.cacheDirectory())
    except Exception:
        pass
    return [directory for directory in directories if directory]


def isUserFile(path):
    """True if ``path`` is a file of the user: not in the application, temporary or cache folders."""
    if not path or not os.path.isfile(path):
        return False
    return not any(_isInside(path, directory) for directory in _applicationDirectories())


def _projectLesionsFolder(create=False):
    """Lesions/ of the open project (A-09), or None without a project."""
    try:
        from . import projects
    except ImportError:
        return None
    return projects.lesionsFolder(create=create)


_projectWatched = False
_projectShown = None  # path of the project the Lesion card last spoke of


def _watchProject():
    """Update the save line of the Lesion card when a project starts or ends (A-09): "Not saved yet" becomes "Saved in the
    project" without waiting for the next edit.
    """
    global _projectWatched
    if _projectWatched:
        return
    try:
        from . import projects
    except ImportError:
        return
    projects.addListener(_onProjectChanged)
    _projectWatched = True


def _onProjectChanged():
    global _projectShown
    try:
        from . import projects

        project = projects.currentProject()
    except Exception:
        return
    path = project.path if project is not None else None
    if path == _projectShown:
        return
    _projectShown = path
    session = extension().session
    if session is not None:
        session.refreshPanel()


def defaultSaveDirectory(sourceVolumeNode=None):
    """Folder proposed by "Save lesion…": Lesions/ of the open project (A-09), else the last one used, else the folder of
    the scan (if it is a file of the user), else Documents, else the home folder. Never the application folder.
    """
    lesions = _projectLesionsFolder(create=True)
    if lesions is not None:
        return lesions
    lastFolder = str(slicer.util.settingsValue(LAST_SAVE_FOLDER_KEY, "") or "")
    if lastFolder and os.path.isdir(lastFolder):
        return lastFolder
    sourceFile = _storageFileName(sourceVolumeNode)
    if sourceFile and isUserFile(sourceFile):
        return os.path.dirname(os.path.abspath(sourceFile))
    home = qt.QDir.homePath()
    documents = os.path.join(home, "Documents")
    return documents if os.path.isdir(documents) else home


def _nextNumberedName(directory):
    """"case-002_lesion.nii.gz" when the newest NIfTI file of ``directory`` is "case-001_lesion.nii.gz", else None."""
    try:
        candidates = [entry for entry in os.scandir(directory) if entry.is_file() and entry.name.lower().endswith(NIFTI_EXTENSIONS)]
    except OSError:
        return None
    if not candidates:
        return None
    newest = max(candidates, key=lambda entry: entry.stat().st_mtime)
    match = _NUMBERED_FILE_PATTERN.match(newest.name)
    if match is None:
        return None
    prefix, number, suffix, extension = match.groups()
    proposal = f"{prefix}{int(number) + 1:0{len(number)}d}{suffix}{extension}"
    return None if os.path.exists(os.path.join(directory, proposal)) else proposal


def defaultLesionFileName(segmentationNode, sourceVolumeNode=None, directory=None):
    """File name proposed by "Save lesion…", for example "sub-01_lesion.nii.gz" or "patient_T1_lesion.nii.gz".

    The BIDS subject of the scan wins; else the scan of a "Lesions (<scan>)" segmentation, else the
    segmentation name (generic names such as "Lesions_1" or "Lesion 2" give way to the scan's name). A name that
    already ends with "lesion" is not repeated. When the name would be generic and the newest file of
    ``directory`` ends with a number ("case-001_lesion"), the next number is proposed ("case-002_lesion").
    """
    sourceFile = _storageFileName(sourceVolumeNode)
    candidates = [os.path.basename(sourceFile) if sourceFile else None,
                  sourceVolumeNode.GetName() if sourceVolumeNode is not None else None,
                  segmentationNode.GetName() if segmentationNode is not None else None]
    for candidate in candidates:
        match = _BIDS_SUBJECT_PATTERN.search(candidate or "")
        if match:
            return f"{match.group(1)}_lesion.nii.gz"
    base = segmentationNode.GetName() if segmentationNode is not None else ""
    match = _SCAN_LESIONS_PATTERN.match(base)
    if match:
        base = match.group(1)
    if _GENERIC_NAME_PATTERN.match(base.strip()) and sourceVolumeNode is not None and not _isTemplateVolume(sourceVolumeNode):
        # "Lesions_1", "Lesion 2": the scan names the case
        base = os.path.basename(sourceFile).split(".")[0] if sourceFile and isUserFile(sourceFile) else sourceVolumeNode.GetName()
    base = _sanitizeFileName(base)
    if base.lower() in _GENERIC_NAMES or _GENERIC_NAME_PATTERN.match(base):
        numbered = _nextNumberedName(directory) if directory else None
        return numbered or "lesion.nii.gz"
    if base.lower().endswith("lesion"):
        return f"{base}.nii.gz"
    return f"{base}_lesion.nii.gz"


def _withNiftiExtension(path):
    if not path.lower().endswith(NIFTI_EXTENSIONS):
        path += ".nii.gz"
    return path


def lesionNodes(editor=None):
    """``(segmentationNode, sourceVolumeNode)`` edited in Draw (the Segment Editor), Nones if there is none."""
    editor = editor or segmentEditor()
    if editor is None:
        return None, None
    return editor.segmentationNode(), editor.sourceVolumeNode()


def _castToUnsignedChar(labelmapNode):
    """Store a label map with at most 255 labels as uint8 (the usual type of lesion masks), else keep its type."""
    imageData = labelmapNode.GetImageData()
    if imageData is None or imageData.GetScalarType() == vtk.VTK_UNSIGNED_CHAR:
        return
    low, high = imageData.GetScalarRange()
    if low < 0 or high > 255:
        return
    cast = vtk.vtkImageCast()
    cast.SetInputData(imageData)
    cast.SetOutputScalarTypeToUnsignedChar()
    cast.Update()
    labelmapNode.SetAndObserveImageData(cast.GetOutput())


def _geometryImage(geometry):
    image = slicer.vtkOrientedImageData()
    if not geometry or not slicer.vtkSegmentationConverter.DeserializeImageGeometry(geometry, image, False):
        return None
    return image


def _geometryMatrix(image):
    matrix = vtk.vtkMatrix4x4()
    image.GetImageToWorldMatrix(matrix)
    return np.array([[matrix.GetElement(row, column) for column in range(4)] for row in range(4)])


def _recordedGridVolume(segmentationNode):
    """A temporary hidden volume of the grid the lesion was loaded on (``Studio.LesionGrid``), when the current
    reference geometry differs from it only by an extent grown to contain the segments; else None.
    """
    importer = _importer()
    attribute = getattr(importer, "LESION_GRID_ATTRIBUTE", "Studio.LesionGrid")
    recorded = _geometryImage(segmentationNode.GetAttribute(attribute))
    current = _geometryImage(segmentationNode.GetSegmentation().GetConversionParameter(
        slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName()))
    if recorded is None or current is None:
        return None
    if recorded.GetExtent() == current.GetExtent() or not np.allclose(_geometryMatrix(recorded), _geometryMatrix(current), atol=1e-4):
        return None
    recordedExtent, currentExtent = recorded.GetExtent(), current.GetExtent()
    contains = all(currentExtent[2 * axis] <= recordedExtent[2 * axis] and currentExtent[2 * axis + 1] >= recordedExtent[2 * axis + 1] for axis in range(3))
    if not contains:
        return None
    volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", slicer.mrmlScene.GenerateUniqueName("StudioLesionGrid"))
    volumeNode.SetHideFromEditors(True)
    volumeNode.SetSaveWithScene(False)
    dims = [recordedExtent[2 * axis + 1] - recordedExtent[2 * axis] + 1 for axis in range(3)]
    imageData = vtk.vtkImageData()
    imageData.SetDimensions(*dims)
    imageData.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
    imageData.GetPointData().GetScalars().Fill(0)
    volumeNode.SetAndObserveImageData(imageData)
    offset = np.eye(4)
    offset[:3, 3] = (recordedExtent[0], recordedExtent[2], recordedExtent[4])
    matrix = _geometryMatrix(recorded) @ offset
    ijkToRas = vtk.vtkMatrix4x4()
    for row in range(4):
        for column in range(4):
            ijkToRas.SetElement(row, column, float(matrix[row, column]))
    volumeNode.SetIJKToRASMatrix(ijkToRas)
    return volumeNode


def saveLesion(path, segmentationNode, referenceVolumeNode=None, segmentIds=None):
    """Write the segments of ``segmentationNode`` as a NIfTI label map (official Slicer writer) and return the path.

    :param path: ``.nii.gz`` or ``.nii`` file (``.nii.gz`` is appended to other names).
    :param referenceVolumeNode: the file has the grid of this volume. Default (None): the lesion's own grid, the
      reference image geometry of the segmentation (the grid it was recorded on when it was loaded, if the reference
      geometry only grew around it), never the grid of the scan or template it is shown on.
    :param segmentIds: segments to write (default: all). Segments get the values 1, 2... in this order, so a
      single lesion segment is a 0/1 mask (uint8 up to 255 segments). A label map holds one value per voxel:
      where segments overlap, the voxel gets the value of the later segment.
    Raise ``RuntimeError`` on failure. No node is left in the scene.
    """
    if segmentationNode is None:
        raise RuntimeError(_("There is no lesion to save"))
    path = _withNiftiExtension(os.path.abspath(str(path)))
    segmentation = segmentationNode.GetSegmentation()
    if segmentIds is None:
        segmentIds = [segmentation.GetNthSegmentID(index) for index in range(segmentation.GetNumberOfSegments())]
    if not segmentIds:
        raise RuntimeError(_("The segmentation {name} has no segment").format(name=segmentationNode.GetName()))
    idArray = vtk.vtkStringArray()
    for segmentId in segmentIds:
        idArray.InsertNextValue(segmentId)

    scene = slicer.mrmlScene
    nodeIdsBefore = {scene.GetNthNode(index).GetID() for index in range(scene.GetNumberOfNodes())}
    try:
        if referenceVolumeNode is None:
            referenceVolumeNode = _recordedGridVolume(segmentationNode)
        labelmapNode = scene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", scene.GenerateUniqueName("StudioLesionExport"))
        labelmapNode.SetHideFromEditors(True)
        exported = slicer.modules.segmentations.logic().ExportSegmentsToLabelmapNode(
            segmentationNode, idArray, labelmapNode, referenceVolumeNode, slicer.vtkSegmentation.EXTENT_REFERENCE_GEOMETRY)
        if not exported:
            raise RuntimeError(_("Failed to export the lesion {name}").format(name=segmentationNode.GetName()))
        _castToUnsignedChar(labelmapNode)
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        if not slicer.util.saveNode(labelmapNode, path):
            raise RuntimeError(_("Failed to write {path}").format(path=path))
    finally:
        # Remove the temporary label map and what the export added (its color table, display and storage nodes)
        for index in reversed(range(scene.GetNumberOfNodes())):
            node = scene.GetNthNode(index)
            if node is not None and node.GetID() not in nodeIdsBefore:
                scene.RemoveNode(node)
    return path


def sidecarPath(path):
    """``sub-01_lesion.json`` next to ``sub-01_lesion.nii.gz``."""
    base = path[:-7] if path.lower().endswith(".nii.gz") else os.path.splitext(path)[0]
    return base + ".json"


def _sidecarSource(sidecar, sourceVolumeNode):
    """The scan of the lesion, relative to the sidecar (its file name on another drive or for an application file)."""
    sourceFile = _storageFileName(sourceVolumeNode)
    if not sourceFile:
        return sourceVolumeNode.GetName() if sourceVolumeNode is not None else None
    try:
        relative = os.path.relpath(sourceFile, os.path.dirname(sidecar))
    except ValueError:
        # Another drive
        return os.path.basename(sourceFile)
    if not isUserFile(sourceFile) and relative.split(os.sep).count("..") > 1:
        # An application file (the bundled template): its name only
        return os.path.basename(sourceFile)
    return relative.replace(os.sep, "/")


def _userTemplateOf(volumeNode):
    """The user's own template (A-10) that ``volumeNode`` shows, or None."""
    if volumeNode is None or not volumeNode.GetAttribute("Studio.TemplateId"):
        return None
    try:
        from . import templates

        return templates.userTemplateOfNode(volumeNode)
    except Exception:
        return None


def _sidecarText(segmentationNode, attribute, key, path):
    """Notes or source reference for the sidecar of ``path``: the lesion's attribute (an empty one was cleared in the
    Lesion card). Without the attribute, a re-save to the file the lesion was loaded from keeps that sidecar's text.
    """
    value = segmentationNode.GetAttribute(attribute)
    if value is not None:
        return value
    storageNode = segmentationNode.GetStorageNode()
    loadedPath = storageNode.GetFileName() if storageNode is not None else None
    if not loadedPath or os.path.normcase(os.path.realpath(loadedPath)) != os.path.normcase(os.path.realpath(path)):
        return ""
    try:
        with open(sidecarPath(path), encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, ValueError):
        return ""
    value = existing.get(key) if isinstance(existing, dict) else None
    return value if isinstance(value, str) else ""


def writeSidecar(path, segmentationNode, sourceVolumeNode=None):
    """Write the BIDS-style sidecar of a saved lesion (D-U3; official Slicer and FSL ignore it). Return its path.

    A lesion drawn on the user's own template (A-10) also names the template ("Template": id, name, space, sha256; never
    a path), so that a colleague can add the same template by its checksum.
    """
    sidecar = sidecarPath(path)
    lesionSpace = space.spaceOf(segmentationNode)
    userTemplate = _userTemplateOf(sourceVolumeNode)
    content = {
        "Source": _sidecarSource(sidecar, sourceVolumeNode) if sourceVolumeNode is not None else None,
        "Space": lesionSpace[0] if lesionSpace else "scanner",
        "Notes": _sidecarText(segmentationNode, NOTES_ATTRIBUTE, "Notes", path),
        "SourceReference": _sidecarText(segmentationNode, SOURCE_REFERENCE_ATTRIBUTE, "SourceReference", path),
        "GeneratedBy": f"Slicer Studio {slicer.app.applicationVersion}",
    }
    if content["Source"] is None:
        del content["Source"]
    if userTemplate is not None:
        content["Template"] = {"id": userTemplate.id, "name": userTemplate.name, "space": userTemplate.space, "sha256": userTemplate.imageSha256}
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump(content, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return sidecar


def _toast(shell, text, **kwargs):
    if shell is not None and shell.isActive():
        shell.toast(text, **kwargs)
    else:
        logging.info(f"Studio Draw: {text}")


def _showInFolder(path):
    from . import projects

    projects.showInFileManager(os.path.dirname(path))


def _commitCardEdits():
    """Notes and source reference still being typed in the Lesion card reach the lesion before it is saved."""
    card = extension().card if _extension is not None else None
    if card is not None:
        card.commitEdits()


def _writeLesion(shell, segmentationNode, sourceVolumeNode, path):
    """Save, record the saved state and toast. Return the written path, or None (the error is shown)."""
    if isLesionEmpty(segmentationNode):
        _toast(shell, _("The lesion is empty: nothing was saved"), ms=4000, level="warning")
        return None
    _commitCardEdits()
    # The file keeps the grid of the lesion (for example 91x109x91 for a mask on the FSL MNI152 2 mm grid, as
    # connectomes expect), not the grid of the scan or template it is drawn on. The scan's grid is only used for
    # a segmentation without a reference geometry.
    referenceGeometry = segmentationNode.GetSegmentation().GetConversionParameter(
        slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName())
    try:
        with slicer.util.WaitCursor():
            path = saveLesion(path, segmentationNode, None if referenceGeometry else sourceVolumeNode)
            writeSidecar(path, segmentationNode, sourceVolumeNode)
    except Exception as e:
        logging.error(f"Studio: cannot save the lesion:\n{traceback.format_exc()}")
        slicer.util.errorDisplay(_("The lesion could not be saved."), detailedText=str(e))
        return None
    savedTime = datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    reference = vtk.vtkWeakReference()
    reference.Set(segmentationNode)
    segmentationNode.SetAttribute(SAVED_FILE_ATTRIBUTE, os.path.basename(path))
    segmentationNode.SetAttribute(SAVED_TIME_ATTRIBUTE, savedTime)
    _savedRecords[segmentationNode.GetID()] = (reference, path, _contentStamp(segmentationNode), savedTime)
    folder = os.path.dirname(path)
    projectLesions = _projectLesionsFolder()
    inProject = projectLesions is not None and os.path.normcase(os.path.realpath(folder)) == os.path.normcase(os.path.realpath(projectLesions))
    if not inProject and folder != slicer.util.settingsValue(LAST_SAVE_FOLDER_KEY, ""):
        # The folders of projects are proposed by the project itself (A-09): only other folders are remembered
        qt.QSettings().setValue(LAST_SAVE_FOLDER_KEY, folder)
    segmentCount = segmentationNode.GetSegmentation().GetNumberOfSegments()
    if segmentCount > 1:
        message = _("Saved {count} segments to {name} (values 1 to {count}; where they overlap, the later one wins)").format(
            count=segmentCount, name=os.path.basename(path))
    else:
        message = _("Saved {name}").format(name=os.path.basename(path))
    _toast(shell, message, ms=4000 if segmentCount > 1 else 3000, action=lambda: _showInFolder(path), actionText=_("Show in folder"), toolTip=path)
    drawSession = _activeSession()
    if drawSession is not None:
        drawSession.refreshPanel()
    return path


def saveLesionDialog(shell=None, path=None):
    """"Save lesion as…": choose a NIfTI file (unless ``path`` is given) and save the lesion edited in Draw.

    Return the written path, or None if cancelled, empty or failed (errors are shown).
    """
    shell = _currentShell(shell)
    _applyPendingSmartFill(shell)
    segmentationNode, sourceVolumeNode = lesionNodes()
    if segmentationNode is None:
        slicer.util.errorDisplay(_("There is no lesion to save. Open a scan or start on the MNI template, then draw the lesion."))
        return None
    if path is None:
        if isLesionEmpty(segmentationNode):
            _toast(shell, _("The lesion is empty: nothing was saved"), ms=4000, level="warning")
            return None
        record = _savedRecord(segmentationNode)
        directory = os.path.dirname(record[1]) if record is not None else defaultSaveDirectory(sourceVolumeNode)
        name = os.path.basename(record[1]) if record is not None else defaultLesionFileName(segmentationNode, sourceVolumeNode, directory)
        segmentCount = segmentationNode.GetSegmentation().GetNumberOfSegments()
        caption = _("Save lesion")
        if segmentCount > 1:
            caption = _("Save lesion: {count} segments → values 1 to {count}").format(count=segmentCount)
        path = qt.QFileDialog.getSaveFileName(slicer.util.mainWindow(), caption, os.path.join(directory, name), NIFTI_FILTER)
        if not path:
            projectLesions = _projectLesionsFolder()
            if projectLesions is not None:
                try:
                    os.rmdir(projectLesions)  # created for the dialog: never leave an empty folder in a project
                except OSError:
                    pass
            return None
    return _writeLesion(shell, segmentationNode, sourceVolumeNode, path)


def saveLesionNow(shell=None):
    """Save lesion (the CTA and Ctrl+Shift+S): silently to the same file after the first save of the session, else with
    the dialog (in a project, A-09, it proposes Lesions/<name>.nii.gz of the project). Return the written path, or None.
    """
    shell = _currentShell(shell)
    _applyPendingSmartFill(shell)
    segmentationNode, sourceVolumeNode = lesionNodes()
    record = _savedRecord(segmentationNode)
    if record is not None:
        return _writeLesion(shell, segmentationNode, sourceVolumeNode, record[1])
    return saveLesionDialog(shell)


#
# Smart fill (A-02): pending previews, assisted provenance
#


def _applyPendingSmartFill(shell=None):
    """Apply a pending Smart fill preview before a primary action (the user reviewed it). Return True if applied."""
    editor = segmentEditor()
    effect = editor.activeEffect() if editor is not None else None
    if effect is None or effect.name != "Smart fill" or not hasattr(effect, "self"):
        return False
    scripted = effect.self()
    if not getattr(scripted, "hasPendingPreview", lambda: False)():
        return False
    scripted.applyPendingPreview()
    _toast(_currentShell(shell), _("Applied Smart fill preview"))
    return True


#: Assisted voxels per subject (or segmentation and segment): {key: {tool: {"voxels": n, "applies": n, "params": [...]}}}
_assistCounts = {}


def _assistKey():
    shell = _currentShell()
    study = getattr(shell, "studySession", None) if shell is not None else None
    if study is not None and hasattr(study, "current"):
        try:
            subject = study.current()
            if subject:
                return str(getattr(subject, "id", subject))
        except Exception:
            pass
    editor = segmentEditor()
    if editor is None or editor.segmentationNode() is None:
        return None
    return f"{editor.segmentationNode().GetID()}/{editor.currentSegmentID()}"


def notifyAssist(tool, addedVoxels, params=None):
    """Count voxels added by an assisted tool (Smart fill) for the current subject (or lesion segment), for
    provenance ("assisted tracing", L-43). Never marks a lesion as automatic or approved.
    """
    key = _assistKey()
    if key is None:
        return
    entry = _assistCounts.setdefault(key, {}).setdefault(str(tool), {"voxels": 0, "applies": 0, "params": []})
    entry["voxels"] += int(addedVoxels or 0)
    entry["applies"] += 1
    if params:
        entry["params"].append(dict(params))
        del entry["params"][:-20]


def assistCounts(key=None):
    """Assisted counts of a subject id or ``"<segmentation id>/<segment id>"`` (default: the current one), or all."""
    if key is None:
        key = _assistKey()
    if key == "all":
        return json.loads(json.dumps(_assistCounts))
    return json.loads(json.dumps(_assistCounts.get(key, {})))


def _onSmartFillApplied(info):
    try:
        notifyAssist("smartfill", info.get("addedVoxels", 0), info.get("params"))
    except Exception:
        logging.error(f"Studio: Smart fill provenance failed:\n{traceback.format_exc()}")


def _smartFillModule():
    return _optionalModule("SegmentEditorEffects.SegmentEditorSmartFillEffect")


def _registerAssistObserver():
    module = _smartFillModule()
    if module is not None and hasattr(module, "addApplyObserver"):
        module.addApplyObserver(_onSmartFillApplied)


def _unregisterAssistObserver():
    module = _smartFillModule()
    if module is not None and hasattr(module, "removeApplyObserver"):
        module.removeApplyObserver(_onSmartFillApplied)


#
# The presentation extension (SegmentEditor.py setPresentation("studio", extension()))
#


def setOptionsStudioLook(editor, studio, hideDiameter=False):
    """The tool options of the Segment Editor in Draw: check boxes drawn as switches (their text on the left, the switch on
    the right) and, with ``hideDiameter``, the classic "Diameter:" row of Paint and Erase hidden (the Draw size row replaces
    it). ``studio=False`` restores the classic look. Nothing else of the options changes.
    """
    # The options frame of the tools (in the options group box in Classic, shown without it in Studio)
    optionsGroupBox = editor.findChild("QFrame", "EffectsOptionsFrame") if editor is not None else None
    if optionsGroupBox is None:
        return
    for checkBox in optionsGroupBox.findChildren("QCheckBox"):
        role = "switch" if studio else None
        if checkBox.property("slicerRole") == role:
            continue
        if role is None and checkBox.property("slicerRole") != "switch":
            continue
        checkBox.setProperty("slicerRole", role)
        if studio:
            checkBox.setProperty("studioClassicSizePolicy", int(checkBox.sizePolicy.horizontalPolicy()))
            checkBox.setSizePolicy(qt.QSizePolicy.Expanding, checkBox.sizePolicy.verticalPolicy())
        elif checkBox.property("studioClassicSizePolicy") is not None:
            policy = checkBox.sizePolicy
            policy.setHorizontalPolicy(int(checkBox.property("studioClassicSizePolicy")))
            checkBox.setSizePolicy(policy)
        style = checkBox.style()
        style.unpolish(checkBox)
        style.polish(checkBox)
        checkBox.updateGeometry()
        checkBox.update()
    for label in optionsGroupBox.findChildren("QLabel"):
        if str(label.text) != "Diameter:" and label.property("studioDiameterLabel") is None:
            continue
        label.setProperty("studioDiameterLabel", True)
        frame = label.parentWidget()
        if frame is None or frame == optionsGroupBox:
            continue
        hidden = bool(hideDiameter)
        if bool(frame.property("studioHiddenByDraw")) == hidden:
            continue
        frame.setProperty("studioHiddenByDraw", hidden)
        frame.setVisible(not hidden)


class DrawExtension:
    """Widgets and behaviour that Draw adds to the studio presentation of the Segment Editor."""

    #: The Studio shell shows the footer in its sticky inspector footer (``_CtaFooter``)
    footerManagedByHost = True

    def __init__(self):
        self.showAllControls = False
        self._session = None
        self._header = None
        self._trailing = None
        self.card = None
        self.displayCard = None
        self._cards = {}  # key -> (widget, order)
        self._drawCards = {}  # key -> controller
        self._drawCardsShell = None
        self._transientHint = None
        self._nativeOffer = None
        self._lastReadout = None
        self._lastReadoutKey = None
        self._lastStats = None

    #
    # Session
    #

    @property
    def session(self):
        return self._session

    def attach(self, drawSession):
        self._session = drawSession
        _watchProject()

    def detach(self, drawSession):
        if self._session is drawSession:
            self._session = None

    def _shell(self):
        return self._session.shell if self._session is not None else _currentShell()

    #
    # Presentation API
    #

    def headerWidget(self):
        if self._header is None:
            self._buildHeader()
        return self._header

    def trailingWidget(self):
        if self._trailing is None:
            self._buildTrailing()
        return self._trailing

    def footerWidget(self):
        return None

    def shortcuts(self):
        # The Draw keys are workspace shortcuts of the shell (N, C, Alt+arrows, Ctrl+Shift+S): listed by the cheat sheet
        return []

    def effectAppearance(self):
        appearance = {}
        for effectName, (text, iconName) in EFFECT_APPEARANCE.items():
            icon = ui.icon(iconName, 20) if os.path.isfile(ui.iconPath(iconName)) else None
            appearance[effectName] = (text, icon)
        return appearance

    def effectToolTip(self, effectName, positionalKey):
        text = EFFECT_APPEARANCE.get(effectName, (None,))[0]
        editor = segmentEditor()
        effect = editor.effectByName(effectName) if editor is not None else None
        if text is None:
            text = effect.title if effect is not None else effectName
        keys = [ui.keyText(key) for key in (EFFECT_MNEMONICS.get(effectName), positionalKey) if key]
        description = _effectDescriptions().get(effectName)
        if description is None and effect is not None:
            helpText = re.sub(r"<[^>]*>", " ", _effectHelpText(effect))
            description = re.split(r"(?<=\.)\s", " ".join(helpText.split()), maxsplit=1)[0]
            description = (description + " " + _("(Space pauses)")).strip()
        return ui.tooltip(text, " · ".join(keys) if keys else None, description)

    def onEnter(self):
        pass

    def onExit(self):
        pass

    def ensureSegmentation(self, editor):
        """A lesion to draw into: the edited segmentation, else one bound to the source volume ("Lesions"), with a
        "Lesion" segment when it has none. Nothing is created while there is no scan (the empty state shows).
        """
        segmentationNode = editor.segmentationNode()
        sourceVolumeNode = editor.sourceVolumeNode()
        if sourceVolumeNode is not None and not _isEditableSource(sourceVolumeNode):
            editor.setSourceVolumeNode(None)
            sourceVolumeNode = None
        if sourceVolumeNode is None:
            widget = _moduleWidget()
            sourceId = widget.getDefaultSourceVolumeNodeID() if widget is not None else None
            candidate = slicer.mrmlScene.GetNodeByID(sourceId) if sourceId else None
            if candidate is not None and _isEditableSource(candidate) and (segmentationNode is None or self._nativeOffer is None):
                sourceVolumeNode = candidate
        if segmentationNode is None:
            if sourceVolumeNode is None:
                return None
            segmentationNode = self._existingSegmentation(sourceVolumeNode)
        if segmentationNode is None:
            # Without a segment: showing a scan in Draw adds no segment (the guided tours and a look leave the lesion
            # empty); the first tool adds the "Lesion" segment (ensureLesionSegment(), _adoptNewSegment())
            importer = _importer()
            name = "Lesions"
            if importer is not None and hasattr(importer, "scanLesionSegmentationName"):
                name = importer.LESION_SEGMENTATION_NAME if _isTemplateVolume(sourceVolumeNode) else importer.scanLesionSegmentationName(sourceVolumeNode)
            segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", name)
            segmentationNode.CreateDefaultDisplayNodes()
            segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(sourceVolumeNode)
            editor.setSegmentationNode(segmentationNode)
        if sourceVolumeNode is not None and editor.sourceVolumeNode() is None and self._nativeOffer is None:
            editor.setSourceVolumeNode(sourceVolumeNode)
        segmentation = segmentationNode.GetSegmentation()
        if not editor.currentSegmentID() and segmentation.GetNumberOfSegments() > 0:
            editor.setCurrentSegmentID(segmentation.GetNthSegmentID(0))
        return segmentationNode

    def ensureLesionSegment(self, editor):
        """The "Lesion" segment of the edited segmentation (added, styled and selected when it has none). Return its ID,
        or None without a segmentation or a source volume.
        """
        segmentationNode = editor.segmentationNode()
        if segmentationNode is None or editor.sourceVolumeNode() is None:
            return None
        segmentation = segmentationNode.GetSegmentation()
        if segmentation.GetNumberOfSegments() == 0:
            importer = _importer()
            name = getattr(importer, "LESION_SEGMENT_NAME", "Lesion")
            color = getattr(importer, "LESION_COLOR", (0.949, 0.329, 0.357))
            segmentId = segmentation.AddEmptySegment("", name, color)
            if importer is not None and hasattr(importer, "styleLesionSegment"):
                importer.styleLesionSegment(segmentationNode, segmentId)
        segmentId = editor.currentSegmentID()
        if not segmentId or segmentation.GetSegment(segmentId) is None:
            segmentId = segmentation.GetNthSegmentID(0)
            editor.setCurrentSegmentID(segmentId)
        return segmentId

    def adoptNewSegment(self, segmentationNode):
        """The first segment that the Segment Editor added ("Segment_1", when a tool was picked on an empty lesion) becomes
        the "Lesion" segment: its name, colour and display.
        """
        if segmentationNode is None or segmentationNode.GetSegmentation().GetNumberOfSegments() != 1:
            return
        segmentation = segmentationNode.GetSegmentation()
        segment = segmentation.GetNthSegment(0)
        if segment is None or not _DEFAULT_SEGMENT_NAME_PATTERN.match(segment.GetName() or ""):
            return
        importer = _importer()
        segment.SetName(getattr(importer, "LESION_SEGMENT_NAME", "Lesion"))
        segment.SetColor(*getattr(importer, "LESION_COLOR", (0.949, 0.329, 0.357)))
        if importer is not None and hasattr(importer, "styleLesionSegment"):
            importer.styleLesionSegment(segmentationNode, segmentation.GetNthSegmentID(0))

    @staticmethod
    def _existingSegmentation(sourceVolumeNode):
        """A segmentation of the scene to edit on ``sourceVolumeNode``: one bound to it, else the first one (as the
        classic Segment Editor does), unless that one is bound to another volume.
        """
        role = slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole()
        segmentations = slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
        for segmentationNode in segmentations:
            if segmentationNode.GetNodeReferenceID(role) == sourceVolumeNode.GetID():
                return segmentationNode
        for segmentationNode in segmentations:
            if not segmentationNode.GetNodeReferenceID(role):
                return segmentationNode
        return None

    #
    # Header: empty state, Lesion card, cards, brush and source chips
    #

    def _buildHeader(self):
        header = qt.QWidget()
        header.setObjectName("StudioDrawHeader")
        header.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Maximum)
        layout = qt.QVBoxLayout(header)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(6)
        self._header = header
        self._headerLayout = layout

        self.emptyState = _EmptyState(self)
        layout.addWidget(self.emptyState.widget)

        self.card = lesioncard.LesionCard(_LesionController(self))
        layout.addWidget(self.card.widget)

        self._cardsBefore = qt.QVBoxLayout()
        self._cardsBefore.setSpacing(6)
        layout.addLayout(self._cardsBefore)

        # Brush size (Paint and Erase): a segmented 1 / 3 / 6 / 12 mm and the diameter field (VISUAL 4.4), and the source chip
        self.chipsRow = qt.QWidget()
        self.chipsRow.setObjectName("StudioDrawChipsRow")
        self.chipsRow.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        chipsLayout = qt.QHBoxLayout(self.chipsRow)
        # Aligned with the cards and the tool tiles
        chipsLayout.setContentsMargins(8, 2, 10, 2)
        chipsLayout.setSpacing(8)
        self.brushChips = {}
        self.brushLabel = ui.label(_("Size"), "body", "text", "StudioBrushSizeLabel")
        chipsLayout.addWidget(self.brushLabel)
        chipsLayout.addStretch(1)
        self.brushSizes, brushGroup = ui.segmented([f"{diameter:g}" for diameter in BRUSH_CHIPS_MM])
        self.brushSizes.setObjectName("StudioBrushSizes")
        # The chips show the current diameter: none is checked when the diameter is another value
        brushGroup.setExclusive(False)
        for diameter, chip in zip(BRUSH_CHIPS_MM, brushGroup.buttons(), strict=False):
            chip.setObjectName("StudioBrushSizeChip")
            chip.setFixedHeight(ui.COMPACT_HEIGHT - 6)
            chip.setMinimumWidth(34)
            chip.setToolTip(ui.tooltip(_("Brush {size} mm").format(size=f"{diameter:g}"), None, _("Brush and Erase diameter")))
            chip.setAccessibleName(_("Brush {size} mm").format(size=f"{diameter:g}"))
            chip.setFocusPolicy(qt.Qt.TabFocus)
            chip.connect("clicked()", lambda diameter=diameter: self._onBrushChip(diameter))
            ui.installButtonKeys(chip)
            self.brushChips[diameter] = chip
        chipsLayout.addWidget(self.brushSizes)
        self.brushField = qt.QDoubleSpinBox()
        self.brushField.setObjectName("StudioBrushSizeField")
        self.brushField.setRange(0.1, 200.0)
        self.brushField.setDecimals(1)
        self.brushField.setSingleStep(0.5)
        self.brushField.setSuffix(" mm")
        self.brushField.setButtonSymbols(qt.QAbstractSpinBox.NoButtons)
        self.brushField.setAlignment(qt.Qt.AlignRight | qt.Qt.AlignVCenter)
        self.brushField.setKeyboardTracking(False)
        self.brushField.setFixedSize(68, ui.COMPACT_HEIGHT)
        self.brushField.setToolTip(ui.tooltip(_("Brush diameter"), "[ ]", _("Brush and Erase diameter in mm")))
        self.brushField.setAccessibleName(_("Brush diameter"))
        self.brushField.connect("valueChanged(double)", self._onBrushField)
        chipsLayout.addWidget(self.brushField)
        self.sourceChip = qt.QToolButton()
        self.sourceChip.setObjectName("StudioSourceChip")
        self.sourceChip.setPopupMode(qt.QToolButton.InstantPopup)
        self.sourceChip.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
        self.sourceChip.setToolTip(ui.tooltip(_("Source scan"), None, _("the scan you draw on (and shown in the views)")))
        self.sourceChip.setFocusPolicy(qt.Qt.TabFocus)
        self.sourceMenu = qt.QMenu(self.sourceChip)
        self.sourceMenu.setObjectName("StudioSourceMenu")
        self.sourceMenu.connect("aboutToShow()", self._fillSourceMenu)
        self.sourceChip.setMenu(self.sourceMenu)
        ui.setRole(self.sourceChip, "ghost")
        chipsLayout.addWidget(self.sourceChip)
        # The size row follows the tool tiles (VISUAL 4.4): the Segment Editor puts it there (toolOptionsWidget()); the
        # header owns it until then
        self.chipsRow.setParent(header)
        self.chipsRow.hide()
        for widget in (self.sourceChip, *self.brushChips.values()):
            ui.setText(widget, "caption")
        # The section label of the tool tiles, which follow the header, with "More tools ▾" on its right (moreToolsHost())
        self.toolsRow = qt.QWidget()
        self.toolsRow.setObjectName("StudioDrawToolsRow")
        self.toolsRow.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.toolsRowLayout = qt.QHBoxLayout(self.toolsRow)
        # Aligned with the inspector padding (VISUAL 4.4: 16)
        self.toolsRowLayout.setContentsMargins(ui.INSPECTOR_PADDING, 8, 0, 0)
        self.toolsRowLayout.setSpacing(6)
        self.toolsLabel = ui.sectionLabel(_("Tools"), "StudioDrawToolsLabel")
        self.toolsRowLayout.addWidget(self.toolsLabel)
        self.toolsRowLayout.addStretch(1)
        layout.addWidget(self.toolsRow)
        self._discoverDrawCards()

    def _buildTrailing(self):
        """After the tool options (VISUAL 4.4): the Overlay row and the Outline switch, the hint line, then the list rows
        (Normalize to MNI and Trace from a figure, which are Draw cards, and Notes).
        """
        self.headerWidget()
        trailing = qt.QWidget()
        trailing.setObjectName("StudioDrawTrailing")
        trailing.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Maximum)
        layout = qt.QVBoxLayout(trailing)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(4)
        self.displayCard = _DisplayCard(self)
        layout.addWidget(self.displayCard.widget)
        # The hint of the tool: one caption line (the card refreshes its text)
        hint = self.card.takeHintLabel()
        hint.setContentsMargins(ui.INSPECTOR_PADDING, 0, 8, 0)
        layout.addWidget(hint)
        layout.addSpacing(10)
        # The list rows: their icons on the inspector padding (the row has an 8 px inner margin)
        rows = qt.QVBoxLayout()
        rows.setContentsMargins(ui.INSPECTOR_PADDING - 10, 0, 2, 0)
        rows.setSpacing(2)
        layout.addLayout(rows)
        self._cardsAfter = qt.QVBoxLayout()
        self._cardsAfter.setSpacing(2)
        rows.addLayout(self._cardsAfter)
        # Notes: a list row that opens the notes fields under it
        self.notesRow = ui.listRow("pencil", _("Notes"), None, self._onNotesRow, "StudioDrawNotesRow")
        self.notesRow.setToolTip(ui.tooltip(_("Notes"), None, _("notes and the source (DOI, figure), saved next to the lesion file")))
        rows.addWidget(self.notesRow)
        notesFrame = self.card.takeNotesFrame()
        notesFrame.setContentsMargins(8, 2, 4, 4)
        rows.addWidget(notesFrame)
        self.card.notesVisibilityChanged = self._onNotesVisibilityChanged
        self._trailing = trailing
        self._discoverDrawCards()

    def moreToolsHost(self):
        """The layout that holds "More tools ▾" in the studio presentation: the right of the Tools label."""
        self.headerWidget()
        return self.toolsRowLayout

    def toolOptionsWidget(self):
        """The size row (Brush and Erase), shown by the Segment Editor right after the tool tiles."""
        self.headerWidget()
        return self.chipsRow

    def _onNotesRow(self):
        if self.card is not None:
            self.card.setNotesVisible(self.card.notesFrame.isHidden())

    def _onNotesVisibilityChanged(self, visible):
        row = getattr(self, "notesRow", None)
        if row is None or not ui._isAlive(row):
            return
        chevron = row.findChild("QLabel", "StudioListRowChevron")
        if chevron is not None:
            chevron.setPixmap(ui.pixmap("chevron-down" if visible else "chevron-right", 14, "text3"))
        if visible:
            row.setAccessibleDescription(_("open"))
        else:
            row.setAccessibleDescription("")

    def _refreshNotesRow(self, notes, sourceReference):
        """The value of the Notes row: the first words of the notes, else the source reference."""
        row = getattr(self, "notesRow", None)
        if row is None or not ui._isAlive(row):
            return
        text = " ".join(str(notes or sourceReference or "").split())
        if len(text) > 18:
            text = text[:17].rstrip() + "…"
        ui.setListRowValue(row, text)

    def _onBrushChip(self, diameter):
        drawSession = self._session
        if drawSession is not None:
            drawSession.setBrushDiameter(diameter)
        else:
            qt.QSettings().setValue(BRUSH_DIAMETER_KEY, float(diameter))

    def _onBrushField(self, diameter):
        """The diameter field: a typed diameter (Enter or focus out), like a size chip."""
        if diameter > 0:
            self._onBrushChip(round(float(diameter), 1))

    def _fillSourceMenu(self):
        self.sourceMenu.clear()
        editor = segmentEditor()
        current = editor.sourceVolumeNode() if editor is not None else None
        for volumeNode in _editableVolumes():
            action = self.sourceMenu.addAction(volumeNode.GetName())
            action.setCheckable(True)
            action.setChecked(_sameNode(volumeNode, current))
            action.connect("triggered()", lambda volumeNode=volumeNode: self.setSourceVolume(volumeNode))

    def setSourceVolume(self, volumeNode):
        """The source chip: draw on ``volumeNode`` and show it in the slice views (figures stay where they are)."""
        editor = segmentEditor()
        if editor is None or not _isEditableSource(volumeNode):
            return
        editor.setSourceVolumeNode(volumeNode)
        importer = _importer()
        if importer is not None and hasattr(importer, "setViewLayers"):
            importer.setViewLayers(background=volumeNode)
        else:
            slicer.util.setSliceViewerLayers(background=volumeNode)

    #
    # Inspector cards (LNM addendum (a)) and drawcard_* discovery
    #

    def addInspectorCard(self, key, widget, order=50):
        self.headerWidget()
        self.trailingWidget()
        self.removeInspectorCard(key)
        self._cards[key] = (widget, order)
        target = self._cardsAfter if order >= CARD_ORDER_AFTER_DISPLAY else self._cardsBefore
        keys = sorted((k for k, (_w, o) in self._cards.items() if (o >= CARD_ORDER_AFTER_DISPLAY) == (order >= CARD_ORDER_AFTER_DISPLAY)),
                      key=lambda k: self._cards[k][1])
        target.insertWidget(keys.index(key), widget)
        widget.show()

    def removeInspectorCard(self, key):
        entry = self._cards.pop(key, None)
        if entry is None:
            return False
        widget = entry[0]
        for layout in (self._cardsBefore, self._cardsAfter):
            if layout.indexOf(widget) >= 0:
                layout.removeWidget(widget)
        widget.hide()
        widget.setParent(None)
        return True

    def _discoverDrawCards(self):
        """Cards of other work packages: ``StudioLib.drawcard_*`` modules with ``DRAW_CARD = {key, order, factory}``,
        ``factory(shell)`` returning a controller with ``widget``, ``refresh(ctx)`` and ``dispose()``.
        """
        if self._header is None or self._trailing is None:
            return
        shell = self._shell()
        if shell is None or self._drawCardsShell is shell:
            return
        self._disposeDrawCards()
        self._drawCardsShell = shell
        package = os.path.dirname(os.path.abspath(__file__))
        for moduleInfo in sorted(pkgutil.iter_modules([package]), key=lambda info: info.name):
            if not moduleInfo.name.startswith("drawcard_"):
                continue
            try:
                module = importlib.import_module(f"{__package__}.{moduleInfo.name}")
                card = getattr(module, "DRAW_CARD", None)
                if not card:
                    continue
                controller = card["factory"](shell)
                self._drawCards[card["key"]] = controller
                self.addInspectorCard(card["key"], controller.widget, card.get("order", 50))
            except Exception:
                logging.error(f"Studio: the Draw card {moduleInfo.name} could not be added:\n{traceback.format_exc()}")

    def _disposeDrawCards(self):
        for key, controller in list(self._drawCards.items()):
            self.removeInspectorCard(key)
            try:
                controller.dispose()
            except Exception:
                logging.error(f"Studio: the Draw card {key} could not be disposed:\n{traceback.format_exc()}")
        self._drawCards = {}
        self._drawCardsShell = None

    def refreshCards(self):
        editor = segmentEditor()
        context = {"shell": self._shell(), "segmentationNode": editor.segmentationNode() if editor is not None else None,
                   "sourceVolumeNode": editor.sourceVolumeNode() if editor is not None else None,
                   "segmentId": editor.currentSegmentID() if editor is not None else None}
        for key, controller in list(self._drawCards.items()):
            try:
                controller.refresh(context)
            except Exception:
                logging.error(f"Studio: the Draw card {key} could not be refreshed:\n{traceback.format_exc()}")

    #
    # State shown by the panel
    #

    def setTransientHint(self, text):
        self._transientHint = text
        if self._session is not None:
            self._session.refreshPanel()

    def showNativeLesionState(self, offer):
        self._nativeOffer = offer
        if self._session is not None:
            self._session.refreshPanel()

    def _nativeOfferShown(self, editor):
        offer = self._nativeOffer
        if offer is None:
            return None
        if editor.sourceVolumeNode() is not None:
            self._nativeOffer = None
            return None
        return offer

    def refresh(self, drawSession, computeMetrics=False):
        """Update the panel: empty state, Lesion card, chips, tile outlines, Display card."""
        if self._header is None:
            return
        self._discoverDrawCards()
        editor = segmentEditor()
        widget = _moduleWidget()
        if editor is None:
            return
        offer = self._nativeOfferShown(editor)
        empty = offer is not None or (editor.sourceVolumeNode() is None and not _editableVolumes())
        self.emptyState.update(offer if offer is not None else None, visible=empty)
        studio = widget is not None and getattr(widget, "presentation", lambda: "classic")() == "studio"
        if studio and editor.visible == empty:
            editor.setVisible(not empty)
        self.card.widget.setVisible(not empty)
        if self._trailing is not None:
            self._trailing.setVisible(not empty)
        if empty:
            self.chipsRow.hide()
            return
        segmentationNode = editor.segmentationNode()
        sourceVolumeNode = editor.sourceVolumeNode()
        activeName = _effectName(editor)
        # Brush and source chips
        brushActive = activeName in ("Paint", "Erase")
        diameter = _settingsFloat(BRUSH_DIAMETER_KEY, DEFAULT_BRUSH_DIAMETER_MM)
        effect = editor.effectByName("Paint")
        try:
            if effect is not None and not effect.integerParameter("BrushDiameterIsRelative"):
                diameter = effect.doubleParameter("BrushAbsoluteDiameter")
            elif effect is not None:
                diameter = None
        except Exception:
            pass
        for size, chip in self.brushChips.items():
            chip.setChecked(diameter is not None and abs(size - diameter) < 1e-3)
        self.brushSizes.setVisible(brushActive)
        self.brushField.setVisible(brushActive and diameter is not None)
        if diameter is not None and not self.brushField.hasFocus() and abs(self.brushField.value - diameter) >= 0.05:
            self.brushField.blockSignals(True)
            self.brushField.setValue(diameter)
            self.brushField.blockSignals(False)
        self.brushLabel.setVisible(brushActive)
        volumes = _editableVolumes()
        showSource = len(volumes) > 1 and sourceVolumeNode is not None
        if showSource:
            self.sourceChip.setText(_("on {name} ▾").format(name=sourceVolumeNode.GetName()))
        self.sourceChip.setVisible(showSource)
        self.chipsRow.setVisible(brushActive or showSource)
        # Lesion card
        cardState = self._cardState(drawSession, editor, segmentationNode, sourceVolumeNode, activeName, computeMetrics)
        self.card.refresh(cardState)
        self._refreshNotesRow(cardState.get("notes"), cardState.get("sourceReference"))
        # Tile outlines: the suggested tool (nothing armed) and the paused tool (Space resumes it)
        if widget is not None and hasattr(widget, "setTileOutline"):
            suggested = drawSession.suggestedEffect() if drawSession is not None else None
            paused = drawSession.pausedEffect() if drawSession is not None and suggested is None else None
            for effectName in EFFECT_APPEARANCE:
                if effectName == suggested:
                    self._setTileOutline(widget, effectName, ui.token("accent-fg"), dashed=False)
                elif effectName == paused:
                    self._setTileOutline(widget, effectName, ui.token("text2"), dashed=True)
                elif widget.tileOutline(effectName) is not None:
                    widget.setTileOutline(effectName, None)
        if self.displayCard is not None:
            self.displayCard.refresh(segmentationNode, sourceVolumeNode)
        self._styleToolsAndOptions(editor, studio, brushActive)

    def _styleToolsAndOptions(self, editor, studio, brushActive):
        """Studio look of the Segment Editor parts that Draw shows (VISUAL 4.4): the key of each tool in the corner of its
        tile, switches for the check boxes of the tool options, and no classic diameter row while the size row is shown.
        The classic presentation gets the check boxes and the diameter row back.
        """
        effectsGroupBox = editor.findChild("QFrame", "EffectsGroupBox")
        for button in effectsGroupBox.findChildren("QToolButton") if effectsGroupBox is not None else ():
            if button.property("slicerRole") != "toolTile":
                continue
            # Single-letter keys only: a longer key ("Shift+F") stays in the tooltip
            key = EFFECT_MNEMONICS.get(str(button.objectName), "") if studio else ""
            key = key if len(key) == 1 else ""
            if (button.property("studioKey") or "") != key:
                button.setProperty("studioKey", key)
                button.update()
        setOptionsStudioLook(editor, studio, hideDiameter=studio and brushActive)

    @staticmethod
    def _setTileOutline(widget, effectName, color, dashed):
        """Outline a tile, unless it already has this outline: setting its style sheet again polishes the outline
        and repaints the window once more (every time Draw is entered and after each change of the editor).
        """
        key = f"{color.name()} {'dashed' if dashed else 'solid'}"
        outline = widget.tileOutline(effectName)
        if outline is not None and outline.property("studioOutlineKey") == key:
            # Only follow the tile (as setTileOutline() does): no change, no repaint
            updateTileOutline = getattr(widget, "_updateTileOutline", None)
            if callable(updateTileOutline):
                updateTileOutline(effectName)
            return
        widget.setTileOutline(effectName, color, dashed=dashed)
        outline = widget.tileOutline(effectName)
        if outline is not None:
            outline.setProperty("studioOutlineKey", key)

    def _cardState(self, drawSession, editor, segmentationNode, sourceVolumeNode, activeName, computeMetrics):
        if segmentationNode is None:
            return {"name": _("No lesion"), "lesionId": None, "lesions": [], "readoutHtml": None, "stats": None, "saveState": None, "offerNewLesion": False,
                    "spaceLine": None, "hint": _("Open a scan or start on the MNI template"), "enabled": False}
        segmentId = _selectedSegmentId(segmentationNode, editor)
        segment = segmentationNode.GetSegmentation().GetSegment(segmentId) if segmentId else None
        displayNode = segmentationNode.GetDisplayNode()
        readoutKey = (segmentationNode.GetID(), segmentId)
        metrics = lesionMetrics(segmentationNode, compute=computeMetrics or self._lastReadout is None or self._lastReadoutKey != readoutKey)
        if metrics is not None:
            self._lastReadoutKey = readoutKey
            sideWord = metrics.get("sides") if _isMniLesion(segmentationNode) else None
            self._lastReadout = lesioncard.readoutHtml(metrics, sideWord, "warn" if sideWord == "Both hemispheres" else "neutral")
            # The stat cells of the card (VISUAL 4.4); none for an empty lesion
            # In scanner space the side is unknown (x = 0 is not the midline): the Side cell says so, no extra row
            self._lastStats = {"ml": metrics["ml"], "parts": metrics["parts"], "gaps": len(metrics["gaps"]),
                               "side": sideWord, "sideLevel": "warn" if sideWord == "Both hemispheres" else "neutral",
                               "sideNote": None if _isMniLesion(segmentationNode) else _("scanner space"),
                               "outside": metrics.get("outside_pct")} if metrics.get("voxels") else None
        saveText, saveToolTip = saveStateText(segmentationNode)
        importer = _importer()
        shell = self._shell()
        offerNewLesion = (lesionSaveState(segmentationNode)[0] == "saved" and getattr(shell, "studySession", None) is None
                          and importer is not None and hasattr(importer, "newLesionSegmentation"))
        spaceLine, spaceLevel = self._spaceLine(segmentationNode, sourceVolumeNode)
        if spaceLevel == "neutral" and spaceLine == _("Scan space") and self._lastStats is not None and self._lastReadoutKey == readoutKey:
            # The Side cell already says "scanner space"
            spaceLine = None
        lesions = [(node.GetID(), node.GetName(), bool(node.GetDisplayNode().GetVisibility()) if node.GetDisplayNode() is not None else True)
                   for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")]
        return {
            "name": segmentationNode.GetName(),
            "lesionId": segmentationNode.GetID(),
            "color": segment.GetColor() if segment is not None else None,
            "visible": bool(displayNode.GetVisibility()) if displayNode is not None else True,
            "lesions": lesions,
            "readoutHtml": self._lastReadout,
            "stats": self._lastStats if self._lastReadoutKey == readoutKey else None,
            "saveState": saveText,
            "saveStateToolTip": saveToolTip or "",
            "offerNewLesion": offerNewLesion,
            "spaceLine": spaceLine,
            "spaceLevel": spaceLevel,
            "hint": self._hint(drawSession, editor, activeName),
            "notes": segmentationNode.GetAttribute(NOTES_ATTRIBUTE) or "",
            "sourceReference": segmentationNode.GetAttribute(SOURCE_REFERENCE_ATTRIBUTE) or "",
            "follow": _settingsBool(FOLLOW_STROKES_KEY, True),
            "enabled": True,
        }

    def _spaceLine(self, segmentationNode, sourceVolumeNode):
        importer = _importer()
        line = importer.lesionSpaceLine(segmentationNode, sourceVolumeNode) if importer is not None and hasattr(importer, "lesionSpaceLine") else None
        if line:
            return line, "warn"
        if sourceVolumeNode is not None and not _isTemplateVolume(sourceVolumeNode) and not _isMniLesion(segmentationNode):
            return _("Scan space"), "neutral"
        userTemplate = _userTemplateOf(sourceVolumeNode)
        if userTemplate is not None:
            # Your own template (A-10): its name and space, so the sides and coordinates are read in the right space
            return _("On {template} · {space}").format(template=userTemplate.name, space=space.spaceLabel(space.spaceOf(sourceVolumeNode))), "neutral"
        return None, "neutral"

    def _hint(self, drawSession, editor, activeName):
        if self._transientHint and activeName == "Smoothing":
            return self._transientHint
        self._transientHint = None
        if drawSession is not None and drawSession.suspendedEffect() is not None:
            title = EFFECT_APPEARANCE.get(drawSession.suspendedEffect(), (drawSession.suspendedEffect(),))[0]
            return _("{tool} resumes after the placement · Esc cancels").format(tool=title)
        if activeName is not None:
            return _toolHints().get(activeName)
        paused = drawSession.pausedEffect() if drawSession is not None else None
        if paused is not None:
            title = EFFECT_APPEARANCE.get(paused, (paused,))[0]
            return _("{tool} paused · Space resumes · drag in a view adjusts contrast").format(tool=title)
        if _canDraw(editor):
            return _("Pick Brush (P) to draw · drag in a view adjusts contrast · Space pauses a tool")
        return None


class _LesionController:
    """Callbacks of the Lesion card."""

    def __init__(self, ext):
        self.ext = ext

    def _nodes(self):
        return lesionNodes()

    def _shell(self):
        return self.ext._shell()

    def renameLesion(self, name):
        """One user-visible name per lesion: the segmentation and its single segment are renamed together."""
        segmentationNode, _source = self._nodes()
        if segmentationNode is None or not name:
            return
        segmentationNode.SetName(name)
        segmentation = segmentationNode.GetSegmentation()
        if segmentation.GetNumberOfSegments() == 1:
            segmentation.GetNthSegment(0).SetName(name)

    def chooseColor(self):
        segmentationNode, _source = self._nodes()
        segmentId = _selectedSegmentId(segmentationNode)
        segment = segmentationNode.GetSegmentation().GetSegment(segmentId) if segmentId else None
        if segment is None:
            return
        color = qt.QColorDialog.getColor(qt.QColor.fromRgbF(*segment.GetColor()), slicer.util.mainWindow(), _("Lesion colour"))
        if color.isValid():
            segment.SetColor(color.redF(), color.greenF(), color.blueF())

    def setLesionVisible(self, visible):
        segmentationNode, _source = self._nodes()
        if segmentationNode is not None and segmentationNode.GetDisplayNode() is not None:
            segmentationNode.GetDisplayNode().SetVisibility(bool(visible))

    def setLesionVisibleById(self, nodeId, visible):
        node = slicer.mrmlScene.GetNodeByID(nodeId)
        if node is not None and node.GetDisplayNode() is not None:
            node.GetDisplayNode().SetVisibility(bool(visible))

    def selectLesion(self, nodeId):
        node = slicer.mrmlScene.GetNodeByID(nodeId)
        editor = segmentEditor()
        if node is None or editor is None:
            return
        importer = _importer()
        _segmentationNode, source = self._nodes()
        if importer is not None and hasattr(importer, "selectInSegmentEditor"):
            importer.selectInSegmentEditor(node, source)
        else:
            editor.setSegmentationNode(node)
        if node.GetDisplayNode() is not None:
            node.GetDisplayNode().SetVisibility(True)

    def centerOnLesion(self):
        centerOnLesion(self._shell())

    def setFollowStrokes(self, follow):
        qt.QSettings().setValue(FOLLOW_STROKES_KEY, bool(follow))

    def keepLargest(self):
        keepLargestPart(self._shell())

    def fillGaps(self):
        fillGaps(self._shell())

    def clipToBrain(self):
        _toast(self._shell(), _("No brain mask is known for this scan"))

    def fillGapsAndSpecks(self):
        fillGapsAndSpecks(self._shell())

    def newLesion(self):
        newLesion(self._shell())

    def _lesionNode(self, lesionId=None):
        """The lesion with ID ``lesionId`` (the card's lesion when the text was typed), else the current lesion."""
        if lesionId:
            node = slicer.mrmlScene.GetNodeByID(lesionId)
            return node if node is not None and node.IsA("vtkMRMLSegmentationNode") else None
        return self._nodes()[0]

    def setNotes(self, text, lesionId=None):
        segmentationNode = self._lesionNode(lesionId)
        if segmentationNode is not None and (segmentationNode.GetAttribute(NOTES_ATTRIBUTE) or "") != text:
            segmentationNode.SetAttribute(NOTES_ATTRIBUTE, text)

    def setSourceReference(self, text, lesionId=None):
        segmentationNode = self._lesionNode(lesionId)
        if segmentationNode is not None and (segmentationNode.GetAttribute(SOURCE_REFERENCE_ATTRIBUTE) or "") != text:
            segmentationNode.SetAttribute(SOURCE_REFERENCE_ATTRIBUTE, text)

    def menuActions(self):
        return lesionMenuActions(self._shell())


def newLesion(shell=None):
    """⋯ New lesion: a new lesion on the scan of Draw (the previous one is hidden, Undo in the toast), Brush armed."""
    shell = _currentShell(shell)
    importer = _importer()
    if importer is None or not hasattr(importer, "newLesionSegmentation"):
        return None
    if shell is not None:
        session(shell).requestArm()
    return importer.newLesionSegmentation(shell=shell)


def hideLesion(shell=None):
    segmentationNode, _source = lesionNodes()
    if segmentationNode is not None and segmentationNode.GetDisplayNode() is not None:
        segmentationNode.GetDisplayNode().SetVisibility(False)


def renameLesion(shell=None):
    ext = extension()
    if ext.card is not None:
        ext.card.startRename()


def focusNotes(shell=None, field="notes"):
    ext = extension()
    if ext.card is not None:
        ext.card.focusNotes(field)


def exportSegmentMasks(shell=None):
    """⋯ Export each segment as a NIfTI mask… (A-11): one binary mask (uint8 0/1) per segment of the lesion, on its own grid
    (or resampled onto a chosen image), each with a sidecar. Return the dialog, or None.
    """
    shell = _currentShell(shell)
    _applyPendingSmartFill(shell)
    _commitCardEdits()
    from . import niftiexport_ui

    return niftiexport_ui.exportLesionMasksDialog(shell)


def lesionMenuActions(shell=None):
    """Items of the Lesion card ⋯ menu: [(label, callback)] (None for a separator)."""
    actions = [
        (_("Rename lesion"), lambda: renameLesion(shell)),
        (_("New lesion"), lambda: newLesion(shell)),
        (_("Hide lesion"), lambda: hideLesion(shell)),
        None,
        (_("Save lesion as…"), lambda: saveLesionDialog(shell)),
        (_("Export each segment as a NIfTI mask…"), lambda: exportSegmentMasks(shell)),
        (_("Fill gaps and specks"), lambda: fillGapsAndSpecks(shell)),
        (_("Keep largest part"), lambda: keepLargestPart(shell)),
    ]
    study = getattr(shell, "studySession", None) if shell is not None else None
    if study is not None:
        actions.insert(4, (_("Save lesion"), lambda: saveLesionNow(shell)))
    return actions


def _discardWidget(widget):
    """Delete a widget that Draw built, once the current event is processed (it may be the button being clicked).

    Its stylesheet binding is removed and it keeps its parent until it is deleted: ``ui.bindStyleSheet()`` holds the
    Python wrapper, and a widget without a parent that Python still holds would be deleted at interpreter exit, when
    its Python event filters (``ui.installButtonKeys()``) can no longer run (the application then exits abnormally).
    """
    for target in (widget, *widget.findChildren("QWidget")):
        ui.bindStyleSheet(target, None)
    widget.hide()
    widget.deleteLater()


class _EmptyState:
    """Draw without a scan: what to do next (USABILITY 2), or the variant for a lesion in scan space."""

    def __init__(self, ext):
        self.ext = ext
        self.widget, layout = ui.card(None)
        self.widget.setObjectName("StudioDrawEmptyState")
        self.widget.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Maximum)
        layout.setSpacing(10)
        layout.addWidget(ui.iconTile("pencil", 36, 18, "StudioDrawEmptyStateIcon"))
        self.label = qt.QLabel()
        self.label.setObjectName("StudioDrawEmptyStateText")
        self.label.setWordWrap(True)
        self.label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        ui.setText(self.label, "section")
        layout.addWidget(self.label)
        self.buttons = qt.QVBoxLayout()
        self.buttons.setSpacing(6)
        layout.addLayout(self.buttons)
        self._offer = "none"
        self.widget.hide()

    def _button(self, text, callback, objectName, primary=False):
        button = ui.primaryButton(text) if primary else qt.QPushButton(text)
        button.setObjectName(objectName)
        if not primary:
            ui.setRole(button, "secondary")
        button.connect("clicked()", callback)
        ui.installButtonKeys(button)
        self.buttons.addWidget(button)
        return button

    def update(self, offer, visible):
        if not visible:
            self.widget.hide()
            return
        key = id(offer) if offer is not None else None
        if key != self._offer:
            self._offer = key
            while self.buttons.count():
                item = self.buttons.takeAt(0)
                if item.widget() is not None:
                    _discardWidget(item.widget())
            importer = _importer()
            shell = self.ext._shell()
            if offer is None:
                self.label.setText(_("Open a scan in Library or start on the MNI template"))
                self._button(_("Open…"), lambda: shell.openFiles() if shell is not None else None, "StudioDrawOpenButton", primary=True)
                if importer is not None:
                    self._button(_("Start on MNI template"), lambda: importer.startOnTemplate(shell=shell), "StudioDrawStartOnTemplateButton")
                self._button(_("Library"), lambda: shell.switchTo("library") if shell is not None else None, "StudioDrawLibraryButton")
            else:
                self.label.setText(offer.get("text") or _("This lesion is in scan space. Open the scan it was drawn on."))
                if offer.get("openWith") is not None and offer.get("sourcePath"):
                    self._button(_("Open with {name}").format(name=os.path.basename(offer["sourcePath"])), offer["openWith"],
                                 "StudioDrawOpenWithButton", primary=True)
                self._button(_("Open scan…"), offer["openScan"], "StudioDrawOpenScanButton", primary=offer.get("openWith") is None)
                link = qt.QPushButton(_("Show on the MNI template anyway"))
                link.setObjectName("StudioDrawShowOnTemplateButton")
                link.setFlat(True)
                ui.setRole(link, "ghost")
                link.connect("clicked()", offer["showOnTemplate"])
                self.buttons.addWidget(link)
        self.widget.show()


class _DisplayCard:
    """The lesion overlay in the views (VISUAL 4.4): an "Overlay" row (the fill opacity, with its percentage) and an
    "Outline" switch. Show in 3D, voxel view and the window/level presets are items of the Draw ⋯ menu
    (``displayMenuActions()``). The widget keeps the objectName of the former Display card (``StudioDisplayCard``).
    """

    #: Window/level presets of the ⋯ menu: (preset, title)
    PRESETS = (("auto", _("Auto")), ("brain", _("Brain")), ("narrow", _("Narrow")))

    def __init__(self, ext):
        self.ext = ext
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioDisplayCard")
        self.widget.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Maximum)
        self.widget.setToolTip(_("Drag in a view also adjusts window/level when no tool is active (Space pauses a tool)"))
        layout = qt.QVBoxLayout(self.widget)
        # Aligned with the inspector padding and the right edge of the size field
        layout.setContentsMargins(ui.INSPECTOR_PADDING, 0, 18, 0)
        layout.setSpacing(0)
        self._updating = False
        self._fillShown = True

        opacityRow = qt.QHBoxLayout()
        opacityRow.setSpacing(10)
        self.opacityLabel = ui.label(_("Overlay"), "body", "text", "StudioDisplayOpacityLabel")
        opacityRow.addWidget(self.opacityLabel)
        self.opacitySlider = qt.QSlider(qt.Qt.Horizontal)
        self.opacitySlider.setObjectName("StudioDisplayOpacitySlider")
        self.opacitySlider.setRange(0, 100)
        self.opacitySlider.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        self.opacitySlider.setToolTip(ui.tooltip(_("Overlay"), "O", _("opacity of the lesion fill in the slice views; O shows the outline only")))
        self.opacitySlider.setAccessibleName(_("Lesion overlay opacity"))
        self.opacitySlider.connect("valueChanged(int)", self._onOpacity)
        opacityRow.addWidget(self.opacitySlider, 1)
        self.opacityValue = ui.label("", "body", "text2", "StudioDisplayOpacityValue")
        ui.tabular(self.opacityValue)
        self.opacityValue.setAlignment(qt.Qt.AlignRight | qt.Qt.AlignVCenter)
        self.opacityValue.setMinimumWidth(self.opacityValue.fontMetrics().horizontalAdvance("100%") + 2)
        opacityRow.addWidget(self.opacityValue)
        rowWidget = qt.QWidget()
        rowWidget.setObjectName("StudioDisplayOpacityRow")
        rowWidget.setLayout(opacityRow)
        opacityRow.setContentsMargins(0, 0, 0, 0)
        rowWidget.setMinimumHeight(ui.ROW_HEIGHT)
        layout.addWidget(rowWidget)

        self.outlineSwitch = ui.switch(_("Outline"), "StudioDisplayOutlineSwitch")
        self.outlineSwitch.setToolTip(ui.tooltip(_("Outline"), None, _("draw the outline of the lesion over the fill")))
        self.outlineSwitch.connect("toggled(bool)", self._onOutline)
        layout.addWidget(self.outlineSwitch)

    def _displayNode(self):
        segmentationNode, _source = lesionNodes()
        return segmentationNode.GetDisplayNode() if segmentationNode is not None else None

    def refresh(self, segmentationNode, sourceVolumeNode):
        displayNode = segmentationNode.GetDisplayNode() if segmentationNode is not None else None
        self._updating = True
        try:
            fill, outline = (True, True)
            opacity = 0.4
            if displayNode is not None:
                fill, outline = bool(displayNode.GetVisibility2DFill()), bool(displayNode.GetVisibility2DOutline())
                segmentId = _selectedSegmentId(segmentationNode)
                opacity = displayNode.GetSegmentOpacity2DFill(segmentId) if segmentId else displayNode.GetOpacity2DFill()
            self._fillShown = fill
            # A hidden fill shows as 0%: moving the slider shows the fill again
            percent = round(opacity * 100) if fill else 0
            if self.opacitySlider.value != percent:
                self.opacitySlider.setValue(percent)
            self._showPercent(percent)
            if self.outlineSwitch.checked != outline:
                self.outlineSwitch.setChecked(outline)
        finally:
            self._updating = False

    def _showPercent(self, percent):
        text = f"{percent}%"
        if str(self.opacityValue.text) != text:
            self.opacityValue.setText(text)

    def _onOpacity(self, value):
        self._showPercent(value)
        displayNode = self._displayNode()
        if self._updating or displayNode is None:
            return
        segmentationNode, _source = lesionNodes()
        segmentId = _selectedSegmentId(segmentationNode)
        if value > 0 and not displayNode.GetVisibility2DFill():
            displayNode.SetVisibility2DFill(True)
        if segmentId:
            displayNode.SetSegmentOpacity2DFill(segmentId, value / 100.0)
        else:
            displayNode.SetOpacity2DFill(value / 100.0)

    def _onOutline(self, checked):
        displayNode = self._displayNode()
        if self._updating or displayNode is None:
            return
        displayNode.SetVisibility2DOutline(bool(checked))
        if not checked and not displayNode.GetVisibility2DFill():
            # Never nothing: without the outline the fill comes back
            displayNode.SetVisibility2DFill(True)

    def isShownIn3D(self):
        displayNode = self._displayNode()
        return bool(displayNode is not None and displayNode.GetVisibility3D() and displayNode.GetPreferredDisplayRepresentationName3D()
                    == slicer.vtkSegmentationConverter.GetBinaryLabelmapRepresentationName())

    def setShownIn3D(self, checked):
        """Show the lesion voxels in the 3D view (GPU, no surface to compute)."""
        displayNode = self._displayNode()
        if displayNode is None:
            return
        if checked:
            displayNode.SetPreferredDisplayRepresentationName3D(slicer.vtkSegmentationConverter.GetBinaryLabelmapRepresentationName())
        displayNode.SetVisibility3D(bool(checked))

    def isVoxelView(self):
        _segmentation, sourceVolumeNode = lesionNodes()
        displayNode = sourceVolumeNode.GetDisplayNode() if sourceVolumeNode is not None else None
        return bool(displayNode is not None and hasattr(displayNode, "GetInterpolate") and not displayNode.GetInterpolate())

    def setVoxelView(self, checked):
        """Crisp voxels (no interpolation) of the scan; on by default on the MNI template."""
        _segmentation, sourceVolumeNode = lesionNodes()
        displayNode = sourceVolumeNode.GetDisplayNode() if sourceVolumeNode is not None else None
        if displayNode is not None and hasattr(displayNode, "SetInterpolate"):
            displayNode.SetInterpolate(0 if checked else 1)

    def applyPreset(self, preset):
        """Window/level preset of the scan: "auto", "brain" or "narrow"."""
        _segmentation, sourceVolumeNode = lesionNodes()
        palette = _optionalModule("StudioLib.palette")
        if palette is not None and hasattr(palette, "applyWindowLevel"):
            palette.applyWindowLevel(preset, self.ext._shell(), volumeNode=sourceVolumeNode)
            return
        displayNode = sourceVolumeNode.GetDisplayNode() if sourceVolumeNode is not None else None
        if displayNode is not None and preset == "auto":
            displayNode.AutoWindowLevelOn()


class _CtaFooter:
    """Sticky inspector footer of Draw: the primary button (Ctrl+Enter) and a secondary text button.

    No study: "Save lesion · Ctrl+Enter" and the link "Map this lesion's network →" (VISUAL 4.4; a lesion in scan space is
    normalized from the panel's "Normalize to MNI" row). With a study: "Done → next · Ctrl+Enter" and the same link.
    objectNames of QW-A kept: StudioDrawFooter, StudioSaveLesionButton.
    """

    def __init__(self, drawSession):
        self.sessionRef = weakref.ref(drawSession)
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioDrawFooter")
        ui.bindStyleSheet(self.widget, lambda: (
            f"QWidget#StudioDrawFooter {{ background-color: {ui.token('window').name()}; "
            f"border-top: 1px solid {ui.token('hairline').name()}; }}"
            # The secondary action is a link under the primary button
            f"QPushButton#StudioDrawSecondaryButton {{ background: transparent; border: none; color: {ui.token('accent-fg').name()};"
            f" padding: 2px 6px; font-size: {ui.fontPx(12)}px; }}"
            "QPushButton#StudioDrawSecondaryButton:hover { text-decoration: underline; }"
            f"QPushButton#StudioDrawSecondaryButton:focus {{ border: 1px solid {ui.token('accent-fg').name()}; border-radius: 4px; }}"))
        self.widget.setAttribute(qt.Qt.WA_StyledBackground, True)
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(ui.INSPECTOR_PADDING, 12, ui.INSPECTOR_PADDING, 10)
        layout.setSpacing(6)
        self.saveButton = ui.primaryButton(_("Save lesion"))
        self.saveButton.setObjectName("StudioSaveLesionButton")
        # The pinned panel action (VISUAL 4.4): 38 px
        self.saveButton.setMinimumHeight(ui.PANEL_ACTION_HEIGHT)
        self.saveButton.connect("clicked()", self._onPrimary)
        ui.installButtonKeys(self.saveButton)
        layout.addWidget(self.saveButton)
        self.secondaryButton = qt.QPushButton()
        self.secondaryButton.setObjectName("StudioDrawSecondaryButton")
        self.secondaryButton.setFlat(True)
        self.secondaryButton.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        self.secondaryButton.connect("clicked()", self._onSecondary)
        ui.setRole(self.secondaryButton, "ghost")
        ui.installButtonKeys(self.secondaryButton)
        layout.addWidget(self.secondaryButton)
        self._secondary = None
        self._primary = None

    def _shell(self):
        drawSession = self.sessionRef()
        return drawSession.shell if drawSession is not None else None

    def update(self):
        """Update the labels; return (primary label, primary callback)."""
        shell = self._shell()
        key = ui.keyText(PRIMARY_SHORTCUT)
        segmentationNode, _source = lesionNodes()
        study = getattr(shell, "studySession", None) if shell is not None else None
        if study is not None:
            label = _("Done → next · {key}").format(key=key)
            self._primary = lambda: doneNext(shell)
            toolTip = ui.tooltip(_("Done → next"), PRIMARY_SHORTCUT, _("save this subject's lesion and open the next one"))
        else:
            label = _("Save lesion · {key}").format(key=key)
            self._primary = lambda: saveLesionNow(shell)
            record = _savedRecord(segmentationNode)
            if record is not None:
                toolTip = ui.tooltip(_("Save lesion"), PRIMARY_SHORTCUT, _("save again to {name} ({path})").format(name=os.path.basename(record[1]), path=record[1]))
            else:
                toolTip = ui.tooltip(_("Save lesion"), PRIMARY_SHORTCUT, _("a NIfTI mask on the lesion's own grid, and a sidecar"))
        if str(self.saveButton.text) != label:
            self.saveButton.setText(label)
        self.saveButton.setToolTip(toolTip)
        self.saveButton.setAccessibleName(label)
        self.saveButton.setEnabled(segmentationNode is not None)
        secondary = self._secondaryAction(segmentationNode, study)
        self._secondary = secondary
        self.secondaryButton.setVisible(secondary is not None)
        if secondary is not None and str(self.secondaryButton.text) != secondary[0]:
            self.secondaryButton.setText(secondary[0])
            self.secondaryButton.setToolTip(secondary[2])
        return label, self._onPrimary

    def _secondaryAction(self, segmentationNode, study):
        """The link under the primary button (VISUAL 4.4): "Map this lesion's network →" while Map is available. A lesion in
        scan space is normalized from the "Normalize to MNI" row of the panel (Map offers it too).
        """
        shell = self._shell()
        mapAvailable = False
        try:
            from . import workspaces

            mapWorkspace = workspaces.workspace("map")
            mapAvailable = mapWorkspace is not None and workspaces.isAvailable(mapWorkspace)
        except Exception:
            mapAvailable = False
        if segmentationNode is None or not mapAvailable:
            return None
        description = (_("compute the network map of this lesion") if _isMniLesion(segmentationNode) or study is not None
                       else _("the lesion is in scan space: Map offers to normalize it to MNI first"))
        return (_("Map this lesion's network →"), lambda: (_applyPendingSmartFill(shell), shell.switchTo("map")),
                ui.tooltip(_("Map network"), "Ctrl+Alt+3", description))

    def _onPrimary(self):
        if self._primary is not None:
            self._primary()

    def _onSecondary(self):
        if self._secondary is not None:
            self._secondary[1]()

    def dispose(self):
        """Unbind the stylesheets and delete the widget (it holds the callbacks, and so the Draw session)."""
        for button in (self.saveButton, self.secondaryButton):
            try:
                button.disconnect("clicked()")
            except Exception:
                pass
        self._primary = None
        self._secondary = None
        _discardWidget(self.widget)
        self.widget = None
        self.saveButton = None
        self.secondaryButton = None


def doneNext(shell=None):
    """Done → next (a study is open, W3-07): save this subject's lesion and open the next one."""
    shell = _currentShell(shell)
    _applyPendingSmartFill(shell)
    study = getattr(shell, "studySession", None) if shell is not None else None
    if study is None:
        return saveLesionNow(shell)
    _commitCardEdits()
    result = study.done(writeLesion=True) if hasattr(study, "done") else None
    if hasattr(study, "next"):
        session(shell).requestArm()
        study.next()
    return result


_extension = None


def extension():
    """The presentation extension of Draw (one per application)."""
    global _extension
    if _extension is None:
        _extension = DrawExtension()
        _installImportGuard()
    return _extension


def addInspectorCard(key, widget, order=50):
    """Add a card to the Draw inspector: between the Lesion card and the tools (``order`` < 100), else after Display."""
    extension().addInspectorCard(key, widget, order)


def removeInspectorCard(key):
    """Remove a card added with ``addInspectorCard()``. Return True if it was there."""
    return extension().removeInspectorCard(key)


def lesionCard():
    """The ``lesioncard.LesionCard`` of Draw (created with the Draw panel), or None."""
    return extension().card


#
# Palette provider (SPEC 2.12, U19)
#


def _paletteEntries(shell=None):
    shell = _currentShell(shell)

    def hasLesion():
        return lesionNodes()[0] is not None

    category = _("Draw")
    entries = [
        {"id": "draw.saveLesionAs", "title": _("Save lesion as…"), "category": category, "key": None, "synonyms": ["save as", "save lesion as", "save mask as"],
         "callback": lambda: saveLesionDialog(shell), "enabled": hasLesion, "description": _("Choose the file of the lesion (NIfTI)")},
        {"id": "draw.renameLesion", "title": _("Rename lesion"), "category": category, "synonyms": ["rename", "lesion name"],
         "callback": lambda: (shell.switchTo("draw") if shell is not None else None, renameLesion(shell)), "enabled": hasLesion},
        {"id": "action.newLesion", "title": _("New lesion"), "category": _("Action"), "synonyms": ["another lesion", "new segmentation", "add lesion", "next lesion"],
         "callback": lambda: newLesion(shell), "enabled": hasLesion, "description": _("A new lesion on the scan of Draw; the previous one is hidden")},
        {"id": "draw.centerLesion", "title": _("Center on lesion"), "category": category, "key": CENTER_SHORTCUT, "synonyms": ["center", "centre", "go to lesion", "find lesion"],
         "callback": lambda: centerOnLesion(shell), "enabled": hasLesion, "description": _("Bring all views to the lesion")},
        {"id": "draw.fillGapsAndSpecks", "title": _("Fill gaps and specks"), "category": category, "synonyms": ["clean up", "cleanup", "fill holes", "remove specks", "despeckle"],
         "callback": lambda: fillGapsAndSpecks(shell), "enabled": hasLesion,
         "description": _("Closes gaps up to 2 mm and removes islands under 10 mm³")},
        {"id": "draw.notes", "title": _("Notes"), "category": category, "synonyms": ["notes", "note", "comment", "metadata"],
         "callback": lambda: (shell.switchTo("draw") if shell is not None else None, focusNotes(shell)), "enabled": hasLesion,
         "description": _("Notes of the lesion, saved in its sidecar")},
        {"id": "draw.sourceReference", "title": _("Source (DOI, figure)"), "category": category, "synonyms": ["doi", "reference", "citation", "figure source"],
         "callback": lambda: (shell.switchTo("draw") if shell is not None else None, focusNotes(shell, "source")), "enabled": hasLesion},
        {"id": "draw.moreTools", "title": _("More tools…"), "category": category, "synonyms": ["more tools", "all tools", "effects"],
         "callback": lambda: _showMoreTools(shell), "description": _("Show every Segment Editor tool in Draw")},
    ]
    return entries


def _showMoreTools(shell=None):
    shell = _currentShell(shell)
    if shell is not None and shell.currentWorkspace() != "draw":
        shell.switchTo("draw")
    widget = _moduleWidget()
    if widget is not None and hasattr(widget, "setMoreToolsShown"):
        widget.setMoreToolsShown(True)


def _registerPaletteProvider():
    palette = _optionalModule("StudioLib.palette")
    if palette is not None and hasattr(palette, "registerProvider"):
        palette.registerProvider(_paletteEntries)


#
# Hooks
#


def enter(shell):
    """Draw becomes the current workspace: the studio presentation, the tool, the CTA and the Draw keys."""
    session(shell).enter()


def leave(shell):
    """Draw is left (another workspace, a transient module or Classic): record the tool, remove the footer."""
    drawSession = _sessions.get(shell)
    if drawSession is not None and drawSession.active:
        drawSession.leave()


#: The importer leaves the space line of a lesion to the Lesion card (U-07 feature detection)
showsLesionSpaceLine = True


def showNativeLesionState(shell, offer):
    """A lesion in scan space shown alone (U-07): the empty-state variant offers its scan."""
    extension().showNativeLesionState(offer)


def helpHtml(shell):
    """``(title, html)`` of the header "?": the active (or suggested) tool, its keys and the Draw keys."""
    editor = segmentEditor()
    effect = editor.activeEffect() if editor is not None else None
    effectName = effect.name if effect is not None else DEFAULT_EFFECT
    if effect is None and editor is not None:
        effect = editor.effectByName(effectName)
    title = EFFECT_APPEARANCE.get(effectName, (effect.title if effect is not None else effectName,))[0]
    keys = [ui.keyText(key) for key in (EFFECT_MNEMONICS.get(effectName),) if key]
    # The effect's own help (HTML written by the effect), without the document tags that would end the popover's text
    helpText = re.sub(r"(?i)</?(html|body|head)[^>]*>", "", _effectHelpText(effect)).strip()
    description = _effectDescriptions().get(effectName)
    if description is None and helpText:
        plain = " ".join(re.sub(r"<[^>]*>", " ", helpText).split())
        description = re.split(r"(?<=\.)\s", plain, maxsplit=1)[0]
    rows = [
        ("P · D · M · E · L · Shift+F", _("Brush, Outline, Smart fill, Erase (again: back), Auto outline, Fill gaps")),
        ("O", _("Fill or outline only")),
        ("Space", _("Pause or resume the tool (a drag then adjusts window/level)")),
        ("Esc", _("Deselect the tool")),
        ("N · Shift+N", _("Next / previous gap slice")),
        ("C", _("Center on lesion")),
        ("Alt+← · Alt+→", _("Previous / next lesion of a set or subject")),
        (ui.keyText(SAVE_LESION_SHORTCUT) + " · " + ui.keyText(PRIMARY_SHORTCUT), _("Save lesion")),
        ("Ctrl+Z · " + ui.keyText(REDO_SHORTCUT), _("Undo · Redo")),
    ]
    table = "".join(f"<tr><td style='padding-right: 12px;'><b>{html.escape(key)}</b></td><td>{html.escape(text)}</td></tr>" for key, text in rows)
    body = ""
    if description:
        body += f"<p>{html.escape(description)}</p>"
    origin = _("Segment Editor effect: {effect}").format(effect=effect.name if effect is not None else effectName)
    body += f"<p><b>{html.escape(_('Key'))}:</b> {html.escape(' · '.join(keys))} · {html.escape(origin)}</p>" if keys else f"<p>{html.escape(origin)}</p>"
    body += f"<h4>{html.escape(_('Draw keys'))}</h4><table>{table}</table>"
    if helpText:
        body += f"<h4>{html.escape(_('More about {tool}').format(tool=title))}</h4><div>{helpText}</div>"
    return _("Draw · {tool}").format(tool=title), body


def displayMenuActions(shell=None):
    """The display items of the Draw ⋯ menu: Show lesion in 3D, Voxel view and the window/level presets (VISUAL 4.4: the
    panel keeps only the Overlay row and the Outline switch).
    """
    displayCard = extension().displayCard
    segmentationNode, sourceVolumeNode = lesionNodes()
    if displayCard is None or segmentationNode is None:
        return []
    actions = [(_("Show lesion in 3D"), lambda: displayCard.setShownIn3D(not displayCard.isShownIn3D()), displayCard.isShownIn3D())]
    sourceDisplay = sourceVolumeNode.GetDisplayNode() if sourceVolumeNode is not None else None
    if sourceDisplay is not None and hasattr(sourceDisplay, "GetInterpolate"):
        actions.append((_("Voxel view"), lambda: displayCard.setVoxelView(not displayCard.isVoxelView()), displayCard.isVoxelView()))
    if sourceVolumeNode is not None:
        actions.append((_("Window/level"), [(title, lambda preset=preset: displayCard.applyPreset(preset), None) for preset, title in _DisplayCard.PRESETS]))
    return actions


def moreMenuActions(shell):
    """⋯ of the panel header: the display items (``displayMenuActions()``) and "Show all controls" (the classic Segment
    Editor controls inside Draw).
    """
    actions = displayMenuActions(shell)
    if actions:
        actions.append(None)
    actions.append((_("Show all controls"), lambda: setShowAllControls(not _settingsBool(SHOW_ALL_CONTROLS_KEY, False), shell),
                    _settingsBool(SHOW_ALL_CONTROLS_KEY, False)))
    return actions


def setShowAllControls(show, shell=None):
    """Show the classic Segment Editor controls (segment table, undo/redo, all tools, selectors) in Draw, or not."""
    qt.QSettings().setValue(SHOW_ALL_CONTROLS_KEY, bool(show))
    ext = extension()
    ext.showAllControls = bool(show)
    widget = _moduleWidget()
    if widget is not None and hasattr(widget, "refreshPresentation"):
        widget.refreshPresentation()


def focusFirst(shell):
    """The keyboard focus starts on the active tool's tile, else the suggested one."""
    editor = segmentEditor()
    widget = _moduleWidget()
    if editor is None or widget is None:
        return
    effectName = _effectName(editor) or DEFAULT_EFFECT
    for button in editor.findChildren("QToolButton"):
        if button.objectName == effectName and button.isVisible():
            button.setFocus(qt.Qt.OtherFocusReason)
            return


# Scene loads in Studio while Draw is not shown: the Segment Editor widget can exist before Draw is entered
_installImportGuard()

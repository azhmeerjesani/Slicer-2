"""Trace a lesion from a published figure (L-29; SPEC_LNM_ADDENDUM 3.10, 5, 6.3, 7.3-7.4; USABILITY 13).

A third of lesion network mapping papers trace lesions from figures of case reports. This module makes that tracing
faster, auditable and free of silent left-right flips, with standard MRML nodes only (SPEC 9.7):

- **Entry points** (Draw on a template): a dropped picture (``importer.loadFiles`` calls ``openFigure()``), ``Ctrl+V``
  (a Draw-scoped shortcut, enabled only while the clipboard holds an image; never in Classic), "Open figure…" (a file
  dialog that starts in ``/data`` in the web build, which has no image clipboard and no host drag-drop), the Lesion card
  ⋯ "Trace from figure…" and the Search entry of the same name. On a scan in scan space a figure stays a reference in the
  Compare layout (U-07): tracing from a figure needs the template.
- **Figure card** (Draw inspector, ``draw.addInspectorCard``): one panel at a time (``Add panel`` gives each panel its
  own figure, plane and fit; every panel paints into the same Lesion segment).

  1. *Crop*: a markups ROI on the figure view; Pillow crops the figure and only the crop is kept (``crop_px`` records
     where it was in the figure).
  2. *Plane*: Axial, Coronal or Sagittal.
  3. *Convention*: "Patient's right is on the [Left | Right] of this figure". Radiological (patient's right on the
     left) is assumed until it is confirmed, once per source record (``record_id``): the answer is
     ``Studio.FigureConvention`` (U-07's attribute and semantics; "unknown" until confirmed). The figure views show
     the side tag and R/L letters (``orientation.py``).
  4. *Landmarks*: two markups point lists, ``FigureLandmarks`` (on the figure, Green view) and ``TemplateLandmarks``
     (on the template, Red view), placed in pairs with suggested names (``figurefit.suggested_landmarks``).
  5. *Fit*: ``figurefit`` similarity by default; affine only with at least 4 pairs. The residual chip reads
     "RMS 1.6 mm · LOO 2.3 mm (5 pairs)" in green, amber or red, with the note that the residual measures landmark
     consistency, not lesion accuracy.

- **Nodes** (standard, tagged ``StudioFigure`` = "1", never written into a lesion file): the crop is a single-slice
  vector volume (Slicer's PNG reader) whose parent ``vtkMRMLLinearTransformNode`` is plane frame ∘ 2D fit
  (``figurefit.figure_ijk_to_ras``); the figure landmarks and the crop ROI sit under the same transform, so they keep
  their pixels when the fit changes. The edge image (Pillow ``FIND_EDGES``) is a scalar volume under it too.
- **Views** (layout 7001, no new layout id; SPEC_LNM_ADDENDUM 5): Red shows the template resliced on the cut plane
  (z, pitch ±20° and roll ±20° in 0.5° steps, ``SetSliceToRASByNTP``) with the figure as foreground at 50 %; Green the
  same plane with the figure alone at 100 % (a figure view: painting, slice stepping and links are blocked there); Yellow
  the orthogonal plane that shows the cut plane's line. The view layers are saved when the trace views are entered and
  restored when they are left. Opacity slider; ``T`` (the slice views' own key) flickers the figure; "Edges" shows the
  edge image as the foreground instead. Brush, Outline and Smart fill paint the template-grid Lesion segment in Red, also
  on an oblique plane.
- **Save** (``saveFigures()``; atomic): in a study, ``sources/<record_id>/fig-<k>.png`` (the crop only; the folder is
  named after the participant when the row has no ``record_id``) and ``fig-<k>_fit.json`` (``figurefit`` fit file), and
  the columns ``figure`` and ``panel`` (the labels entered, if any), ``fig_convention``, ``planes``, ``fit_rms_mm`` and
  ``trace_method`` += figure through the ``StudySession`` (``record_id`` is never written); a draft
  (``.studio/figure/<sid>/``) is written while you work, for recovery. ``showSource(json)`` rebuilds the overlay from a
  fit file (≤ 300 ms).
- **Done** (a study Done hook, order 20): a figure whose convention is not confirmed blocks Done with an inline required
  choice in the Figure card, never a modal; otherwise the fit is saved and the columns are returned. The plane normals go
  to later hooks as ``ctx["figure_plane_normal"]`` / ``ctx["figure_plane_normals"]`` (L-28's dimensionality).
- **Classic**: ``showClassicCard()`` hosts the same card in a non-modal window ("Trace from figure…").

Settings (fork-only): ``Studio/Figure/LastFolder``. Node attributes: ``StudioFigure``, ``Studio.Role`` = figure,
``Studio.FigureConvention``, ``Studio.FigurePanel``, ``Studio.FigureState`` (JSON without any path).
"""

import contextlib
import json
import logging
import math
import os
import shutil
import tempfile
import time
import traceback

import numpy as np
import qt
import slicer
import vtk
from slicer.i18n import tr as _

from . import space

#: Node attribute of every figure-tracing node (SPEC_LNM_ADDENDUM 5)
FIGURE_ATTRIBUTE = "StudioFigure"
PANEL_ATTRIBUTE = "Studio.FigurePanel"
STATE_ATTRIBUTE = "Studio.FigureState"
#: Markups lists of a panel
FIGURE_LANDMARKS_NAME = "FigureLandmarks"
TEMPLATE_LANDMARKS_NAME = "TemplateLandmarks"
#: Views of the trace arrangement (layout 7001)
TRACE_VIEW = "Red"
FIGURE_VIEW = "Green"
ORTHO_VIEW = "Yellow"
#: Green keeps out of linked view changes while it shows a figure (like U-07's Yellow)
FIGURE_VIEW_GROUP = 1
#: Opacity of the figure over the template in Red
DEFAULT_OPACITY = 0.5
#: Draw key (SPEC_LNM_ADDENDUM 7.4)
PASTE_SHORTCUT = "Ctrl+V"
#: Order of the Figure card in the Draw inspector (between the Lesion card and the tools)
CARD_ORDER = 30
CARD_KEY = "figure"
#: Order of the Done hook (before L-28's dimensionality hook, which reads the plane normal)
DONE_HOOK_ORDER = 20
#: Fork-only setting: the folder of the last opened figure
LAST_FOLDER_KEY = "Studio/Figure/LastFolder"
#: The web build's data folder (SPEC_LNM_ADDENDUM 6.3; P-03)
WEB_DATA_FOLDER = "/data"
#: "Show source" budget
REBUILD_BUDGET_MS = 300
#: Template landmarks farther than this from the cut plane are flagged (mm)
OFF_PLANE_WARNING_MM = 2.0
#: Debounce of the refit after a landmark moved, and of the draft write
REFIT_DELAY_MS = 30
DRAFT_DELAY_MS = 2000
#: Cut plane controls: steps of z (mm) and of pitch and roll (degrees)
Z_STEP_MM = 1.0
TILT_STEP_DEG = 0.5
#: Picture file filter of "Open figure…"
FIGURE_FILTER = "Figures (*.png *.jpg *.jpeg *.tif *.tiff *.bmp)"
PLANE_TITLES = {"axial": _("Axial"), "coronal": _("Coronal"), "sagittal": _("Sagittal")}
#: The orthogonal view of each plane: the one where pitch tilts the cut plane's line
ORTHO_ORIENTATION = {"axial": "Sagittal", "coronal": "Sagittal", "sagittal": "Coronal"}
LANDMARK_COLORS = {"figure": (0.98, 0.75, 0.18), "template": (0.25, 0.80, 0.95)}


def _figurefit():
    import importlib

    return importlib.import_module("LesionNetworkMappingLib.figurefit")


def _currentShell(shell=None):
    if shell is not None:
        return shell if shell.isActive() else None
    from . import shell as shellModule

    return shellModule.instance()


def _toast(shell, text, **kwargs):
    shell = _currentShell(shell)
    if shell is not None:
        shell.toast(text, **kwargs)
    else:
        logging.info(f"Studio figure: {text}")


def _draw():
    from . import draw

    return draw


def _importer():
    from . import importer

    return importer


def _studySession():
    try:
        from . import study_ui
    except ImportError:
        return None
    session = study_ui.session()
    return session if session is not None and session.current() else None


def _segmentEditor():
    widget = getattr(slicer.modules, "SegmentEditorWidget", None)
    return getattr(widget, "editor", None) if widget is not None else None


def _sliceWidget(viewName):
    layoutManager = slicer.app.layoutManager()
    return layoutManager.sliceWidget(viewName) if layoutManager is not None else None


def _sliceNode(viewName):
    widget = _sliceWidget(viewName)
    return widget.mrmlSliceNode() if widget is not None else None


def _compositeNode(viewName):
    widget = _sliceWidget(viewName)
    return widget.mrmlSliceCompositeNode() if widget is not None else None


def _matrix(vtkMatrix):
    return np.array([[vtkMatrix.GetElement(row, column) for column in range(4)] for row in range(4)])


def _vtkMatrix(array):
    matrix = vtk.vtkMatrix4x4()
    for row in range(4):
        for column in range(4):
            matrix.SetElement(row, column, float(array[row][column]))
    return matrix


def _ijkToRas(volumeNode):
    matrix = vtk.vtkMatrix4x4()
    volumeNode.GetIJKToRASMatrix(matrix)
    return _matrix(matrix)


def _node(nodeId):
    return slicer.mrmlScene.GetNodeByID(nodeId) if nodeId else None


def _tag(node, panelIndex=None):
    node.SetAttribute(FIGURE_ATTRIBUTE, "1")
    if panelIndex is not None:
        node.SetAttribute(PANEL_ATTRIBUTE, str(panelIndex))


def _connectIdClicked(group, slot):
    """Connect a QButtonGroup's id click: buttonClicked(int) is gone in Qt 6; idClicked(int) exists since Qt 5.15."""
    group.connect("idClicked(int)" if hasattr(group, "idClicked") else "buttonClicked(int)", slot)


@contextlib.contextmanager
def _renderPaused():
    """Pause the rendering of the views (``qSlicerApplication.pauseRender``) for the duration of the block."""
    pause = getattr(slicer.app, "pauseRender", None)
    if pause is None:
        yield
        return
    pause()
    try:
        yield
    finally:
        slicer.app.resumeRender()


def isFigureNode(node):
    """True for a node of figure tracing (``StudioFigure`` = "1")."""
    return node is not None and hasattr(node, "GetAttribute") and node.GetAttribute(FIGURE_ATTRIBUTE) == "1"


def figureNodes():
    """Every node of figure tracing in the scene."""
    nodes = []
    for className in ("vtkMRMLVolumeNode", "vtkMRMLLinearTransformNode", "vtkMRMLMarkupsNode"):
        nodes.extend(node for node in slicer.util.getNodesByClass(className) if isFigureNode(node))
    return nodes


def isWebBuild():
    """The browser build (``SLICER_STUDIO_WEB`` or a container): no image clipboard and no host drag-drop."""
    return bool(os.environ.get("SLICER_STUDIO_WEB")) or os.path.exists("/.dockerenv")


def clipboardHasImage():
    clipboard = qt.QApplication.clipboard()
    mimeData = clipboard.mimeData() if clipboard is not None else None
    return mimeData is not None and mimeData.hasImage()


def _figureFolder():
    folder = os.path.join(slicer.app.temporaryPath, "StudioFigures")
    os.makedirs(folder, exist_ok=True)
    return folder


def _temporaryPng(stem):
    handle, path = tempfile.mkstemp(prefix=f"{stem}-", suffix=".png", dir=_figureFolder())
    os.close(handle)
    return path


def _atomicCopy(source, target):
    temporary = f"{target}.{os.getpid()}.tmp"
    try:
        shutil.copyfile(source, temporary)
        with open(temporary, "rb") as f:
            os.fsync(f.fileno())
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(OSError):
            os.remove(temporary)


def _writeJsonAtomic(path, content):
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(content, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            os.remove(temporary)


def canTrace(sourceVolumeNode=None):
    """True if a figure dropped now is traced on a template: Draw's source (or the shown scan) is a template, or nothing
    is shown yet (the template is started first). A scan in scan space keeps U-07's Compare route.
    """
    importer = _importer()
    if sourceVolumeNode is None:
        editor = _segmentEditor()
        sourceVolumeNode = editor.sourceVolumeNode() if editor is not None else None
        if sourceVolumeNode is None:
            sourceVolumeNode = importer._backgroundVolume()
    if sourceVolumeNode is None:
        return True
    if importer._isTemplate(sourceVolumeNode):
        return True
    detected = space.spaceOf(sourceVolumeNode)
    return bool(detected) and str(detected[0]).startswith("MNI")


#
# Panels
#


class Panel:
    """One figure panel: its crop volume, placement transform, landmark lists, cut plane and fit."""

    def __init__(self, session, index, figureNode):
        self.session = session
        self.index = index
        self.figureId = figureNode.GetID()
        self.transformId = None
        self.figureLandmarksId = None
        self.templateLandmarksId = None
        self.edgesId = None
        self.roiId = None
        self.plane = "axial"
        self.z = 0.0
        self.pitch = 0.0
        self.roll = 0.0
        self.model = "similarity"
        self.fit = None
        self.fitError = None
        self.label = ""
        self.panelLabel = ""
        self.sourceName = ""
        storageNode = figureNode.GetStorageNode()
        fileName = storageNode.GetFileName() if storageNode is not None else None
        if fileName:
            self.sourceName = os.path.basename(fileName)
        dims = figureNode.GetImageData().GetDimensions() if figureNode.GetImageData() is not None else (1, 1, 1)
        self.cropPx = [0, 0, int(dims[0]), int(dims[1])]
        #: The PNG of the crop (written by ``applyCrop()`` or saved); None until then (the figure's own file is never copied)
        self.cropPath = None
        self.savedIndex = None
        _tag(figureNode, index)
        figureNode.SetAttribute(space.ROLE_ATTRIBUTE, space.ROLE_FIGURE)
        if figureNode.GetAttribute(space.FIGURE_CONVENTION_ATTRIBUTE) not in space.CONVENTIONS:
            figureNode.SetAttribute(space.FIGURE_CONVENTION_ATTRIBUTE, "unknown")

    # Nodes

    def figure(self):
        return _node(self.figureId)

    def transform(self):
        return _node(self.transformId)

    def figureLandmarks(self):
        return _node(self.figureLandmarksId)

    def templateLandmarks(self):
        return _node(self.templateLandmarksId)

    def edges(self):
        return _node(self.edgesId)

    def nodes(self):
        return [node for node in (self.figure(), self.transform(), self.figureLandmarks(), self.templateLandmarks(), self.edges(), _node(self.roiId)) if node is not None]

    def size(self):
        image = self.figure().GetImageData()
        dims = image.GetDimensions()
        return int(dims[0]), int(dims[1])

    def title(self):
        text = _("Panel {k}").format(k=self.index)
        return f"{text} · {self.panelLabel}" if self.panelLabel else text

    # Convention

    @property
    def convention(self):
        """"radiological", "neurological" or "unknown" (not confirmed yet)."""
        value = self.figure().GetAttribute(space.FIGURE_CONVENTION_ATTRIBUTE) if self.figure() is not None else None
        return value if value in space.CONVENTIONS else "unknown"

    @property
    def confirmed(self):
        return self.convention in ("radiological", "neurological")

    @property
    def fitConvention(self):
        """The convention the fit uses: the confirmed one, else the radiological default (SPEC_LNM_ADDENDUM 3.10)."""
        return self.convention if self.confirmed else _figurefit().DEFAULT_CONVENTION

    # Geometry

    def cut(self):
        return _figurefit().cut_plane(self.plane, self.z, self.pitch, self.roll)

    def pixelOf(self, localPoint):
        """(u, v) pixel of a point in the figure volume's own (pre-transform) coordinates."""
        rasToIjk = np.linalg.inv(_ijkToRas(self.figure()))
        ijk = rasToIjk @ np.array([*localPoint[:3], 1.0])
        return float(ijk[0]), float(ijk[1]), float(ijk[2])

    def localOf(self, pixel):
        ras = _ijkToRas(self.figure()) @ np.array([float(pixel[0]), float(pixel[1]), 0.0, 1.0])
        return ras[:3]

    def pairs(self):
        """(names, figure pixels (n, 2), template RAS (n, 3)) of the complete landmark pairs, in order."""
        figureList, templateList = self.figureLandmarks(), self.templateLandmarks()
        if figureList is None or templateList is None:
            return [], np.zeros((0, 2)), np.zeros((0, 3))
        names, pixels, points = [], [], []
        count = min(figureList.GetNumberOfControlPoints(), templateList.GetNumberOfControlPoints())
        for index in range(count):
            if not (figureList.GetNthControlPointPositionStatus(index) == figureList.PositionDefined
                    and templateList.GetNthControlPointPositionStatus(index) == templateList.PositionDefined):
                continue
            local = [0.0, 0.0, 0.0]
            figureList.GetNthControlPointPosition(index, local)
            world = [0.0, 0.0, 0.0]
            templateList.GetNthControlPointPositionWorld(index, world)
            pixels.append(self.pixelOf(local)[:2])
            points.append(world)
            names.append(templateList.GetNthControlPointLabel(index) or figureList.GetNthControlPointLabel(index))
        return names, np.array(pixels, dtype=float).reshape(-1, 2), np.array(points, dtype=float).reshape(-1, 3)

    def pairCount(self):
        return len(self.pairs()[0])

    def ijkToRas(self):
        """The figure's IJK to RAS: the fit, or the initial placement before it (a brain about 140 mm wide)."""
        figurefit = _figurefit()
        width, height = self.size()
        if self.fit is not None:
            return figurefit.figure_ijk_to_ras(self.fit, self.cut(), convention=self.fitConvention)
        spacing = figurefit.initial_pixel_size(width)
        return figurefit.figure_ijk_to_ras(None, self.cut(), pixel_spacing=spacing, convention=self.fitConvention, size_px=(width, height))

    def refit(self):
        """Fit the landmark pairs (similarity, or affine with at least 4 pairs). Return the fit or None."""
        figurefit = _figurefit()
        names, pixels, points = self.pairs()
        model = self.model if len(names) >= figurefit.MIN_PAIRS.get(self.model, 2) else "similarity"
        self.fit = None
        self.fitError = None
        if len(names) >= figurefit.MIN_PAIRS["similarity"]:
            try:
                self.fit = figurefit.fit_figure(pixels, points, self.cut(), convention=self.fitConvention, model=model)
            except ValueError as error:
                self.fitError = str(error)
        return self.fit

    def place(self, attach=False):
        """Put the figure (and its landmarks and ROI) where the fit, or the initial placement, says.

        ``attach``: the figure (and its edge image) go under the placement transform. Until then (U-07's Compare view)
        the figure stays where its file put it.
        """
        transform = self.transform()
        if transform is None or self.figure() is None:
            return
        if attach:
            for node in (self.figure(), self.edges()):
                if node is not None and node.GetTransformNodeID() != transform.GetID():
                    node.SetAndObserveTransformNodeID(transform.GetID())
        parent = _figurefit().parent_transform(self.ijkToRas(), _ijkToRas(self.figure()))
        transform.SetMatrixTransformToParent(_vtkMatrix(parent))

    # State (drafts and attributes; no path)

    def state(self):
        names, pixels, points = self.pairs()
        return {
            "plane": self.plane, "z_mm": self.z, "pitch_deg": self.pitch, "roll_deg": self.roll, "model": self.model,
            "convention": self.convention, "crop_px": list(self.cropPx), "figure": self.label, "panel": self.panelLabel,
            "source_name": self.sourceName,
            "landmarks": [{"name": name, "fig_px": [float(value) for value in pixel], "tpl_ras": [float(value) for value in point]}
                          for name, pixel, point in zip(names, pixels, points, strict=True)],
        }

    def storeState(self):
        figureNode = self.figure()
        if figureNode is not None:
            state = self.state()
            state.pop("landmarks")
            figureNode.SetAttribute(STATE_ATTRIBUTE, json.dumps(state, ensure_ascii=False))


class _ViewGuard:
    """Interactor observers (priority 2.0) that keep a figure view a reference: a left press, release or double-click
    while an editor effect is active, a plain wheel and the slice keys are aborted. Ctrl+wheel zoom, right-drag zoom,
    middle-drag pan and landmark placement still work.
    """

    GUARDED_KEYS = ("Up", "Down", "Prior", "Next", "Page_Up", "Page_Down", "Home", "End", "Left", "Right", "f", "b")

    def __init__(self, viewName):
        self.viewName = viewName
        self.interactor = None
        self.tags = []
        self.active = False

    def install(self):
        widget = _sliceWidget(self.viewName)
        interactor = widget.sliceView().interactor() if widget is not None else None
        if interactor is None or (interactor == self.interactor and self.tags):
            return
        self.remove()
        self.interactor = interactor
        for event, handler in ((vtk.vtkCommand.LeftButtonPressEvent, self._onLeft), (vtk.vtkCommand.LeftButtonReleaseEvent, self._onLeft),
                               (vtk.vtkCommand.LeftButtonDoubleClickEvent, self._onLeft), (vtk.vtkCommand.MouseWheelForwardEvent, self._onWheel),
                               (vtk.vtkCommand.MouseWheelBackwardEvent, self._onWheel), (vtk.vtkCommand.KeyPressEvent, self._onKey),
                               (vtk.vtkCommand.CharEvent, self._onKey)):
            self._add(interactor, event, handler)
        self.active = True

    def _add(self, interactor, event, handler):
        command = []

        def callback(caller, eventName):
            try:
                if self.active and handler(caller) and command:
                    command[0].SetAbortFlag(1)
            except Exception:
                logging.error(f"Studio figure view guard failed:\n{traceback.format_exc()}")

        tag = interactor.AddObserver(event, callback, 2.0)
        command.append(interactor.GetCommand(tag))
        self.tags.append(tag)

    def remove(self):
        if self.interactor is not None:
            for tag in self.tags:
                self.interactor.RemoveObserver(tag)
        self.tags = []
        self.interactor = None
        self.active = False

    @staticmethod
    def _onLeft(interactor):
        editor = _segmentEditor()
        return editor is not None and editor.activeEffect() is not None

    @staticmethod
    def _onWheel(interactor):
        return not (interactor.GetControlKey() or interactor.GetShiftKey() or interactor.GetAltKey())

    def _onKey(self, interactor):
        if interactor.GetControlKey() or interactor.GetAltKey():
            return False
        return str(interactor.GetKeySym() or "") in self.GUARDED_KEYS


#
# Session
#


class FigureSession:
    """The figure panels of the lesion being traced, the trace views and the card."""

    def __init__(self, shell=None):
        self.shell = shell
        self.panels = []
        self.current = None
        self.tracing = False
        self.saved = None
        self.sid = None
        #: The row's ``record_id`` (None when the row has none: the figure files then go to ``sources/<sid>/``)
        self.recordId = None
        self.templateId = None
        self.opacity = DEFAULT_OPACITY
        self.showEdges = False
        self.requiredConvention = False
        self.lastSaved = []
        #: Changes since the last save (a subject switch writes a draft of them)
        self.unsaved = False
        self._observations = []
        self._viewObservations = []
        self._updating = False
        self._placing = None
        self._interactionObservation = None
        self._pendingTemplate = None
        self._guard = _ViewGuard(FIGURE_VIEW)
        self._refitTimer = qt.QTimer()
        self._refitTimer.setSingleShot(True)
        self._refitTimer.setInterval(REFIT_DELAY_MS)
        self._refitTimer.connect("timeout()", self._onRefitTimeout)
        self._viewTimer = qt.QTimer()
        self._viewTimer.setSingleShot(True)
        self._viewTimer.setInterval(0)
        self._viewTimer.connect("timeout()", self._checkViews)
        self._draftTimer = qt.QTimer()
        self._draftTimer.setSingleShot(True)
        self._draftTimer.setInterval(DRAFT_DELAY_MS)
        self._draftTimer.connect("timeout()", self.writeDraft)
        study = _studySession()
        if study is not None:
            self.sid = study.current()
            self.recordId = self._recordIdOf(study, self.sid)

    # Panels

    def panel(self):
        return self.panels[self.current] if self.current is not None and 0 <= self.current < len(self.panels) else None

    def templateNode(self):
        node = _node(self.templateId)
        if node is not None:
            return node
        editor = _segmentEditor()
        node = editor.sourceVolumeNode() if editor is not None else None
        if node is None:
            node = _importer()._backgroundVolume()
        if node is not None:
            self.templateId = node.GetID()
        return node

    def _nextIndex(self):
        return max([panel.index for panel in self.panels] + [0]) + 1

    def addPanel(self, figureNode, select=True):
        """Make ``figureNode`` (a picture volume) a new panel: its transform and landmark lists are created."""
        panel = Panel(self, self._nextIndex(), figureNode)
        name = figureNode.GetName()
        scene = slicer.mrmlScene
        transform = scene.AddNewNodeByClass("vtkMRMLLinearTransformNode", scene.GenerateUniqueName(f"{name} placement"))
        _tag(transform, panel.index)
        panel.transformId = transform.GetID()
        suffix = "" if panel.index == 1 else f"_{panel.index}"
        for kind, baseName in (("figure", FIGURE_LANDMARKS_NAME), ("template", TEMPLATE_LANDMARKS_NAME)):
            markups = scene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", baseName + suffix)
            _tag(markups, panel.index)
            markups.CreateDefaultDisplayNodes()
            display = markups.GetDisplayNode()
            wasModifying = display.StartModify()
            display.SetSelectedColor(*LANDMARK_COLORS[kind])
            display.SetColor(*LANDMARK_COLORS[kind])
            display.SetGlyphScale(2.0)
            display.SetTextScale(2.5)
            display.EndModify(wasModifying)
            if kind == "figure":
                markups.SetAndObserveTransformNodeID(transform.GetID())
                panel.figureLandmarksId = markups.GetID()
            else:
                panel.templateLandmarksId = markups.GetID()
        # The convention confirmed for this record (or this session) is not asked again
        confirmed = self.confirmedConvention()
        if confirmed is not None and not panel.confirmed:
            figureNode.SetAttribute(space.FIGURE_CONVENTION_ATTRIBUTE, confirmed)
        template = self.templateNode()
        panel.z = self._initialZ(panel.plane, template)
        self.panels.append(panel)
        self._observePanel(panel)
        if select:
            self.selectPanel(len(self.panels) - 1)
        self.changed()
        return panel

    def _initialZ(self, plane, template):
        """The plane coordinate of Red's current slice, inside the template."""
        axis = {"axial": 2, "coronal": 1, "sagittal": 0}[plane]
        sliceNode = _sliceNode(TRACE_VIEW)
        z = float(_matrix(sliceNode.GetSliceToRAS())[axis, 3]) if sliceNode is not None else 0.0
        low, high = self.zRange(plane, template)
        return float(min(max(z, low), high))

    def zRange(self, plane=None, template=None):
        panel = self.panel()
        plane = plane or (panel.plane if panel is not None else "axial")
        template = template or self.templateNode()
        axis = {"axial": 2, "coronal": 1, "sagittal": 0}[plane]
        if template is None:
            return -100.0, 100.0
        bounds = [0.0] * 6
        template.GetRASBounds(bounds)
        return float(bounds[2 * axis]), float(bounds[2 * axis + 1])

    def selectPanel(self, index):
        if not 0 <= index < len(self.panels):
            return
        self.current = index
        panel = self.panel()
        if self.tracing:
            panel.place(attach=True)
            self._showPanelInViews(panel, fit=True)
        self.changed()

    def removePanel(self, index=None):
        index = self.current if index is None else index
        if index is None or not 0 <= index < len(self.panels):
            return
        panel = self.panels.pop(index)
        self._unobservePanel(panel)
        for node in panel.nodes():
            if slicer.mrmlScene.IsNodePresent(node):
                slicer.mrmlScene.RemoveNode(node)
        if not self.panels:
            self.current = None
            if self.tracing:
                self.leaveTrace()
        else:
            self.selectPanel(min(index, len(self.panels) - 1))
        self.changed()

    # Convention

    def confirmedConvention(self):
        """The convention confirmed for this source record (saved fit files) or, without a study, in this session."""
        for panel in self.panels:
            if panel.confirmed:
                return panel.convention
        folder = self.sourcesFolder()
        if folder and os.path.isdir(folder):
            for name in sorted(os.listdir(folder)):
                if not name.endswith("_fit.json"):
                    continue
                try:
                    with open(os.path.join(folder, name), encoding="utf-8") as f:
                        record = json.load(f)
                except (OSError, ValueError):
                    continue
                if record.get("convention_confirmed") and record.get("convention") in ("radiological", "neurological"):
                    return record["convention"]
        return None

    def setConvention(self, convention, panel=None):
        """Confirm the convention ("radiological": the patient's right is on the left of the figure): every panel of
        this record takes it (it is confirmed once per paper), and the fits are recomputed.
        """
        if convention not in ("radiological", "neurological", "unknown"):
            raise ValueError(f"Unknown figure convention: {convention}")
        targets = self.panels if convention != "unknown" else [panel or self.panel()]
        for target in targets:
            if target is not None and target.figure() is not None:
                target.figure().SetAttribute(space.FIGURE_CONVENTION_ATTRIBUTE, convention)
        if convention != "unknown":
            self.requiredConvention = False
        # U-07's question (Compare) is answered too
        importer = _importer()
        if importer.noticeWidget("figure-convention") is not None and convention != "unknown":
            controller = importer.figureController()
            if controller is not None and any(controller.figureId == target.figureId for target in self.panels):
                importer.answerFigureConvention(convention, controller.figureId)
        for target in self.panels:
            target.refit()
            target.place()
        self.changed()

    def unconfirmedPanels(self):
        return [panel for panel in self.panels if not panel.confirmed]

    # Study

    @staticmethod
    def _recordIdOf(study, sid):
        if sid is None:
            return None
        try:
            value = study.study.value(sid, "record_id")
        except Exception:
            value = None
        return value or None

    def studySession(self):
        study = _studySession()
        if study is None or self.sid is None or study.current() != self.sid:
            return None
        return study

    def sourcesFolder(self):
        study = self.studySession()
        folderId = self.recordId or self.sid
        if study is None or not folderId:
            return None
        return os.path.join(study.study.path, "sources", str(folderId))

    def _markTraceMethod(self):
        """``trace_method`` gets the "figure" token when a fitted figure is saved for the study subject (Save or Done), not
        while it is only being tried: a figure closed without saving leaves the row as it was.
        """
        _addFigureToken(self.studySession(), self.sid)

    # Observation

    def _observePanel(self, panel):
        for node in (panel.figureLandmarks(), panel.templateLandmarks()):
            if node is None:
                continue
            for event in (node.PointModifiedEvent, node.PointAddedEvent, node.PointRemovedEvent, node.PointPositionDefinedEvent):
                tag = node.AddObserver(event, self._onLandmarksModified)
                self._observations.append((panel, node, tag))
            tag = node.AddObserver(node.PointPositionDefinedEvent, lambda caller, event, panel=panel: self._onPointDefined(panel, caller))
            self._observations.append((panel, node, tag))

    def _unobservePanel(self, panel):
        kept = []
        for owner, node, tag in self._observations:
            if owner is panel:
                with contextlib.suppress(Exception):
                    node.RemoveObserver(tag)
            else:
                kept.append((owner, node, tag))
        self._observations = kept

    def _onLandmarksModified(self, caller=None, event=None):
        if not self._updating:
            self._refitTimer.start()

    def _onRefitTimeout(self):
        panel = self.panel()
        if panel is None:
            return
        self.updateFit(panel)

    def updateFit(self, panel=None):
        panel = panel or self.panel()
        if panel is None:
            return None
        self._updating = True
        try:
            fit = panel.refit()
            panel.place()
        finally:
            self._updating = False
        self.changed()
        self.scheduleDraft()
        return fit

    # Landmark placement (one pair: the figure point in Green, then the template point in Red)

    def addLandmarkPair(self):
        """Place the next pair: first on the figure (Green), then on the template (Red)."""
        panel = self.panel()
        if panel is None:
            return
        if not self.tracing:
            self.enterTrace()
        self._startPlacing(panel, "figure")

    def _nextName(self, panel):
        names = _figurefit().suggested_landmarks(panel.plane)
        count = panel.figureLandmarks().GetNumberOfControlPoints()
        return names[count] if count < len(names) else _("Landmark {n}").format(n=count + 1)

    def _startPlacing(self, panel, kind):
        markups = panel.figureLandmarks() if kind == "figure" else panel.templateLandmarks()
        selectionNode = slicer.app.applicationLogic().GetSelectionNode()
        interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        if markups is None or selectionNode is None or interactionNode is None:
            return
        if kind == "figure":
            # A figure point left without its template point (Esc) would pair with the next template point
            templateCount = panel.templateLandmarks().GetNumberOfControlPoints()
            while markups.GetNumberOfControlPoints() > templateCount:
                markups.RemoveNthControlPoint(markups.GetNumberOfControlPoints() - 1)
        if self._interactionObservation is None:
            tag = interactionNode.AddObserver(interactionNode.InteractionModeChangedEvent, self._onInteractionModeChanged)
            self._interactionObservation = (interactionNode, tag)
        self._placing = (panel, kind, self._nextName(panel) if kind == "figure" else panel.figureLandmarks().GetNthControlPointLabel(markups.GetNumberOfControlPoints()))
        selectionNode.SetReferenceActivePlaceNodeClassName("vtkMRMLMarkupsFiducialNode")
        selectionNode.SetActivePlaceNodeID(markups.GetID())
        interactionNode.SetPlaceModePersistence(0)
        interactionNode.SetCurrentInteractionMode(interactionNode.Place)
        view = _("the figure (Green view)") if kind == "figure" else _("the template (Red view)")
        _toast(self.shell, _("Click “{name}” on {view}").format(name=self._placing[2], view=view), ms=4000)
        self.changed()

    def _onPointDefined(self, panel, markups):
        placing = self._placing
        if placing is None or placing[0] is not panel:
            return
        index = markups.GetNumberOfControlPoints() - 1
        if index < 0:
            return
        kind, name = placing[1], placing[2]
        if kind == "figure" and markups.GetID() == panel.figureLandmarksId:
            local = [0.0, 0.0, 0.0]
            markups.GetNthControlPointPosition(index, local)
            u, v, k = panel.pixelOf(local)
            width, height = panel.size()
            if abs(k) > 0.5 or not (-0.5 <= u <= width - 0.5 and -0.5 <= v <= height - 0.5):
                markups.RemoveNthControlPoint(index)
                self._placing = None
                _toast(self.shell, _("Click the landmark on the figure, in the Green view"), ms=4000, level="warning")
                self.changed()
                return
            markups.SetNthControlPointLabel(index, name)
            # The template point is placed once the markups widget has left the Place mode of the figure point
            self._pendingTemplate = panel
            qt.QTimer.singleShot(100, self._startPendingTemplate)
        elif kind == "template" and markups.GetID() == panel.templateLandmarksId:
            # A template landmark lies on the cut plane: one clicked in another view is projected on it
            world = [0.0, 0.0, 0.0]
            markups.GetNthControlPointPositionWorld(index, world)
            cut = panel.cut()
            plane, distance = cut.to_plane([world])
            if abs(float(distance[0])) > 1e-3:
                projected = cut.to_ras(plane)[0]
                markups.SetNthControlPointPositionWorld(index, *projected)
            markups.SetNthControlPointLabel(index, name or _("Landmark {n}").format(n=index + 1))
            self._placing = None
            self.updateFit(panel)
            self.scheduleDraft()

    def _onInteractionModeChanged(self, caller=None, event=None):
        """Esc (or another mode) ends the placement of a pair: the card stops saying "placing…"."""
        interactionNode = self._interactionObservation[0] if self._interactionObservation else None
        if interactionNode is None or interactionNode.GetCurrentInteractionMode() == interactionNode.Place:
            return
        if self._pendingTemplate is not None:
            qt.QTimer.singleShot(0, self._startPendingTemplate)
        elif self._placing is not None:
            qt.QTimer.singleShot(50, self._checkPlacingEnded)

    def _startPendingTemplate(self):
        panel, self._pendingTemplate = self._pendingTemplate, None
        if panel is not None and panel in self.panels:
            self._startPlacing(panel, "template")

    def _checkPlacingEnded(self):
        interactionNode = self._interactionObservation[0] if self._interactionObservation else None
        if self._pendingTemplate is None and self._placing is not None and interactionNode is not None and interactionNode.GetCurrentInteractionMode() != interactionNode.Place:
            self._placing = None
            self.changed()

    def removeLastPair(self):
        panel = self.panel()
        if panel is None:
            return
        self._placing = None
        self._pendingTemplate = None
        for markups in (panel.figureLandmarks(), panel.templateLandmarks()):
            if markups is not None and markups.GetNumberOfControlPoints():
                markups.RemoveNthControlPoint(markups.GetNumberOfControlPoints() - 1)
        figureList, templateList = panel.figureLandmarks(), panel.templateLandmarks()
        # A figure point without its template point goes too
        while figureList.GetNumberOfControlPoints() > templateList.GetNumberOfControlPoints():
            figureList.RemoveNthControlPoint(figureList.GetNumberOfControlPoints() - 1)
        self.updateFit(panel)

    # Plane

    def setPlane(self, plane):
        panel = self.panel()
        if panel is None or plane not in PLANE_TITLES or plane == panel.plane:
            return
        panel.plane = plane
        panel.z = self._initialZ(plane, self.templateNode())
        panel.pitch = panel.roll = 0.0
        self._planeChanged(panel)

    def setCut(self, z=None, pitch=None, roll=None, fromView=False):
        panel = self.panel()
        if panel is None:
            return
        maxTilt = _figurefit().MAX_TILT_DEG
        if z is not None:
            low, high = self.zRange()
            panel.z = float(min(max(float(z), low), high))
        if pitch is not None:
            panel.pitch = float(min(max(float(pitch), -maxTilt), maxTilt))
        if roll is not None:
            panel.roll = float(min(max(float(roll), -maxTilt), maxTilt))
        self._planeChanged(panel, fromView=fromView)

    def _planeChanged(self, panel, fromView=False):
        self._updating = True
        try:
            panel.refit()
            panel.place()
            if self.tracing and panel is self.panel():
                self._applyPlane(panel, skipTrace=fromView)
        finally:
            self._updating = False
        self.changed()
        self.scheduleDraft()

    def setModel(self, model):
        panel = self.panel()
        if panel is None or model not in _figurefit().MODELS:
            return
        panel.model = model
        self.updateFit(panel)

    # Trace views

    def _saveViews(self):
        layoutManager = slicer.app.layoutManager()
        saved = {"layout": layoutManager.layout if layoutManager is not None else None, "views": {}}
        for viewName in (TRACE_VIEW, FIGURE_VIEW, ORTHO_VIEW):
            sliceNode, compositeNode = _sliceNode(viewName), _compositeNode(viewName)
            widget = _sliceWidget(viewName)
            if sliceNode is None or compositeNode is None:
                continue
            sliceToRas = vtk.vtkMatrix4x4()
            sliceToRas.DeepCopy(sliceNode.GetSliceToRAS())
            displayNode = widget.sliceLogic().GetSliceDisplayNode() if widget is not None else None
            saved["views"][viewName] = {
                "sliceToRAS": sliceToRas, "orientation": sliceNode.GetOrientation(), "fieldOfView": list(sliceNode.GetFieldOfView()),
                "xyzOrigin": list(sliceNode.GetXYZOrigin()), "viewGroup": sliceNode.GetViewGroup(), "linked": compositeNode.GetLinkedControl(),
                "background": compositeNode.GetBackgroundVolumeID(), "foreground": compositeNode.GetForegroundVolumeID(),
                "label": compositeNode.GetLabelVolumeID(), "foregroundOpacity": compositeNode.GetForegroundOpacity(),
                "intersections": displayNode.GetIntersectingSlicesVisibility() if displayNode is not None else None,
            }
        return saved

    def _restoreViews(self, saved):
        if not saved:
            return
        layoutManager = slicer.app.layoutManager()
        for viewName, state in saved["views"].items():
            sliceNode, compositeNode = _sliceNode(viewName), _compositeNode(viewName)
            widget = _sliceWidget(viewName)
            if sliceNode is None or compositeNode is None:
                continue
            for setter, nodeId in ((compositeNode.SetBackgroundVolumeID, state["background"]), (compositeNode.SetForegroundVolumeID, state["foreground"]),
                                   (compositeNode.SetLabelVolumeID, state["label"])):
                node = _node(nodeId)
                setter(nodeId if node is not None and not isFigureNode(node) else None)
            compositeNode.SetForegroundOpacity(state["foregroundOpacity"])
            sliceNode.SetViewGroup(state["viewGroup"])
            sliceNode.GetSliceToRAS().DeepCopy(state["sliceToRAS"])
            sliceNode.SetFieldOfView(*state["fieldOfView"])
            sliceNode.SetXYZOrigin(*state["xyzOrigin"])
            sliceNode.UpdateMatrices()
            compositeNode.SetLinkedControl(state["linked"])
            displayNode = widget.sliceLogic().GetSliceDisplayNode() if widget is not None else None
            if displayNode is not None and state["intersections"] is not None:
                displayNode.SetIntersectingSlicesVisibility(state["intersections"])
        if layoutManager is not None and saved["layout"] is not None and layoutManager.layout != saved["layout"]:
            layoutManager.setLayout(saved["layout"])

    def enterTrace(self):
        """Show the trace views (layout 7001): template on the cut plane with the figure over it (Red), the figure alone
        (Green), the orthogonal plane with the cut line (Yellow). The view layers are restored by ``leaveTrace()``.
        """
        panel = self.panel()
        if panel is None or self.tracing:
            return
        from . import layouts

        template = self.templateNode()
        if template is None:
            raise RuntimeError("Figure tracing needs the template in Draw")
        importer = _importer()
        controller = importer.figureController()
        if controller is not None:
            # U-07's Compare view gives the figure back; the convention is asked in the Figure card from now on
            controller.restore(figureRemoved=False)
        self.saved = self._saveViews()
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None and layoutManager.layout != layouts.FOCUS_LAYOUT_ID:
            layoutManager.setLayout(layouts.FOCUS_LAYOUT_ID)
            slicer.app.processEvents()
        self.tracing = True
        for viewName in (TRACE_VIEW, FIGURE_VIEW, ORTHO_VIEW):
            compositeNode = _compositeNode(viewName)
            if compositeNode is not None:
                compositeNode.SetLinkedControl(False)
        for candidate in self.panels:
            candidate.place(attach=True)
        self._showPanelInViews(panel, fit=True)
        self._observeViews()
        self._guard.install()
        _toast(self.shell, _("Draw on the template (left); the figure alone is on the right, the cut line below it"), level="consequence")
        self.changed()

    def _showPanelInViews(self, panel, fit=False):
        template = self.templateNode()
        red, green, yellow = (_compositeNode(name) for name in (TRACE_VIEW, FIGURE_VIEW, ORTHO_VIEW))
        greenSlice = _sliceNode(FIGURE_VIEW)
        if red is None or green is None or yellow is None:
            return
        self._updating = True
        try:
            red.SetBackgroundVolumeID(template.GetID() if template is not None else None)
            foreground = panel.edges() if self.showEdges and panel.edges() is not None else panel.figure()
            red.SetForegroundVolumeID(foreground.GetID())
            red.SetForegroundOpacity(self.opacity)
            green.SetBackgroundVolumeID(panel.figure().GetID())
            green.SetForegroundVolumeID(None)
            green.SetLabelVolumeID(None)
            greenSlice.SetViewGroup(FIGURE_VIEW_GROUP)
            yellow.SetBackgroundVolumeID(template.GetID() if template is not None else None)
            yellow.SetForegroundVolumeID(None)
            self._showLandmarksIn(panel)
            self._applyPlane(panel, fit=fit)
            # The cut plane's line (Red's intersection) is drawn in Yellow; Yellow's own line stays out of Red and Green
            for viewName, visible in ((TRACE_VIEW, True), (FIGURE_VIEW, False), (ORTHO_VIEW, False)):
                widget = _sliceWidget(viewName)
                displayNode = widget.sliceLogic().GetSliceDisplayNode() if widget is not None else None
                if displayNode is not None and bool(displayNode.GetIntersectingSlicesVisibility()) != visible:
                    displayNode.SetIntersectingSlicesVisibility(visible)
        finally:
            self._updating = False

    def _showLandmarksIn(self, current):
        red, green = _sliceNode(TRACE_VIEW), _sliceNode(FIGURE_VIEW)
        for panel in self.panels:
            for markups, view in ((panel.figureLandmarks(), green), (panel.templateLandmarks(), red)):
                display = markups.GetDisplayNode() if markups is not None else None
                if display is None:
                    continue
                display.SetViewNodeIDs([view.GetID()] if view is not None else [])
                display.SetVisibility(panel is current)

    @staticmethod
    def _setPlane(sliceNode, cut, center=None):
        """Put ``sliceNode`` on the cut plane, keeping its in-plane centre (projected on the plane)."""
        if center is None:
            center = _matrix(sliceNode.GetSliceToRAS())[:3, 3]
        normal = np.asarray(cut.normal, dtype=float)
        position = np.asarray(center, dtype=float) - normal * float(np.dot(np.asarray(center, dtype=float) - cut.origin, normal))
        sliceNode.SetSliceToRASByNTP(*normal, *np.asarray(cut.transverse, dtype=float), *position, cut.ORIENTATION)

    def _applyPlane(self, panel, fit=False, skipTrace=False):
        cut = panel.cut()
        redSlice, greenSlice, yellowSlice = (_sliceNode(name) for name in (TRACE_VIEW, FIGURE_VIEW, ORTHO_VIEW))
        if redSlice is None or greenSlice is None or yellowSlice is None:
            return
        if not skipTrace:
            wasModifying = redSlice.StartModify()
            template = self.templateNode()
            if fit and template is not None:
                bounds = [0.0] * 6
                template.GetRASBounds(bounds)
                extent = max(bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4])
                self._fitView(TRACE_VIEW, extent, extent)
            self._setPlane(redSlice, cut, cut.origin if fit else None)
            redSlice.EndModify(wasModifying)
        figureCenter = panel.ijkToRas() @ np.array([(panel.size()[0] - 1) / 2.0, (panel.size()[1] - 1) / 2.0, 0.0, 1.0])
        wasModifying = greenSlice.StartModify()
        self._setPlane(greenSlice, cut, figureCenter[:3] if fit else None)
        if fit:
            self._fitFigureView(panel)
        greenSlice.EndModify(wasModifying)
        orientation = ORTHO_ORIENTATION[panel.plane]
        reoriented = yellowSlice.GetOrientation() != orientation
        yellowSlice.SetOrientation(orientation)
        yellowSlice.JumpSliceByOffsetting(*cut.origin)
        if fit and reoriented:
            widget = _sliceWidget(ORTHO_VIEW)
            if widget is not None:
                widget.sliceLogic().FitSliceToAll()
                yellowSlice.JumpSliceByOffsetting(*cut.origin)

    def _fitFigureView(self, panel):
        """Green shows the whole figure: field of view from the placed figure's size on the plane."""
        width, height = panel.size()
        matrix = panel.ijkToRas()
        self._fitView(FIGURE_VIEW, float(np.linalg.norm(matrix[:3, 0])) * width, float(np.linalg.norm(matrix[:3, 1])) * height)

    @staticmethod
    def _fitView(viewName, extentX, extentY, margin=1.05):
        """A field of view that shows ``extentX`` by ``extentY`` mm whole in the view, centred on the slice origin."""
        sliceNode, widget = _sliceNode(viewName), _sliceWidget(viewName)
        if sliceNode is None or widget is None:
            return
        viewWidth, viewHeight = max(widget.sliceView().width, 1), max(widget.sliceView().height, 1)
        aspect = viewHeight / viewWidth
        fieldX = max(extentX, extentY / aspect) * margin
        sliceNode.SetFieldOfView(fieldX, fieldX * aspect, sliceNode.GetFieldOfView()[2])
        sliceNode.SetXYZOrigin(0, 0, 0)

    def _observeViews(self):
        self._unobserveViews()
        for viewName in (TRACE_VIEW, FIGURE_VIEW):
            for node in (_sliceNode(viewName), _compositeNode(viewName)):
                if node is not None:
                    tag = node.AddObserver(vtk.vtkCommand.ModifiedEvent, lambda caller, event: self._viewTimer.start())
                    self._viewObservations.append((node, tag))
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None:
            layoutManager.connect("layoutChanged(int)", self._onLayoutChanged)

    def _unobserveViews(self):
        for node, tag in self._viewObservations:
            with contextlib.suppress(Exception):
                node.RemoveObserver(tag)
        self._viewObservations = []
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None:
            with contextlib.suppress(Exception):
                layoutManager.disconnect("layoutChanged(int)", self._onLayoutChanged)

    def _onLayoutChanged(self, layoutId=None):
        if self.tracing:
            self._guard.install()
            self._viewTimer.start()

    def _checkViews(self):
        """Keep the figure in Green (the Segment Editor shows its source in every view on some changes) and follow the
        slice offset of Red: scrolling Red moves the cut plane (z) and the figure with it.
        """
        if not self.tracing or self._updating:
            return
        panel = self.panel()
        if panel is None or panel.figure() is None:
            return
        try:
            green = _compositeNode(FIGURE_VIEW)
            if green is not None and green.GetBackgroundVolumeID() != panel.figureId:
                self._updating = True
                try:
                    green.SetBackgroundVolumeID(panel.figureId)
                    green.SetForegroundVolumeID(None)
                    green.SetLinkedControl(False)
                finally:
                    self._updating = False
            redSlice = _sliceNode(TRACE_VIEW)
            if redSlice is None:
                return
            cut = panel.cut()
            sliceToRas = _matrix(redSlice.GetSliceToRAS())
            normal = sliceToRas[:3, 2] / max(np.linalg.norm(sliceToRas[:3, 2]), 1e-12)
            if abs(float(np.dot(normal, cut.normal))) < 0.9999:
                return  # Red was turned by the user: it is not on the cut plane any more
            offset = float(np.dot(sliceToRas[:3, 3], cut.normal))
            planeOffset = float(np.dot(cut.origin, cut.normal))
            if abs(offset - planeOffset) > 1e-3:
                axis = {"axial": 2, "coronal": 1, "sagittal": 0}[panel.plane]
                component = float(cut.normal[axis])
                if abs(component) > 1e-6:
                    self.setCut(z=offset / component, fromView=True)
        except Exception:
            logging.error(f"Studio: figure view check failed:\n{traceback.format_exc()}")

    def leaveTrace(self):
        """Restore the views as they were before the trace views; the figure nodes stay in the scene."""
        if not self.tracing:
            return
        self.tracing = False
        self._placing = None
        self._unobserveViews()
        self._guard.remove()
        self._viewTimer.stop()
        for panel in self.panels:
            for markups in (panel.figureLandmarks(), panel.templateLandmarks()):
                display = markups.GetDisplayNode() if markups is not None else None
                if display is not None:
                    display.SetVisibility(False)
        saved, self.saved = self.saved, None
        try:
            self._restoreViews(saved)
        except Exception:
            logging.error(f"Studio: the views could not be restored after figure tracing:\n{traceback.format_exc()}")
        self.changed()

    # Display

    def setOpacity(self, opacity):
        self.opacity = float(min(max(opacity, 0.0), 1.0))
        red = _compositeNode(TRACE_VIEW)
        if self.tracing and red is not None:
            red.SetForegroundOpacity(self.opacity)

    def setEdges(self, show):
        panel = self.panel()
        self.showEdges = bool(show)
        if panel is None:
            return
        if self.showEdges and panel.edges() is None:
            self._createEdges(panel)
        if self.tracing:
            red = _compositeNode(TRACE_VIEW)
            foreground = panel.edges() if self.showEdges and panel.edges() is not None else panel.figure()
            red.SetForegroundVolumeID(foreground.GetID())
        self.changed()

    def _createEdges(self, panel):
        """The edge image of the crop (Pillow FIND_EDGES) as a scalar volume under the figure's transform."""
        from PIL import Image, ImageFilter

        array = slicer.util.arrayFromVolume(panel.figure())
        pixels = array[0]
        image = Image.fromarray(pixels.astype(np.uint8) if pixels.ndim == 2 else pixels[..., :3].astype(np.uint8))
        edges = np.asarray(image.convert("L").filter(ImageFilter.FIND_EDGES), dtype=np.uint8)
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", slicer.mrmlScene.GenerateUniqueName(panel.figure().GetName() + " edges"))
        slicer.util.updateVolumeFromArray(node, edges[np.newaxis])
        node.SetIJKToRASMatrix(_vtkMatrix(_ijkToRas(panel.figure())))
        node.SetAttribute(space.ROLE_ATTRIBUTE, space.ROLE_FIGURE)
        _tag(node, panel.index)
        node.CreateDefaultDisplayNodes()
        node.GetDisplayNode().SetAutoWindowLevel(False)
        node.GetDisplayNode().SetWindowLevel(128, 64)
        node.SetAndObserveTransformNodeID(panel.transformId)
        panel.edgesId = node.GetID()

    # Crop

    def startCrop(self):
        """A markups ROI over the whole figure, in the view that shows it; ``applyCrop()`` keeps what it covers."""
        panel = self.panel()
        if panel is None:
            return None
        roi = _node(panel.roiId)
        if roi is None:
            roi = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsROINode", slicer.mrmlScene.GenerateUniqueName("Figure crop"))
            _tag(roi, panel.index)
            roi.CreateDefaultDisplayNodes()
            panel.roiId = roi.GetID()
        if panel.transformId and self.tracing:
            roi.SetAndObserveTransformNodeID(panel.transformId)
        else:
            roi.SetAndObserveTransformNodeID(None)
        width, height = panel.size()
        corner0, corner1 = panel.localOf((-0.5, -0.5)), panel.localOf((width - 0.5, height - 0.5))
        center = (corner0 + corner1) / 2.0
        roi.SetCenter(*center)
        roi.SetSize(abs(corner1[0] - corner0[0]), abs(corner1[1] - corner0[1]), 1.0)
        viewNames = [FIGURE_VIEW] if self.tracing else list(space.figureViewNames()) or [FIGURE_VIEW]
        viewIds = [_sliceNode(name).GetID() for name in viewNames if _sliceNode(name) is not None]
        roi.GetDisplayNode().SetViewNodeIDs(viewIds)
        roi.GetDisplayNode().SetVisibility(True)
        self.changed()
        return roi

    def cropBox(self, panel=None):
        """[x, y, width, height] in pixels of the crop ROI (clamped to the figure), or None."""
        panel = panel or self.panel()
        roi = _node(panel.roiId) if panel is not None else None
        if roi is None:
            return None
        center, size = [0.0, 0.0, 0.0], [float(value) for value in roi.GetSize()]
        roi.GetCenter(center)
        corners = []
        for dx in (-0.5, 0.5):
            for dy in (-0.5, 0.5):
                corners.append(panel.pixelOf((center[0] + dx * size[0], center[1] + dy * size[1], center[2]))[:2])
        corners = np.array(corners)
        width, height = panel.size()
        x0 = int(max(0, math.floor(corners[:, 0].min() + 0.5)))
        y0 = int(max(0, math.floor(corners[:, 1].min() + 0.5)))
        x1 = int(min(width, math.ceil(corners[:, 0].max() + 0.5)))
        y1 = int(min(height, math.ceil(corners[:, 1].max() + 0.5)))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return None
        return [x0, y0, x1 - x0, y1 - y0]

    def applyCrop(self, box=None):
        """Crop the figure to ``box`` ([x, y, width, height] pixels; default the ROI) with Pillow; only the crop is kept.

        The figure volume keeps its node (and its views); its file becomes the crop. Figure landmarks keep their pixels.
        """
        from PIL import Image

        panel = self.panel()
        if panel is None:
            return False
        box = box or self.cropBox(panel)
        roi = _node(panel.roiId)
        if roi is not None:
            slicer.mrmlScene.RemoveNode(roi)
            panel.roiId = None
        if box is None:
            self.changed()
            return False
        x, y, width, height = (int(value) for value in box)
        figureNode = panel.figure()
        array = slicer.util.arrayFromVolume(figureNode)
        pixels = array[0]
        image = Image.fromarray(np.ascontiguousarray(pixels))
        cropped = image.crop((x, y, x + width, y + height))
        path = _temporaryPng(f"figure-{panel.index}")
        cropped.save(path, format="PNG")
        croppedArray = np.asarray(cropped)
        self._updating = True
        try:
            slicer.util.updateVolumeFromArray(figureNode, croppedArray[np.newaxis].copy())
            storageNode = figureNode.GetStorageNode()
            if storageNode is not None:
                # A save never writes over the user's figure, and the node reads its crop again
                storageNode.SetFileName(path)
            # Figure landmarks keep the pixels they were placed on
            markups = panel.figureLandmarks()
            for index in range(markups.GetNumberOfControlPoints() if markups is not None else 0):
                local = [0.0, 0.0, 0.0]
                markups.GetNthControlPointPosition(index, local)
                u, v, _k = panel.pixelOf(local)
                markups.SetNthControlPointPosition(index, *panel.localOf((u - x, v - y)))
        finally:
            self._updating = False
        panel.cropPx = [panel.cropPx[0] + x, panel.cropPx[1] + y, width, height]
        panel.cropPath = path
        if panel.edgesId:
            slicer.mrmlScene.RemoveNode(panel.edges())
            panel.edgesId = None
            if self.showEdges:
                self._createEdges(panel)
        self.updateFit(panel)
        if self.tracing:
            self._showPanelInViews(panel, fit=True)
        else:
            controller = _importer().figureController()
            if controller is not None:
                controller._fit()
        panel.storeState()
        self.scheduleDraft()
        return True

    # Save

    def _panelFiles(self, folder, panel):
        """(png, json) of a panel in ``folder``: its saved number, else the next free fig-<k>."""
        if panel.savedIndex is None:
            used = set()
            if os.path.isdir(folder):
                for name in os.listdir(folder):
                    if name.startswith("fig-") and (name.endswith(".png") or name.endswith("_fit.json")):
                        stem = name[4:].split("_", 1)[0].split(".", 1)[0]
                        if stem.isdigit():
                            used.add(int(stem))
            taken = {other.savedIndex for other in self.panels if other.savedIndex is not None}
            k = 1
            while k in used or k in taken:
                k += 1
            panel.savedIndex = k
        stem = f"fig-{panel.savedIndex}"
        return os.path.join(folder, stem + ".png"), os.path.join(folder, stem + "_fit.json")

    def _templateRecord(self):
        template = self.templateNode()
        detected = space.spaceOf(template) if template is not None else None
        spacing = template.GetSpacing() if template is not None else (1.0, 1.0, 1.0)
        name = detected[0] if detected else (template.GetName() if template is not None else "template")
        return {"id": str(name), "res": float(min(spacing)), "sha256": None}

    def _record(self, panel, imageSha):
        figurefit = _figurefit()
        names, pixels, points = panel.pairs()
        cut = panel.cut()
        tracer = ""
        study = self.studySession()
        if study is not None:
            tracer = study.study.value(self.sid, "tracer") or ""
        record = figurefit.make_record(
            panel.fit, panel.plane, panel.fitConvention,
            [{"name": name, "fig_px": pixel, "tpl_ras": point} for name, pixel, point in zip(names, pixels, points, strict=True)],
            cut, image_sha256=imageSha, crop_px=panel.cropPx, template=self._templateRecord(), tracer=str(tracer),
            z_mm=panel.z, pitch_deg=panel.pitch, roll_deg=panel.roll)
        # Unknown keys are kept by the fit file format: the confirmation, the labels and the source's file name (no path)
        record.update({"convention_confirmed": panel.confirmed, "figure": panel.label, "panel": panel.panelLabel,
                       "source_name": panel.sourceName, "panel_index": panel.index})
        if self.sid:
            record["participant_id"] = self.sid
        return record

    def saveFigures(self, folder=None):
        """Write each fitted panel's crop (``fig-<k>.png``) and fit file (``fig-<k>_fit.json``), atomically.

        In a study the folder is ``sources/<record_id>/`` and the row gets the figure columns; otherwise ``folder`` is
        required. Return the written paths.
        """
        study = self.studySession()
        if folder is None:
            folder = self.sourcesFolder()
        if folder is None:
            raise ValueError("Saving a figure fit needs a study or a folder")
        os.makedirs(folder, exist_ok=True)
        figurefit = _figurefit()
        written = []
        for panel in self.panels:
            if panel.fit is None:
                panel.refit()
            if panel.fit is None or panel.figure() is None:
                continue
            pngPath, jsonPath = self._panelFiles(folder, panel)
            source = panel.cropPath
            if not source or not os.path.isfile(source):
                source = self._writeCrop(panel)
            _atomicCopy(source, pngPath)
            record = self._record(panel, figurefit.image_sha256(pngPath))
            figurefit.write_fit_json(jsonPath, record)
            panel.cropPath = pngPath
            storageNode = panel.figure().GetStorageNode()
            if storageNode is not None:
                storageNode.SetFileName(pngPath)
            panel.storeState()
            written.extend([pngPath, jsonPath])
        if study is not None:
            values = self.columns()
            if values:
                study.setValues([self.sid], values)
            if written:
                self._markTraceMethod()
            self.discardDraft()
        self.lastSaved = written
        self.unsaved = False
        self.changed()
        return written

    def _writeCrop(self, panel):
        from PIL import Image

        path = _temporaryPng(f"figure-{panel.index}")
        Image.fromarray(np.ascontiguousarray(slicer.util.arrayFromVolume(panel.figure())[0])).save(path, format="PNG")
        panel.cropPath = path
        return path

    def columns(self):
        """Study columns of the figure tracing (empty without panels).

        Only what the user stated is written: ``record_id`` is never made up (a case report without one stays uncitable
        for preflight), and ``figure`` / ``panel`` are the publication's labels, left out when none were entered.
        """
        if not self.panels:
            return {}
        values = {}
        labels = [panel.label for panel in self.panels if panel.label]
        if labels:
            values["figure"] = ";".join(dict.fromkeys(labels))
        panelLabels = [panel.panelLabel for panel in self.panels if panel.panelLabel]
        if panelLabels:
            values["panel"] = ";".join(panelLabels)
        conventions = {panel.convention for panel in self.panels}
        values["fig_convention"] = conventions.pop() if len(conventions) == 1 else "unknown"
        values["planes"] = ";".join(dict.fromkeys(panel.plane for panel in self.panels))
        residuals = [panel.fit.rms_mm for panel in self.panels if panel.fit is not None]
        if residuals:
            values["fit_rms_mm"] = round(max(residuals), 2)
        return values

    def planeNormals(self):
        return [[float(value) for value in panel.cut().normal] for panel in self.panels]

    # Drafts (study, for recovery)

    def draftFolder(self, sid=None):
        study = _studySession()
        sid = sid or self.sid
        if study is None or not sid:
            return None
        return os.path.join(study.study.path, ".studio", "figure", str(sid))

    def scheduleDraft(self):
        self.unsaved = True
        if self.studySession() is not None:
            self._draftTimer.start()

    def writeDraft(self):
        folder = self.draftFolder()
        if folder is None or not self.panels:
            return None
        try:
            os.makedirs(folder, exist_ok=True)
            states = []
            for panel in self.panels:
                pngPath = os.path.join(folder, f"panel-{panel.index}.png")
                source = panel.cropPath if panel.cropPath and os.path.isfile(panel.cropPath) else self._writeCrop(panel)
                if os.path.abspath(source) != os.path.abspath(pngPath):
                    _atomicCopy(source, pngPath)
                state = panel.state()
                state["png"] = os.path.basename(pngPath)
                states.append(state)
            _writeJsonAtomic(os.path.join(folder, "draft.json"), {"format": "slicer-studio-figure-draft", "version": 1, "participant_id": self.sid, "panels": states})
            return folder
        except Exception:
            logging.warning(f"Studio: the figure draft could not be written:\n{traceback.format_exc()}")
            return None

    def discardDraft(self):
        self._draftTimer.stop()
        folder = self.draftFolder()
        if folder and os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)

    # Rebuild

    def panelFromState(self, state, pngPath, select=True, place=True):
        """A panel rebuilt from a fit record or a draft state and its crop: same plane, convention, landmarks and fit."""
        importer = _importer()
        with importer._ownLoad():
            figureNode = slicer.util.loadVolume(pngPath, {"singleFile": True, "show": False})
        name = state.get("figure") or state.get("source_name") or os.path.splitext(os.path.basename(pngPath))[0]
        figureNode.SetName(slicer.mrmlScene.GenerateUniqueName(str(name)))
        convention = state.get("convention")
        confirmed = state.get("convention_confirmed", convention in ("radiological", "neurological"))
        figureNode.SetAttribute(space.FIGURE_CONVENTION_ATTRIBUTE, convention if confirmed and convention in ("radiological", "neurological") else "unknown")
        panel = self.addPanel(figureNode, select=False)
        panel.cropPath = pngPath
        panel.plane = state.get("plane", "axial")
        cut = state.get("cut_plane") or state
        panel.z = float(cut.get("z_mm", 0.0))
        panel.pitch = float(cut.get("pitch_deg", 0.0))
        panel.roll = float(cut.get("roll_deg", 0.0))
        panel.model = state.get("model", "similarity")
        if isinstance(state.get("crop_px"), list) and len(state["crop_px"]) == 4:
            panel.cropPx = [int(value) for value in state["crop_px"]]
        panel.label = str(state.get("figure") or "")
        panel.panelLabel = str(state.get("panel") or "")
        panel.sourceName = str(state.get("source_name") or "")
        self._updating = True
        try:
            figureList, templateList = panel.figureLandmarks(), panel.templateLandmarks()
            modifying = figureList.StartModify(), templateList.StartModify()
            for landmark in state.get("landmarks", []):
                figureIndex = figureList.AddControlPoint(vtk.vtkVector3d(*panel.localOf(landmark["fig_px"])))
                figureList.SetNthControlPointLabel(figureIndex, landmark.get("name", ""))
                templateIndex = templateList.AddControlPointWorld(vtk.vtkVector3d(*landmark["tpl_ras"]))
                templateList.SetNthControlPointLabel(templateIndex, landmark.get("name", ""))
            figureList.EndModify(modifying[0])
            templateList.EndModify(modifying[1])
            panel.refit()
            if place:
                panel.place()
        finally:
            self._updating = False
        panel.storeState()
        if select:
            self.selectPanel(self.panels.index(panel))
        return panel

    # Changes

    def changed(self):
        for panel in self.panels:
            panel.storeState()
        card = _card
        if card is not None:
            card.refresh()

    def dispose(self, removeNodes=False):
        self._refitTimer.stop()
        if self._interactionObservation is not None:
            with contextlib.suppress(Exception):
                self._interactionObservation[0].RemoveObserver(self._interactionObservation[1])
            self._interactionObservation = None
        self._draftTimer.stop()
        if self.tracing:
            self.leaveTrace()
        self._unobserveViews()
        self._guard.remove()
        for panel in list(self.panels):
            self._unobservePanel(panel)
            if removeNodes:
                for node in panel.nodes():
                    if slicer.mrmlScene.IsNodePresent(node):
                        slicer.mrmlScene.RemoveNode(node)
        self.panels = []
        self.current = None


_session = None


def session(create=False, shell=None):
    """The figure session (None when no figure is traced, unless ``create``)."""
    global _session
    if _session is None and create:
        _session = FigureSession(_currentShell(shell))
    return _session


def closeFigure(removeNodes=False):
    """Leave the trace views and forget the panels (``removeNodes``: remove the figure nodes from the scene too)."""
    global _session
    current, _session = _session, None
    if current is not None:
        current.dispose(removeNodes=removeNodes)
    if _card is not None:
        _card.refresh()


#
# Entry points
#


def openFigure(path, shell=None, result=None):
    """Open a figure (PNG, JPEG, TIFF, BMP) to trace from. Return its volume node, or None.

    On a template, the first figure is shown in the Compare layout (U-07) with the Figure card; while the trace views are
    shown, a figure becomes a new panel there. On a scan in scan space the figure stays a reference (Compare) only.
    """
    shell = _currentShell(shell)
    importer = _importer()
    if result is None:
        result = importer.LoadResult()
    current = session()
    if current is not None and current.tracing and current.panels:
        try:
            with importer._ownLoad(), slicer.util.WaitCursor():
                figureNode = slicer.util.loadVolume(path, {"singleFile": True, "show": False})
        except Exception:
            logging.error(f"Studio: cannot open the figure {path}:\n{traceback.format_exc()}")
            result.errors.append((path, _("Cannot load the figure")))
            return None
        panel = current.addPanel(figureNode)
        result.figures.append(figureNode)
        _toast(shell, _("{panel} added: crop it, then place its landmarks").format(panel=panel.title()), ms=3000)
        _ensureCard()
        return figureNode
    traceable = canTrace()
    if shell is None:
        return None
    if not importer._routeFigure(path, shell, result):
        return None
    figureNode = (result.figures or result.alreadyOpen or [None])[-1]
    if figureNode is None or not traceable:
        return figureNode
    current = session(create=True, shell=shell)
    existing = next((panel for panel in current.panels if panel.figureId == figureNode.GetID()), None)
    if existing is None:
        current.addPanel(figureNode)
    else:
        current.selectPanel(current.panels.index(existing))
    _ensureCard()
    return figureNode


def pasteFigure(shell=None):
    """Ctrl+V in Draw: the clipboard's image becomes a figure (saved as a PNG in the application's temporary folder)."""
    if not clipboardHasImage():
        return None
    image = qt.QApplication.clipboard().image()
    if image is None or image.isNull():
        return None
    path = _temporaryPng("Pasted figure")
    if not image.save(path, "PNG"):
        return None
    return openFigure(path, shell)


def openFigureDialog(shell=None):
    """"Open figure…": a file dialog (in ``/data`` in the web build). Return the figure node or None."""
    start = WEB_DATA_FOLDER if isWebBuild() and os.path.isdir(WEB_DATA_FOLDER) else str(slicer.util.settingsValue(LAST_FOLDER_KEY, "") or "")
    path = qt.QFileDialog.getOpenFileName(slicer.util.mainWindow(), _("Open figure"), start, _(FIGURE_FILTER))
    if not path:
        return None
    qt.QSettings().setValue(LAST_FOLDER_KEY, os.path.dirname(path))
    return openFigure(path, shell)


def traceFromFigure(shell=None):
    """"Trace from figure…" (Lesion card ⋯, Search): Draw on the template, then "Open figure…"."""
    shell = _currentShell(shell)
    if shell is not None and shell.currentWorkspace() != "draw":
        shell.switchTo("draw")
    if shell is None:
        return showClassicCard()
    _ensureCard()
    return openFigureDialog(shell)


def showSource(jsonPath, shell=None):
    """Rebuild a figure overlay from its fit file (and the crop next to it) in the trace views (≤ 300 ms).

    Return ``(panel, milliseconds)``.
    """
    start = time.perf_counter()
    record = _figurefit().read_fit_json(jsonPath)
    pngPath = jsonPath[:-len("_fit.json")] + ".png" if jsonPath.endswith("_fit.json") else os.path.splitext(jsonPath)[0] + ".png"
    if not os.path.isfile(pngPath):
        raise FileNotFoundError(pngPath)
    if _figurefit().image_sha256(pngPath) != record["image_sha256"]:
        _toast(shell, _("The crop of {name} changed since its fit was saved").format(name=os.path.basename(pngPath)), level="warning")
    current = session(create=True, shell=shell)
    with _renderPaused():
        panel = current.panelFromState(dict(record), pngPath, select=False, place=current.tracing)
        panel.savedIndex = None
        stem = os.path.basename(pngPath)
        if stem.startswith("fig-") and stem[4:-4].isdigit():
            panel.savedIndex = int(stem[4:-4])
        if current.tracing:
            current.selectPanel(current.panels.index(panel))
        else:
            current.current = current.panels.index(panel)
            current.enterTrace()
        _ensureCard()
    elapsed = (time.perf_counter() - start) * 1000.0
    return panel, elapsed


def restoreDraft(sid=None, shell=None):
    """Rebuild the panels of a figure draft of the open study subject. Return the number of panels."""
    study = _studySession()
    if study is None:
        return 0
    sid = sid or study.current()
    folder = os.path.join(study.study.path, ".studio", "figure", str(sid))
    path = os.path.join(folder, "draft.json")
    if not os.path.isfile(path):
        return 0
    with open(path, encoding="utf-8") as f:
        draft = json.load(f)
    current = session(create=True, shell=shell)
    count = 0
    for state in draft.get("panels", []):
        pngPath = os.path.join(folder, os.path.basename(str(state.get("png", ""))))
        if os.path.isfile(pngPath):
            current.panelFromState(state, pngPath, select=False)
            count += 1
    if count:
        current.selectPanel(0)
        _ensureCard()
    return count


#
# Figure card
#


class FigureCard:
    """The Figure card (``StudioFigureCard``): panel, crop, plane, convention, landmarks, fit, cut plane and display."""

    def __init__(self):
        import ctk

        from . import ui

        self.widget = qt.QFrame()
        self.widget.setObjectName("StudioFigureCard")
        ui.setRole(self.widget, "card")
        self.widget.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Maximum)
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        header = qt.QHBoxLayout()
        header.setSpacing(6)
        title = qt.QLabel(_("Figure"))
        title.setObjectName("StudioFigureTitle")
        title.setProperty("slicerRole", "sectionHeader")
        header.addWidget(title)
        self.panelCombo = qt.QComboBox()
        self.panelCombo.setObjectName("StudioFigurePanelCombo")
        self.panelCombo.setToolTip(_("Panel of the figure: each has its own plane and fit, all paint the same lesion"))
        self.panelCombo.connect("activated(int)", self._onPanelActivated)
        header.addWidget(self.panelCombo, 1)
        self.moreButton = qt.QToolButton()
        self.moreButton.setObjectName("StudioFigureMore")
        self.moreButton.setText("⋯")
        self.moreButton.setAutoRaise(True)
        self.moreButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.moreButton.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        self.moreButton.setToolTip(_("More figure actions"))
        self.menu = qt.QMenu(self.moreButton)
        self.menu.setObjectName("StudioFigureMenu")
        self.menu.addAction(_("Open figure…")).connect("triggered()", openFigureDialog)
        self.addPanelAction = self.menu.addAction(_("Add panel…"))
        self.addPanelAction.connect("triggered()", self._addPanel)
        self.menu.addAction(_("Show source…")).connect("triggered()", self._showSourceDialog)
        self.removePanelAction = self.menu.addAction(_("Remove this panel"))
        self.removePanelAction.connect("triggered()", lambda: session().removePanel() if session() is not None else None)
        self.menu.addSeparator()
        self.closeAction = self.menu.addAction(_("Close figure"))
        self.closeAction.connect("triggered()", lambda: closeFigure(removeNodes=False))
        self.moreButton.setMenu(self.menu)
        header.addWidget(self.moreButton)
        layout.addLayout(header)

        # Empty state: the entry points
        self.emptyFrame = qt.QWidget()
        emptyLayout = qt.QVBoxLayout(self.emptyFrame)
        emptyLayout.setContentsMargins(0, 0, 0, 0)
        emptyLayout.setSpacing(4)
        self.emptyLabel = qt.QLabel()
        self.emptyLabel.setObjectName("StudioFigureEmptyText")
        self.emptyLabel.setWordWrap(True)
        self.emptyLabel.setProperty("slicerRole", "secondaryText")
        self._wrapped(self.emptyLabel)
        emptyLayout.addWidget(self.emptyLabel)
        self.openButton = qt.QPushButton(_("Open figure…"))
        self.openButton.setObjectName("StudioFigureOpenButton")
        ui.setRole(self.openButton, "secondary")
        self.openButton.connect("clicked()", openFigureDialog)
        emptyLayout.addWidget(self.openButton)
        layout.addWidget(self.emptyFrame)

        # Steps
        self.stepsFrame = qt.QWidget()
        steps = qt.QFormLayout(self.stepsFrame)
        steps.setContentsMargins(0, 0, 0, 0)
        steps.setHorizontalSpacing(8)
        steps.setVerticalSpacing(6)
        steps.setFieldGrowthPolicy(qt.QFormLayout.AllNonFixedFieldsGrow)

        labels = qt.QHBoxLayout()
        self.figureLabelEdit = qt.QLineEdit()
        self.figureLabelEdit.setObjectName("StudioFigureLabelEdit")
        self.figureLabelEdit.setPlaceholderText(_("Fig. 2"))
        self.figureLabelEdit.setToolTip(_("Figure of the publication (the 'figure' column)"))
        self.figureLabelEdit.connect("editingFinished()", self._onLabelsEdited)
        labels.addWidget(self.figureLabelEdit, 2)
        self.panelLabelEdit = qt.QLineEdit()
        self.panelLabelEdit.setObjectName("StudioFigurePanelLabelEdit")
        self.panelLabelEdit.setPlaceholderText(_("B"))
        self.panelLabelEdit.setToolTip(_("Panel of the figure (the 'panel' column)"))
        self.panelLabelEdit.connect("editingFinished()", self._onLabelsEdited)
        labels.addWidget(self.panelLabelEdit, 1)
        steps.addRow(_("Source"), labels)

        cropRow = qt.QHBoxLayout()
        self.cropButton = qt.QPushButton(_("Crop…"))
        self.cropButton.setObjectName("StudioFigureCropButton")
        ui.setRole(self.cropButton, "ghost")
        self.cropButton.setToolTip(ui.tooltip(_("Crop"), None, _("drag the box on the figure, then Apply: only the crop is kept")))
        self.cropButton.connect("clicked()", self._onCrop)
        cropRow.addWidget(self.cropButton)
        self.cropLabel = qt.QLabel()
        self.cropLabel.setObjectName("StudioFigureCropLabel")
        self.cropLabel.setProperty("slicerRole", "secondaryText")
        cropRow.addWidget(self.cropLabel, 1)
        steps.addRow(_("1 Crop"), cropRow)

        self.planeFrame, self.planeGroup = ui.segmented([PLANE_TITLES[plane] for plane in _PLANES])
        self.planeFrame.setObjectName("StudioFigurePlane")
        _connectIdClicked(self.planeGroup, lambda index: session().setPlane(_PLANES[index]) if session() is not None else None)
        steps.addRow(_("2 Plane"), self.planeFrame)

        self.conventionFrame, self.conventionGroup = ui.segmented([_("Left"), _("Right")])
        self.conventionFrame.setObjectName("StudioFigureConvention")
        self.conventionGroup.buttons()[0].setToolTip(_("Radiological: the patient's right is on the left of the figure (most clinical figures)"))
        self.conventionGroup.buttons()[1].setToolTip(_("Neurological: the patient's right is on the right of the figure"))
        _connectIdClicked(self.conventionGroup, self._onConvention)
        self.conventionText = qt.QLabel(_("Patient's right is on the … of the figure"))
        self.conventionText.setObjectName("StudioFigureConventionText")
        self.conventionText.setWordWrap(True)
        steps.addRow(self._wrapped(self.conventionText))
        steps.addRow(_("3 Side"), self.conventionFrame)
        self.conventionState = qt.QLabel()
        self.conventionState.setObjectName("StudioFigureConventionState")
        self.conventionState.setWordWrap(True)
        steps.addRow(self._wrapped(self.conventionState))

        landmarkRow = qt.QHBoxLayout()
        self.addPairButton = qt.QPushButton(_("Add pair"))
        self.addPairButton.setObjectName("StudioFigureAddPairButton")
        ui.setRole(self.addPairButton, "secondary")
        self.addPairButton.setToolTip(ui.tooltip(_("Add a landmark pair"), None, _("click it on the figure (Green), then on the template (Red)")))
        self.addPairButton.connect("clicked()", lambda: session().addLandmarkPair() if session() is not None else None)
        landmarkRow.addWidget(self.addPairButton)
        self.removePairButton = qt.QToolButton()
        self.removePairButton.setObjectName("StudioFigureRemovePairButton")
        self.removePairButton.setText(_("Remove last"))
        self.removePairButton.setAutoRaise(True)
        self.removePairButton.connect("clicked()", lambda: session().removeLastPair() if session() is not None else None)
        landmarkRow.addWidget(self.removePairButton)
        landmarkRow.addStretch(1)
        self.pairLabel = qt.QLabel()
        self.pairLabel.setObjectName("StudioFigurePairLabel")
        self.pairLabel.setProperty("slicerRole", "secondaryText")
        landmarkRow.addWidget(self.pairLabel)
        steps.addRow(_("4 Landmarks"), landmarkRow)

        self.modelFrame, self.modelGroup = ui.segmented([_("Similarity"), _("Affine")])
        self.modelFrame.setObjectName("StudioFigureModel")
        self.modelGroup.buttons()[1].setToolTip(_("Affine fits need at least 4 landmark pairs"))
        _connectIdClicked(self.modelGroup, lambda index: session().setModel(("similarity", "affine")[index]) if session() is not None else None)
        steps.addRow(_("5 Fit"), self.modelFrame)
        self.residualChip = ui.chip("")
        self.residualChip.setObjectName("StudioFigureResidualChip")
        self.residualChip.setSizePolicy(qt.QSizePolicy.Maximum, qt.QSizePolicy.Fixed)
        steps.addRow(self.residualChip)
        self.residualNote = qt.QLabel(_("The residual measures landmark consistency, not lesion accuracy"))
        self.residualNote.setObjectName("StudioFigureResidualNote")
        self.residualNote.setWordWrap(True)
        self.residualNote.setProperty("slicerRole", "secondaryText")
        steps.addRow(self._wrapped(self.residualNote))
        self.fitWarning = qt.QLabel()
        self.fitWarning.setObjectName("StudioFigureFitWarning")
        self.fitWarning.setWordWrap(True)
        steps.addRow(self._wrapped(self.fitWarning))

        self.zSlider = self._slider(ctk, "StudioFigureZ", Z_STEP_MM, 1, _(" mm"), lambda value: session().setCut(z=value) if session() is not None else None)
        steps.addRow(_("Slice z"), self.zSlider)
        self.pitchSlider = self._slider(ctk, "StudioFigurePitch", TILT_STEP_DEG, 1, "°", lambda value: session().setCut(pitch=value) if session() is not None else None)
        steps.addRow(_("Pitch"), self.pitchSlider)
        self.rollSlider = self._slider(ctk, "StudioFigureRoll", TILT_STEP_DEG, 1, "°", lambda value: session().setCut(roll=value) if session() is not None else None)
        steps.addRow(_("Roll"), self.rollSlider)
        maxTilt = _figurefit().MAX_TILT_DEG
        for slider in (self.pitchSlider, self.rollSlider):
            slider.minimum = -maxTilt
            slider.maximum = maxTilt

        displayRow = qt.QHBoxLayout()
        self.opacitySlider = qt.QSlider(qt.Qt.Horizontal)
        self.opacitySlider.setObjectName("StudioFigureOpacity")
        self.opacitySlider.setRange(0, 100)
        self.opacitySlider.setValue(round(DEFAULT_OPACITY * 100))
        self.opacitySlider.setToolTip(ui.tooltip(_("Figure opacity over the template"), "T", _("T flickers the figure")))
        self.opacitySlider.connect("valueChanged(int)", lambda value: session().setOpacity(value / 100.0) if session() is not None else None)
        displayRow.addWidget(self.opacitySlider, 1)
        self.edgesButton = qt.QToolButton()
        self.edgesButton.setObjectName("StudioFigureEdges")
        self.edgesButton.setText(_("Edges"))
        self.edgesButton.setCheckable(True)
        self.edgesButton.setToolTip(_("Show the figure's edges over the template instead of the figure"))
        self.edgesButton.connect("toggled(bool)", lambda checked: session().setEdges(checked) if session() is not None else None)
        displayRow.addWidget(self.edgesButton)
        steps.addRow(_("Display"), displayRow)

        actions = qt.QHBoxLayout()
        self.traceButton = qt.QPushButton(_("Fit to template"))
        self.traceButton.setObjectName("StudioFigureTraceButton")
        ui.setRole(self.traceButton, "secondary")
        self.traceButton.setToolTip(ui.tooltip(_("Trace views"), None, _("template on the cut plane with the figure over it (Red), the figure (Green), the cut line (Yellow)")))
        self.traceButton.connect("clicked()", self._onTraceToggled)
        actions.addWidget(self.traceButton)
        self.saveButton = qt.QPushButton(_("Save fit"))
        self.saveButton.setObjectName("StudioFigureSaveButton")
        ui.setRole(self.saveButton, "ghost")
        self.saveButton.setToolTip(_("Save the crop and the fit file of each fitted panel (with a study: in sources/<record>/)"))
        self.saveButton.connect("clicked()", self._onSave)
        actions.addWidget(self.saveButton)
        actions.addStretch(1)
        steps.addRow(actions)
        layout.addWidget(self.stepsFrame)
        self._styleConvention()
        self.refresh()

    @staticmethod
    def _wrapped(label):
        """A word-wrapped label that takes the card's width and the height its text needs (Ignored width, like the other
        Draw cards: a long text never widens the inspector).
        """
        policy = qt.QSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        label.setSizePolicy(policy)
        return label

    @staticmethod
    def _slider(ctk, objectName, step, decimals, suffix, callback):
        slider = ctk.ctkSliderWidget()
        slider.setObjectName(objectName)
        slider.singleStep = step
        slider.pageStep = step * 10
        slider.decimals = decimals
        slider.suffix = suffix
        slider.tracking = True
        slider.connect("valueChanged(double)", callback)
        return slider

    def _styleConvention(self):
        from . import ui

        ui.bindStyleSheet(self.widget, lambda: (
            f"QLabel#StudioFigureConventionState[required=\"true\"] {{ color: {ui.token('warning').name()}; font-weight: 600; }}"
            f"QLabel#StudioFigureFitWarning {{ color: {ui.token('warning').name()}; }}"))

    def isAlive(self):
        try:
            self.widget.objectName  # noqa: B018
        except Exception:
            return False
        return True

    def refresh(self):
        if not self.isAlive():
            return
        from . import ui

        current = session()
        panel = current.panel() if current is not None else None
        hasPanel = panel is not None and panel.figure() is not None
        self.emptyFrame.setVisible(not hasPanel)
        self.stepsFrame.setVisible(hasPanel)
        self.panelCombo.setVisible(hasPanel)
        self.addPanelAction.setEnabled(hasPanel)
        self.removePanelAction.setEnabled(hasPanel)
        self.closeAction.setEnabled(current is not None)
        if not hasPanel:
            text = _("Trace a lesion from a published figure: drop a picture here, paste it ({key})").format(key=ui.keyText(PASTE_SHORTCUT))
            if isWebBuild():
                text = _("Trace a lesion from a published figure: open the picture (in /data)")
            elif not canTrace():
                text = _("Tracing from a figure needs the MNI template: start on the template first")
            self.emptyLabel.setText(text)
            return
        figurefit = _figurefit()
        self.panelCombo.blockSignals(True)
        self.panelCombo.clear()
        for candidate in current.panels:
            self.panelCombo.addItem(candidate.title())
        self.panelCombo.setCurrentIndex(current.current)
        self.panelCombo.blockSignals(False)
        for edit, value in ((self.figureLabelEdit, panel.label), (self.panelLabelEdit, panel.panelLabel)):
            if not edit.hasFocus() and edit.text != value:
                edit.setText(value)
        width, height = panel.size()
        roiActive = panel.roiId is not None
        self.cropButton.setText(_("Apply crop") if roiActive else _("Crop…"))
        self.cropLabel.setText(_("{w} × {h} px").format(w=width, h=height))  # noqa: RUF001
        self.planeGroup.button(_PLANES.index(panel.plane)).setChecked(True)
        # Convention: radiological is assumed until it is confirmed (once per record)
        convention = panel.convention
        for index, value in enumerate(("radiological", "neurological")):
            self.conventionGroup.button(index).setChecked(convention == value or (convention == "unknown" and value == "radiological"))
        if panel.confirmed:
            self.conventionState.setText(_("Confirmed for this paper"))
        else:
            self.conventionState.setText(_("Required: confirm the side") if current.requiredConvention
                                         else _("Not confirmed (radiological assumed)"))
        required = "true" if (current.requiredConvention or not panel.confirmed) else "false"
        if self.conventionState.property("required") != required:
            self.conventionState.setProperty("required", required)
            self.conventionState.style().unpolish(self.conventionState)
            self.conventionState.style().polish(self.conventionState)
        # Landmarks and fit
        pairs = panel.pairCount()
        placing = current._placing is not None and current._placing[0] is panel
        self.pairLabel.setText(_("{n} pairs").format(n=pairs) if not placing else _("placing “{name}”…").format(name=current._placing[2]))
        self.removePairButton.setEnabled(pairs > 0 or (panel.figureLandmarks() is not None and panel.figureLandmarks().GetNumberOfControlPoints() > 0))
        affineButton = self.modelGroup.button(1)
        affineButton.setEnabled(pairs >= figurefit.MIN_AFFINE_PAIRS)
        model = panel.fit.model if panel.fit is not None else ("affine" if panel.model == "affine" and pairs >= figurefit.MIN_AFFINE_PAIRS else "similarity")
        self.modelGroup.button(0 if model == "similarity" else 1).setChecked(True)
        if panel.fit is not None:
            text, level = figurefit.residual_chip(panel.fit.rms_mm, panel.fit.loo_rms_mm, panel.fit.n_pairs)
        else:
            text, level = _("No fit yet: {n} of 2 pairs").format(n=pairs), figurefit.residual_level(None)
        self.residualChip.setText(text)
        self.residualChip.setToolTip(level.text)
        ui.setChipLevel(self.residualChip, {"green": "ok", "amber": "warn", "red": "danger"}.get(level.level, "neutral"))
        warnings = []
        if panel.fitError:
            warnings.append(_("Fit failed: {error}").format(error=panel.fitError))
        if panel.fit is not None and panel.fit.mirror_suspected:
            warnings.append(_("The mirrored figure fits much better: check the side"))
        if panel.fit is not None and len(panel.fit.out_of_plane_mm) and float(np.max(panel.fit.out_of_plane_mm)) > OFF_PLANE_WARNING_MM:
            warnings.append(_("Some template landmarks are off the cut plane ({mm:.1f} mm): place them again").format(mm=float(np.max(panel.fit.out_of_plane_mm))))
        self.fitWarning.setText("\n".join(warnings))
        self.fitWarning.setVisible(bool(warnings))
        # Cut plane
        low, high = current.zRange()
        for slider, value in ((self.zSlider, panel.z), (self.pitchSlider, panel.pitch), (self.rollSlider, panel.roll)):
            slider.blockSignals(True)
            if slider is self.zSlider:
                slider.minimum, slider.maximum = low, high
            slider.value = value
            slider.blockSignals(False)
        self.opacitySlider.blockSignals(True)
        self.opacitySlider.setValue(round(current.opacity * 100))
        self.opacitySlider.blockSignals(False)
        self.edgesButton.blockSignals(True)
        self.edgesButton.setChecked(current.showEdges)
        self.edgesButton.blockSignals(False)
        self.traceButton.setText(_("Done tracing views") if current.tracing else _("Fit to template"))
        self.saveButton.setEnabled(any(candidate.fit is not None for candidate in current.panels))

    # Events

    def _onPanelActivated(self, index):
        if session() is not None:
            session().selectPanel(index)

    def _onLabelsEdited(self):
        current = session()
        panel = current.panel() if current is not None else None
        if panel is None:
            return
        panel.label = self.figureLabelEdit.text.strip()
        panel.panelLabel = self.panelLabelEdit.text.strip()
        current.changed()
        current.scheduleDraft()

    def _onCrop(self):
        current = session()
        panel = current.panel() if current is not None else None
        if panel is None:
            return
        if panel.roiId is None:
            current.startCrop()
        else:
            current.applyCrop()

    def _onConvention(self, index):
        current = session()
        if current is not None:
            current.setConvention(("radiological", "neurological")[index])

    def _onTraceToggled(self):
        current = session()
        if current is None:
            return
        if current.tracing:
            current.leaveTrace()
        else:
            current.enterTrace()

    def _onSave(self):
        current = session()
        if current is None:
            return
        folder = None
        if current.sourcesFolder() is None:
            start = WEB_DATA_FOLDER if isWebBuild() and os.path.isdir(WEB_DATA_FOLDER) else str(slicer.util.settingsValue(LAST_FOLDER_KEY, "") or "")
            folder = qt.QFileDialog.getExistingDirectory(slicer.util.mainWindow(), _("Folder for the figure fit"), start)
            if not folder:
                return
        try:
            written = current.saveFigures(folder)
        except Exception as error:
            logging.error(f"Studio: the figure fit could not be saved:\n{traceback.format_exc()}")
            _toast(current.shell, _("The figure fit could not be saved: {error}").format(error=error), ms=6000, level="warning")
            return
        _toast(current.shell, _("Saved {n} figure files").format(n=len(written)), ms=2500)

    def _addPanel(self):
        if isWebBuild() or not clipboardHasImage():
            openFigureDialog()
        else:
            pasteFigure()

    def _showSourceDialog(self):
        current = session()
        start = (current.sourcesFolder() if current is not None else None) or str(slicer.util.settingsValue(LAST_FOLDER_KEY, "") or "")
        path = qt.QFileDialog.getOpenFileName(slicer.util.mainWindow(), _("Show source"), start, _("Figure fit (*_fit.json)"))
        if path:
            try:
                showSource(path)
            except Exception as error:
                logging.error(f"Studio: cannot show the figure source {path}:\n{traceback.format_exc()}")
                _toast(None, _("Cannot show the source: {error}").format(error=error), ms=6000, level="warning")

    def dispose(self):
        from . import importer

        importer.releaseWidget(self.widget)


_PLANES = ("axial", "coronal", "sagittal")
_card = None
_classicWindow = None
_cardInClassicWindow = False


def card():
    """The Figure card (created once)."""
    global _card
    if _card is None or not _card.isAlive():
        _card = FigureCard()
    return _card


def _ensureCard():
    """Add the Figure card to the Draw inspector and "Trace from figure…" to the Lesion card ⋯ menu (once the Draw
    panel exists).
    """
    try:
        draw = _draw()
        extension = draw.extension()
        if extension._header is None:
            return False
        global _cardInClassicWindow
        figureCard = card()
        if _cardInClassicWindow or extension._cards.get(CARD_KEY, (None,))[0] is not figureCard.widget:
            # (Re)added: the card may come back from the Classic window
            extension.addInspectorCard(CARD_KEY, figureCard.widget, CARD_ORDER)
            _cardInClassicWindow = False
        lesionCard = draw.lesionCard()
        if lesionCard is not None:
            lesionCard.removeMenuAction(TRACE_ACTION_TEXT)
            lesionCard.addMenuAction(TRACE_ACTION_TEXT, traceFromFigure)
        figureCard.refresh()
        _updateCardVisibility()
        return True
    except Exception:
        logging.error(f"Studio: the Figure card could not be added:\n{traceback.format_exc()}")
        return False


TRACE_ACTION_TEXT = _("Trace from figure…")


def _updateCardVisibility():
    """The card is shown in Draw on a template (or while a figure is traced), never on a scan in scan space."""
    if _card is None or not _card.isAlive():
        return
    current = session()
    visible = (current is not None and bool(current.panels)) or canTrace()
    _card.widget.setVisible(bool(visible))
    if visible:
        _card.refresh()


def showClassicCard():
    """Classic ("Trace from figure…" of the Studio module panel): the same card in a non-modal window."""
    global _classicWindow, _cardInClassicWindow
    figureCard = card()
    _cardInClassicWindow = True
    if _classicWindow is None:
        _classicWindow = qt.QDialog(slicer.util.mainWindow())
        _classicWindow.setObjectName("StudioFigureClassicWindow")
        _classicWindow.setWindowTitle(_("Trace from figure"))
        _classicWindow.setModal(False)
        qt.QVBoxLayout(_classicWindow)
    _classicWindow.layout().addWidget(figureCard.widget)
    figureCard.widget.show()
    figureCard.refresh()
    _classicWindow.show()
    _classicWindow.raise_()
    return _classicWindow


#
# Study Done hook
#


def doneHook(sid, labelmapArray, ijkToRas, ctx):
    """Done → next: an unconfirmed convention blocks Done (inline required choice); otherwise the fit is saved and the
    figure columns are returned. The plane normals go to later hooks (L-28).
    """
    global _pendingFigureToken
    current = session()
    if current is None or not current.panels or current.sid != sid:
        return None
    if current.unconfirmedPanels():
        from . import study_ui

        current.requiredConvention = True
        current.changed()
        _ensureCard()
        if _card is not None:
            _card.conventionFrame.setFocus(qt.Qt.OtherFocusReason)
        raise study_ui.Blocking(_("Confirm on which side of the figure the patient's right is (Figure card), then Done again"))
    ctx["figure_plane_normals"] = current.planeNormals()
    ctx["figure_plane_normal"] = ctx["figure_plane_normals"][0]
    if current.saveFigures():
        _pendingFigureToken = sid
    return current.columns()


def _addFigureToken(studySession, sid):
    """Add the "figure" token to ``trace_method`` of ``sid`` (kept as is when it has it)."""
    if studySession is None or not sid:
        return
    try:
        method = studySession.study.value(sid, "trace_method")
        tokens = [token for token in str(method or "").split("+") if token]
        if "figure" in tokens:
            return
        if not tokens:
            tokens = ["manual"]
        tokens.append("figure")
        studySession.setValues([sid], {"trace_method": "+".join(tokens)})
    except Exception:
        logging.warning(f"Studio: trace_method of {sid} could not be updated:\n{traceback.format_exc()}")


#: The subject whose Done saved a figure fit: Done writes ``trace_method`` from the row it read before its hooks ran, so
#: the "figure" token is added again when Done notifies the study listeners (``_onStudyChanged``)
_pendingFigureToken = None


def _onStudyChanged(studySession=None):
    """Hook the Done of every study session; a subject switch closes the previous subject's figure (its draft stays)."""
    global _pendingFigureToken
    try:
        from . import study_ui
    except ImportError:
        return
    studySession = studySession or study_ui.session()
    if studySession is None:
        return
    if _pendingFigureToken is not None:
        sid, _pendingFigureToken = _pendingFigureToken, None
        _addFigureToken(studySession, sid)
    studySession.removeDoneHook(doneHook)
    studySession.addDoneHook(doneHook, order=DONE_HOOK_ORDER)
    studySession.removeOpenHook(_onSubjectOpened)
    studySession.addOpenHook(_onSubjectOpened)


def _onSubjectOpened(sid, ctx):
    current = session()
    if current is not None and current.sid != sid:
        if current.unsaved:
            current.writeDraft()
        closeFigure(removeNodes=True)
    study = _studySession()
    if study is None:
        return
    draft = os.path.join(study.study.path, ".studio", "figure", str(sid), "draft.json")
    if os.path.isfile(draft) and session() is None:
        shell = _currentShell()
        if shell is not None:
            shell.toast(_("A figure tracing of {sid} was not saved").format(sid=sid), sticky=True, action=lambda: restoreDraft(sid), actionText=_("Restore figure"))


#
# Plug-in (installed by the importer while Studio is shown)
#


_installedShell = None
_selectorConnected = None


def _paletteEntries(shell=None):
    return [{"id": "draw.traceFromFigure", "title": TRACE_ACTION_TEXT, "category": _("Draw"),
             "synonyms": ["figure", "trace figure", "case report", "paste figure", "open figure", "figure tracing"],
             "callback": lambda: traceFromFigure(shell), "description": _("Trace a lesion from a published figure on the template")}]


def _onClipboardChanged(*args):
    _updatePasteShortcut()


def _updatePasteShortcut():
    """Ctrl+V is enabled only while the clipboard holds an image (the shell installs it in Draw)."""
    shell = _installedShell
    if shell is None:
        return
    enabled = clipboardHasImage() and not isWebBuild()
    portable = qt.QKeySequence(PASTE_SHORTCUT).toString()
    for shortcut in shell.shortcuts():
        try:
            if shortcut.key.toString() == portable:
                shortcut.setEnabled(enabled)
        except Exception:
            pass


def _onModuleSelected(moduleName):
    if moduleName == "SegmentEditor":
        qt.QTimer.singleShot(0, _afterDrawShown)


def _afterDrawShown():
    _updatePasteShortcut()
    _ensureCard()


def install(shell):
    """Studio: Ctrl+V in Draw, the Figure card, the Lesion card menu item, the Search entry and the study Done hook."""
    global _installedShell, _selectorConnected
    _installedShell = shell
    shell.removeWorkspaceShortcut("draw", PASTE_SHORTCUT)
    shell.addWorkspaceShortcut("draw", PASTE_SHORTCUT, lambda: pasteFigure(shell), _("Paste a figure to trace from"))
    clipboard = qt.QApplication.clipboard()
    if clipboard is not None:
        with contextlib.suppress(Exception):
            clipboard.disconnect("dataChanged()", _onClipboardChanged)
        clipboard.connect("dataChanged()", _onClipboardChanged)
    selector = shell.mainWindow.moduleSelector() if hasattr(shell.mainWindow, "moduleSelector") else None
    if selector is not None and _selectorConnected is not selector:
        selector.connect("moduleSelected(QString)", _onModuleSelected)
        _selectorConnected = selector
    try:
        from . import palette

        palette.unregisterProvider(_paletteEntries)
        palette.registerProvider(_paletteEntries)
    except Exception:
        pass
    try:
        from . import study_ui

        study_ui.removeListener(_onStudyChanged)
        study_ui.addListener(_onStudyChanged)
        _onStudyChanged()
    except Exception:
        logging.error(f"Studio: the figure Done hook could not be installed:\n{traceback.format_exc()}")
    scene = slicer.mrmlScene
    global _sceneObserver
    if _sceneObserver is None:
        _sceneObserver = scene.AddObserver(scene.StartCloseEvent, _onSceneClosing)
    _updatePasteShortcut()
    _ensureCard()


_sceneObserver = None


def _onSceneClosing(caller=None, event=None):
    current = session()
    if current is not None:
        # The views get back their view group, links, slice geometry and layout (the slice and composite nodes are
        # singletons that outlive the scene close: Green would stay out of view group 0 and be saved so); the panels go
        # with their nodes
        if current.tracing:
            current.leaveTrace()
        current.tracing = False
        current.saved = None
        closeFigure()


def uninstall(shell):
    """Classic: the trace views are restored (the figure nodes stay), Ctrl+V is removed."""
    global _installedShell
    current = session()
    if current is not None and current.tracing:
        current.leaveTrace()
    if shell is not None:
        with contextlib.suppress(Exception):
            shell.removeWorkspaceShortcut("draw", PASTE_SHORTCUT)
    clipboard = qt.QApplication.clipboard()
    if clipboard is not None:
        with contextlib.suppress(Exception):
            clipboard.disconnect("dataChanged()", _onClipboardChanged)
    _installedShell = None

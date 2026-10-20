"""The in-view probe of Slicer Studio (SPEC 6.4 and 6.6, USABILITY 7): position, value and lesion under the mouse.

In Studio the Data Probe panel is hidden (``⋯ > Show Data Probe`` brings it back). Instead, the slice view under the
mouse shows one line at its bottom-right corner, for example:

- on an MNI grid: "Left 11.2 · P 17.8 · S 9.5 (MNI) · 412 · Lesion" (the hemisphere word for x; letters for y and z);
- in scanner space: "L 11.2 · P 17.8 · S 9.5 (scanner) · 412 · Lesion": letters are directions from the image origin,
  not hemispheres, so no hemisphere word is shown;
- over a figure: "Figure · pixel (120, 48) · 0.82" (no coordinates: a figure is not in patient space);
- with a statistical map in the foreground (Map): "T 8.31 · Left 24 · P 8 · S 12 (MNI) · Thalamus L", with the label of a
  loaded atlas (a dseg label map) when there is one.

Coordinates never carry a minus sign. "MNI" is used when the background volume is on an MNI grid
(``StudioLib.space.isMni``), otherwise "scanner".

Cost: the plug-in observes the crosshair node's ``CursorPositionModifiedEvent`` and only starts a 33 ms single-shot
timer (at most 30 updates per second). The update runs when the timer fires, skipped while a mouse button is held
(so nothing runs while painting), writes one right-justified ``vtkTextActor`` in the renderer of the hovered view's
corner annotation (12 px, #E8E8EC, with a shadow) and schedules a render of that view only. The text is hidden when
the mouse leaves the view (the cursor position becomes invalid). Wheel steps under a still mouse refresh it through
the hovered view's slice node.

Plug-in protocol: ``install(shell)`` / ``uninstall(shell)``; Classic shows no probe actor.
"""

import logging
import math
import re
import time
import traceback

import qt
import slicer
import vtk
from slicer.i18n import tr as _

#: Coalescing interval of the updates (at most 30 updates per second)
UPDATE_INTERVAL_MS = 33
#: Text size in points and its distance from the view corner in pixels
FONT_SIZE = 12
MARGIN = 8
TEXT_COLOR = (0.910, 0.910, 0.925)  # #E8E8EC
#: Separator of the parts of the probe text
SEPARATOR = " · "
#: Segments checked per segmentation (a segmentation with more segments is checked up to this number)
MAX_SEGMENTS = 64

_controller = None


def _isAlive(qobject):
    if qobject is None:
        return False
    try:
        qobject.objectName
    except Exception:
        return False
    return True


def _mouseButtonDown():
    buttons = qt.QApplication.mouseButtons()
    try:
        # Explicit integers: a QFlags value may not compare equal to an enum value (Qt 6)
        return int(buttons) != 0
    except (TypeError, ValueError):
        return buttons != qt.Qt.NoButton


def _space():
    import importlib

    try:
        return importlib.import_module("StudioLib.space")
    except ImportError:
        return None


#
# Text
#


def formatNumber(value):
    """A value as shown by the probe: an integer as is, else 3 significant digits (one decimal from 100 up)."""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return ""
    if float(value).is_integer() and abs(value) < 1e9:
        return str(int(value))
    if abs(value) >= 100:
        return f"{value:.1f}"
    return f"{value:.3g}"


def formatCoordinates(ras, mni, decimals=1, hemisphere=True):
    """Coordinates as "Left 11.2 · P 17.8 · S 9.5 (MNI)" in MNI space, "L 11.2 · P 17.8 · S 9.5 (scanner)" otherwise; never a minus sign.

    In MNI space x is a hemisphere word ("Left", "Right"; "Midline" at 0); in scanner space it is a direction letter from
    the image origin.
    """
    x, y, z = (round(float(value), decimals) for value in ras[:3])

    def magnitude(value):
        return f"{abs(value):.{decimals}f}"

    if mni and hemisphere:
        if x == 0:
            xText = f"{_('Midline')} {magnitude(0.0)}"
        else:
            xText = f"{_('Left') if x < 0 else _('Right')} {magnitude(x)}"
    else:
        xText = f"{'L' if x < 0 else 'R'} {magnitude(x)}"
    yText = f"{'P' if y < 0 else 'A'} {magnitude(y)}"
    zText = f"{'I' if z < 0 else 'S'} {magnitude(z)}"
    space = "MNI" if mni else _("scanner")
    return f"{xText}{SEPARATOR}{yText}{SEPARATOR}{zText} ({space})"


def _ijkOf(volumeNode, ras):
    """Integer IJK of a RAS position in a volume (with its parent transforms), None outside the image."""
    image = volumeNode.GetImageData() if volumeNode is not None else None
    if image is None:
        return None
    point = list(ras[:3])
    transformNode = volumeNode.GetParentTransformNode()
    if transformNode is not None:
        worldToLocal = vtk.vtkGeneralTransform()
        transformNode.GetTransformFromWorld(worldToLocal)
        point = list(worldToLocal.TransformPoint(point))
    rasToIjk = vtk.vtkMatrix4x4()
    volumeNode.GetRASToIJKMatrix(rasToIjk)
    ijk = rasToIjk.MultiplyPoint(point + [1.0])
    indices = [math.floor(ijk[axis] + 0.5) for axis in range(3)]
    dimensions = image.GetDimensions()
    if any(index < 0 or index >= dimensions[axis] for axis, index in enumerate(indices)):
        return None
    return indices


def voxelValue(volumeNode, ras):
    """``(ijk, value)`` of a volume at a RAS position, or ``(None, None)`` outside. A vector image gives its luminance."""
    ijk = _ijkOf(volumeNode, ras)
    if ijk is None:
        return None, None
    image = volumeNode.GetImageData()
    components = image.GetNumberOfScalarComponents()
    if components == 1:
        return ijk, image.GetScalarComponentAsDouble(ijk[0], ijk[1], ijk[2], 0)
    rgb = [image.GetScalarComponentAsDouble(ijk[0], ijk[1], ijk[2], component) for component in range(min(components, 3))]
    if len(rgb) < 3:
        return ijk, rgb[0]
    return ijk, 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def _figureValue(volumeNode, value):
    """A figure pixel as 0..1 (8-bit images), else the value."""
    image = volumeNode.GetImageData()
    if value is not None and image is not None and image.GetScalarType() in (vtk.VTK_UNSIGNED_CHAR, vtk.VTK_CHAR, vtk.VTK_SIGNED_CHAR):
        return value / 255.0
    return value


def segmentsAt(ras, sliceNode=None):
    """Names of the visible segments at a RAS position (segmentations displayed in ``sliceNode``'s view, if given)."""
    names = []
    binaryName = slicer.vtkSegmentationConverter.GetBinaryLabelmapRepresentationName()
    for segmentationNode in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
        displayNode = segmentationNode.GetDisplayNode()
        if displayNode is None or not displayNode.GetVisibility() or not displayNode.GetVisibility2D():
            continue
        if sliceNode is not None and not displayNode.IsDisplayableInView(sliceNode.GetID()):
            continue
        point = list(ras[:3])
        transformNode = segmentationNode.GetParentTransformNode()
        if transformNode is not None:
            worldToLocal = vtk.vtkGeneralTransform()
            transformNode.GetTransformFromWorld(worldToLocal)
            point = list(worldToLocal.TransformPoint(point))
        segmentation = segmentationNode.GetSegmentation()
        if not segmentation.ContainsRepresentation(binaryName):
            continue
        segmentIds = vtk.vtkStringArray()
        segmentation.GetSegmentIDs(segmentIds)
        for index in range(min(segmentIds.GetNumberOfValues(), MAX_SEGMENTS)):
            segmentId = segmentIds.GetValue(index)
            if not displayNode.GetSegmentVisibility(segmentId):
                continue
            segment = segmentation.GetSegment(segmentId)
            labelmap = segment.GetRepresentation(binaryName) if segment is not None else None
            if labelmap is None or labelmap.GetPointData().GetScalars() is None:
                continue
            worldToImage = vtk.vtkMatrix4x4()
            labelmap.GetWorldToImageMatrix(worldToImage)
            ijk = worldToImage.MultiplyPoint(point + [1.0])
            extent = labelmap.GetExtent()
            indices = [math.floor(ijk[axis] + 0.5) for axis in range(3)]
            if any(indices[axis] < extent[2 * axis] or indices[axis] > extent[2 * axis + 1] for axis in range(3)):
                continue
            value = labelmap.GetScalarComponentAsDouble(indices[0], indices[1], indices[2], 0)
            if value != 0 and int(value) == segment.GetLabelValue():
                names.append(segment.GetName())
    return names


_STAT_LABEL_PATTERNS = (
    (re.compile(r"(?i)(^|[_\-\s.])(desc-)?(t|tmap|tstat)([_\-\s.]|$)"), "T"),
    (re.compile(r"(?i)(^|[_\-\s.])(desc-)?(z|zmap|zstat)([_\-\s.]|$)"), "Z"),
    (re.compile(r"(?i)(^|[_\-\s.])(desc-)?(fz|avgrfz)([_\-\s.]|$)"), "Fz"),
    (re.compile(r"(?i)(^|[_\-\s.])(desc-)?(r|avgr)([_\-\s.]|$)"), "r"),
)


def statLabel(volumeNode):
    """The statistic of a map from its name ("T" for "sub-01_desc-T_statmap"), else "Map"."""
    names = [volumeNode.GetName() or ""]
    storageNode = volumeNode.GetStorageNode()
    if storageNode is not None and storageNode.GetFileName():
        names.append(storageNode.GetFileName().replace("\\", "/").split("/")[-1])
    for name in names:
        for pattern, label in _STAT_LABEL_PATTERNS:
            if pattern.search(name):
                return label
    return _("Map")


def _isAtlas(volumeNode):
    if volumeNode is None or volumeNode.GetImageData() is None:
        return False
    if volumeNode.GetAttribute("Studio.Role") == "atlas":
        return True
    storageNode = volumeNode.GetStorageNode()
    fileName = storageNode.GetFileName() if storageNode is not None else ""
    return bool(fileName) and "_dseg" in fileName.replace("\\", "/").split("/")[-1]


def atlasLabelAt(ras):
    """The label name of the first loaded atlas (a "_dseg" label image) at a RAS position, or None."""
    for volumeNode in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        if not _isAtlas(volumeNode):
            continue
        value = voxelValue(volumeNode, ras)[1]
        if value is None or value == 0:
            return None
        displayNode = volumeNode.GetDisplayNode()
        colorNode = displayNode.GetColorNode() if displayNode is not None else None
        name = colorNode.GetColorName(int(value)) if colorNode is not None else ""
        return name if name and name != "(none)" else formatNumber(value)
    return None


class _SpaceCache:
    """``space.isMni`` and ``space.isFigure`` of the background volumes, cached by node and modification time."""

    def __init__(self):
        self._cache = {}

    def get(self, volumeNode):
        key = (volumeNode.GetID(), volumeNode.GetMTime())
        cached = self._cache.get(volumeNode.GetID())
        if cached is not None and cached[0] == key:
            return cached[1]
        space = _space()
        isFigure = bool(space is not None and space.isFigure(volumeNode))
        isMni = bool(space is not None and not isFigure and space.isMni(volumeNode))
        self._cache[volumeNode.GetID()] = (key, (isMni, isFigure))
        return isMni, isFigure


_spaceCache = _SpaceCache()


def probeText(sliceWidget, ras, xyz=None, workspace=None):
    """The probe line of a slice view at a RAS position (see the module docstring), "" when there is nothing to show."""
    sliceLogic = sliceWidget.sliceLogic()
    sliceNode = sliceWidget.mrmlSliceNode()
    background = sliceLogic.GetBackgroundLayer().GetVolumeNode() if sliceLogic is not None else None
    foreground = sliceLogic.GetForegroundLayer().GetVolumeNode() if sliceLogic is not None else None
    space = _space()
    viewName = sliceNode.GetLayoutName() if sliceNode is not None else None
    if background is not None:
        isMni, isFigure = _spaceCache.get(background)
    else:
        isMni, isFigure = False, False
    if not isFigure and space is not None and viewName and background is not None and background.GetImageData() is not None:
        dims = background.GetImageData().GetDimensions()
        isFigure = 1 in dims and space.isFigureView(viewName)
    if isFigure:
        ijk, value = voxelValue(background, ras)
        if ijk is None:
            return _("Figure")
        parts = [_("Figure"), _("pixel ({i}, {j})").format(i=ijk[0], j=ijk[1])]
        figureValue = _figureValue(background, value)
        if figureValue is not None:
            parts.append(f"{figureValue:.2f}" if not float(figureValue).is_integer() or figureValue <= 1 else formatNumber(figureValue))
        return SEPARATOR.join(parts)
    statMap = None
    if foreground is not None and foreground.GetImageData() is not None:
        role = foreground.GetAttribute("Studio.Role")
        if role == "statmap" or workspace == "map":
            statMap = foreground
    parts = []
    if statMap is not None:
        ijk, value = voxelValue(statMap, ras)
        if value is not None:
            parts.append(f"{statLabel(statMap)} {formatNumber(value)}")
        mni = _spaceCache.get(statMap)[0] or isMni
        parts.append(formatCoordinates(ras, mni, decimals=0))
        label = atlasLabelAt(ras) if mni else None
        if label:
            parts.append(label)
        return SEPARATOR.join(parts)
    parts.append(formatCoordinates(ras, isMni))
    if background is not None:
        ijk, value = voxelValue(background, ras)
        if value is not None:
            parts.append(formatNumber(value))
    segments = segmentsAt(ras, sliceNode)
    if segments:
        parts.append(", ".join(dict.fromkeys(segments)))
    return SEPARATOR.join(parts)


#
# Rendering
#


class _ViewText:
    """The probe text actor of one slice view."""

    def __init__(self, sliceWidget):
        self.sliceWidget = sliceWidget
        self.sliceView = sliceWidget.sliceView()
        self.renderer = self._findRenderer()
        actor = vtk.vtkTextActor()
        if hasattr(actor, "SetObjectName"):
            actor.SetObjectName("StudioProbeText")
        actor.SetTextScaleModeToNone()
        actor.PickableOff()
        reference = vtk.vtkCoordinate()
        reference.SetCoordinateSystemToNormalizedViewport()
        reference.SetValue(1.0, 0.0)
        position = actor.GetPositionCoordinate()
        position.SetCoordinateSystemToDisplay()
        position.SetReferenceCoordinate(reference)
        position.SetValue(-MARGIN, MARGIN)
        textProperty = actor.GetTextProperty()
        textProperty.SetFontFamilyToArial()
        textProperty.SetFontSize(FONT_SIZE)
        textProperty.ShadowOn()
        textProperty.SetColor(*TEXT_COLOR)
        textProperty.SetJustificationToRight()
        textProperty.SetVerticalJustificationToBottom()
        actor.SetInput("")
        actor.VisibilityOff()
        self.actor = actor
        self.text = ""
        self.renderer.AddViewProp(actor)

    def _findRenderer(self):
        renderers = self.sliceView.renderWindow().GetRenderers()
        cornerAnnotation = self.sliceView.cornerAnnotation()
        for index in range(renderers.GetNumberOfItems()):
            renderer = renderers.GetItemAsObject(index)
            if cornerAnnotation is not None and renderer.HasViewProp(cornerAnnotation):
                return renderer
        return renderers.GetFirstRenderer()

    def isCurrent(self, sliceWidget):
        return _isAlive(self.sliceView) and sliceWidget is not None and sliceWidget.sliceView() == self.sliceView

    def setText(self, text):
        """Show ``text`` (hide for ""). Return True if a render was scheduled."""
        if text == self.text:
            return False
        self.text = text
        self.actor.SetInput(text or "")
        self.actor.SetVisibility(bool(text))
        if _isAlive(self.sliceView):
            self.sliceView.scheduleRender()
        return True

    def remove(self):
        self.renderer.RemoveViewProp(self.actor)
        if _isAlive(self.sliceView) and self.text:
            self.sliceView.scheduleRender()
        self.text = ""


class Probe:
    """Follows the cursor of the slice views and shows the probe text in the hovered one."""

    def __init__(self, shell):
        self.shell = shell
        self.views = {}
        self.currentView = None
        #: Number of times the text was computed (tests: rate and painting)
        self.updateCount = 0
        self.lastUpdateMs = None
        self.crosshair = None
        self.crosshairTag = None
        self.sliceNode = None
        self.sliceNodeTag = None
        self.timer = qt.QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(UPDATE_INTERVAL_MS)
        self.timer.connect("timeout()", self._update)
        self.sceneTags = [slicer.mrmlScene.AddObserver(slicer.vtkMRMLScene.EndCloseEvent, self._onSceneClosed)]
        self._observeCrosshair()
        layoutManager = slicer.app.layoutManager()
        self.layoutManager = layoutManager
        if layoutManager is not None:
            layoutManager.connect("layoutChanged(int)", self._onLayoutChanged)

    def _observeCrosshair(self):
        crosshair = slicer.mrmlScene.GetNodeByID("vtkMRMLCrosshairNodedefault") or slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
        if crosshair is self.crosshair and self.crosshairTag is not None:
            return
        self._unobserveCrosshair()
        self.crosshair = crosshair
        if crosshair is not None:
            self.crosshairTag = crosshair.AddObserver(slicer.vtkMRMLCrosshairNode.CursorPositionModifiedEvent, self._onCursorMoved)

    def _unobserveCrosshair(self):
        if self.crosshair is not None and self.crosshairTag is not None:
            self.crosshair.RemoveObserver(self.crosshairTag)
        self.crosshair = None
        self.crosshairTag = None

    def _onCursorMoved(self, caller, event):
        # Called for every mouse move over a view: only (re)start the coalescing timer
        if not self.timer.isActive():
            self.timer.start()

    def _onSliceNodeModified(self, caller, event):
        if not self.timer.isActive():
            self.timer.start()

    def _onSceneClosed(self, caller, event):
        self._observeCrosshair()
        self.hide()

    def _onLayoutChanged(self, layoutId=None):
        self.hide()
        for name in list(self.views):
            self.views.pop(name).remove()

    def _watchSliceNode(self, sliceNode):
        if sliceNode is self.sliceNode:
            return
        if self.sliceNode is not None and self.sliceNodeTag is not None:
            self.sliceNode.RemoveObserver(self.sliceNodeTag)
        self.sliceNode = sliceNode
        self.sliceNodeTag = sliceNode.AddObserver(vtk.vtkCommand.ModifiedEvent, self._onSliceNodeModified) if sliceNode is not None else None

    def _view(self, name):
        layoutManager = self.layoutManager
        sliceWidget = layoutManager.sliceWidget(name) if layoutManager is not None else None
        view = self.views.get(name)
        if view is not None and not view.isCurrent(sliceWidget):
            self.views.pop(name).remove()
            view = None
        if view is None and sliceWidget is not None:
            view = _ViewText(sliceWidget)
            self.views[name] = view
        return view

    def hide(self):
        """Hide the text (the mouse left the views)."""
        if self.currentView is not None:
            view = self.views.get(self.currentView)
            if view is not None:
                view.setText("")
        self.currentView = None
        self._watchSliceNode(None)

    def _update(self):
        try:
            self.update()
        except Exception:
            logging.error(f"Studio: probe update failed:\n{traceback.format_exc()}")

    def update(self):
        """Compute and show the probe text now. Return the text shown ("" when hidden, None when skipped)."""
        if _mouseButtonDown():
            # A button is held (painting, dragging): nothing is computed
            return None
        if self.crosshair is None:
            self._observeCrosshair()
            if self.crosshair is None:
                return None
        startTime = time.perf_counter()
        xyz = [0.0, 0.0, 0.0]
        sliceNode = self.crosshair.GetCursorPositionXYZ(xyz)
        if sliceNode is None:
            self.hide()
            return ""
        name = sliceNode.GetLayoutName()
        if self.currentView is not None and self.currentView != name:
            self.hide()
        view = self._view(name)
        if view is None:
            return None
        # From the view's own XYToRAS, so that a wheel step under a still mouse is followed
        xyToRas = sliceNode.GetXYToRAS()
        ras = list(xyToRas.MultiplyPoint(xyz + [1.0]))[:3]
        workspace = self.shell.currentWorkspace() if self.shell is not None else None
        text = probeText(view.sliceWidget, ras, xyz, workspace)
        self.currentView = name
        self._watchSliceNode(sliceNode)
        view.setText(text)
        self.updateCount += 1
        self.lastUpdateMs = (time.perf_counter() - startTime) * 1000.0
        return text

    def text(self, viewName=None):
        """The text shown in a view (the current view by default), "" if none."""
        view = self.views.get(viewName or self.currentView) if (viewName or self.currentView) else None
        return view.text if view is not None else ""

    def dispose(self):
        self.timer.stop()
        try:
            self.timer.disconnect("timeout()", self._update)
        except Exception:
            pass
        self._unobserveCrosshair()
        self._watchSliceNode(None)
        for tag in self.sceneTags:
            slicer.mrmlScene.RemoveObserver(tag)
        self.sceneTags = []
        if _isAlive(self.layoutManager):
            try:
                self.layoutManager.disconnect("layoutChanged(int)", self._onLayoutChanged)
            except Exception:
                pass
        for view in self.views.values():
            try:
                view.remove()
            except Exception:
                logging.error(f"Studio: cannot remove the probe text:\n{traceback.format_exc()}")
        self.views = {}
        self.currentView = None


def instance():
    """The probe while Studio is shown, else None."""
    return _controller


def install(shell):
    """Studio plug-in entry point: show the probe in the hovered slice view."""
    global _controller
    uninstall(shell)
    if shell is None or not shell.isActive() or slicer.app.layoutManager() is None:
        return
    _controller = Probe(shell)


def uninstall(shell):
    """Remove every observer and text actor (Classic shows no probe)."""
    global _controller
    if _controller is None:
        return
    controller = _controller
    _controller = None
    controller.dispose()

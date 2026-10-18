"""Orientation letters at the edges of the slice views of Slicer Studio (laterality cue).

Slice views are shown in the radiological convention (the patient's right on the left of the screen) and
nothing else in the view says so. In Studio every slice view shows the anatomical direction of its four
screen edges: for the default axial view "R" on the left edge, "L" on the right edge, "A" on the top
edge and "P" on the bottom edge; "S"/"I" at the top/bottom of the coronal and sagittal views.

The letters are computed from the SliceToRAS matrix of the slice node, so they stay correct for any
orientation, flip or in-plane rotation: the first column of SliceToRAS is the RAS direction of the
screen's +x (the right edge) and the second column the RAS direction of the screen's +y (the top edge);
the left and bottom edges are the opposite directions.

Rule for oblique directions (``directionLetters()``): a direction within 25 degrees of an anatomical
axis shows that axis' letter only. Otherwise, every axis whose component is at least sin(25 degrees)
(0.42) of the unit direction is listed, largest component first: "RA" for a 45 degree rotation in the
axial plane, "RAS" for a direction equally between three axes.

Rendering: four bold ``vtkTextActor`` per slice view, in the renderer of the view's corner annotation.
The corner annotation is not used for the letters, although SliceViewAnnotations only writes its
corners (positions 0..3): all positions of a ``vtkCornerAnnotation`` share one text property, so bold
letters would also turn the corner text bold, and ``ctkVTKAbstractView.setCornerAnnotationText()``
clears all positions.

Updates: the plug-in observes the ModifiedEvent of each slice node and only marks the view; a 0 ms
single-shot timer then compares the direction part of SliceToRAS with the last one shown and renders
the views whose letters changed (``scheduleRender()``). Nothing runs on hover. Views added or removed
by layout changes are followed through ``layoutManager.layoutChanged``.

This module is an optional plug-in of the Studio shell: ``install(shell)`` shows the letters (unless
the ``Studio/OrientationLetters`` setting is false), ``uninstall(shell)`` removes every actor and
observer, so that Classic shows nothing new.
"""

import logging
import math
import traceback

import qt
import slicer
import vtk

SETTINGS_KEY = "Studio/OrientationLetters"

#: Letters of the positive and negative RAS axes
_POSITIVE_LETTERS = ("R", "A", "S")
_NEGATIVE_LETTERS = ("L", "P", "I")

#: A direction within this angle of an anatomical axis shows one letter (see the module docstring)
SINGLE_LETTER_ANGLE_DEGREES = 25.0
_SECOND_LETTER_COMPONENT = math.sin(math.radians(SINGLE_LETTER_ANGLE_DEGREES))

#: Size of the letters in points: the size of the corner annotations of SliceViewAnnotations (14)
FONT_SIZE = 14
#: Distance of the letters from the view edge, in pixels
EDGE_MARGIN = 6
#: Slice views have a dark background in the dark and the light theme, so the letters are always light
#: (the text color of the dark theme), with a shadow for bright images
LETTER_COLOR = (0.957, 0.957, 0.961)  # #f4f4f5
LETTER_OPACITY = 0.9

#: Edges, in the order of ``edgeLetters()``: (name, reference point in normalized viewport coordinates,
#: offset in pixels, horizontal justification, vertical justification)
EDGES = (
    ("left", (0.0, 0.5), (EDGE_MARGIN, 0), "left", "center"),
    ("right", (1.0, 0.5), (-EDGE_MARGIN, 0), "right", "center"),
    ("top", (0.5, 1.0), (0, -EDGE_MARGIN), "center", "top"),
    ("bottom", (0.5, 0.0), (0, EDGE_MARGIN), "center", "bottom"),
)

_controller = None


def directionLetters(direction):
    """Return the anatomical letters of a RAS direction (see the module docstring for the rule).

    ``directionLetters((-1, 0, 0))`` is "L", ``directionLetters((0.7071, 0.7071, 0))`` is "RA".
    An empty string is returned for a null vector.
    """
    length = math.sqrt(sum(component * component for component in direction))
    if length < 1e-9:
        return ""
    unit = [component / length for component in direction]
    order = sorted(range(3), key=lambda axis: -abs(unit[axis]))
    letters = ""
    for rank, axis in enumerate(order):
        component = unit[axis]
        if rank > 0 and abs(component) < _SECOND_LETTER_COMPONENT:
            break
        letters += _POSITIVE_LETTERS[axis] if component > 0 else _NEGATIVE_LETTERS[axis]
        if rank == 0 and math.degrees(math.acos(min(abs(component), 1.0))) <= SINGLE_LETTER_ANGLE_DEGREES:
            break
    return letters


def _directionKey(sliceToRAS):
    """The two in-plane screen directions of a SliceToRAS matrix (columns 0 and 1), rounded."""
    return tuple(round(sliceToRAS.GetElement(row, column), 6) for column in (0, 1) for row in range(3))


def edgeLetters(sliceToRAS):
    """Return the letters of the (left, right, top, bottom) screen edges of a slice view.

    ``sliceToRAS`` is the ``vtkMatrix4x4`` SliceToRAS of the slice node.
    """
    screenX = [sliceToRAS.GetElement(row, 0) for row in range(3)]
    screenY = [sliceToRAS.GetElement(row, 1) for row in range(3)]
    return (
        directionLetters([-component for component in screenX]),
        directionLetters(screenX),
        directionLetters(screenY),
        directionLetters([-component for component in screenY]),
    )


def _isAlive(qobject):
    """False if the C++ object was deleted."""
    try:
        qobject.objectName
    except Exception:
        return False
    return True


class _ViewLetters:
    """The four edge letters of one slice view."""

    def __init__(self, controller, sliceViewName, sliceWidget):
        self.controller = controller
        self.name = sliceViewName
        self.sliceWidget = sliceWidget
        self.sliceView = sliceWidget.sliceView()
        self.sliceNode = sliceWidget.mrmlSliceNode()
        self.renderer = self._findRenderer()
        self.directionKey = None
        self.letters = ("", "", "", "")
        self.actors = []
        for edgeName, reference, offset, horizontal, vertical in EDGES:
            self.actors.append(self._createActor(edgeName, reference, offset, horizontal, vertical))
        for actor in self.actors:
            self.renderer.AddViewProp(actor)
        self.observerTag = self.sliceNode.AddObserver(vtk.vtkCommand.ModifiedEvent, self._onSliceNodeModified)

    def _findRenderer(self):
        """The renderer of the corner annotation (the first renderer if it is not found)."""
        renderers = self.sliceView.renderWindow().GetRenderers()
        cornerAnnotation = self.sliceView.cornerAnnotation()
        for index in range(renderers.GetNumberOfItems()):
            renderer = renderers.GetItemAsObject(index)
            if cornerAnnotation is not None and renderer.HasViewProp(cornerAnnotation):
                return renderer
        return renderers.GetFirstRenderer()

    @staticmethod
    def _createActor(edgeName, reference, offset, horizontal, vertical):
        actor = vtk.vtkTextActor()
        if hasattr(actor, "SetObjectName"):
            actor.SetObjectName(f"StudioOrientationLetter_{edgeName}")
        actor.SetTextScaleModeToNone()
        actor.PickableOff()
        referenceCoordinate = vtk.vtkCoordinate()
        referenceCoordinate.SetCoordinateSystemToNormalizedViewport()
        referenceCoordinate.SetValue(reference[0], reference[1])
        position = actor.GetPositionCoordinate()
        position.SetCoordinateSystemToDisplay()
        position.SetReferenceCoordinate(referenceCoordinate)
        position.SetValue(offset[0], offset[1])
        textProperty = actor.GetTextProperty()
        textProperty.SetFontFamilyToArial()
        textProperty.SetFontSize(FONT_SIZE)
        textProperty.BoldOn()
        textProperty.ShadowOn()
        textProperty.SetColor(*LETTER_COLOR)
        textProperty.SetOpacity(LETTER_OPACITY)
        {"left": textProperty.SetJustificationToLeft, "right": textProperty.SetJustificationToRight,
         "center": textProperty.SetJustificationToCentered}[horizontal]()
        {"top": textProperty.SetVerticalJustificationToTop, "bottom": textProperty.SetVerticalJustificationToBottom,
         "center": textProperty.SetVerticalJustificationToCentered}[vertical]()
        actor.SetInput("")
        return actor

    def _onSliceNodeModified(self, caller, event):
        # Called for every slice node change (slice offset, pan, zoom...): only mark the view, the
        # comparison of the matrices runs once in the controller's 0 ms timer
        self.controller.markDirty(self.name)

    def isCurrent(self, sliceWidget):
        """True if this object still shows the letters of ``sliceWidget`` (same widget and slice node)."""
        return (_isAlive(self.sliceView) and sliceWidget is not None and sliceWidget.sliceView() == self.sliceView
                and sliceWidget.mrmlSliceNode() == self.sliceNode)

    def update(self):
        """Update the letters if the in-plane directions changed. Return True if a render was requested."""
        sliceToRAS = self.sliceNode.GetSliceToRAS()
        key = _directionKey(sliceToRAS)
        if key == self.directionKey:
            return False
        self.directionKey = key
        letters = edgeLetters(sliceToRAS)
        if letters == self.letters:
            return False
        self.letters = letters
        for actor, text in zip(self.actors, letters, strict=True):
            actor.SetInput(text)
        if _isAlive(self.sliceView):
            self.sliceView.scheduleRender()
        return True

    def remove(self):
        if self.observerTag is not None:
            self.sliceNode.RemoveObserver(self.observerTag)
            self.observerTag = None
        for actor in self.actors:
            self.renderer.RemoveViewProp(actor)
        self.actors = []
        if _isAlive(self.sliceView):
            self.sliceView.scheduleRender()


class _OrientationLetters:
    """Letters of all slice views of the layout manager."""

    def __init__(self, layoutManager):
        self.layoutManager = layoutManager
        self.views = {}
        self.dirty = set()
        self.updateTimer = qt.QTimer()
        self.updateTimer.setSingleShot(True)
        self.updateTimer.setInterval(0)
        self.updateTimer.connect("timeout()", self._updateDirtyViews)
        self.layoutManager.connect("layoutChanged(int)", self._onLayoutChanged)

    def markDirty(self, sliceViewName):
        self.dirty.add(sliceViewName)
        if not self.updateTimer.isActive():
            self.updateTimer.start()

    def _onLayoutChanged(self, layout=None):
        # Views are created after the layout is set up: synchronize when the application processes events
        self.markDirty(None)

    def _updateDirtyViews(self):
        dirty = self.dirty
        self.dirty = set()
        try:
            if None in dirty:
                self.synchronizeViews()
                return
            for name in dirty:
                view = self.views.get(name)
                if view is not None:
                    view.update()
        except Exception:
            logging.error(f"Studio: orientation letters update failed:\n{traceback.format_exc()}")

    def synchronizeViews(self):
        """Create the letters of new slice views, remove those of deleted views and update all of them."""
        if not _isAlive(self.layoutManager):
            return
        names = list(self.layoutManager.sliceViewNames())
        for name in list(self.views.keys()):
            sliceWidget = self.layoutManager.sliceWidget(name) if name in names else None
            if not self.views[name].isCurrent(sliceWidget):
                self.views.pop(name).remove()
        for name in names:
            if name in self.views:
                continue
            sliceWidget = self.layoutManager.sliceWidget(name)
            if sliceWidget is None or sliceWidget.mrmlSliceNode() is None:
                continue
            self.views[name] = _ViewLetters(self, name, sliceWidget)
        for view in self.views.values():
            view.update()

    def dispose(self):
        self.updateTimer.stop()
        try:
            self.updateTimer.disconnect("timeout()", self._updateDirtyViews)
        except Exception:
            pass
        if _isAlive(self.layoutManager):
            try:
                self.layoutManager.disconnect("layoutChanged(int)", self._onLayoutChanged)
            except Exception:
                pass
        for view in self.views.values():
            try:
                view.remove()
            except Exception:
                logging.error(f"Studio: cannot remove the orientation letters of {view.name}:\n{traceback.format_exc()}")
        self.views = {}
        self.dirty = set()


def isEnabled():
    """Value of the ``Studio/OrientationLetters`` setting (default: true)."""
    return slicer.util.settingsValue(SETTINGS_KEY, True, converter=slicer.util.toBool)


def setEnabled(shell, enabled):
    """Store the ``Studio/OrientationLetters`` setting and show or hide the letters in Studio."""
    qt.QSettings().setValue(SETTINGS_KEY, bool(enabled))
    if enabled and shell is not None:
        install(shell)
    else:
        uninstall(shell)


def instance():
    """The controller of the letters while they are shown (None otherwise). Used by the tests."""
    return _controller


def viewLetters(sliceViewName):
    """The (left, right, top, bottom) letters shown in a slice view, None if the view has no letters."""
    if _controller is None:
        return None
    view = _controller.views.get(sliceViewName)
    return view.letters if view is not None else None


def update():
    """Update the letters now instead of waiting for the application to process events (tests, captures)."""
    if _controller is None:
        return
    _controller.updateTimer.stop()
    _controller.dirty = set()
    _controller.synchronizeViews()


def install(shell):
    """Show the orientation letters in every slice view (if the ``Studio/OrientationLetters`` setting is on)."""
    global _controller
    uninstall(shell)
    if not isEnabled():
        return
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None:
        return
    _controller = _OrientationLetters(layoutManager)
    _controller.synchronizeViews()


def uninstall(shell):
    """Remove all letters, observers and connections."""
    global _controller
    if _controller is None:
        return
    controller = _controller
    _controller = None
    controller.dispose()

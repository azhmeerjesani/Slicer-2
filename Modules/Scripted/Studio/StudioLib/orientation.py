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

The slice view ruler (View controller > ruler) is drawn at the bottom centre of the view, where the
bottom letter is: while a ruler is shown, the bottom letter is raised above its ticks (the ruler type is
part of the key compared by the timer, the ruler geometry is a fraction of the view height so the
position follows resizes without any update).

Display statement (plain words): axial and coronal views, whose screen-right direction (column 0 of
SliceToRAS) is axis-aligned to x (|x| > 0.9), say in the bottom-right corner "Radiological · L on right"
(the default) or "Neurological · L on left" (a flipped view), VISUAL 4.3. The statement is an 11 px
``vtkTextActor`` in the caption color, above the lines of the lower-right corner annotation if there are any, updated
with the letters. Sagittal and oblique views have none. The bottom-left corner keeps the name of the image
(SliceViewAnnotations).

Figure views (``space.isFigureView()``): a 2D figure has no patient directions of its own, so a figure view
shows no A/P/S/I letters and no statement. It shows a tag "Figure · side unknown" until the figure's
convention is answered, then "Figure · patient right on the left" with "R" on the left edge and "L" on the
right edge (the importer mirrors a neurological figure, so the answer always ends so). These two side letters
are separate actors (``StudioFigureLetter_*``), not ``StudioOrientationLetter_*``.

Re-centring: when the background volume of a slice view changes and the slice offset is outside the new
background along the view normal, the offset moves to the nearest bound, half a voxel inside, so that the view
never stays black. Figure views are skipped, and a view whose offset is inside the volume never moves.

This module is an optional plug-in of the Studio shell: ``install(shell)`` shows the letters while the
shell is active (unless the ``Studio/OrientationLetters`` setting is false), ``uninstall(shell)``
removes every actor and observer, so that Classic shows nothing new.
"""

import logging
import math
import traceback

import qt
import slicer
import vtk
from slicer.i18n import tr as _

SETTINGS_KEY = "Studio/OrientationLetters"

#: Letters of the positive and negative RAS axes
_POSITIVE_LETTERS = ("R", "A", "S")
_NEGATIVE_LETTERS = ("L", "P", "I")

#: A direction within this angle of an anatomical axis shows one letter (see the module docstring)
SINGLE_LETTER_ANGLE_DEGREES = 25.0
_SECOND_LETTER_COMPONENT = math.sin(math.radians(SINGLE_LETTER_ANGLE_DEGREES))

#: Size of the letters in points: 11, weight 600 (bold), like the corner text of the flat views (VISUAL 4.3)
FONT_SIZE = 11
#: Distance of the letters from the view edge, in pixels
EDGE_MARGIN = 6
#: Slice views have a dark background in the dark and the light theme, so the letters always have the caption color of
#: the dark theme (text3, #8b8b94: 6:1 on black), with a shadow for bright images
LETTER_COLOR = (0x8B / 255.0, 0x8B / 255.0, 0x94 / 255.0)
LETTER_OPACITY = 1.0

#: Edges, in the order of ``edgeLetters()``: (name, reference point in normalized viewport coordinates,
#: offset in pixels, horizontal justification, vertical justification)
EDGES = (
    ("left", (0.0, 0.5), (EDGE_MARGIN, 0), "left", "center"),
    ("right", (1.0, 0.5), (-EDGE_MARGIN, 0), "right", "center"),
    ("top", (0.5, 1.0), (0, -EDGE_MARGIN), "center", "top"),
    ("bottom", (0.5, 0.0), (0, EDGE_MARGIN), "center", "bottom"),
)
#: Geometry of the slice view ruler (vtkMRMLRulerDisplayableManager), as fractions of the view height:
#: distance of the ruler line from the bottom edge and length of its major ticks per ruler type
#: (thin: base, thick: 2x base). The ruler text is to the right of the ruler, not above its centre.
_RULER_LINE_MARGIN = 0.015
_RULER_TICK_LENGTHS = {1: 0.015, 2: 0.03}  # vtkMRMLAbstractViewNode.RulerTypeThin, RulerTypeThick

#: Display statement (bottom-right) and figure tag (bottom-left), above the lines of the corner annotation in their corner
STATEMENT_FONT_SIZE = 11
STATEMENT_COLOR = LETTER_COLOR
STATEMENT_OPACITY = 1.0
#: The figure tag keeps its size: it is the only cue of a figure view
TAG_FONT_SIZE = 12
TAG_COLOR = (0.78, 0.78, 0.82)
#: Opacity of the dark box behind the figure tag and the figure side letters (a figure is mostly white)
FIGURE_BACKGROUND_OPACITY = 0.6
#: Position of the lower-left text of a vtkCornerAnnotation (SetTextActorsPosition) and the gap above it
CORNER_ANNOTATION_MARGIN = 5
STATEMENT_GAP = 6
#: |x| of the screen-right direction from which a view is "axis-aligned" for the statement
STATEMENT_AXIS_THRESHOLD = 0.9

_controller = None
_quitConnected = False


def displayStatement(sliceToRAS, short=False):
    """Plain-words display statement of a view: "Radiological · L on right" or "Neurological · L on left".

    ``sliceToRAS`` is the ``vtkMatrix4x4`` SliceToRAS of the slice node. The statement exists only when the
    screen-right direction (column 0) is along x (|x| > 0.9): axial and coronal views, flipped or not. An empty
    string is returned for sagittal and oblique views. ``short``: the side alone ("L on right"), for narrow views.
    """
    x = sliceToRAS.GetElement(0, 0)
    length = math.sqrt(sum(sliceToRAS.GetElement(row, 0) ** 2 for row in range(3)))
    if length < 1e-9 or abs(x) / length <= STATEMENT_AXIS_THRESHOLD:
        return ""
    if short:
        return _("L on right") if x < 0 else _("L on left")
    return _("Radiological · L on right") if x < 0 else _("Neurological · L on left")


def figureTag(convention):
    """Tag of a figure view for the convention of its figure ("radiological", "neurological", else unknown)."""
    if convention in ("radiological", "neurological"):
        return _("Figure · patient right on the left")
    return _("Figure · side unknown")


def directionLetters(direction):
    """Return the anatomical letters of a RAS direction (see the module docstring for the rule).

    ``directionLetters((-1, 0, 0))`` is "L", ``directionLetters((0.7071, 0.7071, 0))`` is "RA".
    An empty string is returned for a null vector.
    """
    length = math.sqrt(sum(component * component for component in direction))
    if length < 1e-9:
        return ""
    unit = [component / length for component in direction]
    # Largest component first; equal components (within rounding) in R, A, S order
    order = sorted(range(3), key=lambda axis: -round(abs(unit[axis]), 6))
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


def bottomReference(rulerType):
    """Reference point (normalized viewport coordinates) of the bottom letter for a ruler type.

    Without a ruler the letter is at the bottom edge; with a ruler it is above the ruler's major ticks.
    """
    tickLength = _RULER_TICK_LENGTHS.get(rulerType)
    if tickLength is None:
        return (0.5, 0.0)
    return (0.5, _RULER_LINE_MARGIN + tickLength)


def _rulerType(sliceNode):
    """The ruler type of a slice node (0: none)."""
    return sliceNode.GetRulerType() if hasattr(sliceNode, "GetRulerType") else 0


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


def _spaceModule():
    """StudioLib.space (figure views), None if this build does not have it."""
    try:
        from . import space
    except Exception:
        return None
    return space


class _ViewLetters:
    """The edge letters, the display statement and the figure tag of one slice view."""

    def __init__(self, controller, sliceViewName, sliceWidget):
        self.controller = controller
        self.name = sliceViewName
        self.sliceWidget = sliceWidget
        self.sliceView = sliceWidget.sliceView()
        self.sliceNode = sliceWidget.mrmlSliceNode()
        self.renderer = self._findRenderer()
        self.directionKey = None
        self.figureKey = None
        self.rulerType = 0
        self.letters = ("", "", "", "")
        #: The patient directions of a figure whose convention was answered: ("R", "L"), else ("", "")
        self.figureLetters = ("", "")
        self.statement = ""
        #: The short form of the statement ("L on right") and the text shown: a narrow view shows the short form, or
        #: raises the statement above the bottom letter when even that would touch it
        self.shortStatement = ""
        self.shownStatement = ""
        #: {(text, font size, bold): (width, height)} of measured texts, in display pixels
        self.textSizes = {}
        self.tag = ""
        self.isFigure = False
        self.convention = None
        #: {corner position: (text, height in pixels)} of the lower corners of the corner annotation
        self.cornerTexts = {}
        #: Heights of the corner text under the statement (lower right) and under the figure tag (lower left)
        self.statementOffset = 0
        self.tagOffset = 0
        self.actors = []
        for edgeName, reference, offset, horizontal, vertical in EDGES:
            self.actors.append(self._createActor(edgeName, reference, offset, horizontal, vertical))
        #: Side letters of a figure whose convention is known (left edge: R, right edge: L)
        self.figureActors = [self._createActor(edgeName, reference, offset, horizontal, vertical, "StudioFigureLetter_")
                             for edgeName, reference, offset, horizontal, vertical in EDGES[:2]]
        self.statementActor = self._createTextActor("StudioOrientationStatement", (1.0, 0.0), (-CORNER_ANNOTATION_MARGIN, CORNER_ANNOTATION_MARGIN),
                                                    "right", "bottom", STATEMENT_FONT_SIZE, False, STATEMENT_COLOR, STATEMENT_OPACITY)
        self.tagActor = self._createTextActor("StudioFigureTag", (0.0, 0.0), (CORNER_ANNOTATION_MARGIN, 0),
                                              "left", "bottom", TAG_FONT_SIZE, False, TAG_COLOR, 1.0, True)
        self.otherActors = [*self.figureActors, self.statementActor, self.tagActor]
        for actor in (*self.actors, *self.otherActors):
            self.renderer.AddViewProp(actor)
        self.observerTag = self.sliceNode.AddObserver(vtk.vtkCommand.ModifiedEvent, self._onSliceNodeModified)
        # The composite node (background volume) and the background node (the attributes of a figure)
        self.compositeNode = None
        self.compositeTag = None
        self.backgroundNode = None
        self.backgroundTag = None
        self.backgroundId = None
        self._bindComposite()
        self.backgroundId = self.compositeNode.GetBackgroundVolumeID() if self.compositeNode is not None else None
        self._bindBackground()

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
    def _createTextActor(objectName, reference, offset, horizontal, vertical, fontSize, bold, color, opacity, background=False):
        actor = vtk.vtkTextActor()
        if hasattr(actor, "SetObjectName"):
            actor.SetObjectName(objectName)
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
        textProperty.SetFontSize(fontSize)
        textProperty.SetBold(bold)
        textProperty.ShadowOn()
        textProperty.SetColor(*color)
        textProperty.SetOpacity(opacity)
        if background and hasattr(textProperty, "SetBackgroundOpacity"):
            # A figure is mostly white: a translucent dark box keeps the light text readable
            textProperty.SetBackgroundColor(0.0, 0.0, 0.0)
            textProperty.SetBackgroundOpacity(FIGURE_BACKGROUND_OPACITY)
        {"left": textProperty.SetJustificationToLeft, "right": textProperty.SetJustificationToRight,
         "center": textProperty.SetJustificationToCentered}[horizontal]()
        {"top": textProperty.SetVerticalJustificationToTop, "bottom": textProperty.SetVerticalJustificationToBottom,
         "center": textProperty.SetVerticalJustificationToCentered}[vertical]()
        actor.SetInput("")
        return actor

    @classmethod
    def _createActor(cls, edgeName, reference, offset, horizontal, vertical, prefix="StudioOrientationLetter_"):
        return cls._createTextActor(f"{prefix}{edgeName}", reference, offset, horizontal, vertical, FONT_SIZE, True, LETTER_COLOR,
                                    LETTER_OPACITY if prefix == "StudioOrientationLetter_" else 1.0, prefix != "StudioOrientationLetter_")

    def _onSliceNodeModified(self, caller, event):
        # Called for every slice node change (slice offset, pan, zoom...): only mark the view, the
        # comparison of the matrices runs once in the controller's 0 ms timer
        self.controller.markDirty(self.name)

    def _onCompositeModified(self, caller, event):
        # Background volume changes: figure state and re-centring, in the controller's 0 ms timer
        self.controller.markDirty(self.name)

    def _bindComposite(self):
        """Observe the composite node of the view (it is replaced when the scene is closed)."""
        compositeNode = self.sliceWidget.mrmlSliceCompositeNode() if hasattr(self.sliceWidget, "mrmlSliceCompositeNode") else None
        if compositeNode == self.compositeNode:
            return
        if self.compositeNode is not None and self.compositeTag is not None:
            self.compositeNode.RemoveObserver(self.compositeTag)
        self.compositeNode = compositeNode
        self.compositeTag = None
        if compositeNode is not None:
            self.compositeTag = compositeNode.AddObserver(vtk.vtkCommand.ModifiedEvent, self._onCompositeModified)

    def _bindBackground(self):
        """Observe the background volume of the view (its attributes: Studio.Role, Studio.FigureConvention)."""
        node = slicer.mrmlScene.GetNodeByID(self.backgroundId) if self.backgroundId else None
        if node == self.backgroundNode:
            return
        if self.backgroundNode is not None and self.backgroundTag is not None:
            self.backgroundNode.RemoveObserver(self.backgroundTag)
        self.backgroundNode = node
        self.backgroundTag = None
        if node is not None:
            self.backgroundTag = node.AddObserver(vtk.vtkCommand.ModifiedEvent, self._onCompositeModified)

    def isCurrent(self, sliceWidget):
        """True if this object still shows the letters of ``sliceWidget`` (same widget and slice node)."""
        return (_isAlive(self.sliceView) and sliceWidget is not None and sliceWidget.sliceView() == self.sliceView
                and sliceWidget.mrmlSliceNode() == self.sliceNode)

    def _updateBottomPosition(self):
        """Raise the bottom letter above the ruler when the ruler type changed. Return True if it moved."""
        rulerType = _rulerType(self.sliceNode)
        if rulerType == self.rulerType:
            return False
        self.rulerType = rulerType
        reference = bottomReference(rulerType)
        self.actors[3].GetPositionCoordinate().GetReferenceCoordinate().SetValue(reference[0], reference[1])
        return True

    def _figureKey(self):
        """What decides whether the view shows a figure and which convention it has (cheap: no scene search)."""
        node = self.backgroundNode
        if node is None:
            return None
        sliceToRAS = self.sliceNode.GetSliceToRAS()
        normal = tuple(round(sliceToRAS.GetElement(row, 2), 3) for row in range(3))
        return (node.GetID(), node.GetAttribute("Studio.Role"), node.GetAttribute("Studio.FigureConvention"), normal)

    def _recentre(self):
        """Move the slice offset into the new background volume (half a voxel inside the nearest bound).

        Only a view whose offset is outside the volume along the view normal is moved. Figure views are skipped.
        Return True if the offset changed.
        """
        if self.isFigure or self.backgroundNode is None:
            return False
        sliceLogic = self.sliceWidget.sliceLogic()
        bounds = [0.0] * 6
        sliceLogic.GetBackgroundSliceBounds(bounds)
        low, high = bounds[4], bounds[5]
        if not high > low:
            return False
        offset = self.sliceNode.GetSliceOffset()
        if low <= offset <= high:
            return False
        spacing = sliceLogic.GetBackgroundSliceSpacing()
        half = min(0.5 * abs(spacing[2]), 0.5 * (high - low))
        sliceLogic.SetSliceOffset(low + half if offset < low else high - half)
        return True

    def _cornerTextHeight(self, position=0):
        """Height in pixels of the text of a lower corner of the corner annotation (0: lower left, 1: lower right; 0 if it
        has none). The last measure of each corner is kept until its text changes.
        """
        cornerAnnotation = self.sliceView.cornerAnnotation()
        text = cornerAnnotation.GetText(position) if cornerAnnotation is not None else ""
        cached = self.cornerTexts.get(position)
        if cached is not None and cached[0] == text:
            return cached[1]
        height = 0
        if text.strip():
            probe = vtk.vtkTextActor()
            probe.GetTextProperty().ShallowCopy(cornerAnnotation.GetTextProperty())
            probe.GetTextProperty().SetFontSize(max(cornerAnnotation.GetMinimumFontSize(), min(cornerAnnotation.GetMaximumFontSize(), 14)))
            probe.SetInput(text)
            box = [0.0] * 4
            probe.GetBoundingBox(self.renderer, box)
            height = math.ceil(box[3] - box[2])
        self.cornerTexts[position] = (text, height)
        return height

    def _textSize(self, actor, text):
        """(width, height) in display pixels of ``text`` in the font of ``actor`` (measured once per text and font)."""
        textProperty = actor.GetTextProperty()
        key = (text, textProperty.GetFontSize(), bool(textProperty.GetBold()))
        size = self.textSizes.get(key)
        if size is None:
            probe = vtk.vtkTextActor()
            probe.GetTextProperty().ShallowCopy(textProperty)
            probe.SetInput(text)
            box = [0.0] * 4
            probe.GetBoundingBox(self.renderer, box)
            size = (math.ceil(box[1] - box[0]), math.ceil(box[3] - box[2]))
            self.textSizes[key] = size
        return size

    def _fitStatement(self, statementY):
        """The statement text and its height above the bottom for the view width: the full statement when it clears the
        bottom letter, else the short form, else the short form raised above the letter.
        """
        bottomLetter = self.letters[3]
        if not self.statement or not bottomLetter:
            return self.statement, statementY
        width, height = self.renderer.GetSize()
        letterWidth, letterHeight = self._textSize(self.actors[3], bottomLetter)
        letterBottom = bottomReference(self.rulerType)[1] * height + EDGE_MARGIN
        letterTop = letterBottom + letterHeight
        # Room right of the bottom letter (centred in the view) for the right-aligned statement
        room = width / 2.0 - letterWidth / 2.0 - STATEMENT_GAP - CORNER_ANNOTATION_MARGIN
        for text in (self.statement, self.shortStatement):
            textWidth, textHeight = self._textSize(self.statementActor, text)
            if textWidth <= room or statementY >= letterTop or statementY + textHeight <= letterBottom:
                return text, statementY
        return self.shortStatement, math.ceil(letterTop) + STATEMENT_GAP

    def _placeStatement(self):
        """Put the statement above the lower-right lines of the corner annotation and the tag above its lower-left lines;
        in a narrow view the statement is shortened or raised so that it never runs into the bottom letter.

        Return True if they moved or the statement text changed.
        """
        previous = (self.statementOffset, self.tagOffset, self.shownStatement, tuple(self.statementActor.GetPositionCoordinate().GetValue()[:2]))
        self.statementOffset = self._cornerTextHeight(1) if self.statement else self.statementOffset
        self.tagOffset = self._cornerTextHeight(0) if self.tag else self.tagOffset
        statementY = CORNER_ANNOTATION_MARGIN + (self.statementOffset + STATEMENT_GAP if self.statementOffset else 0)
        tagY = CORNER_ANNOTATION_MARGIN + (self.tagOffset + STATEMENT_GAP if self.tagOffset else 0)
        shown, statementY = self._fitStatement(statementY)
        if shown != self.shownStatement:
            self.shownStatement = shown
            self.statementActor.SetInput(shown)
        self.statementActor.GetPositionCoordinate().SetValue(-CORNER_ANNOTATION_MARGIN, statementY)
        self.tagActor.GetPositionCoordinate().SetValue(CORNER_ANNOTATION_MARGIN, tagY)
        current = (self.statementOffset, self.tagOffset, self.shownStatement, tuple(self.statementActor.GetPositionCoordinate().GetValue()[:2]))
        return current != previous

    def _updateFigureState(self):
        """Re-evaluate whether the view shows a figure (and its convention). Return True if the figure state changed."""
        space = _spaceModule()
        figure = False
        convention = None
        if space is not None and self.backgroundNode is not None:
            try:
                figure = bool(space.isFigureView(self.name))
                convention = space.figureConvention(self.backgroundNode) if figure else None
            except Exception:
                logging.error(f"Studio: figure state of {self.name} failed:\n{traceback.format_exc()}")
        changed = (figure, convention) != (self.isFigure, self.convention)
        self.isFigure = figure
        self.convention = convention
        return changed

    def update(self):
        """Update the letters, statement and tag if the view, its background or its figure state changed.

        Return True if a render was requested.
        """
        changed = self._updateBottomPosition()
        self._bindComposite()
        backgroundId = self.compositeNode.GetBackgroundVolumeID() if self.compositeNode is not None else None
        backgroundChanged = backgroundId != self.backgroundId
        if backgroundChanged:
            self.backgroundId = backgroundId
        self._bindBackground()
        sliceToRAS = self.sliceNode.GetSliceToRAS()
        key = _directionKey(sliceToRAS)
        figureKey = self._figureKey()
        if key != self.directionKey or figureKey != self.figureKey or backgroundChanged:
            self.figureKey = figureKey
            self._updateFigureState()
            if backgroundChanged and self._recentre():
                changed = True
            self.directionKey = key
            if self.isFigure:
                answered = self.convention in ("radiological", "neurological")
                letters = ("", "", "", "")
                figureLetters = ("R", "L") if answered else ("", "")
                statement = ""
                tag = figureTag(self.convention)
            else:
                letters = edgeLetters(sliceToRAS)
                figureLetters = ("", "")
                statement = displayStatement(sliceToRAS)
                self.shortStatement = displayStatement(sliceToRAS, short=True)
                tag = ""
            if letters != self.letters:
                self.letters = letters
                for actor, text in zip(self.actors, letters, strict=True):
                    actor.SetInput(text)
                changed = True
            if figureLetters != self.figureLetters:
                self.figureLetters = figureLetters
                for actor, text in zip(self.figureActors, figureLetters, strict=True):
                    actor.SetInput(text)
                changed = True
            if statement != self.statement:
                self.statement = statement
                if not statement:
                    self.shownStatement = ""
                    self.statementActor.SetInput("")
                changed = True
            if tag != self.tag:
                self.tag = tag
                self.tagActor.SetInput(tag)
                changed = True
        if (self.statement or self.tag) and self._placeStatement():
            changed = True
        if not changed:
            return False
        if _isAlive(self.sliceView):
            self.sliceView.scheduleRender()
        return True

    def remove(self):
        if self.observerTag is not None:
            self.sliceNode.RemoveObserver(self.observerTag)
            self.observerTag = None
        if self.compositeNode is not None and self.compositeTag is not None:
            self.compositeNode.RemoveObserver(self.compositeTag)
        self.compositeNode = None
        self.compositeTag = None
        if self.backgroundNode is not None and self.backgroundTag is not None:
            self.backgroundNode.RemoveObserver(self.backgroundTag)
        self.backgroundNode = None
        self.backgroundTag = None
        for actor in (*self.actors, *self.otherActors):
            self.renderer.RemoveViewProp(actor)
        self.actors = []
        self.figureActors = []
        self.otherActors = []
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
        # A closed or imported scene replaces the composite and background nodes of the views
        self.sceneTags = [slicer.mrmlScene.AddObserver(event, self._onSceneChanged)
                          for event in (slicer.vtkMRMLScene.EndCloseEvent, slicer.vtkMRMLScene.EndImportEvent)]

    def _onSceneChanged(self, caller=None, event=None):
        self.markDirty(None)

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
        for tag in self.sceneTags:
            slicer.mrmlScene.RemoveObserver(tag)
        self.sceneTags = []
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


def _isShellActive(shell):
    """True if the Studio shell is shown (letters are never shown in Classic or without a shell)."""
    return shell is not None and hasattr(shell, "isActive") and shell.isActive()


def setEnabled(shell, enabled):
    """Store the ``Studio/OrientationLetters`` setting and show or hide the letters in Studio.

    In Classic only the setting is stored: the letters appear the next time Studio is shown.
    """
    qt.QSettings().setValue(SETTINGS_KEY, bool(enabled))
    if enabled and _isShellActive(shell):
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
    if view is None:
        return None
    # A figure whose convention is answered shows R and L only
    return (*view.figureLetters, "", "") if view.isFigure else view.letters


def viewStatement(sliceViewName):
    """The display statement shown in a slice view ("Left is on screen right"), "" if it has none, None without letters."""
    if _controller is None:
        return None
    view = _controller.views.get(sliceViewName)
    return view.statement if view is not None else None


def viewShownStatement(sliceViewName):
    """The statement text a slice view shows now (the short form in a narrow view), "" if none, None without letters."""
    if _controller is None:
        return None
    view = _controller.views.get(sliceViewName)
    return view.shownStatement if view is not None else None


def viewTag(sliceViewName):
    """The figure tag shown in a slice view ("Figure · side unknown"), "" if it is not a figure, None without letters."""
    if _controller is None:
        return None
    view = _controller.views.get(sliceViewName)
    return view.tag if view is not None else None


def update():
    """Update the letters now instead of waiting for the application to process events (tests, captures)."""
    if _controller is None:
        return
    _controller.updateTimer.stop()
    _controller.dirty = set()
    _controller.synchronizeViews()


def install(shell):
    """Show the orientation letters in every slice view while the Studio shell is active.

    Nothing is shown in Classic, without a shell or if the ``Studio/OrientationLetters`` setting is off.
    """
    global _controller, _quitConnected
    uninstall(shell)
    if not _isShellActive(shell) or not isEnabled():
        return
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None:
        return
    _controller = _OrientationLetters(layoutManager)
    _controller.synchronizeViews()
    if not _quitConnected:
        # Observers with Python callbacks must not outlive the interpreter
        slicer.app.connect("aboutToQuit()", _onAboutToQuit)
        _quitConnected = True


def _onAboutToQuit():
    uninstall(None)


def uninstall(shell):
    """Remove all letters, observers and connections."""
    global _controller
    if _controller is None:
        return
    controller = _controller
    _controller = None
    controller.dispose()

"""View layouts of Slicer Studio (SPEC 6.3 and 9.7 rule 3).

The Studio layouts use user layout IDs of the range 7001-7099, which the fork reserves:

- 7001 "Studio Focus": a large Red axial view on the left, Green coronal over Yellow sagittal on the right;
- 7002 "Studio Focus + 3D": 7001 with a 3D view under Yellow;
- 7003 "Studio Row": Red, Green and Yellow side by side.

7004 is reserved for the Trace layout (L-29) and 7005 "Studio Compare" (template and figure) is defined by
``StudioLib.importer``.

The fork registers them in C++ (``vtkMRMLLayoutLogic``) in every mode, and ``vtkMRMLLayoutNode``
saves them in scenes with a portable fallback, so scenes stay readable by other Slicer versions.
``register()`` only adds the descriptions that the running build does not define, with the same
canonical XML (byte-identical to the C++ strings).
"""

import logging
import traceback

import qt
import slicer
import vtk
from slicer.i18n import tr as _

FOCUS_LAYOUT_ID = 7001
FOCUS_3D_LAYOUT_ID = 7002
ROW_LAYOUT_ID = 7003

#: First and last user layout IDs reserved by the fork
STUDIO_LAYOUT_ID_FIRST = 7001
STUDIO_LAYOUT_ID_LAST = 7099

_RED_VIEW = (
    '<view class="vtkMRMLSliceNode" singletontag="Red">'
    '<property name="orientation" action="default">Axial</property>'
    '<property name="viewlabel" action="default">R</property>'
    '<property name="viewcolor" action="default">#F34A33</property>'
    "</view>")

_GREEN_VIEW = (
    '<view class="vtkMRMLSliceNode" singletontag="Green">'
    '<property name="orientation" action="default">Coronal</property>'
    '<property name="viewlabel" action="default">G</property>'
    '<property name="viewcolor" action="default">#6EB04B</property>'
    "</view>")

_YELLOW_VIEW = (
    '<view class="vtkMRMLSliceNode" singletontag="Yellow">'
    '<property name="orientation" action="default">Sagittal</property>'
    '<property name="viewlabel" action="default">Y</property>'
    '<property name="viewcolor" action="default">#EDD54C</property>'
    "</view>")

_THREE_D_VIEW = (
    '<view class="vtkMRMLViewNode" singletontag="1">'
    '<property name="viewlabel" action="default">1</property>'
    "</view>")

FOCUS_LAYOUT_XML = (
    '<layout type="horizontal" split="true">'
    '<item splitSize="720">' + _RED_VIEW + "</item>"
    '<item splitSize="280">'
    '<layout type="vertical" split="true">'
    "<item>" + _GREEN_VIEW + "</item>"
    "<item>" + _YELLOW_VIEW + "</item>"
    "</layout>"
    "</item>"
    "</layout>")

FOCUS_3D_LAYOUT_XML = (
    '<layout type="horizontal" split="true">'
    '<item splitSize="720">' + _RED_VIEW + "</item>"
    '<item splitSize="280">'
    '<layout type="vertical" split="true">'
    "<item>" + _GREEN_VIEW + "</item>"
    "<item>" + _YELLOW_VIEW + "</item>"
    "<item>" + _THREE_D_VIEW + "</item>"
    "</layout>"
    "</item>"
    "</layout>")

ROW_LAYOUT_XML = (
    '<layout type="horizontal" split="true">'
    "<item>" + _RED_VIEW + "</item>"
    "<item>" + _GREEN_VIEW + "</item>"
    "<item>" + _YELLOW_VIEW + "</item>"
    "</layout>")

#: Layout ID -> (title, canonical XML description)
LAYOUTS = {
    FOCUS_LAYOUT_ID: ("Studio Focus", FOCUS_LAYOUT_XML),
    FOCUS_3D_LAYOUT_ID: ("Studio Focus + 3D", FOCUS_3D_LAYOUT_XML),
    ROW_LAYOUT_ID: ("Studio Row", ROW_LAYOUT_XML),
}


def isStudioLayout(layoutId):
    """Return True if ``layoutId`` is in the user layout range reserved by the fork."""
    return STUDIO_LAYOUT_ID_FIRST <= layoutId <= STUDIO_LAYOUT_ID_LAST


def layoutNode():
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None:
        return None
    return layoutManager.layoutLogic().GetLayoutNode()


def register(node=None):
    """Add the Studio layout descriptions that are not defined yet. Return the list of added IDs.

    Idempotent: layouts already defined (by the C++ layout logic of the fork) are left untouched.
    """
    if node is None:
        node = layoutNode()
    if node is None:
        return []
    added = []
    for layoutId, (title, description) in LAYOUTS.items():
        if not node.IsLayoutDescription(layoutId):
            node.AddLayoutDescription(layoutId, description)
            added.append(layoutId)
    return added


#
# Layout picker
#

_SINGLE_LAYOUTS = {
    "Red": slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView,
    "Yellow": slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpYellowSliceView,
    "Green": slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpGreenSliceView,
}

#: Layout ID of "Studio Compare" (``importer.COMPARE_LAYOUT_ID``)
COMPARE_LAYOUT_ID = 7005

#: Entries of the top bar layout picker: (key, title, shortcut, Lucide icon, layout ID).
#: "single" has no layout ID: it maximizes a slice view inside the current layout (``toggleSingle()``).
PICKER_ENTRIES = (
    ("focus", "Focus", "Alt+1", "layout-panel-left", FOCUS_LAYOUT_ID),
    ("single", "Single", "Alt+2", "square", None),
    ("fourup", "Four-up", "Alt+3", "layout-grid", slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView),
    ("row", "Row", "Alt+4", "columns-3", ROW_LAYOUT_ID),
    ("focus3d", "Focus + 3D", "Alt+5", "box", FOCUS_3D_LAYOUT_ID),
)

#: The Compare entry of the picker: listed only while a figure is shown (``figureShown()``). It has no key.
COMPARE_ENTRY = ("compare", "Compare", "", "between-horizontal-start", COMPARE_LAYOUT_ID)

#: One line for each tile tooltip ("Name · key · description")
PICKER_DESCRIPTIONS = {
    "focus": "One large axial view, with coronal and sagittal beside it",
    "single": "Enlarge the view you worked in; press again or Esc to restore",
    "fourup": "Axial, coronal, sagittal and 3D",
    "row": "Axial, coronal and sagittal side by side",
    "focus3d": "Focus with a 3D view",
    "compare": "The template where you draw, next to the figure",
}


def singleLayoutId(sliceViewName):
    """One-up layout of a slice view (Red when the view has no one-up layout). Kept for scripts: "Single" now maximizes."""
    return _SINGLE_LAYOUTS.get(sliceViewName, _SINGLE_LAYOUTS["Red"])


def figureShown():
    """True while a figure is shown in a slice view (the Compare layout is then listed in the picker)."""
    try:
        from . import space

        if space.figureViewNames():
            return True
    except Exception:
        logging.error(f"Studio: figure views check failed:\n{traceback.format_exc()}")
    try:
        from . import importer

        return importer.figureController() is not None
    except Exception:
        return False


def pickerEntries(includeCompare=None):
    """The entries of the picker: ``PICKER_ENTRIES`` and, while a figure is shown, ``COMPARE_ENTRY``."""
    if includeCompare is None:
        includeCompare = figureShown()
    return PICKER_ENTRIES + ((COMPARE_ENTRY,) if includeCompare else ())


def layoutName(layoutId):
    """Name of a layout for messages: the title of a picker entry, else the name in the Layout menu, else "Layout <id>"."""
    for key, title, shortcut, iconName, entryLayoutId in PICKER_ENTRIES + (COMPARE_ENTRY,):
        if entryLayoutId == layoutId:
            return title
    mainWindow = slicer.util.mainWindow()
    layoutMenu = mainWindow.findChild("QMenu", "LayoutMenu") if mainWindow is not None else None
    if layoutMenu is not None:
        for action in layoutMenu.actions():
            try:
                if int(action.data()) == layoutId:
                    return str(action.text).replace("&", "")
            except (TypeError, ValueError):
                continue
    if layoutId in LAYOUTS:
        return LAYOUTS[layoutId][0]
    return _("Layout {id}").format(id=layoutId)


#
# The slice view that "Single" enlarges
#


def sliceViewNamesInLayout():
    """Names of the slice views that the current layout shows (``layoutManager.sliceViewNames()`` also lists the views of
    other layouts that were created earlier).
    """
    _node, layoutManager = _layoutNodeAndManager()
    if layoutManager is None:
        return []
    viewNodes = layoutManager.layoutLogic().GetViewNodes()
    names = []
    for index in range(viewNodes.GetNumberOfItems() if viewNodes is not None else 0):
        node = viewNodes.GetItemAsObject(index)
        if node.IsA("vtkMRMLSliceNode"):
            names.append(node.GetName())
    return names


def threeDViewNamesInLayout():
    """Names of the 3D views that the current layout shows (``layoutManager.threeDViewCount`` counts the views that were
    created, also the ones of other layouts).
    """
    _node, layoutManager = _layoutNodeAndManager()
    if layoutManager is None:
        return []
    viewNodes = layoutManager.layoutLogic().GetViewNodes()
    names = []
    for index in range(viewNodes.GetNumberOfItems() if viewNodes is not None else 0):
        node = viewNodes.GetItemAsObject(index)
        if node.IsA("vtkMRMLViewNode"):
            names.append(node.GetName())
    return names


class HoveredSliceViewTracker(qt.QObject):
    """Remember which slice view the user is in: the one under the pointer and the one worked in last.

    VTK observers (never aborting; a priority above the one of the slice interactor style, which handles and aborts
    wheel and button events) on the interactor of every slice view
    record ``hoveredNow`` (Enter and Leave) and ``lastInteracted`` (wheel, left, right and middle button
    press, key press); the observers are installed again after every layout change. There is no
    per-mouse-move work in Python.

    ``singleTarget()`` is the view that "Single" enlarges: the hovered one, else the one used last, else
    "Red". The layout picker is clicked outside the views, so it enlarges the view used last; Alt+2 over a
    view enlarges the hovered one.
    """

    #: Priority of the observers: before the slice interactor style (0) and the figure guard (2), which abort events
    PRIORITY = 10.0

    #: Interactor events that mean "the user works in this view"
    INTERACTION_EVENTS = ("MouseWheelForwardEvent", "MouseWheelBackwardEvent", "LeftButtonPressEvent",
                          "RightButtonPressEvent", "MiddleButtonPressEvent", "KeyPressEvent")

    def __init__(self):
        qt.QObject.__init__(self)
        self.hoveredNow = None
        self.lastInteracted = None
        #: The slice view that the pointer entered or the user worked in last (the target of F6 "views")
        self.lastSliceViewName = "Red"
        self._observed = {}
        self._layoutManager = slicer.app.layoutManager()
        if self._layoutManager is not None:
            self._layoutManager.connect("layoutChanged(int)", self._onLayoutChanged)
        self._onLayoutChanged()

    @staticmethod
    def _interactor(sliceWidget):
        sliceView = sliceWidget.sliceView()
        interactor = sliceView.interactor() if hasattr(sliceView, "interactor") else None
        if interactor is None:
            style = sliceView.interactorStyle()
            interactor = style.GetInteractor() if style is not None else None
        return interactor

    def _onLayoutChanged(self, layoutId=None):
        if self._layoutManager is None:
            return
        names = list(self._layoutManager.sliceViewNames())
        for name in list(self._observed):
            if name not in names:
                self._removeObservers(name)
        for name in names:
            sliceWidget = self._layoutManager.sliceWidget(name)
            interactor = self._interactor(sliceWidget) if sliceWidget is not None else None
            if interactor is None:
                continue
            known = self._observed.get(name)
            if known is not None:
                if known[0] == interactor:
                    continue
                self._removeObservers(name)
            tags = [interactor.AddObserver(vtk.vtkCommand.EnterEvent, lambda caller, event, name=name: self._onEnter(name), self.PRIORITY),
                    interactor.AddObserver(vtk.vtkCommand.LeaveEvent, lambda caller, event, name=name: self._onLeave(name), self.PRIORITY)]
            for eventName in self.INTERACTION_EVENTS:
                tags.append(interactor.AddObserver(getattr(vtk.vtkCommand, eventName), lambda caller, event, name=name: self._onInteraction(name), self.PRIORITY))
            self._observed[name] = (interactor, tags)
        if self.hoveredNow is not None and self.hoveredNow not in names:
            self.hoveredNow = None

    def _removeObservers(self, name):
        interactor, tags = self._observed.pop(name, (None, []))
        for tag in tags:
            try:
                interactor.RemoveObserver(tag)
            except Exception:
                # The interactor was deleted with its view
                pass

    def _onEnter(self, name):
        self.hoveredNow = name
        self.lastSliceViewName = name

    def _onLeave(self, name):
        if self.hoveredNow == name:
            self.hoveredNow = None

    def _onInteraction(self, name):
        self.lastInteracted = name
        self.lastSliceViewName = name

    def singleTarget(self):
        """Name of the slice view that "Single" enlarges (always a view of the current layout when there is one)."""
        name = self.hoveredNow or self.lastInteracted or "Red"
        names = sliceViewNamesInLayout()
        if names and name not in names:
            name = "Red" if "Red" in names else names[0]
        return name

    def singleLayoutId(self):
        """One-up layout of the target (kept for scripts)."""
        return singleLayoutId(self.singleTarget())

    def dispose(self):
        for name in list(self._observed):
            self._removeObservers(name)
        if self._layoutManager is not None:
            self._layoutManager.disconnect("layoutChanged(int)", self._onLayoutChanged)
            self._layoutManager = None


def pickerLayoutId(key, tracker=None):
    """Layout ID of a picker entry key ("focus", "fourup", "row", "focus3d" or "compare").

    "single" is not a layout: this returns the one-up layout of the target view, for scripts that want one.
    """
    for entryKey, title, shortcut, iconName, layoutId in PICKER_ENTRIES + (COMPARE_ENTRY,):
        if entryKey != key:
            continue
        if layoutId is not None:
            return layoutId
        return tracker.singleLayoutId() if tracker is not None else singleLayoutId("Red")
    raise ValueError(f"Unknown layout picker entry: {key}")


#
# Single: one enlarged view, one exit
#


def _layoutNodeAndManager():
    layoutManager = slicer.app.layoutManager()
    layoutNode = layoutManager.layoutLogic().GetLayoutNode() if layoutManager is not None else None
    return layoutNode, layoutManager


def maximizedViewNames():
    """Names of the views that are maximized in the current layout (usually zero or one)."""
    layoutNode, _manager = _layoutNodeAndManager()
    if layoutNode is None:
        return []
    return [layoutNode.GetMaximizedViewNode(index).GetName() for index in range(layoutNode.GetNumberOfMaximizedViewNodes())]


def isMaximized():
    """True while a view is maximized in the current layout (by "Single", a double-click or the view bar button)."""
    layoutNode, _manager = _layoutNodeAndManager()
    return layoutNode is not None and layoutNode.GetNumberOfMaximizedViewNodes() > 0


def restoreMaximized():
    """Restore the layout of a maximized view. Return True if there was one."""
    layoutNode, layoutManager = _layoutNodeAndManager()
    if layoutNode is None or layoutNode.GetNumberOfMaximizedViewNodes() == 0:
        return False
    layoutManager.removeAllMaximizedViewNodes()
    return True


def maximizeView(viewName):
    """Maximize the slice view ``viewName`` inside the current layout. Return its name, or None if there is no slice view."""
    _node, layoutManager = _layoutNodeAndManager()
    if layoutManager is None:
        return None
    names = sliceViewNamesInLayout()
    if not names:
        return None
    if viewName not in names:
        viewName = "Red" if "Red" in names else names[0]
    sliceNode = layoutManager.sliceWidget(viewName).mrmlSliceNode()
    layoutManager.removeAllMaximizedViewNodes()
    layoutManager.addMaximizedViewNode(sliceNode)
    return viewName


def toggleSingle(tracker=None):
    """"Single": maximize the target view, or restore the layout when a view is maximized. Return the maximized view name or None."""
    if restoreMaximized():
        return None
    name = tracker.singleTarget() if tracker is not None else "Red"
    return maximizeView(name)


def applyPickerEntry(key, tracker=None):
    """Apply an entry of the picker ("focus", "single", "fourup", "row", "focus3d" or "compare").

    Every entry but "single" leaves a maximized view first (one enlarged state, one exit), then sets its layout.
    Return the layout ID that is current afterwards, or None when there is no layout manager.
    """
    layoutNode, layoutManager = _layoutNodeAndManager()
    if layoutManager is None:
        return None
    if key == "single":
        toggleSingle(tracker)
        return layoutManager.layout
    layoutId = pickerLayoutId(key, tracker)
    if layoutId >= STUDIO_LAYOUT_ID_FIRST and not layoutNode.IsLayoutDescription(layoutId):
        if layoutId == COMPARE_LAYOUT_ID:
            try:
                from . import importer

                importer.ensureCompareLayout()
            except Exception:
                logging.error(f"Studio: cannot register the Compare layout:\n{traceback.format_exc()}")
        else:
            register(layoutNode)
    restoreMaximized()
    layoutManager.setLayout(layoutId)
    return layoutManager.layout


#
# View commands of the layout popover (crosshair, intersections, link, reset)
#

#: The last crosshair mode that showed something (it comes back when the crosshair is turned on again)
_lastCrosshairMode = None


def crosshairNode():
    return slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")


def crosshairVisible():
    node = crosshairNode()
    return node is not None and node.GetCrosshairMode() != slicer.vtkMRMLCrosshairNode.NoCrosshair


def setCrosshairVisible(visible):
    """Show the crosshair (the mode that was shown last, else the basic one) or hide it."""
    global _lastCrosshairMode
    node = crosshairNode()
    if node is None:
        return
    none = slicer.vtkMRMLCrosshairNode.NoCrosshair
    if visible:
        node.SetCrosshairMode(_lastCrosshairMode if _lastCrosshairMode not in (None, none) else slicer.vtkMRMLCrosshairNode.ShowBasic)
    else:
        if node.GetCrosshairMode() != none:
            _lastCrosshairMode = node.GetCrosshairMode()
        node.SetCrosshairMode(none)


def intersectionsVisible():
    logic = slicer.app.applicationLogic()
    return bool(logic.GetIntersectingSlicesEnabled(slicer.vtkMRMLApplicationLogic.IntersectingSlicesVisibility))


def setIntersectionsVisible(visible):
    slicer.app.applicationLogic().SetIntersectingSlicesEnabled(slicer.vtkMRMLApplicationLogic.IntersectingSlicesVisibility, bool(visible))


def _sliceCompositeNodes(exceptFigures=True):
    from . import space

    return [node for node in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode") if not (exceptFigures and space.isFigureView(node))]


def viewsLinked():
    """True if every slice view but the figure views is linked."""
    nodes = _sliceCompositeNodes()
    return bool(nodes) and all(node.GetLinkedControl() for node in nodes)


def setViewsLinked(linked):
    """Link or unlink the slice views. A figure view stays unlinked, so that the figure keeps its own slice (offset 0)."""
    for node in _sliceCompositeNodes():
        node.SetLinkedControl(bool(linked))


def resetViews():
    """Fit the slice views and the 3D views. A figure view keeps its slice (offset 0) and its mirroring, and is fitted to the figure."""
    from . import space

    _node, layoutManager = _layoutNodeAndManager()
    if layoutManager is None:
        return
    figures = {}
    for name in space.figureViewNames():
        sliceWidget = layoutManager.sliceWidget(name)
        if sliceWidget is not None:
            matrix = vtk.vtkMatrix4x4()
            matrix.DeepCopy(sliceWidget.mrmlSliceNode().GetSliceToRAS())
            figures[name] = matrix
    layoutManager.resetSliceViews()
    layoutManager.resetThreeDViews()
    for name, matrix in figures.items():
        sliceWidget = layoutManager.sliceWidget(name)
        sliceNode = sliceWidget.mrmlSliceNode()
        sliceNode.GetSliceToRAS().DeepCopy(matrix)
        sliceNode.UpdateMatrices()
        sliceWidget.sliceLogic().FitSliceToAll()

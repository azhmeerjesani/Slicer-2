"""View layouts of Slicer Studio (SPEC 6.3 and 9.7 rule 3).

The Studio layouts use user layout IDs of the range 7001-7099, which the fork reserves:

- 7001 "Studio Focus": a large Red axial view on the left, Green coronal over Yellow sagittal on the right;
- 7002 "Studio Focus + 3D": 7001 with a 3D view under Yellow;
- 7003 "Studio Row": Red, Green and Yellow side by side.

The fork registers them in C++ (``vtkMRMLLayoutLogic``) in every mode, and ``vtkMRMLLayoutNode``
saves them in scenes with a portable fallback, so scenes stay readable by other Slicer versions.
``register()`` only adds the descriptions that the running build does not define, with the same
canonical XML (byte-identical to the C++ strings).
"""

import qt
import slicer

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

#: Entries of the top bar layout picker: (key, title, shortcut, Lucide icon, layout ID).
#: The layout ID of "single" is None: it follows the slice view that was hovered last.
PICKER_ENTRIES = (
    ("focus", "Focus", "Alt+1", "layout-panel-left", FOCUS_LAYOUT_ID),
    ("single", "Single", "Alt+2", "square", None),
    ("fourup", "Four-up", "Alt+3", "layout-grid", slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView),
    ("row", "Row", "Alt+4", "columns-3", ROW_LAYOUT_ID),
)


def singleLayoutId(sliceViewName):
    """One-up layout of a slice view (Red when the view has no one-up layout)."""
    return _SINGLE_LAYOUTS.get(sliceViewName, _SINGLE_LAYOUTS["Red"])


class HoveredSliceViewTracker(qt.QObject):
    """Remember the slice view that the mouse entered last (for the "Single" layout).

    An Enter event filter is installed on the slice widgets of the current layout, and again
    after every layout change.
    """

    def __init__(self):
        qt.QObject.__init__(self)
        self.lastSliceViewName = "Red"
        self._watched = []
        self._layoutManager = slicer.app.layoutManager()
        if self._layoutManager is not None:
            self._layoutManager.connect("layoutChanged(int)", self._onLayoutChanged)
        self._onLayoutChanged()

    def _onLayoutChanged(self, layoutId=None):
        self._removeFilters()
        if self._layoutManager is None:
            return
        for name in self._layoutManager.sliceViewNames():
            sliceWidget = self._layoutManager.sliceWidget(name)
            if sliceWidget is None:
                continue
            sliceWidget.installEventFilter(self)
            self._watched.append(sliceWidget)

    def _removeFilters(self):
        for widget in self._watched:
            try:
                widget.removeEventFilter(self)
            except Exception:
                # the widget was deleted
                pass
        self._watched = []

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Enter:
            name = getattr(obj, "sliceViewName", None)
            if name:
                self.lastSliceViewName = name
        return False

    def singleLayoutId(self):
        return singleLayoutId(self.lastSliceViewName)

    def dispose(self):
        self._removeFilters()
        if self._layoutManager is not None:
            self._layoutManager.disconnect("layoutChanged(int)", self._onLayoutChanged)
            self._layoutManager = None


def pickerLayoutId(key, tracker=None):
    """Layout ID of a picker entry key ("focus", "single", "fourup" or "row")."""
    for entryKey, title, shortcut, iconName, layoutId in PICKER_ENTRIES:
        if entryKey != key:
            continue
        if layoutId is not None:
            return layoutId
        return tracker.singleLayoutId() if tracker is not None else singleLayoutId("Red")
    raise ValueError(f"Unknown layout picker entry: {key}")

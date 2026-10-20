"""Workspaces of Slicer Studio: Library, Draw and Map (SPEC 2.1 and 2.6).

A workspace is a module shown in the inspector (the module panel), plus a remembered view layout
and an optional hooks module. Hooks modules (``StudioLib.library``, ``StudioLib.draw``,
``StudioLib.mappage``) are imported lazily (``hooks(ws)``); they may define:

- ``enter(shell)`` and ``leave(shell)``;
- ``helpHtml(shell)``: ``(title, html)`` for the "?" popover of the panel header (the current tool), or None
  for the module help;
- ``moreMenuActions(shell)``: ``[(text, callback, checked or None)]``, extra items of the header "⋯" menu;
- ``focusFirst(shell)``: give the keyboard focus to the first control of the workspace after it is entered.

A missing hooks module means "no hooks", so later pages plug in without editing the shell.

Any other module selected in the inspector is "transient": no workspace tab is checked and the
panel header shows "← <workspace>", a button that returns to the workspace the module was opened from
(Esc does too, see ``Shell.handleEscape()``). The keyboard focus starts on the module panel itself.
"""

import importlib
import logging
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import layouts, ui


class Workspace:
    """Description of a workspace.

    :param key: identifier used by ``shell.switchTo()`` ("library", "draw" or "map").
    :param title: name shown in the top bar, also used in settings keys (``Studio/<title>/Layout``).
    :param moduleName: module shown in the inspector.
    :param iconName: Lucide icon name.
    :param hooksModule: Python module that defines the optional ``enter(shell)``/``leave(shell)`` hooks.
    :param defaultLayout: view layout of the workspace, None to keep the current layout (Library).
    :param caption: what the workspace is, shown after the title in the panel header ("Draw · Segment Editor").
    """

    def __init__(self, key, title, moduleName, iconName, hooksModule=None, defaultLayout=None, caption=None):
        self.key = key
        self.title = title
        self.moduleName = moduleName
        self.iconName = iconName
        self.hooksModule = hooksModule
        self.defaultLayout = defaultLayout
        self.caption = caption

    @property
    def layoutSettingsKey(self):
        return f"Studio/{self.title}/Layout"

    def __repr__(self):
        return f"Workspace({self.key!r})"


WORKSPACES = (
    Workspace("library", "Library", "Studio", "library", hooksModule="StudioLib.library", caption=_("Open data")),
    Workspace("draw", "Draw", "SegmentEditor", "brush", hooksModule="StudioLib.draw", defaultLayout=layouts.FOCUS_LAYOUT_ID,
              caption=_("Segment Editor")),
    Workspace("map", "Map", "LesionNetworkMapping", "waypoints", hooksModule="StudioLib.mappage", defaultLayout=layouts.FOCUS_LAYOUT_ID,
              caption=_("Lesion network mapping")),
)


def workspace(key):
    """Return the workspace of a key or title (case insensitive), or None."""
    if not key:
        return None
    key = str(key).lower()
    for ws in WORKSPACES:
        if key in (ws.key, ws.title.lower()):
            return ws
    return None


def workspaceForModule(moduleName):
    """Return the workspace that shows the module ``moduleName``, or None."""
    for ws in WORKSPACES:
        if ws.moduleName == moduleName:
            return ws
    return None


def moduleExists(moduleName):
    """Return True if the module is loaded (without the warning of qSlicerModuleManager::module())."""
    return hasattr(slicer.modules, moduleName.lower())


def isAvailable(ws):
    """Return True if the module of the workspace is loaded in this application."""
    return moduleExists(ws.moduleName)


_hooksCache = {}


def hooks(ws):
    """Return the hooks module of a workspace (imported once), or None if there is none."""
    if ws.hooksModule is None:
        return None
    if ws.hooksModule in _hooksCache:
        return _hooksCache[ws.hooksModule]
    module = None
    try:
        module = importlib.import_module(ws.hooksModule)
    except ModuleNotFoundError as e:
        if e.name != ws.hooksModule:
            # The hooks module exists, but something that it imports is missing
            logging.error(f"Studio: cannot import {ws.hooksModule}:\n{traceback.format_exc()}")
    except Exception:
        logging.error(f"Studio: cannot import {ws.hooksModule}:\n{traceback.format_exc()}")
    _hooksCache[ws.hooksModule] = module
    return module


def hookFunction(ws, name):
    """The function ``name`` of the hooks module of ``ws``, or None."""
    module = hooks(ws) if ws is not None else None
    function = getattr(module, name, None) if module is not None else None
    return function if callable(function) else None


def callHook(ws, name, shell, default=None):
    """Call the hook ``name(shell)`` of ``ws`` and return its result (``default`` if there is no such hook or it failed)."""
    function = hookFunction(ws, name)
    if function is None:
        return default
    try:
        return function(shell)
    except Exception:
        logging.error(f"Studio: {ws.hooksModule}.{name}() failed:\n{traceback.format_exc()}")
    return default


def _callHook(ws, name, shell):
    if hookFunction(ws, name) is None:
        return False
    callHook(ws, name, shell)
    return True


class LibraryPlaceholderCanvas:
    """Minimal Library canvas, used until the Library page (``StudioLib.library``) is available."""

    def __init__(self, shell):
        self.shell = shell
        self.widget = qt.QFrame()
        self.widget.setObjectName("StudioLibraryPlaceholderCanvas")
        ui.bindStyleSheet(self.widget, lambda: f"QFrame#StudioLibraryPlaceholderCanvas {{ background-color: {ui.token('canvas').name()}; border: none; }}")

        outerLayout = qt.QVBoxLayout(self.widget)
        outerLayout.addStretch(2)
        column = qt.QWidget()
        column.setFixedWidth(480)
        columnLayout = qt.QVBoxLayout(column)
        columnLayout.setContentsMargins(0, 0, 0, 0)
        columnLayout.setSpacing(12)

        title = qt.QLabel(_("Library"))
        title.setObjectName("StudioLibraryPlaceholderTitle")
        ui._font(title, 20, qt.QFont.DemiBold)
        title.setAlignment(qt.Qt.AlignCenter)
        columnLayout.addWidget(title)

        subtitle = qt.QLabel(_("Open a scan to start drawing a lesion."))
        subtitle.setObjectName("StudioLibraryPlaceholderSubtitle")
        subtitle.setAlignment(qt.Qt.AlignCenter)
        ui.bindStyleSheet(subtitle, lambda: f"QLabel#StudioLibraryPlaceholderSubtitle {{ color: {ui.token('text2').name()}; }}")
        columnLayout.addWidget(subtitle)

        self.openButton = qt.QPushButton(_("Open…"))
        self.openButton.setObjectName("StudioLibraryOpenButton")
        self.openButton.setMinimumHeight(36)
        ui.setRole(self.openButton, "secondary")
        self.openButton.connect("clicked()", self.shell.openFiles)
        buttonRow = qt.QHBoxLayout()
        buttonRow.addStretch(1)
        buttonRow.addWidget(self.openButton)
        buttonRow.addStretch(1)
        columnLayout.addLayout(buttonRow)

        columnRow = qt.QHBoxLayout()
        columnRow.addStretch(1)
        columnRow.addWidget(column)
        columnRow.addStretch(1)
        outerLayout.addLayout(columnRow)
        outerLayout.addStretch(3)

    def dispose(self):
        ui.bindStyleSheet(self.widget, None)
        self.widget.hide()
        self.widget.deleteLater()


class WorkspaceController:
    """Switching between workspaces and following module selections (SPEC 2.6)."""

    def __init__(self, shell):
        self.shell = shell
        #: Key of the current workspace, None when a transient module is shown
        self.current = None
        #: Key of the last workspace (the close button of a transient module returns to it)
        self.last = None
        #: Name of the transient module shown in the inspector, None in a workspace
        self.transientModule = None
        self._switching = False
        self._placeholderCanvas = None

    def currentWorkspace(self):
        """Key of the current workspace, or None while a transient module is shown."""
        return self.current

    def isSwitching(self):
        return self._switching

    def rememberedLayout(self, ws):
        if ws.defaultLayout is None:
            return None
        value = slicer.util.settingsValue(ws.layoutSettingsKey, ws.defaultLayout, converter=int)
        node = layouts.layoutNode()
        if node is not None and value >= layouts.STUDIO_LAYOUT_ID_FIRST and not node.IsLayoutDescription(value):
            return ws.defaultLayout
        return value

    def switchTo(self, key, keepLayout=False):
        """Show a workspace: its module in the inspector and its remembered layout.

        With ``keepLayout``, the current layout is kept (for example the layout of a scene that was just opened).
        Asking again for the current workspace (its tab or its shortcut) brings it back into view:
        the inspector that the user hid is shown again and the focus mode is left.
        """
        ws = workspace(key)
        if ws is None:
            raise ValueError(f"Unknown Studio workspace: {key}")
        if ws.key == self.current and self.transientModule is None:
            self.shell._onCurrentWorkspaceReselected(ws)
            return
        if not isAvailable(ws):
            self.shell.toast(_("{workspace} is not available in this build").format(workspace=ws.title))
            return
        self._switching = True
        try:
            self._leave()
            self.transientModule = None
            if ws.key == "library" and getattr(hooks(ws), "enter", None) is None:
                self.shell.setCanvas(self._libraryPlaceholderCanvas().widget)
            elif ws.key != "library":
                self.shell.setCanvas(None)
            if self.shell.currentModuleName() != ws.moduleName:
                self._selectModule(ws.moduleName)
            layoutId = self.rememberedLayout(ws) if not keepLayout else None
            layoutManager = slicer.app.layoutManager()
            if layoutId is not None and layoutManager is not None and layoutManager.layout != layoutId:
                layoutManager.setLayout(layoutId)
            self._enter(ws, shrinkPanel=True)
        finally:
            self._switching = False
        self._persistLastWorkspace(ws)

    def _selectModule(self, moduleName):
        """Select the module of a workspace with the module selector of the shell's main window.

        Same as ``slicer.util.selectModule()``, which looks the main window up among all top-level widgets (twice, about
        2 ms each with the Studio menus and popovers).
        """
        mainWindow = getattr(self.shell, "mainWindow", None)
        selector = mainWindow.moduleSelector() if mainWindow is not None and hasattr(mainWindow, "moduleSelector") else None
        if selector is None:
            slicer.util.selectModule(moduleName)
            return
        selector.selectModule(moduleName)

    def _enter(self, ws, shrinkPanel):
        self.current = ws.key
        self.last = ws.key
        self.transientModule = None
        self.shell._onWorkspaceEntered(ws, shrinkPanel=shrinkPanel)
        _callHook(ws, "enter", self.shell)
        if hookFunction(ws, "focusFirst") is not None:
            # After the module widget and the canvas are laid out
            qt.QTimer.singleShot(0, lambda key=ws.key: self._focusFirst(key))

    def _focusFirst(self, key):
        if self.current != key or not self.shell.isActive():
            return
        callHook(workspace(key), "focusFirst", self.shell)

    def _leave(self):
        if self.current is None:
            return
        ws = workspace(self.current)
        self.current = None
        _callHook(ws, "leave", self.shell)
        self.shell._onWorkspaceLeft(ws)

    def leaveCurrent(self):
        """Leave the current workspace (when switching to the Classic interface)."""
        self._leave()
        self.transientModule = None

    def returnToLastWorkspace(self):
        self.switchTo(self.last or "library")

    def onModuleSelected(self, moduleName):
        """Follow modules selected by other means than ``switchTo()`` (module finder, extensions...)."""
        if self._switching or not moduleName:
            return
        ws = workspaceForModule(moduleName)
        if ws is not None:
            if ws.key == self.current and self.transientModule is None:
                return
            # A workspace module selected by another way, for example the Subject Hierarchy "Segment"
            # action: enter the workspace but keep the layout chosen by the caller.
            self._switching = True
            try:
                self._leave()
                if ws.key == "library" and getattr(hooks(ws), "enter", None) is None:
                    self.shell.setCanvas(self._libraryPlaceholderCanvas().widget)
                elif ws.key != "library":
                    self.shell.setCanvas(None)
                self._enter(ws, shrinkPanel=False)
            finally:
                self._switching = False
            self._persistLastWorkspace(ws)
            return
        if self.transientModule == moduleName:
            return
        self._leave()
        self.transientModule = moduleName
        self.shell.setCanvas(None)
        self.shell._onTransientModuleEntered(moduleName)

    def onLayoutChanged(self, layoutId):
        """Remember the layout chosen in a workspace."""
        if self._switching or self.current is None or self.transientModule is not None:
            return
        ws = workspace(self.current)
        if ws.defaultLayout is None:
            return
        settings = qt.QSettings()
        if slicer.util.settingsValue(ws.layoutSettingsKey, ws.defaultLayout, converter=int) != layoutId:
            settings.setValue(ws.layoutSettingsKey, layoutId)

    def _persistLastWorkspace(self, ws):
        settings = qt.QSettings()
        if settings.value("Studio/LastWorkspace") != ws.title:
            settings.setValue("Studio/LastWorkspace", ws.title)

    def _libraryPlaceholderCanvas(self):
        if self._placeholderCanvas is None:
            self._placeholderCanvas = LibraryPlaceholderCanvas(self.shell)
        return self._placeholderCanvas

    def dispose(self):
        if self._placeholderCanvas is not None:
            self._placeholderCanvas.dispose()
            self._placeholderCanvas = None

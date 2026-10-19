r"""Shell of Slicer Studio, the streamlined default user interface (SPEC 2).

On a normal launch, ``onStartupCompleted()`` applies Studio to the main window: one 44 px top bar
replaces the menubar, the toolbars and the status bar, and the module panel becomes a 340 px
inspector on the right with a 40 px header. Workspaces (Library, Draw, Map) are modules shown in
the inspector. Nothing is deleted or renamed: the Classic interface is restored exactly by
``Shell.classic()`` (``Ctrl+Alt+0``).

Studio never runs under ``--testing`` or ``--no-main-window``, nor with ``SLICER_CLASSIC_UI=1`` or
``Studio/Enabled=false``. ``instance()`` returns None in these cases and in Classic: every consumer
must handle None.

Public API (SPEC 2.10): ``instance()``, ``Shell.switchTo()``, ``currentWorkspace()``, ``toast()``,
``setBreadcrumb()``, ``setStatusChip()``, ``setInspectorFooter()``, ``setCanvas()``,
``setInspectorVisible()``, ``toggleInspector()``, ``setFocusMode()``, ``handleEscape()``,
``setPrimaryAction()``, ``addWorkspaceShortcut()``, ``studySession``, ``classic()``, ``studio()``
and ``panelWidth``.

Navigation ("where am I, how do I get back"): the tab or the shortcut of the current workspace shows
the inspector again and leaves the focus mode; ``Ctrl+\`` shows or hides the inspector (Tab and
Shift+Tab keep moving the keyboard focus); the focus mode shows an "Exit focus mode" pill; Esc backs
out of one navigation state at a time (see ``Shell.handleEscape()``); Classic -> Studio returns to the
workspace of the current module.

Settings are stored under ``Studio/*`` only. The shell does not change values of keys that other
Slicer installations read (SPEC 9.7 rule 5), except that after the application window is closed it
replaces what Slicer saved there while Studio was shown by the Classic equivalent: the classic
window state in ``MainWindow/windowState`` and a portable layout in ``MainWindow/layout``.
"""

import importlib
import logging
import os
import time
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import hud, layouts, topbar, ui, workspaces

#: Plug-ins installed when Studio is applied (``install(shell)``) and removed in Classic
#: (``uninstall(shell)``). Missing modules are skipped.
OPTIONAL_PLUGINS = ["StudioLib.importer", "StudioLib.probe", "StudioLib.journal", "StudioLib.study_ui", "StudioLib.orientation", "StudioLib.finder"]

DEFAULT_PANEL_WIDTH = 340
MIN_PANEL_WIDTH = 300
MAX_PANEL_WIDTH = 480
PANEL_HEADER_HEIGHT = 40
PANEL_HEADER_BUTTON_SIZE = 26
#: Help popover of the panel header: a card under the header, as wide as the inspector
HELP_POPOVER_PADDING = 12
HELP_POPOVER_MIN_WIDTH = 240
HELP_POPOVER_MIN_HEIGHT = 200
HELP_POPOVER_MAX_HEIGHT = 420

#: Shows or hides the inspector. Not Tab: Tab and Shift+Tab move the keyboard focus, also out of the views.
INSPECTOR_SHORTCUT = "Ctrl+\\"
#: Second key of the inspector, for keyboard layouts where Ctrl+\ cannot be typed (German, French: AltGr)
INSPECTOR_ALTERNATE_SHORTCUT = "F6"
FOCUS_MODE_SHORTCUTS = ("F11", "Ctrl+6")

#: Explicit minimum width of the module panel in Studio. The minimum size hint of the module panel is the
#: width of the module widget, so a wide tool (for example the Paint options) would widen the inspector and
#: move the views under the cursor: the inspector keeps its width and wide contents scroll instead.
_MODULE_PANEL_MINIMUM_WIDTH = 240
#: Widest inspector that a workspace module may ask for (its layout minimum). Wider contents scroll.
MAX_WORKSPACE_PANEL_WIDTH = 600
#: Widest layout minimum seen so far for each workspace module, kept across sessions so that the inspector
#: does not widen again when a tool is chosen: "Studio/WorkspacePanelWidths/<module name>" holds
#: "<width>@<layout signature>" (see ``Shell._layoutSignature()``)
_WORKSPACE_PANEL_WIDTHS_GROUP = "Studio/WorkspacePanelWidths"

#: Part of the available screen area given to the main window when Slicer did not restore its geometry
FIRST_RUN_WINDOW_FRACTION = 0.9

#: Main window actions that keep their classic behavior in Studio: they are not attached to the
#: main window (Ctrl+Z/Y are owned by the Segment Editor, Ctrl+6 by the focus mode, Ctrl+O by Studio)
_KEEP_UNATTACHED_ACTIONS = ("EditUndoAction", "EditRedoAction", "WindowMaximizeViewAction", "FileAddDataAction")

_PORTABLE_LAYOUT = slicer.vtkMRMLLayoutNode.SlicerLayoutConventionalView

_shell = None
_closeGuard = None
#: The "Panel hidden" hint is shown once per application session
_panelHiddenHintShown = False


def instance():
    """Return the live Studio shell, or None (Classic, testing, no main window, Studio disabled)."""
    if _shell is not None and _shell.isActive():
        return _shell
    return None


def _settingsBool(key, default):
    return slicer.util.settingsValue(key, default, converter=slicer.util.toBool)


def associatedWidgets(action):
    """Widgets that ``action`` was added to (Qt 5 has associatedWidgets(), Qt 6 only associatedObjects())."""
    if hasattr(action, "associatedWidgets"):
        return list(action.associatedWidgets())
    # Not isinstance(obj, qt.QWidget): PythonQt wrappers do not inherit from the wrappers of their C++ base classes,
    # so that is False for a QMenu, a QToolBar or the main window, and the list would always be empty
    return [obj for obj in action.associatedObjects() if obj.isWidgetType()]


def onStartupCompleted(startTime=None):
    """Apply Studio after the application started (connected by the Studio module).

    ``startTime`` (``time.perf_counter()``) is the start of the apply time measurement.
    """
    if startTime is None:
        startTime = time.perf_counter()
    if slicer.app.testingEnabled() or slicer.app.commandOptions().noMainWindow:
        return
    mainWindow = slicer.util.mainWindow()
    if mainWindow is None:
        return
    installCloseGuard(mainWindow)
    if os.environ.get("SLICER_CLASSIC_UI") == "1":
        return
    if not _settingsBool("Studio/Enabled", True):
        return
    shell = _shell if _shell is not None else Shell()
    shell.installClassicSwitch()
    if str(slicer.util.settingsValue("Studio/Mode", "studio")).lower() == "classic":
        return
    fitWindowToScreen(mainWindow)
    shell.apply(startTime=startTime)


def fitWindowToScreen(mainWindow, fraction=FIRST_RUN_WINDOW_FRACTION):
    """Give the main window most of the screen when Slicer did not restore a saved geometry.

    Without a saved geometry (first run, or ``MainWindow/RestoreGeometry`` off) the window opens at the
    835x570 px size of its form, too small for the inspector and the views. The settings are only read.
    Return True if the window was resized.
    """
    settings = slicer.app.userSettings()
    restored = slicer.util.toBool(settings.value("MainWindow/RestoreGeometry", False)) and settings.contains("MainWindow/geometry")
    if restored or mainWindow.isMaximized() or mainWindow.isFullScreen():
        return False
    screen = mainWindow.screen() if callable(getattr(mainWindow, "screen", None)) else None
    if screen is None:
        screen = slicer.app.primaryScreen  # a property in PythonQt, not a method
    if screen is None:
        return False
    available = screen.availableGeometry
    if callable(available):
        # A method when the property is not wrapped as an attribute
        available = available()
    width = int(available.width() * fraction)
    height = int(available.height() * fraction)
    if mainWindow.width >= width and mainWindow.height >= height:
        return False
    mainWindow.resize(width, height)
    mainWindow.move(available.x() + (available.width() - width) // 2, available.y() + (available.height() - height) // 2)
    return True


#
# Saved window state guard
#


class MainWindowCloseGuard(qt.QObject):
    """Keep the settings that Slicer saves when the main window is closed readable by Classic and
    by other Slicer installations (they share the settings file).

    ``qSlicerMainWindow::closeEvent`` asks for confirmation (possibly in modal dialogs), then saves
    ``MainWindow/windowState`` and ``MainWindow/layout`` and the window is hidden. On that Hide event,
    the guard replaces the window state saved while Studio was shown by the classic window state,
    and a fork layout (IDs 7001-7099, unknown to other Slicer versions) by the conventional layout.
    """

    def __init__(self, mainWindow):
        qt.QObject.__init__(self)
        self.mainWindow = mainWindow
        #: Function that returns the classic window state to save, or None (Studio not shown)
        self.classicStateProvider = None
        self._closing = False
        mainWindow.installEventFilter(self)
        slicer.app.connect("aboutToQuit()", self._onAboutToQuit)

    def eventFilter(self, obj, event):
        if obj != self.mainWindow:
            return False
        eventType = event.type()
        if eventType == qt.QEvent.Close:
            self._closing = True
            qt.QTimer.singleShot(0, self._checkCloseCancelled)
        elif eventType == qt.QEvent.Hide and self._closing:
            # The close was accepted: the window state and the layout have just been saved
            self._closing = False
            self._fixSavedState()
        return False

    def _checkCloseCancelled(self):
        if not self._closing:
            return
        if slicer.app.activeModalWidget() is not None:
            # Still asking for confirmation (the timer runs in the event loop of the dialog)
            qt.QTimer.singleShot(100, self._checkCloseCancelled)
            return
        if self.mainWindow.isVisible():
            self._closing = False

    def _onAboutToQuit(self):
        if self._closing:
            self._closing = False
            self._fixSavedState()

    def _fixSavedState(self):
        settings = qt.QSettings()
        if not slicer.util.toBool(settings.value("MainWindow/RestoreGeometry", False)):
            # Slicer did not save the window state and layout
            return
        classicState = self.classicStateProvider() if self.classicStateProvider is not None else None
        if classicState is not None:
            settings.setValue("MainWindow/windowState", classicState)
        try:
            layoutId = int(settings.value("MainWindow/layout", 0))
        except (TypeError, ValueError):
            layoutId = 0
        if layouts.isStudioLayout(layoutId):
            settings.setValue("MainWindow/layout", _PORTABLE_LAYOUT)
        settings.sync()


def installCloseGuard(mainWindow):
    global _closeGuard
    if _closeGuard is None:
        _closeGuard = MainWindowCloseGuard(mainWindow)
    return _closeGuard


#
# Panel header
#


class PanelHeader:
    """The 40 px header of the inspector, which replaces the module panel title bar (SPEC 2.5)."""

    def __init__(self, shell):
        self.shell = shell
        self.widget = qt.QFrame()
        self.widget.setObjectName("StudioPanelHeader")
        self.widget.setFixedHeight(PANEL_HEADER_HEIGHT)
        layout = qt.QHBoxLayout(self.widget)
        # QDockWidget reserves the size hint height of a title bar widget (not its fixed height):
        # the 26 px buttons and these margins make the size hint 40 px
        verticalMargin = (PANEL_HEADER_HEIGHT - PANEL_HEADER_BUTTON_SIZE) // 2
        layout.setContentsMargins(12, verticalMargin, 6, verticalMargin)
        layout.setSpacing(2)

        self.prefixLabel = qt.QLabel(_("Module ·"))
        self.prefixLabel.setObjectName("StudioPanelHeaderPrefix")
        ui._font(self.prefixLabel, 13)
        self.prefixLabel.hide()
        layout.addWidget(self.prefixLabel)
        layout.addSpacing(2)
        self.titleLabel = qt.QLabel()
        self.titleLabel.setObjectName("StudioPanelHeaderTitle")
        ui._font(self.titleLabel, 15, qt.QFont.DemiBold)
        layout.addWidget(self.titleLabel, 1)

        self.helpButton = self._button("StudioPanelHelpButton", "circle-help", _("Help and acknowledgment"))
        self.helpButton.connect("clicked()", self.showHelp)
        layout.addWidget(self.helpButton)

        self.moreButton = self._button("StudioPanelMoreButton", "ellipsis", _("More"))
        self.moreButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.moreMenu = qt.QMenu(self.moreButton)
        self.moreMenu.setObjectName("StudioPanelMoreMenu")
        self.dataProbeAction = self.moreMenu.addAction(_("Show Data Probe"))
        self.dataProbeAction.setCheckable(True)
        self.dataProbeAction.connect("toggled(bool)", self.shell.setDataProbeVisible)
        self.moreMenu.addAction(_("Reset panel width")).connect("triggered()", self.shell.resetPanelWidth)
        self.moreMenu.addSeparator()
        self.moreMenu.addAction(_("Open in Classic")).connect("triggered()", self.shell.classic)
        self.moreMenu.connect("aboutToShow()", self._updateMoreMenu)
        self.moreButton.setMenu(self.moreMenu)
        layout.addWidget(self.moreButton)

        self.closeButton = self._button("StudioPanelCloseButton", "x", _("Close"))
        self.closeButton.connect("clicked()", self.shell.closeTransientModule)
        self.closeButton.hide()
        layout.addWidget(self.closeButton)

        ui.bindStyleSheet(self.widget, lambda: (
            f"QFrame#StudioPanelHeader {{ background-color: {ui.token('window').name()}; border: none; border-bottom: 1px solid {ui.token('hairline').name()}; }}"
            f"QLabel#StudioPanelHeaderPrefix {{ color: {ui.token('text2').name()}; }}"))
        self.refreshIcons()
        ui.addPaletteListener(self.refreshIcons)

    @staticmethod
    def _button(objectName, iconName, toolTip):
        button = qt.QToolButton()
        button.setObjectName(objectName)
        button.setToolTip(toolTip)
        button.setAutoRaise(True)
        button.setFocusPolicy(qt.Qt.NoFocus)
        button.setIconSize(qt.QSize(16, 16))
        button.setFixedSize(PANEL_HEADER_BUTTON_SIZE, PANEL_HEADER_BUTTON_SIZE)
        button.setProperty("studioIcon", iconName)
        ui.setRole(button, "ghost")
        return button

    def refreshIcons(self):
        for button in (self.helpButton, self.moreButton, self.closeButton):
            button.setIcon(ui.icon(button.property("studioIcon"), 16))

    def setTitle(self, title, transient=False, closeToolTip=None):
        self.titleLabel.setText(title)
        self.prefixLabel.setVisible(transient)
        self.closeButton.setVisible(transient)
        if closeToolTip:
            self.closeButton.setToolTip(closeToolTip)

    def _updateMoreMenu(self):
        dataProbe = self.shell.mainWindow.findChild("QWidget", "DataProbeCollapsibleWidget")
        self.dataProbeAction.blockSignals(True)
        self.dataProbeAction.setChecked(dataProbe is not None and not dataProbe.isHidden())
        self.dataProbeAction.blockSignals(False)

    def helpPopover(self):
        """Create the help popover of the current module, or return None if no module is shown.

        It is a menu that holds a text browser, to be shown under the header (``helpPopoverPosition()``) over the
        whole inspector width. It is a card with a border and padding, so that it reads as a surface of its own
        rather than as text over the inspector.
        """
        moduleName = self.shell.currentModuleName()
        module = slicer.app.moduleManager().module(moduleName) if moduleName and workspaces.moduleExists(moduleName) else None
        if module is None:
            return None
        html = f'<h3 style="margin-top: 0px;">{module.title}</h3>{module.helpText}'
        if module.acknowledgementText:
            html += f"<h4>{_('Acknowledgment')}</h4>{module.acknowledgementText}"
        menu = qt.QMenu(self.helpButton)
        menu.setObjectName("StudioHelpPopover")
        card = ui.token("card").name()
        menu.setStyleSheet(
            f"QMenu#StudioHelpPopover {{ background-color: {card}; border: 1px solid {ui.token('border').name()}; padding: 0px; }}"
            f"QTextBrowser#StudioHelpBrowser {{ background-color: {card}; border: none; }}")
        browser = qt.QTextBrowser()
        browser.setObjectName("StudioHelpBrowser")
        browser.setOpenExternalLinks(True)
        browser.setFrameShape(qt.QFrame.NoFrame)
        browser.setHtml(html)
        browser.document.setDocumentMargin(HELP_POPOVER_PADDING)
        # As wide as the inspector (inside the 1 px border), as high as the inspector allows
        dock = self.widget.parentWidget()
        available = (dock.height if dock is not None else HELP_POPOVER_MAX_HEIGHT) - self.widget.height - HELP_POPOVER_PADDING
        browser.setFixedSize(max(self.widget.width - 2, HELP_POPOVER_MIN_WIDTH), max(HELP_POPOVER_MIN_HEIGHT, min(HELP_POPOVER_MAX_HEIGHT, available)))
        action = qt.QWidgetAction(menu)
        action.setDefaultWidget(browser)
        menu.addAction(action)
        return menu

    def helpPopoverPosition(self):
        """Global position of the help popover: the bottom left corner of the header."""
        return self.widget.mapToGlobal(qt.QPoint(0, self.widget.height))

    def showHelp(self):
        menu = self.helpPopover()
        if menu is None:
            return
        menu.exec_(self.helpPopoverPosition())
        menu.deleteLater()

    def dispose(self):
        ui.removePaletteListener(self.refreshIcons)
        ui.bindStyleSheet(self.widget, None)
        self.widget.hide()
        self.widget.deleteLater()


#
# Event filter
#


class _ShellEventFilter(qt.QObject):
    def __init__(self, shell):
        qt.QObject.__init__(self)
        self.shell = shell

    def eventFilter(self, obj, event):
        try:
            # Only the Esc key of the Esc stack is consumed (True)
            return bool(self.shell._onEvent(obj, event))
        except Exception:
            logging.error(f"Studio event handler failed:\n{traceback.format_exc()}")
        return False


#
# Shell
#


class Shell:
    """The Studio shell of the main window. Use ``instance()`` to get the live shell."""

    def __init__(self, force=False):
        """Create the shell. ``force=True`` allows applying it under ``--testing`` (Studio tests)."""
        global _shell
        self.mainWindow = slicer.util.mainWindow()
        if self.mainWindow is None:
            raise RuntimeError("The Studio shell needs the application main window")
        if slicer.app.testingEnabled() and not force:
            raise RuntimeError("The Studio shell does not run when testing is enabled (use force=True in tests)")
        self.force = force
        #: Duration of the last apply() in milliseconds (including the import of the shell when applied at startup)
        self.applyDurationMs = None
        #: Study session of the Library (set by the study plug-in), None if no study is open
        self.studySession = None
        self.hoverTracker = None
        self.workspaces = None
        self.topBar = None
        self.panelHeader = None
        self._active = False
        self._classicState = None
        self._classicSwitchAction = None
        self._classicSwitchSeparator = None
        self._canvas = None
        self._renderPausedByCanvas = False
        self._toast = None
        self._footerSlot = None
        self._footerWidget = None
        self._primaryAction = None
        self._primaryActionLabel = ""
        self._shortcuts = []
        self._workspaceShortcuts = {}
        self._installedWorkspaceShortcuts = []
        self._addedActions = []
        self._watchedToolBars = []
        #: [toolBar, visible in Classic] of the toolbars that the classic window state does not restore
        #: (added after Studio was applied, or without objectName)
        self._unsavedToolBars = []
        self._classicToolBarNames = set()
        self._toolBarScanTimer = None
        self._plugins = []
        self._connections = []
        self._focusModeHidden = None
        self._focusModePill = None
        self._panelWidthTimer = None
        self._panelUpdateTimer = None
        self._pendingPanelShrink = False
        #: Widest inspector width needed by each workspace module ({module name: px}), see _workspacePanelWidth()
        self._workspacePanelWidths = {}
        #: Contents of the module panel scroll area, watched for layout changes (a tool was chosen)
        self._watchedPanelContents = None
        #: True while the inspector is hidden by the user (shortcut, Ctrl+5, menu), not by a workspace page
        self._inspectorHiddenByUser = False
        #: (toggle view action, slot) of the toolbars that Studio hides
        self._toolBarToggleConnections = []
        #: Toolbars without objectName that the user showed in this session (not persisted)
        self._sessionAllowedToolBars = []
        #: Classic minimum width of the module panel and policy of its horizontal scroll bar
        self._classicModulePanelState = None
        #: ctkFittedTextBrowser widgets given an explicit minimum width in Studio
        self._fittedTextBrowsers = []
        #: True from an Esc shortcut override that the Esc stack handled to the matching key press
        self._escapeHandled = False
        self._eventFilter = None
        if _shell is None:
            _shell = self

    #
    # State
    #

    def isActive(self):
        """True while the Studio interface is shown."""
        return self._active

    def currentWorkspace(self):
        """Key of the current workspace ("library", "draw" or "map"), None for a transient module or in Classic."""
        if not self._active or self.workspaces is None:
            return None
        return self.workspaces.currentWorkspace()

    def currentModuleName(self):
        """Name of the module shown in the inspector."""
        return self.mainWindow.moduleSelector().selectedModule

    def _modulePanel(self):
        return self.mainWindow.findChild("QWidget", "ModulePanel")

    def _panelDock(self):
        return self.mainWindow.findChild("QDockWidget", "PanelDockWidget")

    def _dataProbe(self):
        return self.mainWindow.findChild("QWidget", "DataProbeCollapsibleWidget")

    def _fileAddDataAction(self):
        return self.mainWindow.findChild("QAction", "FileAddDataAction")

    def _connect(self, sender, signal, slot):
        sender.connect(signal, slot)
        self._connections.append((sender, signal, slot))

    def _disconnectAll(self):
        for sender, signal, slot in reversed(self._connections):
            try:
                sender.disconnect(signal, slot)
            except Exception:
                pass
        self._connections = []

    #
    # Classic switch (available in both interfaces)
    #

    def installClassicSwitch(self):
        """Add the checkable "Studio interface" action (Ctrl+Alt+0) to the View menu and the main window."""
        if self._classicSwitchAction is not None:
            return
        action = qt.QAction(_("Studio interface"), self.mainWindow)
        action.setObjectName("StudioInterfaceAction")
        action.setCheckable(True)
        action.setChecked(self._active)
        action.setShortcut(qt.QKeySequence("Ctrl+Alt+0"))
        action.setToolTip(_("Switch between the Studio and the Classic interface (Ctrl+Alt+0)"))
        action.setProperty("studioDescription", _("Classic ⇄ Studio interface"))
        action.connect("triggered(bool)", self._onClassicSwitchTriggered)
        viewMenu = self.mainWindow.findChild("QMenu", "ViewMenu")
        if viewMenu is not None:
            self._classicSwitchSeparator = viewMenu.addSeparator()
            viewMenu.addAction(action)
        # Keep the shortcut working when the menubar is hidden
        self.mainWindow.addAction(action)
        self._classicSwitchAction = action
        installCloseGuard(self.mainWindow).classicStateProvider = self._classicStateIfActive

    def _onClassicSwitchTriggered(self, checked):
        if checked:
            self.studio()
        else:
            self.classic()

    def _updateClassicSwitchAction(self):
        if self._classicSwitchAction is not None:
            self._classicSwitchAction.setChecked(self._active)

    def _classicStateIfActive(self):
        return self._classicState if self._active else None

    def toggleClassic(self):
        if self._active:
            self.classic()
        else:
            self.studio()

    def studio(self):
        """Switch to the Studio interface and remember the choice.

        Coming from Classic in the middle of a session, Studio returns to the workspace of the current
        module (Draw for the Segment Editor), else to the last workspace (``Studio/LastWorkspace``).
        """
        qt.QSettings().setValue("Studio/Mode", "studio")
        self.apply(midSession=True)

    def classic(self, persist=True):
        """Restore the Classic interface exactly as it was (SPEC 2.7) and remember the choice."""
        if not self._active:
            return
        self._restoreClassic()
        if persist:
            qt.QSettings().setValue("Studio/Mode", "classic")
        self._updateClassicSwitchAction()

    def _restoreClassic(self):
        """Undo apply(). Also used to recover from a failed apply(), so each part may be missing."""
        mainWindow = self.mainWindow
        if self.workspaces is not None:
            self.workspaces.leaveCurrent()
        self.setCanvas(None)
        self.setFocusMode(False)
        self._uninstallPlugins()
        self._setSegmentEditorPresentation("classic")
        self._active = False
        self._disconnectAll()
        self._removeEventFilters()
        self._escapeHandled = False
        self._inspectorHiddenByUser = False
        for shortcut in self._installedWorkspaceShortcuts + self._shortcuts:
            self._deleteShortcut(shortcut)
        self._installedWorkspaceShortcuts = []
        self._shortcuts = []
        for timer in (self._toolBarScanTimer, self._panelUpdateTimer, self._panelWidthTimer):
            if timer is not None:
                timer.stop()
        self._toolBarScanTimer = None
        self._panelUpdateTimer = None
        self._pendingPanelShrink = False
        for toolBar, slot in self._watchedToolBars:
            try:
                toolBar.disconnect("visibilityChanged(bool)", slot)
            except Exception:
                pass
        self._watchedToolBars = []
        for action, slot in self._toolBarToggleConnections:
            try:
                action.disconnect("triggered(bool)", slot)
            except Exception:
                pass
        self._toolBarToggleConnections = []
        self._sessionAllowedToolBars = []
        for action in self._addedActions:
            mainWindow.removeAction(action)
        self._addedActions = []
        fileAddDataAction = self._fileAddDataAction()
        if fileAddDataAction is not None:
            fileAddDataAction.setShortcuts(self._classicFileAddDataShortcuts)

        if self.topBar is not None:
            self.topBar.dispose()
            self.topBar = None
        if self._toast is not None:
            self._toast.dispose()
            self._toast = None
        if self._focusModePill is not None:
            self._focusModePill.dispose()
            self._focusModePill = None
        if self.hoverTracker is not None:
            self.hoverTracker.dispose()
            self.hoverTracker = None
        if self.workspaces is not None:
            self.workspaces.dispose()
            self.workspaces = None

        dock = self._panelDock()
        self._removeFooterSlot()
        if dock.titleBarWidget() != self._classicTitleBar:
            dock.setTitleBarWidget(self._classicTitleBar)
            if self._classicTitleBar is not None:
                self._classicTitleBar.show()
        if self.panelHeader is not None:
            self.panelHeader.dispose()
            self.panelHeader = None
        dock.setProperty("features", self._classicDockFeatures)
        self._setModulePanelConstrained(False)

        mainWindow.restoreState(self._classicState)
        for toolBar, visible in self._unsavedToolBars:
            try:
                # Skip toolbars that their owner removed from the main window (removeToolBar() hides them in
                # Classic) or reparented: showing them would draw a stray widget over the main window
                if (visible and toolBar.isHidden() and self._isMainWindowToolBar(toolBar)
                        and toolBar.objectName not in self._classicToolBarNames):
                    toolBar.show()
            except Exception:
                # Deleted by its owner
                pass
        self._unsavedToolBars = []
        mainWindow.menuBar().setVisible(self._classicVisibility["menuBar"])
        if mainWindow.statusBar() is not None:
            mainWindow.statusBar().setVisible(self._classicVisibility["statusBar"])
        dataProbe = self._dataProbe()
        if dataProbe is not None:
            dataProbe.setVisible(self._classicVisibility["dataProbe"])
        self._modulePanel().helpAndAcknowledgmentVisible = self._classicVisibility["help"]
        sequences = getattr(slicer.modules, "sequences", None)
        if sequences is not None and self._classicVisibility.get("sequencesAutoShowToolBar") is not None:
            sequences.autoShowToolBar = self._classicVisibility["sequencesAutoShowToolBar"]

    #
    # Apply
    #

    def apply(self, startTime=None, midSession=False):
        """Show the Studio interface (SPEC 2.3).

        At startup Studio shows ``Studio/StartWorkspace``. With ``midSession`` (from Classic, see
        ``studio()``) it shows the workspace of the current module, else the last workspace.
        """
        if self._active:
            return
        if startTime is None:
            startTime = time.perf_counter()
        try:
            self._apply(midSession=midSession)
        except Exception:
            logging.error(f"Studio could not be applied, restoring the Classic interface:\n{traceback.format_exc()}")
            if self._active:
                try:
                    self._restoreClassic()
                except Exception:
                    logging.error(f"Studio: restoring the Classic interface failed:\n{traceback.format_exc()}")
            self._updateClassicSwitchAction()
            raise
        self._updateClassicSwitchAction()
        self.applyDurationMs = (time.perf_counter() - startTime) * 1000.0
        logging.debug(f"Studio applied in {self.applyDurationMs:.1f} ms")

    def _apply(self, midSession=False):
        mainWindow = self.mainWindow
        settings = qt.QSettings()
        dock = self._panelDock()
        modulePanel = self._modulePanel()
        dataProbe = self._dataProbe()

        # 1. Remember the classic state
        if self._dockAreasFollowSizeHints():
            # Applied at startup, before the first layout of the window: saveState() would store the
            # pre-layout width of the module panel. Store the width that the first layout gives it.
            width = min(max(dock.sizeHint.width(), dock.minimumSizeHint.width(), dock.minimumWidth), dock.maximumWidth)
            mainWindow.resizeDocks([dock], [width], qt.Qt.Horizontal)
        self._classicState = mainWindow.saveState()
        settings.setValue("Studio/ClassicWindowState", self._classicState)
        self._classicToolBarNames = {toolBar.objectName for toolBar in self._mainWindowToolBars() if toolBar.objectName}
        self._unsavedToolBars = []
        self._classicTitleBar = dock.titleBarWidget()
        # Through the property() / setProperty() pair: with Qt 6, dock.features is an opaque object that setFeatures() rejects
        self._classicDockFeatures = dock.property("features")
        sequences = getattr(slicer.modules, "sequences", None)
        self._classicVisibility = {
            "menuBar": not mainWindow.menuBar().isHidden(),
            "statusBar": mainWindow.statusBar() is not None and not mainWindow.statusBar().isHidden(),
            "dataProbe": dataProbe is not None and not dataProbe.isHidden(),
            "help": modulePanel.helpAndAcknowledgmentVisible,
            "sequencesAutoShowToolBar": sequences.autoShowToolBar if sequences is not None else None,
        }
        fileAddDataAction = self._fileAddDataAction()
        self._classicFileAddDataShortcuts = list(fileAddDataAction.shortcuts()) if fileAddDataAction is not None else []

        # 2. Hide the classic chrome (nothing is deleted)
        self._active = True
        self._eventFilter = _ShellEventFilter(self)
        self.workspaces = workspaces.WorkspaceController(self)
        mainWindow.menuBar().hide()
        if mainWindow.statusBar() is not None:
            mainWindow.statusBar().hide()
        for toolBar in self._mainWindowToolBars():
            self._hideToolBar(toolBar)
        # Toolbars added later (extensions) are hidden too: see _onEvent()
        self._toolBarScanTimer = qt.QTimer()
        self._toolBarScanTimer.setSingleShot(True)
        self._toolBarScanTimer.setInterval(0)
        self._toolBarScanTimer.connect("timeout()", self._hideNewToolBars)
        if sequences is not None:
            sequences.autoShowToolBar = False
        if dataProbe is not None:
            dataProbe.hide()
        modulePanel.helpAndAcknowledgmentVisible = False

        # 3. Keep the shortcuts of the hidden menus and toolbars working
        self._attachChromeShortcutActions()
        if fileAddDataAction is not None:
            # A second Ctrl+O shortcut would be ambiguous: Qt would trigger neither
            fileAddDataAction.setShortcuts([])
        self._addShortcut("Ctrl+O", self.openFiles, _("Open files"))
        self._addShortcut("Ctrl+Return", self.triggerPrimaryAction, _("Primary action of the inspector"))
        self._addShortcut("Ctrl+Enter", self.triggerPrimaryAction, _("Primary action of the inspector"))
        if not hasattr(mainWindow, "setFocusMode"):
            for keySequence in FOCUS_MODE_SHORTCUTS:
                self._addShortcut(keySequence, self.toggleFocusMode, _("Focus mode"))
        # Not Tab: Tab and Shift+Tab move the keyboard focus (out of the views too)
        self._addShortcut(INSPECTOR_SHORTCUT, self.toggleInspector, _("Show or hide the inspector"))
        self._addShortcut(INSPECTOR_ALTERNATE_SHORTCUT, self.toggleInspector, _("Show or hide the inspector"))

        # 4. Inspector on the right with the Studio panel header and a footer slot
        if dock.floating:
            dock.setFloating(False)
        # Not movable or floatable. Closable keeps its toggle view action (Ctrl+5, View > Module Panel)
        # enabled; the panel header has no title bar close button.
        dock.setFeatures(qt.QDockWidget.DockWidgetClosable)
        mainWindow.addDockWidget(qt.Qt.RightDockWidgetArea, dock)
        dock.show()
        self.panelHeader = PanelHeader(self)
        dock.setTitleBarWidget(self.panelHeader.widget)
        self._insertFooterSlot()
        self._setModulePanelConstrained(True)
        # The widths that the workspace modules needed in earlier sessions: the first workspace has the width
        # of all workspaces at once
        for ws in workspaces.WORKSPACES:
            self._workspacePanelWidth(ws.moduleName, 0)
        mainWindow.resizeDocks([dock], [max([self.panelWidth, *self._workspacePanelWidths.values()])], qt.Qt.Horizontal)

        # 5. Top bar
        self.hoverTracker = layouts.HoveredSliceViewTracker()
        self.topBar = topbar.TopBar(self)
        mainWindow.addToolBar(qt.Qt.TopToolBarArea, self.topBar.toolBar)
        self.topBar.toolBar.show()
        self._addTopBarShortcuts()
        self._toast = hud.Toast(mainWindow.centralWidget())
        self._focusModePill = hud.CornerPill(mainWindow.centralWidget(), "StudioExitFocusModeButton", _("Exit focus mode"), "minimize-2",
                                             lambda: self.setFocusMode(False), _("Exit focus mode (F11 or Esc)"))

        # 6. Layouts (registered in C++ by the fork, only added here on builds that lack them)
        layouts.register()

        self._connect(mainWindow.moduleSelector(), "moduleSelected(QString)", self.workspaces.onModuleSelected)
        self._connect(modulePanel, "moduleAdded(QString)", self._onModuleAdded)
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None:
            self._connect(layoutManager, "layoutChanged(int)", self.workspaces.onLayoutChanged)
        self._connect(dock.toggleViewAction(), "triggered(bool)", self._onPanelToggleTriggered)
        mainWindow.installEventFilter(self._eventFilter)
        mainWindow.centralWidget().installEventFilter(self._eventFilter)
        dock.installEventFilter(self._eventFilter)

        # 7. Draw presentation of the Segment Editor, if it exists already
        self._setSegmentEditorPresentation("studio")
        self._installPlugins()

        # Build the heavy workspace pages when the application is idle
        qt.QTimer.singleShot(300, lambda: self._prewarm("SegmentEditor"))
        qt.QTimer.singleShot(600, lambda: self._prewarm("LesionNetworkMapping"))

        # 8. Start workspace
        self.switchTo(self._midSessionWorkspace() if midSession else self._startWorkspace())

    @staticmethod
    def _availableWorkspaceKey(keyOrTitle):
        ws = workspaces.workspace(keyOrTitle)
        return ws.key if ws is not None and workspaces.isAvailable(ws) else None

    def _startWorkspace(self):
        """Workspace shown when Studio is applied at startup (``Studio/StartWorkspace``, "Last" allowed)."""
        startWorkspace = slicer.util.settingsValue("Studio/StartWorkspace", "Library")
        if str(startWorkspace).lower() == "last":
            startWorkspace = slicer.util.settingsValue("Studio/LastWorkspace", "Library")
        return self._availableWorkspaceKey(startWorkspace) or "library"

    def _midSessionWorkspace(self):
        """Workspace shown when the user comes back from Classic: the one of the current module, else the last one."""
        ws = workspaces.workspaceForModule(self.currentModuleName())
        key = self._availableWorkspaceKey(ws.key) if ws is not None else None
        return key or self._availableWorkspaceKey(slicer.util.settingsValue("Studio/LastWorkspace", "Library")) or "library"

    def _dockAreasFollowSizeHints(self):
        """True while the main window is shown for the first time (not laid out yet) and Slicer did
        not restore a saved window state: the first layout then sizes the module panel from its size
        hint, but saveState() still holds its pre-layout width.
        """
        mainWindow = self.mainWindow
        dock = self._panelDock()
        if mainWindow.testAttribute(qt.Qt.WA_Mapped) or dock.floating or dock.isHidden():
            return False
        settings = qt.QSettings()
        if not slicer.util.toBool(settings.value("MainWindow/RestoreGeometry", False)):
            return True
        savedState = settings.value("MainWindow/windowState")
        return savedState.isEmpty() if hasattr(savedState, "isEmpty") else not savedState

    def _isMainWindowToolBar(self, toolBar):
        """True if the toolbar is in a toolbar area of the main window (floating or not)."""
        mainWindow = self.mainWindow
        return toolBar.parentWidget() == mainWindow and mainWindow.toolBarArea(toolBar) != qt.Qt.NoToolBarArea

    def _mainWindowToolBars(self):
        """Toolbars of the main window (in its toolbar areas, floating or not)."""
        return [toolBar for toolBar in self.mainWindow.findChildren("QToolBar") if self._isMainWindowToolBar(toolBar)]

    def _hideNewToolBars(self):
        """Hide the toolbars added to the main window since Studio was applied (extensions)."""
        if not self._active:
            return
        watched = [toolBar for toolBar, slot in self._watchedToolBars]
        for toolBar in self._mainWindowToolBars():
            if toolBar not in watched:
                self._hideToolBar(toolBar)

    def _hideToolBar(self, toolBar):
        if self.topBar is not None and toolBar == self.topBar.toolBar:
            return
        if toolBar.objectName == "StudioTopBar":
            return
        if toolBar not in [watched for watched, slot in self._watchedToolBars]:
            slot = lambda visible, toolBar=toolBar: self._onToolBarVisibilityChanged(toolBar, visible)
            toolBar.connect("visibilityChanged(bool)", slot)
            self._watchedToolBars.append((toolBar, slot))
            # View > Toolbars: triggered() is only emitted for the user (or trigger()), not when the toolbar
            # shows itself or is shown by code
            toggleViewAction = toolBar.toggleViewAction()
            toggleSlot = lambda checked, toolBar=toolBar: self._onToolBarToggledByUser(toolBar, checked)
            toggleViewAction.connect("triggered(bool)", toggleSlot)
            self._toolBarToggleConnections.append((toggleViewAction, toggleSlot))
            if toolBar.objectName not in self._classicToolBarNames:
                # restoreState() does not restore it: remember if it is shown, or about to be shown by
                # the main window layout (hidden but not explicitly)
                visible = not (toolBar.isHidden() and toolBar.testAttribute(qt.Qt.WA_WState_ExplicitShowHide))
                self._unsavedToolBars.append([toolBar, visible])
        if not self._isToolBarAllowed(toolBar):
            toolBar.hide()

    @staticmethod
    def _allowedToolBars():
        """Object names of the toolbars shown in Studio (``Studio/AllowedToolbars``)."""
        allowed = slicer.util.settingsValue("Studio/AllowedToolbars", [])
        if isinstance(allowed, str):
            allowed = [allowed] if allowed else []
        return [str(name) for name in (allowed or [])]

    def _isToolBarAllowed(self, toolBar):
        if toolBar.objectName:
            return toolBar.objectName in self._allowedToolBars()
        return toolBar in self._sessionAllowedToolBars

    def _onToolBarVisibilityChanged(self, toolBar, visible):
        if not visible or not self._active:
            return
        for entry in self._unsavedToolBars:
            if entry[0] == toolBar:
                # Its extension showed it: show it in Classic too
                entry[1] = True
        if self._isToolBarAllowed(toolBar):
            return
        # An extension toolbar showed itself (for example the sequence browser): hide it again, unless the
        # user showed it from View > Toolbars (handled by _onToolBarToggledByUser() before the timer fires)
        qt.QTimer.singleShot(0, lambda: toolBar.hide() if self._active and not self._isToolBarAllowed(toolBar) else None)

    def _onToolBarToggledByUser(self, toolBar, checked):
        """The user showed or hid a toolbar from View > Toolbars: remember it in ``Studio/AllowedToolbars``."""
        if not self._active:
            return
        name = toolBar.objectName
        if name:
            allowed = self._allowedToolBars()
            if checked and name not in allowed:
                allowed.append(name)
            elif not checked and name in allowed:
                allowed.remove(name)
            else:
                return
            settings = qt.QSettings()
            if allowed:
                settings.setValue("Studio/AllowedToolbars", allowed)
            else:
                settings.remove("Studio/AllowedToolbars")
        elif checked and toolBar not in self._sessionAllowedToolBars:
            self._sessionAllowedToolBars.append(toolBar)
        elif not checked and toolBar in self._sessionAllowedToolBars:
            self._sessionAllowedToolBars.remove(toolBar)

    def _chromeShortcutActions(self):
        """Actions with a shortcut that only belong to the menubar, the status bar or hidden toolbars."""
        mainWindow = self.mainWindow
        roots = [mainWindow.menuBar()]
        if mainWindow.statusBar() is not None:
            roots.append(mainWindow.statusBar())
        roots += [toolBar for toolBar, slot in self._watchedToolBars]

        def isChrome(widget, depth=0):
            if widget is None or depth > 8:
                return False
            if widget.inherits("QMenu"):
                owners = associatedWidgets(widget.menuAction())
                if owners:
                    return all(isChrome(owner, depth + 1) for owner in owners)
            ancestor = widget
            while ancestor is not None and ancestor != mainWindow:
                if ancestor in roots:
                    return True
                ancestor = ancestor.parentWidget()
            return False

        actions = []
        for action in mainWindow.findChildren("QAction"):
            if action.objectName in _KEEP_UNATTACHED_ACTIONS or not action.shortcuts():
                continue
            if action.shortcutContext not in (qt.Qt.WindowShortcut, qt.Qt.ApplicationShortcut):
                continue
            owners = associatedWidgets(action)
            if not owners or mainWindow in owners:
                continue
            if all(isChrome(owner) for owner in owners):
                actions.append(action)
        return actions

    def _attachChromeShortcutActions(self):
        self._addedActions = []
        for action in self._chromeShortcutActions():
            self.mainWindow.addAction(action)
            self._addedActions.append(action)

    def _insertFooterSlot(self):
        contents = self._panelDock().widget()
        layout = contents.layout()
        self._footerSlot = qt.QWidget()
        self._footerSlot.setObjectName("StudioInspectorFooter")
        footerLayout = qt.QVBoxLayout(self._footerSlot)
        footerLayout.setContentsMargins(0, 0, 0, 0)
        footerLayout.setSpacing(0)
        self._footerSlot.hide()
        layout.insertWidget(layout.indexOf(self._modulePanel()) + 1, self._footerSlot)

    def _removeFooterSlot(self):
        if self._footerSlot is None:
            return
        self.setInspectorFooter(None)
        layout = self._panelDock().widget().layout()
        layout.removeWidget(self._footerSlot)
        self._footerSlot.hide()
        self._footerSlot.setObjectName("")
        self._footerSlot.deleteLater()
        self._footerSlot = None

    def _addTopBarShortcuts(self):
        for index, ws in enumerate(workspaces.WORKSPACES):
            self._addShortcut(f"Ctrl+Alt+{index + 1}", lambda key=ws.key: self.switchTo(key), _("{workspace} workspace").format(workspace=ws.title))
        # "Search modules" until the command palette (StudioLib/palette.py, W3-06) also finds tools and actions
        self._addShortcut("Ctrl+K", self.openPalette, _("Search modules"))
        self._addShortcut("Ctrl+/", self.toggleCheatSheet, _("Keyboard shortcuts"))
        for key, title, shortcut, iconName, layoutId in layouts.PICKER_ENTRIES:
            self._addShortcut(shortcut, lambda key=key: self.setLayoutFromPicker(key), _("{layout} layout").format(layout=title))

    #
    # Shortcuts
    #

    def _createShortcut(self, keySequence, callback, description, parent=None, context=qt.Qt.WindowShortcut):
        shortcut = qt.QShortcut(qt.QKeySequence(keySequence), parent if parent is not None else self.mainWindow)
        shortcut.setObjectName("StudioShortcut")
        shortcut.setContext(context)
        shortcut.setProperty("studioShortcut", True)
        shortcut.setProperty("studioDescription", description)
        shortcut.connect("activated()", callback)
        return shortcut

    def _addShortcut(self, keySequence, callback, description, parent=None, context=qt.Qt.WindowShortcut):
        shortcut = self._createShortcut(keySequence, callback, description, parent, context)
        self._shortcuts.append(shortcut)
        return shortcut

    @staticmethod
    def _deleteShortcut(shortcut):
        try:
            shortcut.setEnabled(False)
            shortcut.setObjectName("")
            shortcut.setProperty("studioShortcut", False)
            shortcut.deleteLater()
        except Exception:
            # Already deleted with its parent
            pass

    def shortcuts(self):
        """QShortcuts installed by Studio (global and for the current workspace)."""
        return list(self._shortcuts) + list(self._installedWorkspaceShortcuts)

    def addWorkspaceShortcut(self, wsKey, keySequence, callback, description=""):
        """Register a main window shortcut that is only active in the workspace ``wsKey``.

        It is installed when the workspace is entered and removed when it is left.
        """
        ws = workspaces.workspace(wsKey)
        if ws is None:
            raise ValueError(f"Unknown Studio workspace: {wsKey}")
        entries = self._workspaceShortcuts.setdefault(ws.key, [])
        entries.append((keySequence, callback, description))
        if self._active and self.currentWorkspace() == ws.key:
            self._installedWorkspaceShortcuts.append(self._createShortcut(keySequence, callback, description))

    def removeWorkspaceShortcut(self, wsKey, keySequence):
        ws = workspaces.workspace(wsKey)
        if ws is None:
            return
        portable = qt.QKeySequence(keySequence).toString()
        self._workspaceShortcuts[ws.key] = [entry for entry in self._workspaceShortcuts.get(ws.key, []) if qt.QKeySequence(entry[0]).toString() != portable]
        for shortcut in list(self._installedWorkspaceShortcuts):
            if shortcut.key.toString() == portable:
                self._installedWorkspaceShortcuts.remove(shortcut)
                self._deleteShortcut(shortcut)

    #
    # Workspaces
    #

    def switchTo(self, key, keepLayout=False):
        """Show the workspace ``key`` ("library", "draw" or "map"), in its remembered layout unless ``keepLayout``."""
        if not self._active:
            return
        self.workspaces.switchTo(key, keepLayout=keepLayout)

    def closeTransientModule(self):
        """Return from a transient module to the last workspace."""
        if self._active:
            self.workspaces.returnToLastWorkspace()

    def _onWorkspaceEntered(self, ws, shrinkPanel):
        self.topBar.setCurrentWorkspace(ws.key)
        self.panelHeader.setTitle(ws.title)
        for keySequence, callback, description in self._workspaceShortcuts.get(ws.key, []):
            self._installedWorkspaceShortcuts.append(self._createShortcut(keySequence, callback, description))
        self._showInspectorForModule()
        self._schedulePanelWidthUpdate(shrink=shrinkPanel)

    def _onCurrentWorkspaceReselected(self, ws):
        """The tab or the shortcut of the current workspace was used: bring the workspace back into view.

        The focus mode is left and an inspector that the user hid is shown again. An inspector that the
        workspace page hid itself (the Library empty state) stays hidden.
        """
        if self.focusMode():
            self.setFocusMode(False)
        if self._inspectorHiddenByUser and not self.isInspectorVisible():
            self.setInspectorVisible(True)

    def _onWorkspaceLeft(self, ws):
        for shortcut in self._installedWorkspaceShortcuts:
            self._deleteShortcut(shortcut)
        self._installedWorkspaceShortcuts = []
        self._primaryAction = None
        self._primaryActionLabel = ""
        self.setInspectorFooter(None)

    def _onTransientModuleEntered(self, moduleName):
        module = slicer.app.moduleManager().module(moduleName) if workspaces.moduleExists(moduleName) else None
        title = module.title if module is not None else moduleName
        self.topBar.setCurrentWorkspace(None)
        last = workspaces.workspace(self.workspaces.last or "library")
        self.panelHeader.setTitle(title, transient=True, closeToolTip=_("Close and return to {workspace}").format(workspace=last.title))
        self._showInspectorForModule()

    def _showInspectorForModule(self):
        # Selecting a module shows the module panel (qSlicerMainWindow): in focus mode, leave the
        # focus mode rather than showing the inspector without the top bar
        if self.focusMode():
            self.setFocusMode(False)
        self.setInspectorVisible(True)

    def _onModuleAdded(self, moduleName):
        if moduleName == "SegmentEditor":
            self._setSegmentEditorPresentation("studio")
        self._fitTextBrowsers()
        # A transient module may need a wider panel than the stored width
        self._schedulePanelWidthUpdate(shrink=False)

    def _prewarm(self, moduleName):
        if not self._active or not workspaces.moduleExists(moduleName):
            return
        module = slicer.app.moduleManager().module(moduleName)
        try:
            module.widgetRepresentation()
        except Exception:
            logging.error(f"Studio: cannot create the {moduleName} module widget:\n{traceback.format_exc()}")
        if moduleName == "SegmentEditor":
            self._setSegmentEditorPresentation("studio")

    @staticmethod
    def _setSegmentEditorPresentation(name):
        widget = getattr(slicer.modules, "SegmentEditorWidget", None)
        if widget is not None and hasattr(widget, "setPresentation"):
            try:
                widget.setPresentation(name)
            except Exception:
                logging.error(f"Studio: cannot set the Segment Editor presentation:\n{traceback.format_exc()}")

    def setLayoutFromPicker(self, key):
        """Apply a layout of the top bar picker ("focus", "single", "fourup" or "row")."""
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None:
            layoutManager.setLayout(layouts.pickerLayoutId(key, self.hoverTracker))

    #
    # Plug-ins and lazily imported pages
    #

    def _installPlugins(self):
        self._plugins = []
        for name in OPTIONAL_PLUGINS:
            module = self._importOptional(name)
            if module is None or not hasattr(module, "install"):
                continue
            try:
                module.install(self)
                self._plugins.append(module)
            except Exception:
                logging.error(f"Studio: {name}.install() failed:\n{traceback.format_exc()}")

    def _uninstallPlugins(self):
        for module in reversed(self._plugins):
            try:
                if hasattr(module, "uninstall"):
                    module.uninstall(self)
            except Exception:
                logging.error(f"Studio: {module.__name__}.uninstall() failed:\n{traceback.format_exc()}")
        self._plugins = []

    @staticmethod
    def _importOptional(name):
        """Import a Studio module that may not exist in this build (None if it does not)."""
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name != name:
                logging.error(f"Studio: cannot import {name}:\n{traceback.format_exc()}")
        except Exception:
            logging.error(f"Studio: cannot import {name}:\n{traceback.format_exc()}")
        return None

    def openFiles(self):
        """Open files (Ctrl+O): the Studio direct-load dialog if available, else Add Data."""
        importer = self._importOptional("StudioLib.importer")
        if importer is not None and hasattr(importer, "openFilesDialog"):
            importer.openFilesDialog(self)
            return
        action = self._fileAddDataAction()
        if action is not None:
            action.trigger()
        else:
            slicer.util.openAddDataDialog()

    def openPalette(self):
        """Command palette (Ctrl+K), or the module finder if the palette is not available."""
        palette = self._importOptional("StudioLib.palette")
        if palette is not None and hasattr(palette, "open"):
            palette.open(self)
            return
        self.mainWindow.moduleSelector().showModuleFinder()

    def toggleCheatSheet(self):
        """Keyboard shortcut overlay (Ctrl+/)."""
        cheatsheet = self._importOptional("StudioLib.cheatsheet")
        if cheatsheet is not None and hasattr(cheatsheet, "toggle"):
            cheatsheet.toggle(self)
            return
        self.toast(_("Ctrl+K search · Ctrl+Alt+1/2/3 workspaces · Alt+1..4 layouts · {inspector} panel · F11 focus mode · Ctrl+Alt+0 Classic").format(
            inspector=self.inspectorShortcutText(alternate=True)), 5000)

    #
    # Inspector
    #

    @property
    def panelWidth(self):
        """Width of the inspector (300-480 px, persisted in Studio/PanelWidth)."""
        width = slicer.util.settingsValue("Studio/PanelWidth", DEFAULT_PANEL_WIDTH, converter=int)
        return min(max(width, MIN_PANEL_WIDTH), MAX_PANEL_WIDTH)

    @panelWidth.setter
    def panelWidth(self, width):
        width = min(max(int(width), MIN_PANEL_WIDTH), MAX_PANEL_WIDTH)
        qt.QSettings().setValue("Studio/PanelWidth", width)
        if self._active:
            self._updatePanelWidth(shrink=True)

    def resetPanelWidth(self):
        self.panelWidth = DEFAULT_PANEL_WIDTH

    def _schedulePanelWidthUpdate(self, shrink):
        """Update the inspector width when the event loop runs again (the module widget is laid out
        first). Several requests are coalesced into one update; ``shrink`` is kept if any asked for it.
        """
        self._pendingPanelShrink = self._pendingPanelShrink or shrink
        if self._panelUpdateTimer is None:
            self._panelUpdateTimer = qt.QTimer()
            self._panelUpdateTimer.setSingleShot(True)
            self._panelUpdateTimer.setInterval(0)
            self._panelUpdateTimer.connect("timeout()", self._onPanelUpdateTimeout)
        self._panelUpdateTimer.start()

    def _onPanelUpdateTimeout(self):
        shrink = self._pendingPanelShrink
        self._pendingPanelShrink = False
        self._updatePanelWidth(shrink)

    def _updatePanelWidth(self, shrink=False):
        """Size the inspector to the stored width, or to the width that its module needs (SPEC 2.5).

        The module panel has an explicit minimum width and wider contents scroll horizontally
        (``_setModulePanelConstrained()``): the inspector width does not follow every change of the
        module widget. Instead:

        - The workspaces share one width: the stored width, or the widest layout minimum that a workspace
          module needed so far (``_workspacePanelWidth()``, kept across sessions), and never narrower. Once
          every tool was used, choosing a tool or a workspace does not move the views under the cursor,
          and the options are not clipped. The user cannot drag the inspector narrower than that either.
        - A transient module may make the inspector as wide as its widget needs.

        With ``shrink`` (entering a workspace, setting the width, showing the inspector again) the
        inspector gets back to that width; otherwise it only widens.
        """
        if not self._active:
            return
        dock = self._panelDock()
        if dock.isHidden():
            return
        contents = dock.widget()
        target = self.panelWidth
        needed = self._modulePanelNeededWidth()
        contentsMinimumWidth = _MODULE_PANEL_MINIMUM_WIDTH
        if self.currentWorkspace() is None:
            target = max(target, needed)
        else:
            self._workspacePanelWidth(self.currentModuleName(), min(needed, MAX_WORKSPACE_PANEL_WIDTH))
            # The workspaces share one width, so that switching between them does not resize (and render
            # again) the views
            held = max(self._workspacePanelWidths.values(), default=0)
            target = max(target, held)
            # The dock widget is as wide as its contents plus its margins
            contentsMinimumWidth = max(contentsMinimumWidth, held - max(0, dock.width - contents.width))
        if contents.minimumWidth != contentsMinimumWidth:
            contents.setMinimumWidth(contentsMinimumWidth)
        if dock.width < target or (shrink and dock.width != target):
            self.mainWindow.resizeDocks([dock], [target], qt.Qt.Horizontal)

    def _modulePanelNeededWidth(self):
        """Width of the inspector at which the module widget is not clipped (its layout minimum).

        Room for the vertical scroll bar is always kept, so that a module widget getting taller than the
        inspector does not widen it.
        """
        dock = self._panelDock()
        modulePanel = self._modulePanel()
        scrollArea = modulePanel.findChild("QScrollArea", "ScrollArea") if modulePanel is not None else None
        if scrollArea is None or scrollArea.widget() is None:
            # The minimum size hint of the module panel is the width of the module widget (qSlicerModulePanel)
            return modulePanel.minimumSizeHint.width() + max(0, dock.width - modulePanel.width) if modulePanel is not None else 0
        chrome = max(0, dock.width - scrollArea.width) + 2 * scrollArea.frameWidth + scrollArea.verticalScrollBar().sizeHint.width()
        return scrollArea.widget().minimumSizeHint.width() + chrome

    def _workspacePanelWidth(self, moduleName, needed):
        """Widest inspector width that the workspace module ``moduleName`` needed so far, at least ``needed``.

        Kept in ``Studio/WorkspacePanelWidths/<module name>`` with a signature of what the layout depends on
        (``_layoutSignature()``): another Slicer build or application font measures again.
        """
        if not moduleName:
            return needed
        held = self._workspacePanelWidths.get(moduleName)
        key = f"{_WORKSPACE_PANEL_WIDTHS_GROUP}/{moduleName}"
        if held is None:
            held = 0
            stored = str(qt.QSettings().value(key, "") or "")
            width, _separator, signature = stored.partition("@")
            if signature == self._layoutSignature():
                try:
                    held = min(max(int(width), 0), MAX_WORKSPACE_PANEL_WIDTH)
                except ValueError:
                    held = 0
        if needed > held:
            held = needed
            qt.QSettings().setValue(key, f"{held}@{self._layoutSignature()}")
        self._workspacePanelWidths[moduleName] = held
        return held

    @staticmethod
    def _layoutSignature():
        """What the layout minimum of a module widget depends on: the Slicer build and the application font."""
        font = slicer.app.font()
        return f"{slicer.app.applicationVersion}-{slicer.app.revision}-{font.family()}-{font.pointSizeF()}-{font.pixelSize()}"

    def resetWorkspacePanelWidths(self):
        """Forget the widths needed by the workspace modules (they are measured again as tools are used)."""
        self._workspacePanelWidths = {}
        qt.QSettings().remove(_WORKSPACE_PANEL_WIDTHS_GROUP)
        if self._active:
            self._updatePanelWidth(shrink=True)

    def _setModulePanelConstrained(self, constrained):
        """Keep the inspector width independent of the width of the module widget (Studio), or restore Classic.

        The module panel reports the width of the module widget as its minimum size hint, which sets the
        minimum width of the dock widget: selecting a tool with wide options (Paint: 425 -> 550 px) widened
        the inspector and shrank the views. In Studio the module panel gets an explicit minimum width and
        its scroll area a horizontal scroll bar when needed.
        """
        modulePanel = self._modulePanel()
        if modulePanel is None:
            return
        # The dock widget contents (module panel, footer, Data Probe) report the module widget width as
        # their minimum size too (their size policy does not shrink below the size hint)
        contents = self._panelDock().widget()
        scrollArea = modulePanel.findChild("QScrollArea", "ScrollArea")
        if constrained:
            if self._classicModulePanelState is None:
                self._classicModulePanelState = (modulePanel.minimumWidth, contents.minimumWidth,
                                                 scrollArea.horizontalScrollBarPolicy if scrollArea is not None else None)
            modulePanel.setMinimumWidth(_MODULE_PANEL_MINIMUM_WIDTH)
            contents.setMinimumWidth(_MODULE_PANEL_MINIMUM_WIDTH)
            if scrollArea is not None:
                scrollArea.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAsNeeded)
                # A tool chosen in the module widget changes its layout minimum: see _updatePanelWidth()
                if self._eventFilter is not None and scrollArea.widget() is not None and self._watchedPanelContents is None:
                    self._watchedPanelContents = scrollArea.widget()
                    self._watchedPanelContents.installEventFilter(self._eventFilter)
            self._fitTextBrowsers()
            return
        self._unfitTextBrowsers()
        if self._classicModulePanelState is not None:
            modulePanelMinimumWidth, contentsMinimumWidth, policy = self._classicModulePanelState
            modulePanel.setMinimumWidth(modulePanelMinimumWidth)
            contents.setMinimumWidth(contentsMinimumWidth)
            if scrollArea is not None and policy is not None:
                scrollArea.setHorizontalScrollBarPolicy(policy)
            self._classicModulePanelState = None

    def _fitTextBrowsers(self):
        """Give the fitted text browsers of the module panel (the Segment Editor effect help) an explicit
        minimum width. Their minimum size hint is their current width, so that they never let the panel
        get narrower once it was wide.
        """
        if not self._active:
            return
        modulePanel = self._modulePanel()
        if modulePanel is None:
            return
        for browser in modulePanel.findChildren("ctkFittedTextBrowser"):
            if browser.minimumWidth == 0 and browser not in self._fittedTextBrowsers:
                browser.setMinimumWidth(1)
                self._fittedTextBrowsers.append(browser)

    def _unfitTextBrowsers(self):
        for browser in self._fittedTextBrowsers:
            try:
                if browser.minimumWidth == 1:
                    browser.setMinimumWidth(0)
            except Exception:
                # Deleted with its module widget
                pass
        self._fittedTextBrowsers = []

    def _onDockResized(self):
        # Persist the width when the user drags the separator between the views and the inspector
        if not (qt.QApplication.mouseButtons() & qt.Qt.LeftButton):
            return
        if self._panelWidthTimer is None:
            self._panelWidthTimer = qt.QTimer()
            self._panelWidthTimer.setSingleShot(True)
            self._panelWidthTimer.setInterval(300)
            self._panelWidthTimer.connect("timeout()", self._persistDraggedPanelWidth)
        self._panelWidthTimer.start()

    def _persistDraggedPanelWidth(self):
        if not self._active:
            return
        dock = self._panelDock()
        width = min(max(dock.width, MIN_PANEL_WIDTH), MAX_PANEL_WIDTH)
        if width != self.panelWidth:
            qt.QSettings().setValue("Studio/PanelWidth", width)
        if width != dock.width:
            # Dragged out of the 300-480 px range: snap back into it (but not narrower than a workspace
            # module needs, see _updatePanelWidth())
            self._updatePanelWidth(shrink=True)

    def setInspectorVisible(self, visible):
        """Show or hide the inspector (the module panel dock widget)."""
        if not self._active:
            return
        if visible:
            self._inspectorHiddenByUser = False
        dock = self._panelDock()
        if dock.isHidden() == (not visible):
            return
        # When shown, the width is updated on its Show event
        dock.setVisible(visible)

    def isInspectorVisible(self):
        return self._active and not self._panelDock().isHidden()

    def toggleInspector(self):
        r"""Show or hide the inspector (``Ctrl+\``, the app menu). Hiding it shows how to get it back."""
        if not self._active:
            return
        if self.focusMode():
            # The inspector is hidden by the focus mode: leave it
            self.setFocusMode(False)
            self.setInspectorVisible(True)
            return
        visible = not self.isInspectorVisible()
        self.setInspectorVisible(visible)
        if not visible:
            self._onInspectorHiddenByUser()

    def _onPanelToggleTriggered(self, checked):
        # View > Module Panel (Ctrl+5) toggles the dock widget directly
        if checked:
            self._inspectorHiddenByUser = False
        else:
            self._onInspectorHiddenByUser()

    def _onInspectorHiddenByUser(self):
        global _panelHiddenHintShown
        self._inspectorHiddenByUser = True
        if not _panelHiddenHintShown:
            _panelHiddenHintShown = True
            self.toast(_("Panel hidden · {key} to show").format(key=self.inspectorShortcutText(alternate=True)), 3000)

    @staticmethod
    def inspectorShortcutText(alternate=False):
        r"""The key that shows or hides the inspector, as displayed on this platform (for example "Ctrl+\").

        With ``alternate``, both keys ("Ctrl+\ or F6"): not every keyboard layout can type Ctrl+\.
        """
        text = qt.QKeySequence(INSPECTOR_SHORTCUT).toString(qt.QKeySequence.NativeText)
        if not alternate:
            return text
        return _("{key} or {alternateKey}").format(
            key=text, alternateKey=qt.QKeySequence(INSPECTOR_ALTERNATE_SHORTCUT).toString(qt.QKeySequence.NativeText))

    def setInspectorFooter(self, widget=None):
        """Show ``widget`` in the sticky footer below the module panel (None: no footer)."""
        if self._footerSlot is None:
            return
        layout = self._footerSlot.layout()
        if self._footerWidget is not None and self._footerWidget != widget:
            try:
                # The owner of the footer widget keeps it (it is not deleted here)
                layout.removeWidget(self._footerWidget)
                self._footerWidget.hide()
                self._footerWidget.setParent(None)
            except Exception:
                # The previous footer widget was deleted by its owner
                pass
        self._footerWidget = widget
        if widget is not None:
            layout.addWidget(widget)
            widget.show()
        self._footerSlot.setVisible(widget is not None)

    def inspectorFooter(self):
        return self._footerWidget

    def setDataProbeVisible(self, visible):
        dataProbe = self._dataProbe()
        if dataProbe is not None:
            dataProbe.setVisible(visible)

    #
    # Canvas
    #

    def setCanvas(self, widget=None):
        """Show ``widget`` over the view area (rendering of the views is paused), or hide the canvas (None)."""
        if self._canvas is not None and self._canvas != widget:
            try:
                self._canvas.hide()
            except Exception:
                # The canvas was deleted by its owner
                pass
        self._canvas = widget
        layoutManager = slicer.app.layoutManager()
        if widget is not None:
            centralWidget = self.mainWindow.centralWidget()
            if widget.parentWidget() != centralWidget:
                widget.setParent(centralWidget)
            widget.setGeometry(centralWidget.rect)
            widget.show()
            widget.raise_()
            if self._toast is not None and self._toast.isVisible():
                self._toast.label.raise_()
            if self._focusModePill is not None:
                self._focusModePill.raise_()
            if not self._renderPausedByCanvas and layoutManager is not None:
                layoutManager.setRenderPaused(True)
                self._renderPausedByCanvas = True
        elif self._renderPausedByCanvas:
            if layoutManager is not None:
                layoutManager.setRenderPaused(False)
            self._renderPausedByCanvas = False

    def canvas(self):
        return self._canvas

    def isRenderPausedByCanvas(self):
        return self._renderPausedByCanvas

    #
    # Top bar
    #

    def toast(self, text, ms=1600):
        """Show a short message near the bottom of the view area."""
        if self._toast is not None:
            self._toast.show(text, ms)

    def setBreadcrumb(self, primary, secondary=None, onClick=None):
        """Set the top bar breadcrumb ("study / subject"). ``primary=None`` shows the first volume name."""
        if self.topBar is not None:
            self.topBar.setBreadcrumb(primary, secondary, onClick)

    def setStatusChip(self, text=None, level="neutral"):
        """Show a status chip next to the workspace switcher (level: neutral, busy, ok, warn). None hides it."""
        if self.topBar is not None:
            self.topBar.setStatusChip(text, level)

    def setPrimaryAction(self, callback=None, label=""):
        """Set the action of Ctrl+Enter (the primary button of the current workspace)."""
        self._primaryAction = callback
        self._primaryActionLabel = label

    def primaryActionLabel(self):
        return self._primaryActionLabel if self._primaryAction is not None else ""

    def triggerPrimaryAction(self):
        if self._primaryAction is None:
            return
        try:
            self._primaryAction()
        except Exception:
            logging.error(f"Studio primary action failed:\n{traceback.format_exc()}")

    #
    # Focus mode
    #

    def focusMode(self):
        if hasattr(self.mainWindow, "focusMode"):
            # Slot or property of the main window (W3-03)
            focusMode = self.mainWindow.focusMode
            return bool(focusMode() if callable(focusMode) else focusMode)
        return self._focusModeHidden is not None

    def setFocusMode(self, enabled):
        """Hide (or show again) the inspector and the top bar.

        In the focus mode a low-emphasis "Exit focus mode" pill stays in the top-right corner of the
        views, and entering it says how to leave it (F11 or Esc).
        """
        wasFocusMode = self.focusMode()
        if hasattr(self.mainWindow, "setFocusMode"):
            if self._active or not enabled:
                self.mainWindow.setFocusMode(enabled)
        elif enabled != (self._focusModeHidden is not None):
            if enabled:
                if not self._active:
                    return
                widgets = [self._panelDock(), self.topBar.toolBar]
                self._focusModeHidden = [widget for widget in widgets if not widget.isHidden()]
                for widget in self._focusModeHidden:
                    widget.hide()
            else:
                for widget in self._focusModeHidden:
                    widget.show()
                self._focusModeHidden = None
        self._updateFocusModeIndicators(wasFocusMode)

    def _updateFocusModeIndicators(self, wasFocusMode):
        focusMode = self._active and self.focusMode()
        if self._focusModePill is not None:
            if focusMode:
                self._focusModePill.show()
            else:
                self._focusModePill.hide()
        if focusMode and not wasFocusMode:
            self.toast(_("Focus mode · {keys} or Esc to exit").format(
                keys=qt.QKeySequence(FOCUS_MODE_SHORTCUTS[0]).toString(qt.QKeySequence.NativeText)), 2500)

    def toggleFocusMode(self):
        self.setFocusMode(not self.focusMode())

    #
    # Events
    #

    #: Widgets that keep Esc for themselves (text input, consoles): the Esc stack does not apply
    _ESCAPE_EXEMPT_FOCUS_CLASSES = ("QLineEdit", "QAbstractSpinBox", "QTextEdit", "QPlainTextEdit", "ctkConsole")

    def handleEscape(self):
        """Back out of the innermost navigation state, as Esc does in Studio. Return True if a state was left.

        The Esc stack, handled in this order (one state per key press):

        1. Place mode or Adjust window/level mode of the mouse -> the default mode (view transform);
        2. a maximized view (double-click on a view, or the view bar button) -> the layout is restored;
        3. the focus mode (F11) -> left.

        If none applies, the key is not consumed and goes on to its other handlers: the Segment Editor's
        Esc then deselects the active tool, and the views cancel their current interaction. Popups and
        dialogs close themselves, and text fields and consoles keep their own Esc (see ``_onEscapeKey()``).
        A transient module is left with the close button of the inspector header, not with Esc.
        """
        if not self._active:
            return False
        modes = (slicer.vtkMRMLInteractionNode.Place, slicer.vtkMRMLInteractionNode.AdjustWindowLevel)
        handled = False
        for interactionNode in slicer.util.getNodesByClass("vtkMRMLInteractionNode"):
            if interactionNode.GetCurrentInteractionMode() in modes:
                interactionNode.SwitchToViewTransformMode()
                handled = True
        if handled:
            return True
        layoutManager = slicer.app.layoutManager()
        layoutNode = layoutManager.layoutLogic().GetLayoutNode() if layoutManager is not None else None
        if layoutNode is not None and layoutNode.GetNumberOfMaximizedViewNodes() > 0:
            viewNode = layoutNode.GetMaximizedViewNode(layoutNode.GetNumberOfMaximizedViewNodes() - 1)
            layoutManager.removeMaximizedViewNode(viewNode)
            return True
        if self.focusMode():
            self.setFocusMode(False)
            return True
        return False

    @staticmethod
    def _hasKeyModifiers(event):
        """True if Shift, Ctrl, Alt or Meta is held (the keypad modifier does not count)."""
        mask = qt.Qt.ShiftModifier | qt.Qt.ControlModifier | qt.Qt.AltModifier | qt.Qt.MetaModifier
        try:
            # Explicit integers: a QFlags value may not compare equal to an enum value (Qt 6)
            return bool(int(event.modifiers()) & int(mask))
        except (TypeError, ValueError):
            return bool(event.modifiers() & mask)

    def _onEscapeKey(self, event):
        """Esc reached the main window (the focus widget did not use it). Return True to consume it.

        The Esc stack runs when Qt asks whether a shortcut may take the key (ShortcutOverride): accepting
        it there keeps the window-wide Esc shortcut of the Segment Editor from firing for the same press.
        The key press that follows is then consumed. Key presses without a shortcut override (sent
        directly to a widget) run the stack too.
        """
        eventType = event.type()
        if eventType == qt.QEvent.ShortcutOverride:
            self._escapeHandled = False
        elif self._escapeHandled:
            self._escapeHandled = False
            return True
        if self._hasKeyModifiers(event):
            return False
        if slicer.app.activePopupWidget() is not None or slicer.app.activeModalWidget() is not None:
            return False
        focusWidget = slicer.app.focusWidget()
        if focusWidget is not None and any(focusWidget.inherits(className) for className in self._ESCAPE_EXEMPT_FOCUS_CLASSES):
            return False
        if not self.handleEscape():
            return False
        if eventType == qt.QEvent.ShortcutOverride:
            event.accept()
            self._escapeHandled = True
        return True

    def _onEvent(self, obj, event):
        """Event filter of the main window, its central widget and the inspector. Return True to consume the event."""
        eventType = event.type()
        if eventType in (qt.QEvent.ShortcutOverride, qt.QEvent.KeyPress):
            if obj == self.mainWindow and event.key() == qt.Qt.Key_Escape:
                return self._onEscapeKey(event)
            return False
        if eventType == qt.QEvent.Resize:
            if obj == self.mainWindow.centralWidget():
                if self._canvas is not None:
                    self._canvas.setGeometry(obj.rect)
                if self._toast is not None:
                    self._toast.updatePosition()
                if self._focusModePill is not None:
                    self._focusModePill.updatePosition()
            elif obj == self._panelDock():
                self._onDockResized()
            elif obj == self.mainWindow:
                # In a small window the views keep their minimum width and the inspector gets narrower
                # than the stored width: give it back when the window grows
                self._schedulePanelWidthUpdate(shrink=False)
        elif eventType in (qt.QEvent.ChildAdded, qt.QEvent.ChildPolished, qt.QEvent.LayoutRequest) and obj == self.mainWindow:
            # Toolbars added later (extensions) are hidden like the others. A toolbar constructed with
            # the main window as parent is a child before it is a QToolBar, and it may be put in a
            # toolbar area (which requests a layout) only later: look for new toolbars when the event
            # loop runs again.
            if self._toolBarScanTimer is not None:
                self._toolBarScanTimer.start()
        elif eventType == qt.QEvent.LayoutRequest and obj == self._watchedPanelContents:
            # The module widget changed (a tool was chosen): the inspector may need to widen once
            self._schedulePanelWidthUpdate(shrink=False)
        elif eventType == qt.QEvent.Show and obj == self._panelDock():
            # Shown again (Ctrl+\, Ctrl+5, focus mode): back to the width of the workspaces
            self._schedulePanelWidthUpdate(shrink=True)
        return False

    def _removeEventFilters(self):
        if self._eventFilter is None:
            return
        for obj in (self.mainWindow, self.mainWindow.centralWidget(), self._panelDock()):
            obj.removeEventFilter(self._eventFilter)
        if self._watchedPanelContents is not None:
            try:
                self._watchedPanelContents.removeEventFilter(self._eventFilter)
            except Exception:
                # Deleted with the module panel
                pass
            self._watchedPanelContents = None
        self._eventFilter = None

    #
    # Cleanup
    #

    def dispose(self):
        """Restore Classic without remembering it, and remove every trace of the shell (tests)."""
        global _shell
        self.classic(persist=False)
        if self._classicSwitchAction is not None:
            self.mainWindow.removeAction(self._classicSwitchAction)
            viewMenu = self.mainWindow.findChild("QMenu", "ViewMenu")
            if viewMenu is not None:
                viewMenu.removeAction(self._classicSwitchAction)
                if self._classicSwitchSeparator is not None:
                    viewMenu.removeAction(self._classicSwitchSeparator)
                    self._classicSwitchSeparator = None
            self._classicSwitchAction.setObjectName("")
            self._classicSwitchAction.deleteLater()
            self._classicSwitchAction = None
        if _closeGuard is not None and _closeGuard.classicStateProvider == self._classicStateIfActive:
            _closeGuard.classicStateProvider = None
        if _shell is self:
            _shell = None

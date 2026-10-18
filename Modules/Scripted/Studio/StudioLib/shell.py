"""Shell of Slicer Studio, the streamlined default user interface (SPEC 2).

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
``setInspectorVisible()``, ``setPrimaryAction()``, ``addWorkspaceShortcut()``, ``studySession``,
``classic()``, ``studio()`` and ``panelWidth``.

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
OPTIONAL_PLUGINS = ["StudioLib.importer", "StudioLib.probe", "StudioLib.journal", "StudioLib.study_ui"]

DEFAULT_PANEL_WIDTH = 340
MIN_PANEL_WIDTH = 300
MAX_PANEL_WIDTH = 480
PANEL_HEADER_HEIGHT = 40
PANEL_HEADER_BUTTON_SIZE = 26

#: Main window actions that keep their classic behavior in Studio: they are not attached to the
#: main window (Ctrl+Z/Y are owned by the Segment Editor, Ctrl+6 by the focus mode, Ctrl+O by Studio)
_KEEP_UNATTACHED_ACTIONS = ("EditUndoAction", "EditRedoAction", "WindowMaximizeViewAction", "FileAddDataAction")

_PORTABLE_LAYOUT = slicer.vtkMRMLLayoutNode.SlicerLayoutConventionalView

_shell = None
_closeGuard = None


def instance():
    """Return the live Studio shell, or None (Classic, testing, no main window, Studio disabled)."""
    if _shell is not None and _shell.isActive():
        return _shell
    return None


def _settingsBool(key, default):
    return slicer.util.settingsValue(key, default, converter=slicer.util.toBool)


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
    shell.apply(startTime=startTime)


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

    def showHelp(self):
        moduleName = self.shell.currentModuleName()
        module = slicer.app.moduleManager().module(moduleName) if moduleName and workspaces.moduleExists(moduleName) else None
        if module is None:
            return
        html = f"<h3>{module.title}</h3>{module.helpText}"
        if module.acknowledgementText:
            html += f"<h4>{_('Acknowledgment')}</h4>{module.acknowledgementText}"
        menu = qt.QMenu(self.helpButton)
        menu.setObjectName("StudioHelpPopover")
        browser = qt.QTextBrowser()
        browser.setObjectName("StudioHelpBrowser")
        browser.setOpenExternalLinks(True)
        browser.setFrameShape(qt.QFrame.NoFrame)
        browser.setHtml(html)
        browser.setFixedSize(380, 360)
        action = qt.QWidgetAction(menu)
        action.setDefaultWidget(browser)
        menu.addAction(action)
        menu.exec_(self.helpButton.mapToGlobal(qt.QPoint(self.helpButton.width - 380, self.helpButton.height)))
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
            self.shell._onEvent(obj, event)
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
        self._tabShortcut = None
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
        self._panelWidthTimer = None
        #: Minimum inspector width of each workspace module, as last measured while it was shown
        self._workspaceMinimumWidths = {}
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
        """Switch to the Studio interface and remember the choice."""
        qt.QSettings().setValue("Studio/Mode", "studio")
        self.apply()

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
        for shortcut in self._installedWorkspaceShortcuts + self._shortcuts:
            self._deleteShortcut(shortcut)
        self._installedWorkspaceShortcuts = []
        self._shortcuts = []
        self._tabShortcut = None
        if self._toolBarScanTimer is not None:
            self._toolBarScanTimer.stop()
            self._toolBarScanTimer = None
        for toolBar, slot in self._watchedToolBars:
            try:
                toolBar.disconnect("visibilityChanged(bool)", slot)
            except Exception:
                pass
        self._watchedToolBars = []
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
        dock.setFeatures(self._classicDockFeatures)

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

    def apply(self, startTime=None):
        """Show the Studio interface (SPEC 2.3)."""
        if self._active:
            return
        if startTime is None:
            startTime = time.perf_counter()
        try:
            self._apply()
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

    def _apply(self):
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
        self._classicDockFeatures = dock.features
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
        self._workspaceMinimumWidths = {}
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
            self._addShortcut("F11", self.toggleFocusMode, _("Focus mode"))
            self._addShortcut("Ctrl+6", self.toggleFocusMode, _("Focus mode"))
        self._tabShortcut = self._addShortcut("Tab", self.toggleInspector, _("Show or hide the inspector"),
                                              parent=mainWindow.centralWidget(), context=qt.Qt.WidgetWithChildrenShortcut)
        self._tabShortcut.setEnabled(False)

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
        mainWindow.resizeDocks([dock], [self.panelWidth], qt.Qt.Horizontal)

        # 5. Top bar
        self.hoverTracker = layouts.HoveredSliceViewTracker()
        self.topBar = topbar.TopBar(self)
        mainWindow.addToolBar(qt.Qt.TopToolBarArea, self.topBar.toolBar)
        self.topBar.toolBar.show()
        self._addTopBarShortcuts()
        self._toast = hud.Toast(mainWindow.centralWidget())

        # 6. Layouts (registered in C++ by the fork, only added here on builds that lack them)
        layouts.register()

        self._connect(mainWindow.moduleSelector(), "moduleSelected(QString)", self.workspaces.onModuleSelected)
        self._connect(modulePanel, "moduleAdded(QString)", self._onModuleAdded)
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None:
            self._connect(layoutManager, "layoutChanged(int)", self.workspaces.onLayoutChanged)
        self._connect(slicer.app, "focusChanged(QWidget*,QWidget*)", self._onFocusChanged)
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
        startWorkspace = slicer.util.settingsValue("Studio/StartWorkspace", "Library")
        if str(startWorkspace).lower() == "last":
            startWorkspace = slicer.util.settingsValue("Studio/LastWorkspace", "Library")
        if workspaces.workspace(startWorkspace) is None or not workspaces.isAvailable(workspaces.workspace(startWorkspace)):
            startWorkspace = "library"
        self.switchTo(startWorkspace)

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
            if toolBar.objectName not in self._classicToolBarNames:
                # restoreState() does not restore it: remember if it is shown, or about to be shown by
                # the main window layout (hidden but not explicitly)
                visible = not (toolBar.isHidden() and toolBar.testAttribute(qt.Qt.WA_WState_ExplicitShowHide))
                self._unsavedToolBars.append([toolBar, visible])
        if toolBar.objectName not in self._allowedToolBars():
            toolBar.hide()

    @staticmethod
    def _allowedToolBars():
        allowed = slicer.util.settingsValue("Studio/AllowedToolbars", [])
        if isinstance(allowed, str):
            allowed = [allowed] if allowed else []
        return list(allowed or [])

    def _onToolBarVisibilityChanged(self, toolBar, visible):
        if not visible or not self._active:
            return
        for entry in self._unsavedToolBars:
            if entry[0] == toolBar:
                # Its extension showed it: show it in Classic too
                entry[1] = True
        if toolBar.objectName in self._allowedToolBars():
            return
        # An extension toolbar showed itself (for example the sequence browser): hide it again
        qt.QTimer.singleShot(0, lambda: toolBar.hide() if self._active and toolBar.objectName not in self._allowedToolBars() else None)

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
                owners = widget.menuAction().associatedWidgets()
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
            owners = action.associatedWidgets()
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
        self._addShortcut("Ctrl+K", self.openPalette, _("Search tools and actions"))
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

    def switchTo(self, key):
        """Show the workspace ``key`` ("library", "draw" or "map")."""
        if not self._active:
            return
        self.workspaces.switchTo(key)

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
        # The module widget may need a wider panel than the stored width
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
        self.toast(_("Ctrl+K search · Ctrl+Alt+1/2/3 workspaces · Alt+1..4 layouts · Ctrl+Alt+0 Classic"), 4000)

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
        # Queued: the module widget is laid out first
        qt.QTimer.singleShot(0, lambda: self._updatePanelWidth(shrink))

    def _updatePanelWidth(self, shrink=False):
        """Size the inspector to the stored width, or wider if the module needs it.

        The workspaces share one width: the stored width, or the largest minimum width of the
        workspace modules (as last measured while each one was shown), so that switching between
        workspaces does not resize (and render again) the views. A transient module may make the
        inspector wider; with ``shrink`` (entering a workspace, setting the width) it gets back
        to the width of the workspaces.
        """
        if not self._active:
            return
        dock = self._panelDock()
        if dock.isHidden():
            return
        minimumWidth = dock.minimumSizeHint.width()
        current = self.currentWorkspace()
        if current is not None:
            if self.currentModuleName() == workspaces.workspace(current).moduleName:
                self._workspaceMinimumWidths[current] = minimumWidth
            minimumWidth = max([minimumWidth, *self._workspaceMinimumWidths.values()])
        target = max(self.panelWidth, minimumWidth)
        if dock.width < target or (shrink and dock.width != target):
            self.mainWindow.resizeDocks([dock], [target], qt.Qt.Horizontal)

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
            # Dragged out of the 300-480 px range: snap back into it (but not narrower than the module needs)
            self._updatePanelWidth(shrink=True)

    def setInspectorVisible(self, visible):
        """Show or hide the inspector (the module panel dock widget)."""
        if not self._active:
            return
        dock = self._panelDock()
        if dock.isHidden() == (not visible):
            return
        # When shown, the width is updated on its Show event
        dock.setVisible(visible)

    def isInspectorVisible(self):
        return self._active and not self._panelDock().isHidden()

    def toggleInspector(self):
        self.setInspectorVisible(not self.isInspectorVisible())

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
        """Hide (or show again) the inspector and the top bar."""
        if hasattr(self.mainWindow, "setFocusMode"):
            if self._active or not enabled:
                self.mainWindow.setFocusMode(enabled)
            return
        if enabled == (self._focusModeHidden is not None):
            return
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

    def toggleFocusMode(self):
        self.setFocusMode(not self.focusMode())

    #
    # Events
    #

    def _onFocusChanged(self, old, now):
        if self._tabShortcut is None:
            return
        # Tab toggles the inspector only while a view has the keyboard focus
        isView = False
        widget = now
        for level in range(3):
            if widget is None:
                break
            if widget.inherits("ctkVTKAbstractView"):
                isView = True
                break
            widget = widget.parentWidget()
        self._tabShortcut.setEnabled(isView)

    def _onEvent(self, obj, event):
        eventType = event.type()
        if eventType == qt.QEvent.Resize:
            if obj == self.mainWindow.centralWidget():
                if self._canvas is not None:
                    self._canvas.setGeometry(obj.rect)
                if self._toast is not None:
                    self._toast.updatePosition()
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
        elif eventType == qt.QEvent.Show and obj == self._panelDock():
            # Shown again (Tab, Ctrl+5, focus mode): back to the width of the workspaces
            self._schedulePanelWidthUpdate(shrink=True)

    def _removeEventFilters(self):
        if self._eventFilter is None:
            return
        for obj in (self.mainWindow, self.mainWindow.centralWidget(), self._panelDock()):
            obj.removeEventFilter(self._eventFilter)
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

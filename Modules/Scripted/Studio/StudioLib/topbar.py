"""Top bar of Slicer Studio (SPEC 2.4).

A 44 px toolbar ``StudioTopBar``, left to right:

- the app menu (``≡``) and the breadcrumb;
- the workspace switcher [Library | Draw | Map] (``Ctrl+Alt+1/2/3``) and a status chip;
- Undo/Redo (Draw only), the layout picker (``Alt+1..4``), the search pill (``Ctrl+K``)
  and the error badge.
"""

import logging
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import appmenu, layouts, ui, workspaces

TOP_BAR_HEIGHT = 44
SEARCH_PILL_WIDTH = 240
ICON_SIZE = 18
UNDO_REDO_POLL_INTERVAL_MS = 250

_ERROR_LEVEL = 0x40  # ctkErrorLogLevel::Error
_FATAL_LEVEL = 0x100  # ctkErrorLogLevel::Fatal (Critical is 0x80, None is 0x200)
_ERROR_LEVEL_NAMES = ("Error", "Critical", "Fatal")


def _toolButton(objectName, iconName, toolTip, role="ghost"):
    button = qt.QToolButton()
    button.setObjectName(objectName)
    button.setToolTip(toolTip)
    button.setAutoRaise(True)
    button.setIconSize(qt.QSize(ICON_SIZE, ICON_SIZE))
    button.setFixedSize(30, 30)
    button.setFocusPolicy(qt.Qt.NoFocus)
    button.setProperty("studioIcon", iconName)
    ui.setRole(button, role)
    return button


class TopBar:
    """Builds and updates the Studio top bar. ``toolBar`` is the ``StudioTopBar`` QToolBar."""

    def __init__(self, shell):
        self.shell = shell
        mainWindow = shell.mainWindow
        self._editorButtons = None
        self._sceneObservations = []
        self._explicitBreadcrumb = None
        self._breadcrumbCallback = None

        self.toolBar = qt.QToolBar(_("Studio"), mainWindow)
        self.toolBar.setObjectName("StudioTopBar")
        self.toolBar.setMovable(False)
        self.toolBar.setFloatable(False)
        self.toolBar.setFixedHeight(TOP_BAR_HEIGHT)
        self.toolBar.setContextMenuPolicy(qt.Qt.PreventContextMenu)
        self.toolBar.setProperty("SlicerKeepInFocusMode", True)
        # The top bar is not a user-toggled toolbar: keep it out of the View > Toolbars menus
        self.toolBar.toggleViewAction().setVisible(False)
        ui.setRole(self.toolBar, "topBar")

        contents = qt.QWidget()
        contents.setObjectName("StudioTopBarContents")
        contents.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
        layout = qt.QHBoxLayout(contents)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(4)
        self.toolBar.addWidget(contents)

        # Left: app menu and breadcrumb
        self.menuButton = _toolButton("StudioAppMenuButton", "menu", _("Menu"))
        self.menuButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.appMenu = appmenu.AppMenu(shell, self.menuButton)
        self.menuButton.setMenu(self.appMenu.menu)
        layout.addWidget(self.menuButton)

        self.breadcrumbButton = qt.QToolButton()
        self.breadcrumbButton.setObjectName("StudioBreadcrumb")
        self.breadcrumbButton.setAutoRaise(True)
        self.breadcrumbButton.setFocusPolicy(qt.Qt.NoFocus)
        self.breadcrumbButton.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
        self.breadcrumbButton.setMaximumWidth(360)
        ui._font(self.breadcrumbButton, 13)
        ui.bindStyleSheet(self.breadcrumbButton, lambda: (
            f"QToolButton#StudioBreadcrumb {{ background-color: transparent; color: {ui.token('text2').name()}; border: none; border-radius: 6px; padding: 3px 6px; }}"
            f"QToolButton#StudioBreadcrumb:hover {{ background-color: {ui.token('control-hover').name()}; color: {ui.token('text').name()}; }}"))
        self.breadcrumbButton.connect("clicked()", self._onBreadcrumbClicked)
        layout.addWidget(self.breadcrumbButton)
        layout.addStretch(1)

        # Center: workspace switcher and status chip
        self.switcherFrame, self.switcherGroup = ui.segmented([ws.title for ws in workspaces.WORKSPACES])
        self.switcherFrame.setObjectName("StudioWorkspaceSwitcher")
        self.workspaceButtons = {}
        for index, ws in enumerate(workspaces.WORKSPACES):
            button = self.switcherGroup.button(index)
            button.setObjectName(f"Studio{ws.title}WorkspaceButton")
            button.setProperty("studioIcon", ws.iconName)
            button.setProperty("studioWorkspace", ws.key)
            button.setIconSize(qt.QSize(16, 16))
            button.setFocusPolicy(qt.Qt.NoFocus)
            button.setFixedHeight(28)
            if workspaces.isAvailable(ws):
                button.setToolTip(_("{workspace} (Ctrl+Alt+{index})").format(workspace=ws.title, index=index + 1))
            else:
                button.setEnabled(False)
                button.setToolTip(_("Not available in this build"))
            button.connect("clicked()", lambda key=ws.key: self.shell.switchTo(key))
            self.workspaceButtons[ws.key] = button
        layout.addWidget(self.switcherFrame)

        self.statusChip = ui.chip("")
        self.statusChip.setObjectName("StudioStatusChip")
        self.statusChip.hide()
        layout.addSpacing(8)
        layout.addWidget(self.statusChip)
        layout.addStretch(1)

        # Right cluster
        self.undoButton = _toolButton("StudioUndoButton", "undo-2", _("Undo"))
        self.undoButton.connect("clicked()", self._onUndo)
        layout.addWidget(self.undoButton)
        self.redoButton = _toolButton("StudioRedoButton", "redo-2", _("Redo"))
        self.redoButton.connect("clicked()", self._onRedo)
        layout.addWidget(self.redoButton)
        for button in (self.undoButton, self.redoButton):
            # Visible in Draw only: keep their space so that the switcher does not move between workspaces
            sizePolicy = button.sizePolicy
            sizePolicy.setRetainSizeWhenHidden(True)
            button.setSizePolicy(sizePolicy)
        self.undoRedoTimer = qt.QTimer(self.toolBar)
        self.undoRedoTimer.setInterval(UNDO_REDO_POLL_INTERVAL_MS)
        self.undoRedoTimer.connect("timeout()", self.updateUndoRedo)
        self.setUndoRedoVisible(False)

        self.layoutButton = _toolButton("StudioLayoutButton", "layout-panel-left", _("Layout"))
        self.layoutButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.layoutMenu = qt.QMenu(self.layoutButton)
        self.layoutMenu.setObjectName("StudioLayoutMenu")
        self.layoutMenu.connect("aboutToShow()", self._updateLayoutPicker)
        self._buildLayoutPicker()
        self.layoutButton.setMenu(self.layoutMenu)
        layout.addWidget(self.layoutButton)
        layout.addSpacing(4)

        self.searchPill = self._buildSearchPill()
        layout.addWidget(self.searchPill)
        layout.addSpacing(4)

        self.errorBadge = _toolButton("StudioErrorBadge", "circle-alert", _("Errors were logged. Click to open the error log (Ctrl+0)."))
        self.errorBadge.setFixedSize(qt.QSize(52, 30))
        self.errorBadge.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        self.errorBadge.connect("clicked()", self._onErrorBadgeClicked)
        layout.addWidget(self.errorBadge)

        self.refreshIcons()
        ui.addPaletteListener(self.refreshIcons)

        self._connectErrorLog()
        self._observeScene()
        self._updateDefaultBreadcrumb()

    #
    # Widgets
    #

    def _buildSearchPill(self):
        pill = qt.QPushButton()
        pill.setObjectName("StudioSearchPill")
        pill.setFixedSize(SEARCH_PILL_WIDTH, 30)
        pill.setFocusPolicy(qt.Qt.NoFocus)
        pill.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        pill.setToolTip(_("Search modules, tools and actions (Ctrl+K)"))
        pillLayout = qt.QHBoxLayout(pill)
        pillLayout.setContentsMargins(10, 0, 8, 0)
        pillLayout.setSpacing(6)
        self.searchIcon = qt.QLabel()
        self.searchIcon.setObjectName("StudioSearchIcon")
        pillLayout.addWidget(self.searchIcon)
        # The pill opens the module finder (ranked by StudioLib/finder.py): say "Search modules" until the
        # command palette (StudioLib/palette.py, W3-06) also finds tools and actions
        self.searchPlaceholder = qt.QLabel(_("Search modules"))
        self.searchPlaceholder.setObjectName("StudioSearchPlaceholder")
        pillLayout.addWidget(self.searchPlaceholder, 1)
        searchKey = ui.kbd(_("Ctrl K"))
        pillLayout.addWidget(searchKey)
        for child in (self.searchIcon, self.searchPlaceholder, searchKey):
            child.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
        ui.bindStyleSheet(pill, lambda: (
            f"QPushButton#StudioSearchPill {{ background-color: {ui.token('base').name()}; border: 1px solid {ui.token('border').name()}; border-radius: 15px; }}"
            f"QPushButton#StudioSearchPill:hover {{ border-color: {ui.token('text3').name()}; }}"
            f"QLabel#StudioSearchPlaceholder {{ color: {ui.token('text3').name()}; font-size: 13px; background: transparent; }}"))
        pill.connect("clicked()", self.shell.openPalette)
        return pill

    def _buildLayoutPicker(self):
        container = qt.QWidget()
        container.setObjectName("StudioLayoutPicker")
        grid = qt.QGridLayout(container)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setSpacing(6)
        self.layoutTiles = {}
        for index, (key, title, shortcut, iconName, layoutId) in enumerate(layouts.PICKER_ENTRIES):
            tile = qt.QToolButton()
            tile.setObjectName(f"StudioLayoutTile_{key}")
            tile.setText(f"{title}\n{shortcut}")
            tile.setToolTip(f"{title} ({shortcut})")
            tile.setToolButtonStyle(qt.Qt.ToolButtonTextUnderIcon)
            tile.setIconSize(qt.QSize(24, 24))
            tile.setFixedSize(96, 64)
            tile.setCheckable(True)
            tile.setAutoRaise(True)
            tile.setProperty("studioIcon", iconName)
            ui.setRole(tile, "ghost")
            tile.connect("clicked()", lambda key=key: self._onLayoutTileClicked(key))
            grid.addWidget(tile, index // 2, index % 2)
            self.layoutTiles[key] = tile
        moreButton = qt.QPushButton(_("More layouts…"))
        moreButton.setObjectName("StudioMoreLayoutsButton")
        ui.setRole(moreButton, "ghost")
        moreButton.connect("clicked()", self._onMoreLayouts)
        grid.addWidget(moreButton, 2, 0, 1, 2)
        action = qt.QWidgetAction(self.layoutMenu)
        action.setDefaultWidget(container)
        self.layoutMenu.addAction(action)

    def refreshIcons(self):
        """Re-tint the icons for the current palette."""
        buttons = [self.menuButton, self.undoButton, self.redoButton, self.layoutButton, *self.workspaceButtons.values(), *self.layoutTiles.values()]
        for button in buttons:
            size = button.iconSize.width()
            button.setIcon(ui.icon(button.property("studioIcon"), size))
        self.errorBadge.setIcon(ui.icon("circle-alert", ICON_SIZE, role="danger"))
        self.searchIcon.setPixmap(ui.pixmap("search", 14, "text3"))

    #
    # Workspaces
    #

    def setCurrentWorkspace(self, key):
        """Check the tab of a workspace, or no tab (key None) for transient modules."""
        button = self.workspaceButtons.get(key) if key else None
        if button is not None:
            button.setChecked(True)
        else:
            self.switcherGroup.setExclusive(False)
            for workspaceButton in self.workspaceButtons.values():
                workspaceButton.setChecked(False)
            self.switcherGroup.setExclusive(True)
        self.setUndoRedoVisible(key == "draw")

    #
    # Breadcrumb and status chip
    #

    def setBreadcrumb(self, primary, secondary=None, onClick=None):
        if primary is None:
            self._explicitBreadcrumb = None
            self._breadcrumbCallback = None
            self._updateDefaultBreadcrumb()
            return
        self._explicitBreadcrumb = (primary, secondary)
        self._breadcrumbCallback = onClick
        self._showBreadcrumb(primary, secondary)

    def _showBreadcrumb(self, primary, secondary):
        text = primary if not secondary else f"{primary}  /  {secondary}"
        self.breadcrumbButton.setText(text)
        self.breadcrumbButton.setToolTip(text)
        self.breadcrumbButton.setVisible(bool(text))

    def _updateDefaultBreadcrumb(self, *args):
        if self._explicitBreadcrumb is not None:
            return
        volumeNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLScalarVolumeNode")
        self._showBreadcrumb(volumeNode.GetName() if volumeNode is not None else "", None)

    def _onBreadcrumbClicked(self):
        if self._breadcrumbCallback is not None:
            try:
                self._breadcrumbCallback()
            except Exception:
                logging.error(f"Studio breadcrumb callback failed:\n{traceback.format_exc()}")

    def _observeScene(self):
        self._breadcrumbTimer = qt.QTimer(self.toolBar)
        self._breadcrumbTimer.setSingleShot(True)
        self._breadcrumbTimer.setInterval(200)
        self._breadcrumbTimer.connect("timeout()", self._updateDefaultBreadcrumb)
        for event in (slicer.vtkMRMLScene.NodeAddedEvent, slicer.vtkMRMLScene.NodeRemovedEvent, slicer.vtkMRMLScene.EndCloseEvent, slicer.vtkMRMLScene.EndImportEvent):
            tag = slicer.mrmlScene.AddObserver(event, self._onSceneChanged)
            self._sceneObservations.append(tag)

    def _onSceneChanged(self, caller, event):
        self._breadcrumbTimer.start()

    def setStatusChip(self, text=None, level="neutral"):
        if not text:
            self.statusChip.hide()
            return
        self.statusChip.setText(text)
        ui.setChipLevel(self.statusChip, level)
        self.statusChip.show()

    #
    # Undo/redo of the Segment Editor
    #

    def setUndoRedoVisible(self, visible):
        self.undoButton.setVisible(visible)
        self.redoButton.setVisible(visible)
        if visible:
            self.updateUndoRedo()
            self.undoRedoTimer.start()
        else:
            self.undoRedoTimer.stop()

    def _segmentEditor(self):
        widget = getattr(slicer.modules, "SegmentEditorWidget", None)
        return widget.editor if widget is not None else None

    def updateUndoRedo(self):
        """Mirror the enabled state of the (possibly hidden) Undo/Redo buttons of the Segment Editor."""
        editor = self._segmentEditor()
        if editor is None:
            self.undoButton.setEnabled(False)
            self.redoButton.setEnabled(False)
            return
        if self._editorButtons is None:
            self._editorButtons = (editor.findChild("QToolButton", "UndoButton"), editor.findChild("QToolButton", "RedoButton"))
        undoButton, redoButton = self._editorButtons
        undoEnabled = undoButton is not None and undoButton.enabled
        redoEnabled = redoButton is not None and redoButton.enabled
        if self.undoButton.enabled != undoEnabled:
            self.undoButton.setEnabled(undoEnabled)
        if self.redoButton.enabled != redoEnabled:
            self.redoButton.setEnabled(redoEnabled)

    def _onUndo(self):
        editor = self._segmentEditor()
        if editor is not None:
            editor.undo()
            self.updateUndoRedo()

    def _onRedo(self):
        editor = self._segmentEditor()
        if editor is not None:
            editor.redo()
            self.updateUndoRedo()

    #
    # Layouts
    #

    def _updateLayoutPicker(self):
        layoutManager = slicer.app.layoutManager()
        current = layoutManager.layout if layoutManager is not None else None
        for key, tile in self.layoutTiles.items():
            tile.setChecked(layouts.pickerLayoutId(key, self.shell.hoverTracker) == current)

    def _onLayoutTileClicked(self, key):
        self.layoutMenu.close()
        self.shell.setLayoutFromPicker(key)

    def _onMoreLayouts(self):
        self.layoutMenu.close()
        layoutMenu = self.shell.mainWindow.findChild("QMenu", "LayoutMenu")
        if layoutMenu is not None:
            layoutMenu.popup(self.layoutButton.mapToGlobal(qt.QPoint(0, self.layoutButton.height)))

    #
    # Error badge
    #

    def _connectErrorLog(self):
        self.errorLogModel = slicer.app.errorLogModel()
        self._scannedRows = 0
        self._errorRows = 0
        self._seenErrorRows = 0
        self._errorCountTimer = qt.QTimer(self.toolBar)
        self._errorCountTimer.setSingleShot(True)
        self._errorCountTimer.setInterval(100)
        self._errorCountTimer.connect("timeout()", self._scanErrors)
        self.errorLogModel.connect("entryAdded(ctkErrorLogLevel::LogLevel)", self._onLogEntryAdded)
        self.errorLogModel.connect("modelReset()", self._rescanErrors)
        self.errorLogModel.connect("rowsRemoved(QModelIndex,int,int)", self._rescanErrors)
        errorLogWidget = self.shell.mainWindow.errorLogWidget()
        if errorLogWidget is not None:
            errorLogWidget.connect("userViewed()", self._onErrorLogViewed)
        self._scanErrors()

    def _scanErrors(self):
        """Count the log entries (rows) of Error, Critical or Fatal level that were not scanned yet."""
        model = self.errorLogModel
        rowCount = model.rowCount()
        if rowCount < self._scannedRows:
            self._rescanErrors()
            return
        levelColumn = 2  # ctkErrorLogAbstractModel::LogLevelColumn
        for row in range(self._scannedRows, rowCount):
            if model.index(row, levelColumn).data() in _ERROR_LEVEL_NAMES:
                self._errorRows += 1
        self._scannedRows = rowCount
        self._updateErrorBadge()

    def _rescanErrors(self, *args):
        # The log was cleared or filtered
        self._scannedRows = 0
        self._errorRows = 0
        self._scanErrors()
        self._seenErrorRows = min(self._seenErrorRows, self._errorRows)
        self._updateErrorBadge()

    def _onLogEntryAdded(self, level):
        # Entries of a message written in several parts are grouped in one row: count rows, not signals
        if _ERROR_LEVEL <= int(level) <= _FATAL_LEVEL:
            self._errorCountTimer.start()

    def _onErrorLogViewed(self):
        self._scanErrors()
        self._seenErrorRows = self._errorRows
        self._updateErrorBadge()

    def _updateErrorBadge(self):
        count = self.errorCount()
        self.errorBadge.setText(str(count) if count < 100 else "99+")
        self.errorBadge.setVisible(count > 0)

    def errorCount(self):
        """Number of errors logged since the error log was last viewed."""
        return self._errorRows - self._seenErrorRows

    def _onErrorBadgeClicked(self):
        self.shell.mainWindow.errorLogDockWidget().toggleViewAction().trigger()
        self._onErrorLogViewed()

    #
    # Cleanup
    #

    def dispose(self):
        ui.removePaletteListener(self.refreshIcons)
        self.undoRedoTimer.stop()
        self._breadcrumbTimer.stop()
        self._errorCountTimer.stop()
        for tag in self._sceneObservations:
            slicer.mrmlScene.RemoveObserver(tag)
        self._sceneObservations = []
        self.errorLogModel.disconnect("entryAdded(ctkErrorLogLevel::LogLevel)", self._onLogEntryAdded)
        self.errorLogModel.disconnect("modelReset()", self._rescanErrors)
        self.errorLogModel.disconnect("rowsRemoved(QModelIndex,int,int)", self._rescanErrors)
        errorLogWidget = self.shell.mainWindow.errorLogWidget()
        if errorLogWidget is not None:
            errorLogWidget.disconnect("userViewed()", self._onErrorLogViewed)
        self.appMenu.dispose()
        mainWindow = self.shell.mainWindow
        mainWindow.removeToolBar(self.toolBar)
        self.toolBar.hide()
        # The toolbar keeps its parent until Qt deletes it: PythonQt deletes parentless objects
        # created from Python when their wrapper is released, which may happen during that deletion.
        self.toolBar.setObjectName("")
        self.toolBar.deleteLater()

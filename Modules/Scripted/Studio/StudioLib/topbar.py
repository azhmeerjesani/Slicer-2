"""Top bar of Slicer Studio (SPEC 2.4, USABILITY 2, 6 and 10).

A 44 px toolbar ``StudioTopBar`` whose contents are a 3-column grid, so that the workspace switcher is centred
on the window whatever the other columns hold (no fixed widths, the window can still shrink):

- left: the app menu (``≡``), the title (the name of the open project or study, V-02; a click opens Library), the
  chevron of the project menu (Rename…, Show in Finder/Files, New project, Recent projects, Close project) and the
  save status (``setSaveStatus()``, A-01b, A-09: "Saved" with a check, "Saving…", "Not saved");
- centre: the workspace switcher [Library | Draw | Map] (``Ctrl+Alt+1/2/3``), one segmented control (VISUAL 4.1);
- right: the status chip, Undo/Redo (Draw only), the layout picker (``Alt+1..5``), the search field (``Ctrl+K``)
  and the error badge.

Every button takes the keyboard focus (Tab; the arrow keys move inside the switcher), is pressed by Space and
Enter also in Draw, and has a "Name · key · description" tooltip. ``setCompact(bool)`` collapses the bar for a
narrow window (the search pill becomes an icon, the switcher shows icons only).
"""

import importlib.util
import logging
import os
import re
import traceback

import qt
import slicer
import vtk
from slicer.i18n import tr as _

from . import appmenu, layouts, space, ui, workspaces

TOP_BAR_HEIGHT = 44
#: Widest the search pill grows; it shrinks with the window
SEARCH_PILL_WIDTH = 280
SEARCH_PILL_COMPACT_WIDTH = 30
#: Narrowest the search pill gets before the bar goes compact: the icon, "Search" and the key chip
SEARCH_PILL_MIN_WIDTH = 132
BREADCRUMB_MAX_WIDTH = 360
BREADCRUMB_COMPACT_MAX_WIDTH = 160
BREADCRUMB_MIN_WIDTH = 64
#: Between the scan and the lesion of the default breadcrumb
BREADCRUMB_SEPARATOR = " \u203a "
#: Opacity of the tab of a workspace that is not part of this build
UNAVAILABLE_OPACITY = 0.55
#: Horizontal padding and frame of the breadcrumb button (its stylesheet), which the elided text must leave free
_BREADCRUMB_PADDING = 16
ICON_SIZE = 18
UNDO_REDO_POLL_INTERVAL_MS = 250
#: Autosave status (A-01b): icon size, and the window widths below which it shows its icon only (the text stays in
#: the tooltip): always below SAVE_STATUS_COMPACT_WIDTH, and below SAVE_STATUS_CHIP_COMPACT_WIDTH while the status
#: chip is shown too
SAVE_STATUS_ICON_SIZE = 14
SAVE_STATUS_COMPACT_WIDTH = 1500
SAVE_STATUS_CHIP_COMPACT_WIDTH = 1700
#: Level of the autosave status -> (icon, colour token). The busy icon is static: no spinner (SPEC 10)
SAVE_STATUS_LEVELS = {
    "neutral": ("history", "text3"),
    "busy": ("loader-circle", "text3"),
    "ok": ("circle-check", "success"),
    "warn": ("triangle-alert", "warning"),
}
#: Text color of the save status per level: a calm text3, except when something needs attention
SAVE_STATUS_TEXT = {"neutral": "text3", "busy": "text3", "ok": "text3", "warn": "warning"}
#: Where the README explains the Map workspace (the section "Lesion network mapping")
README_MAP_URL = "https://github.com/azhmeerjesani/Slicer-2#lesion-network-mapping"

_ERROR_LEVEL = 0x40  # ctkErrorLogLevel::Error
_FATAL_LEVEL = 0x100  # ctkErrorLogLevel::Fatal (Critical is 0x80, None is 0x200)
_ERROR_LEVEL_NAMES = ("Error", "Critical", "Fatal")
#: Columns of ctkErrorLogAbstractModel
_LEVEL_COLUMN = 2
_DESCRIPTION_COLUMN = 4
#: The first DICOM use of a profile (no database yet): CTK logs that the database folder has no database, then
#: SQL errors of queries sent before the new database is open; official Slicer logs the same. The badge does not
#: count them (they stay in the error log): the notice, and the "Driver not loaded" SQL errors that follow it
#: within a few log rows. The same SQL error elsewhere is counted.
_DICOM_FIRST_RUN_NOTICE = "Database folder does not contain ctkDICOM.sql file"
#: Lines of those SQL errors (the log groups consecutive messages of a level in one row: any other line is counted)
_DICOM_FIRST_RUN_SQL_LINES = re.compile(r"^(?:SQL failed:.*|Error:\s*|Driver not loaded Driver not loaded|\s*)$")
_DICOM_FIRST_RUN_ROWS = 8
#: At most this many of those SQL errors per notice (CTK logs three): a longer burst is counted
_DICOM_FIRST_RUN_MAX_SQL_ERRORS = 4

#: Description of each workspace in its tooltip
_WORKSPACE_DESCRIPTIONS = {
    "library": "Open scans, lesion masks and sample data; recent files",
    "draw": "Segment Editor: brush, outline and erase lesions",
    "map": "Lesion network mapping",
}


def _toolButton(objectName, iconName, toolTip, role="ghost"):
    button = qt.QToolButton()
    button.setObjectName(objectName)
    button.setToolTip(toolTip)
    button.setAutoRaise(True)
    button.setIconSize(qt.QSize(ICON_SIZE, ICON_SIZE))
    button.setFixedSize(30, 30)
    button.setFocusPolicy(qt.Qt.TabFocus)
    button.setProperty("studioIcon", iconName)
    ui.setRole(button, role)
    ui.installButtonKeys(button)
    return button


class _SwitcherKeys(qt.QObject):
    """Left and Right move the keyboard focus between the buttons of the workspace switcher."""

    def __init__(self, topBar):
        qt.QObject.__init__(self)
        self.topBar = topBar

    def eventFilter(self, obj, event):
        if event.type() != qt.QEvent.KeyPress or ui.hasKeyModifiers(event):
            return False
        key = event.key()
        if key not in (qt.Qt.Key_Left, qt.Qt.Key_Right):
            return False
        buttons = [button for button in self.topBar.workspaceButtons.values() if button.isVisible() and button.isEnabled()]
        index = next((i for i, button in enumerate(buttons) if button == obj), None)
        if index is None:
            return False
        step = 1 if key == qt.Qt.Key_Right else -1
        if obj.isRightToLeft():
            step = -step
        if 0 <= index + step < len(buttons):
            buttons[index + step].setFocus(qt.Qt.TabFocusReason)
        return True


class _ResizeFilter(qt.QObject):
    """Calls ``callback()`` when the watched widget is resized."""

    def __init__(self, callback):
        qt.QObject.__init__(self)
        self.callback = callback

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Resize:
            self.callback()
        return False


class TopBar:
    """Builds and updates the Studio top bar. ``toolBar`` is the ``StudioTopBar`` QToolBar."""

    def __init__(self, shell):
        self.shell = shell
        mainWindow = shell.mainWindow
        self._editorButtons = None
        self._sceneObservations = []
        self._explicitBreadcrumb = None
        self._breadcrumbCallback = None
        self._breadcrumbText = ""
        self._compact = False
        self._editor = None
        self._mapPopover = None
        self._moreLayoutsMenu = None
        #: Layout requests of the user in progress (picker, Alt+1..5, palette, "More layouts…"): only these are
        #: announced while Library is shown, never the layout set at startup or by a loaded scene
        self._userLayoutRequests = 0
        self._moreLayoutsMenuOpen = False
        self._threeDPrepared = False
        self._redSliceNode = None
        self._redSliceVisible = None
        self._redSliceTouched = False
        self._redSliceObservation = None
        self._layoutManager = slicer.app.layoutManager()

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
        self.contents = contents
        # Three columns: left (stretch 1), the switcher (natural width) and right (stretch 1). Both side columns get the
        # same minimum width (the larger of the two minimum size hints) so that the switcher is centred on the window.
        self.grid = qt.QGridLayout(contents)
        self.grid.setContentsMargins(8, 0, 8, 0)
        self.grid.setHorizontalSpacing(0)
        self.grid.setVerticalSpacing(0)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 0)
        self.grid.setColumnStretch(2, 1)
        self.toolBar.addWidget(contents)
        ui.bindStyleSheet(contents, self._contentsStyleSheet)

        self.leftContainer = qt.QWidget()
        self.leftContainer.setObjectName("StudioTopBarLeft")
        leftLayout = qt.QHBoxLayout(self.leftContainer)
        leftLayout.setContentsMargins(0, 0, 0, 0)
        leftLayout.setSpacing(4)
        self.rightContainer = qt.QWidget()
        self.rightContainer.setObjectName("StudioTopBarRight")
        rightLayout = qt.QHBoxLayout(self.rightContainer)
        rightLayout.setContentsMargins(0, 0, 0, 0)
        rightLayout.setSpacing(4)

        # Left: app menu and breadcrumb
        self.menuButton = _toolButton("StudioAppMenuButton", "menu", ui.tooltip(_("Menu"), "F10", _("File, edit, view and help")))
        self.menuButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.appMenu = appmenu.AppMenu(shell, self.menuButton)
        self.menuButton.setMenu(self.appMenu.menu)
        leftLayout.addWidget(self.menuButton)

        self.breadcrumbButton = qt.QToolButton()
        self.breadcrumbButton.setObjectName("StudioBreadcrumb")
        self.breadcrumbButton.setAutoRaise(True)
        self.breadcrumbButton.setFocusPolicy(qt.Qt.NoFocus)
        self.breadcrumbButton.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
        self.breadcrumbButton.setMinimumWidth(BREADCRUMB_MIN_WIDTH)
        self.breadcrumbButton.setMaximumWidth(BREADCRUMB_MAX_WIDTH)
        self.breadcrumbButton.setProperty("studioClickable", False)
        ui.installButtonKeys(self.breadcrumbButton)
        ui.bindStyleSheet(self.breadcrumbButton, self._breadcrumbStyleSheet)
        self.breadcrumbButton.connect("clicked()", self._onBreadcrumbClicked)
        leftLayout.addWidget(self.breadcrumbButton)
        # The project menu (V-02): a chevron after the title
        self.projectMenuButton = _toolButton("StudioProjectMenuButton", "chevron-down", ui.tooltip(
            _("Project"), None, _("Rename, show in {manager}, new project, recent projects").format(manager=self._fileManagerName())))
        self.projectMenuButton.setFixedSize(qt.QSize(22, 28))
        self.projectMenuButton.setIconSize(qt.QSize(14, 14))
        self.projectMenuButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.projectMenuButton.setAccessibleName(_("Project menu"))
        self.projectMenu = qt.QMenu(self.projectMenuButton)
        self.projectMenu.setObjectName("StudioProjectMenu")
        self.projectMenu.connect("aboutToShow()", self._fillProjectMenu)
        self.projectMenuButton.setMenu(self.projectMenu)
        self.projectMenuButton.hide()
        leftLayout.addWidget(self.projectMenuButton)
        # The save status of the project follows its name (A-01b, A-09; added below)
        self._leftLayout = leftLayout
        self._leftResizeFilter = _ResizeFilter(self._elideBreadcrumb)
        self.leftContainer.installEventFilter(self._leftResizeFilter)

        # Centre: workspace switcher
        self.switcherFrame, self.switcherGroup = ui.segmented([ws.title for ws in workspaces.WORKSPACES])
        self.switcherFrame.setObjectName("StudioWorkspaceSwitcher")
        # One segmented control (VISUAL 4.1): a 3 px padded canvas track, 28 px items
        self.switcherFrame.layout().setContentsMargins(3, 3, 3, 3)
        self.switcherFrame.layout().setSpacing(2)
        self.workspaceButtons = {}
        self._switcherKeys = _SwitcherKeys(self)
        for index, ws in enumerate(workspaces.WORKSPACES):
            button = self.switcherGroup.button(index)
            button.setObjectName(f"Studio{ws.title}WorkspaceButton")
            button.setProperty("studioIcon", ws.iconName)
            button.setProperty("studioWorkspace", ws.key)
            button.setProperty("studioParent", False)
            button.setIconSize(qt.QSize(16, 16))
            button.setFocusPolicy(qt.Qt.TabFocus)
            button.setFixedHeight(28)
            ui.setText(button, "name")
            ui.installButtonKeys(button)
            button.installEventFilter(self._switcherKeys)
            button.connect("clicked()", lambda key=ws.key: self._onWorkspaceClicked(key))
            self.workspaceButtons[ws.key] = button
        self.refreshWorkspaceAvailability()

        # Right: status chip, undo/redo, layout, search and error badge. The chip is in the right column so that it
        # never moves the switcher.
        self.statusChip = ui.chip("")
        self.statusChip.setObjectName("StudioStatusChip")
        self.statusChip.hide()
        rightLayout.addWidget(self.statusChip)
        # Autosave status (A-01b): "Autosaved · 2 min ago" and its menu, hidden until something was autosaved
        self.saveStatusButton = qt.QToolButton()
        self.saveStatusButton.setObjectName("StudioSaveStatus")
        self.saveStatusButton.setAutoRaise(True)
        self.saveStatusButton.setFocusPolicy(qt.Qt.NoFocus)
        self.saveStatusButton.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        self.saveStatusButton.setIconSize(qt.QSize(SAVE_STATUS_ICON_SIZE, SAVE_STATUS_ICON_SIZE))
        self.saveStatusButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.saveStatusButton.setFixedHeight(28)
        ui._font(self.saveStatusButton, ui.fontPx(12))
        policy = self.saveStatusButton.sizePolicy
        policy.setRetainSizeWhenHidden(False)
        self.saveStatusButton.setSizePolicy(policy)
        self.saveStatusButton.setToolTip(ui.tooltip(_("Autosave"), None, _("Your work is copied to a private recovery folder. This is not saving.")))
        self.saveStatusButton.hide()
        self._saveStatus = None
        self._saveStatusTitle = None
        ui.bindStyleSheet(self.saveStatusButton, self._saveStatusStyleSheet)
        self._leftLayout.addWidget(self.saveStatusButton)
        self._leftLayout.addStretch(1)
        rightLayout.addStretch(1)

        self.undoButton = _toolButton("StudioUndoButton", "undo-2", ui.tooltip(_("Undo"), "Ctrl+Z", _("Undo the last lesion edit")))
        self.undoButton.connect("clicked()", self._onUndo)
        rightLayout.addWidget(self.undoButton)
        self.redoButton = _toolButton("StudioRedoButton", "redo-2", ui.tooltip(_("Redo"), "Ctrl+Y or Ctrl+Shift+Z", _("Redo the last undone edit")))
        self.redoButton.connect("clicked()", self._onRedo)
        rightLayout.addWidget(self.redoButton)
        self.undoRedoTimer = qt.QTimer(self.toolBar)
        self.undoRedoTimer.setInterval(UNDO_REDO_POLL_INTERVAL_MS)
        self.undoRedoTimer.connect("timeout()", self.updateUndoRedo)
        self.setUndoRedoVisible(False)

        self.layoutButton = _toolButton("StudioLayoutButton", "layout-panel-left", ui.tooltip(
            _("Layout"), "Alt+1…5", _("Focus, Single, Four-up, Row, Focus + 3D and view options")))
        self.layoutButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.layoutMenu = qt.QMenu(self.layoutButton)
        self.layoutMenu.setObjectName("StudioLayoutMenu")
        self.layoutMenu.setToolTipsVisible(True)
        self.layoutMenu.connect("aboutToShow()", self._updateLayoutPicker)
        self._buildLayoutPicker()
        self.layoutButton.setMenu(self.layoutMenu)
        rightLayout.addWidget(self.layoutButton)
        rightLayout.addSpacing(4)

        self.searchPill = self._buildSearchPill()
        rightLayout.addWidget(self.searchPill, 1)
        rightLayout.addSpacing(4)

        self.errorBadge = _toolButton("StudioErrorBadge", "circle-alert", ui.tooltip(_("Errors"), "Ctrl+0", _("open the error log")))
        self.errorBadge.setFixedSize(qt.QSize(52, 30))
        self.errorBadge.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        self.errorBadge.setFocusPolicy(qt.Qt.NoFocus)
        self.errorBadge.connect("clicked()", self._onErrorBadgeClicked)
        rightLayout.addWidget(self.errorBadge)

        self.grid.addWidget(self.leftContainer, 0, 0)
        self.grid.addWidget(self.switcherFrame, 0, 1)
        self.grid.addWidget(self.rightContainer, 0, 2)
        self.updateColumnMinimums()

        self.refreshIcons()
        ui.addPaletteListener(self.refreshIcons)

        self._connectErrorLog()
        self._observeScene()
        self._connectEditor()
        self._updateDefaultBreadcrumb()
        self._connectLayoutManager()
        # "Single" is a maximize: the shortcuts (Alt+1..5), the palette and the picker all go through the shell
        shell.setLayoutFromPicker = self.setLayoutFromPicker

    #
    # Widgets
    #

    def _contentsStyleSheet(self):
        """Scoped rules of the top bar: the parent-tab cue (the roles paint the rest)."""
        accent = ui.token("accent-fg").name()
        return (
            f'QWidget#StudioTopBarContents QToolButton[studioParent="true"] {{ color: {accent}; border: none; border-bottom: 2px solid {accent}; border-radius: 0px; }}')

    def _breadcrumbStyleSheet(self):
        size = ui.fontPx(13)
        accent = ui.token("accent-fg").name()
        # The project (or scan) name: 13 px Medium in the text color (VISUAL 4.1)
        return (
            f"QToolButton#StudioBreadcrumb {{ background-color: transparent; color: {ui.token('text').name()}; border: none; border-radius: 6px; padding: 3px 6px;"
            f" font-size: {size}px; font-weight: 500; }}"
            f"QToolButton#StudioBreadcrumb[studioClickable=\"true\"]:hover {{ background-color: {ui.token('control-hover').name()}; color: {ui.token('text').name()}; }}"
            f"QToolButton#StudioBreadcrumb:focus {{ border: 2px solid {accent}; padding: 1px 4px; }}")

    def _buildSearchPill(self):
        pill = qt.QPushButton()
        pill.setObjectName("StudioSearchPill")
        pill.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        pill.setMinimumWidth(SEARCH_PILL_MIN_WIDTH)
        pill.setMaximumWidth(SEARCH_PILL_WIDTH)
        pill.setFixedHeight(30)
        pill.setFocusPolicy(qt.Qt.TabFocus)
        pill.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        pill.setToolTip(ui.tooltip(_("Search"), "Ctrl+K", _("modules, tools, actions, views, files")))
        self.pillLayout = qt.QHBoxLayout(pill)
        self.pillLayout.setContentsMargins(10, 0, 8, 0)
        self.pillLayout.setSpacing(6)
        self.searchIcon = qt.QLabel()
        self.searchIcon.setObjectName("StudioSearchIcon")
        self.pillLayout.addWidget(self.searchIcon)
        # The pill opens the command palette (StudioLib/palette.py); until it exists it opens the module finder
        hasPalette = self.shell._importOptional("StudioLib.palette") is not None
        self._searchPlaceholderTexts = (_("Search actions, tools and data"), _("Search")) if hasPalette else (_("Search modules"), _("Search"))
        self.searchPlaceholder = qt.QLabel(self._searchPlaceholderTexts[1])
        self.searchPlaceholder.setObjectName("StudioSearchPlaceholder")
        # The long placeholder when it fits, else "Search" (``_fitSearchPlaceholder()``)
        self.searchPlaceholder.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        self.searchPlaceholder.setMinimumWidth(qt.QFontMetrics(self.searchPlaceholder.font).horizontalAdvance(self._searchPlaceholderTexts[1]))
        self._placeholderResizeFilter = _ResizeFilter(self._fitSearchPlaceholder)
        self.searchPlaceholder.installEventFilter(self._placeholderResizeFilter)
        self.pillLayout.addWidget(self.searchPlaceholder, 1)
        self.searchKey = ui.kbd(ui.keyText("Ctrl+K"))
        self.searchKey.setFixedHeight(18)
        self.pillLayout.addWidget(self.searchKey)
        for child in (self.searchIcon, self.searchPlaceholder, self.searchKey):
            child.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
        ui.bindStyleSheet(pill, self._searchPillStyleSheet)
        ui.installButtonKeys(pill)
        pill.connect("clicked()", self.shell.openPalette)
        return pill

    def _fitSearchPlaceholder(self):
        """The long placeholder ("Search actions, tools and data") when the pill has room for it, else "Search"."""
        label = self.searchPlaceholder
        longText, shortText = self._searchPlaceholderTexts
        text = longText if qt.QFontMetrics(label.font).horizontalAdvance(longText) <= label.width else shortText
        if label.text != text:
            label.setText(text)

    def _searchPillStyleSheet(self):
        size = ui.fontPx(13)
        keySize = ui.fontPx(11)
        # VISUAL 4.1: a 30 px field on the canvas color with an 8 px radius, and the key chip
        return (
            f"QPushButton#StudioSearchPill {{ background-color: {ui.token('canvas').name()}; border: 1px solid {ui.token('border').name()}; border-radius: 8px; }}"
            f"QPushButton#StudioSearchPill:hover {{ border-color: {ui.token('control-border').name()}; }}"
            f"QPushButton#StudioSearchPill:focus {{ border: 2px solid {ui.token('accent-fg').name()}; }}"
            f"QLabel#StudioSearchPlaceholder {{ color: {ui.token('text3').name()}; font-size: {size}px; background: transparent; }}"
            f"QPushButton#StudioSearchPill QLabel#StudioKbd {{ background-color: transparent; color: {ui.token('text2').name()};"
            f" border: 1px solid {ui.token('border').name()}; border-radius: 5px; padding: 0px 5px; font-size: {keySize}px; }}")

    def _buildLayoutPicker(self):
        container = qt.QWidget()
        container.setObjectName("StudioLayoutPicker")
        self._pickerGrid = qt.QGridLayout(container)
        grid = self._pickerGrid
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setSpacing(6)
        self.layoutTiles = {}
        for index, (key, title, shortcut, iconName, layoutId) in enumerate(layouts.PICKER_ENTRIES + (layouts.COMPARE_ENTRY,)):
            tile = qt.QToolButton()
            tile.setObjectName(f"StudioLayoutTile_{key}")
            tile.setText(f"{title}\n{shortcut}")
            tile.setToolTip(ui.tooltip(title, shortcut, layouts.PICKER_DESCRIPTIONS.get(key, "")))
            tile.setToolButtonStyle(qt.Qt.ToolButtonTextUnderIcon)
            tile.setIconSize(qt.QSize(24, 24))
            tile.setFixedSize(96, 64)
            tile.setCheckable(True)
            tile.setAutoRaise(True)
            tile.setFocusPolicy(qt.Qt.TabFocus)
            tile.setProperty("studioIcon", iconName)
            ui.setRole(tile, "ghost")
            ui.installButtonKeys(tile)
            tile.connect("clicked()", lambda key=key: self._onLayoutTileClicked(key))
            grid.addWidget(tile, index // 2, index % 2)
            self.layoutTiles[key] = tile
        self.layoutTiles["compare"].hide()
        moreButton = qt.QPushButton(_("More layouts…"))
        moreButton.setObjectName("StudioMoreLayoutsButton")
        moreButton.setToolTip(ui.tooltip(_("More layouts"), None, _("Studio layouts and every layout of the classic Layout menu")))
        ui.setRole(moreButton, "ghost")
        moreButton.connect("clicked()", self._onMoreLayouts)
        grid.addWidget(moreButton, 3, 0, 1, 2)
        action = qt.QWidgetAction(self.layoutMenu)
        action.setDefaultWidget(container)
        self.layoutMenu.addAction(action)

        # The View section: commands that official Slicer keeps in toolbars
        self.layoutMenu.addSection(_("View"))
        self.viewActions = {}
        for key, text, iconName, toolTip, checkable in (
                ("crosshair", _("Crosshair"), "crosshair", ui.tooltip(_("Crosshair"), "Shift+move", _("show where the other views cut")), True),
                ("intersections", _("Slice intersections"), "combine", ui.tooltip(_("Slice intersections"), None, _("show the lines where the slices cross")), True),
                ("link", _("Link views"), "link", ui.tooltip(_("Link views"), None, _("scroll, zoom and pan the slice views together; a figure keeps its own slice")), True),
                ("reset", _("Reset views"), "scaling", ui.tooltip(_("Reset views"), None, _("fit the slice views and the 3D view")), False)):
            viewAction = self.layoutMenu.addAction(text)
            viewAction.setObjectName(f"StudioViewAction_{key}")
            viewAction.setCheckable(checkable)
            viewAction.setToolTip(toolTip)
            viewAction.setProperty("studioIcon", iconName)
            viewAction.connect("triggered(bool)", lambda checked, key=key: self._onViewAction(key, checked))
            self.viewActions[key] = viewAction

    @staticmethod
    def _fileManagerName():
        try:
            from . import projects

            return projects.fileManagerName()
        except Exception:
            return _("Files")

    def refreshIcons(self):
        """Re-tint the icons for the current palette."""
        buttons = [self.menuButton, self.projectMenuButton, self.undoButton, self.redoButton, self.layoutButton, *self.workspaceButtons.values(),
                   *self.layoutTiles.values()]
        for button in buttons:
            size = button.iconSize.width()
            button.setIcon(ui.icon(button.property("studioIcon"), size))
        for action in self.viewActions.values():
            action.setIcon(ui.icon(action.property("studioIcon"), 16))
        self.errorBadge.setIcon(ui.icon("circle-alert", ICON_SIZE, role="danger"))
        self.searchIcon.setPixmap(ui.pixmap("search", 16, "text3"))
        self._updateSaveStatusIcon()

    #
    # Geometry: the centred switcher and the compact mode
    #

    def updateColumnMinimums(self):
        """Give both side columns the same minimum width, the larger of the two minimum size hints of their contents.

        Called whenever a side changes (breadcrumb text, status chip shown or hidden, compact mode): the switcher stays
        centred, and the columns still give way when the window is narrow.
        """
        minimum = max(self.leftContainer.minimumSizeHint.width(), self.rightContainer.minimumSizeHint.width())
        self.grid.setColumnMinimumWidth(0, minimum)
        self.grid.setColumnMinimumWidth(2, minimum)
        return minimum

    def isCompact(self):
        return self._compact

    def setCompact(self, compact):
        """Narrow mode (driven by the window width): the search pill is a 30 px icon button (no placeholder and key
        chip), the switcher shows icons only (the tooltips stay) and the breadcrumb is at most 160 px wide.
        """
        compact = bool(compact)
        if compact == self._compact:
            return
        self._compact = compact
        self.searchPlaceholder.setVisible(not compact)
        self.searchKey.setVisible(not compact)
        if compact:
            self.pillLayout.setContentsMargins(0, 0, 0, 0)
            self.searchIcon.setAlignment(qt.Qt.AlignCenter)
            self.searchPill.setMinimumWidth(SEARCH_PILL_COMPACT_WIDTH)
            self.searchPill.setMaximumWidth(SEARCH_PILL_COMPACT_WIDTH)
        else:
            self.pillLayout.setContentsMargins(10, 0, 8, 0)
            self.searchIcon.setAlignment(qt.Qt.AlignLeft | qt.Qt.AlignVCenter)
            self.searchPill.setMinimumWidth(SEARCH_PILL_MIN_WIDTH)
            self.searchPill.setMaximumWidth(SEARCH_PILL_WIDTH)
        style = qt.Qt.ToolButtonIconOnly if compact else qt.Qt.ToolButtonTextBesideIcon
        for button in self.workspaceButtons.values():
            button.setToolButtonStyle(style)
        self.breadcrumbButton.setMaximumWidth(BREADCRUMB_COMPACT_MAX_WIDTH if compact else BREADCRUMB_MAX_WIDTH)
        self._elideBreadcrumb()
        self.updateSaveStatusCompact()
        self.updateColumnMinimums()

    #
    # Workspaces
    #

    def _workspaceToolTip(self, ws, available):
        key = f"Ctrl+Alt+{workspaces.WORKSPACES.index(ws) + 1}"
        if available:
            return ui.tooltip(ws.title, key, _(_WORKSPACE_DESCRIPTIONS.get(ws.key, ws.caption or "")))
        return ui.tooltip(ws.title, key, _("not part of this build yet; click to see why"))

    def refreshWorkspaceAvailability(self):
        """A workspace that is not available (Map without its module) stays enabled but cannot be checked: a click
        explains it, and the exclusive button group never unchecks the real current tab. It is checkable again as
        soon as the workspace becomes available.
        """
        for ws in workspaces.WORKSPACES:
            button = self.workspaceButtons.get(ws.key)
            if button is None:
                continue
            available = workspaces.isAvailable(ws)
            if bool(button.property("studioUnavailable")) == (not available) and button.toolTip:
                continue
            button.setEnabled(True)
            button.setCheckable(available)
            button.setProperty("studioUnavailable", not available)
            # The application style paints the text of a segment in its own color, so the dimmed look is an opacity effect
            effectClass = getattr(qt, "QGraphicsOpacityEffect", None)
            if available or effectClass is None:
                button.setGraphicsEffect(None)
            else:
                effect = effectClass(button)
                effect.setOpacity(UNAVAILABLE_OPACITY)
                button.setGraphicsEffect(effect)
            button.setToolTip(self._workspaceToolTip(ws, available))
            style = button.style()
            style.unpolish(button)
            style.polish(button)
        if hasattr(self, "viewActions"):
            self.refreshIcons()

    def _onWorkspaceClicked(self, key):
        ws = workspaces.workspace(key)
        self.refreshWorkspaceAvailability()
        if ws is not None and not workspaces.isAvailable(ws):
            self.showUnavailablePopover(key)
            return
        self.shell.switchTo(key)

    def setCurrentWorkspace(self, key):
        """Check the tab of a workspace, or no tab (key None) for transient modules.

        With no tab checked, the tab of the workspace that the transient module was opened from is shown in the
        "parent" style (accent text and underline): clicking it returns.
        """
        button = self.workspaceButtons.get(key) if key else None
        parentKey = None
        if button is not None:
            button.setChecked(True)
        else:
            self.switcherGroup.setExclusive(False)
            for workspaceButton in self.workspaceButtons.values():
                workspaceButton.setChecked(False)
            self.switcherGroup.setExclusive(True)
            if not key:
                workspacesController = getattr(self.shell, "workspaces", None)
                parentKey = getattr(self.shell, "_transientOrigin", None) or getattr(workspacesController, "last", None)
        for workspaceKey, workspaceButton in self.workspaceButtons.items():
            # Qt keeps one Tab stop in a group of checkable buttons (the one that had the focus last): give them all
            # back, so that Tab reaches every tab again (the arrow keys move between them, ``_SwitcherKeys``)
            workspaceButton.setFocusPolicy(qt.Qt.TabFocus)
            isParent = workspaceKey == parentKey
            if bool(workspaceButton.property("studioParent")) != isParent:
                workspaceButton.setProperty("studioParent", isParent)
                style = workspaceButton.style()
                style.unpolish(workspaceButton)
                style.polish(workspaceButton)
        self.setUndoRedoVisible(key == "draw")
        self._connectEditor()
        self._updateDefaultBreadcrumb()

    def showUnavailablePopover(self, key="map"):
        """Explain a workspace that is not in this build (Esc or a click outside closes it). Return the popover."""
        ws = workspaces.workspace(key)
        anchor = self.workspaceButtons.get(key)
        if ws is None or anchor is None:
            return None
        if self._mapPopover is not None:
            self._mapPopover.close()
        menu = qt.QMenu(anchor)
        menu.setObjectName("StudioMapUnavailablePopover")
        panel = qt.QWidget()
        panelLayout = qt.QVBoxLayout(panel)
        panelLayout.setContentsMargins(12, 10, 12, 10)
        panelLayout.setSpacing(8)
        text = qt.QLabel(_("Map computes the brain network connected to a lesion from a normative connectome. "
                           "It is not part of this build yet; when it is, it appears here as the Map tab."))
        text.setObjectName("StudioMapUnavailableText")
        text.setWordWrap(True)
        text.setFixedWidth(360)
        panelLayout.addWidget(text)
        links = [f'<a href="readme">{_("Read about Map in the README")}</a>']
        if self._libraryInstalled():
            links.append(f'<a href="copy">{_("Copy command-line…")}</a>')
        linkLabel = qt.QLabel(" · ".join(links))
        linkLabel.setObjectName("StudioMapUnavailableLinks")
        linkLabel.setTextFormat(qt.Qt.RichText)
        linkLabel.setTextInteractionFlags(qt.Qt.TextBrowserInteraction)
        linkLabel.connect("linkActivated(QString)", self._onUnavailableLink)
        panelLayout.addWidget(linkLabel)
        action = qt.QWidgetAction(menu)
        action.setDefaultWidget(panel)
        menu.addAction(action)
        self._mapPopover = menu
        self.shell.registerEscapeOverlay(menu, menu.close)
        menu.connect("aboutToHide()", self._onUnavailablePopoverHidden)
        menu.popup(anchor.mapToGlobal(qt.QPoint(0, anchor.height + 4)))
        return menu

    @staticmethod
    def _libraryInstalled():
        """True when the lesion network mapping library can be imported (the command line is then worth offering)."""
        try:
            return importlib.util.find_spec("LesionNetworkMappingLib") is not None
        except (ImportError, ValueError):
            return False

    @staticmethod
    def readmeUrl():
        """The README that explains Map: the copy next to the application when there is one, else the project page."""
        home = str(slicer.app.slicerHome)
        for folder in (home, os.path.dirname(home)):
            path = os.path.join(folder, "README.md")
            if os.path.isfile(path):
                return qt.QUrl.fromLocalFile(path)
        return qt.QUrl(README_MAP_URL)

    @staticmethod
    def libraryCommand():
        """The command line that runs the lesion network mapping engine from a terminal."""
        return f'"{slicer.app.launcherExecutableFilePath}" --launch PythonSlicer -m LesionNetworkMappingLib.engine --help'

    def _onUnavailableLink(self, link):
        popover = self._mapPopover
        if link == "copy":
            qt.QApplication.clipboard().setText(self.libraryCommand())
            self.shell.toast(_("Command copied"))
        elif link == "readme":
            from . import projects

            projects.openLink(self.readmeUrl())
        if popover is not None:
            popover.close()

    def _onUnavailablePopoverHidden(self):
        popover = self._mapPopover
        self._mapPopover = None
        if popover is not None:
            self.shell.unregisterEscapeOverlay(popover)
            popover.deleteLater()

    #
    # Breadcrumb and status chip
    #

    def setBreadcrumb(self, primary, secondary=None, onClick=None, separator=None):
        """Name where the user is. An explicit breadcrumb (a loaded set: "sub-05 · 5 of 20"; a study: "study / subject")
        wins until ``setBreadcrumb(None)``; it is clickable when ``onClick`` is given. The default names the Draw
        source scan and the lesion ("patient_T1 > Lesions") and opens Library.
        """
        if primary is None:
            self._explicitBreadcrumb = None
            self._breadcrumbCallback = None
            self._updateDefaultBreadcrumb()
            return
        self._explicitBreadcrumb = (primary, secondary)
        self._breadcrumbCallback = onClick
        if separator is None:
            separator = " · " if secondary and re.fullmatch(r"\d+ of \d+", str(secondary)) else "  /  "
        self._showBreadcrumb(primary, secondary, separator, clickable=onClick is not None)

    def _showBreadcrumb(self, primary, secondary, separator, clickable):
        text = primary if not secondary else f"{primary}{separator}{secondary}"
        self._breadcrumbText = text
        if text:
            description = _("Show in Library") if clickable else _("where you are")
            self.breadcrumbButton.setToolTip(ui.tooltip(text, None, description))
        else:
            self.breadcrumbButton.setToolTip(ui.tooltip(_("Breadcrumb"), None, _("nothing is open yet")))
        self.breadcrumbButton.setVisible(bool(text))
        self.breadcrumbButton.setFocusPolicy(qt.Qt.TabFocus if clickable and text else qt.Qt.NoFocus)
        self.breadcrumbButton.setCursor(qt.QCursor(qt.Qt.PointingHandCursor if clickable and text else qt.Qt.ArrowCursor))
        if bool(self.breadcrumbButton.property("studioClickable")) != bool(clickable and text):
            self.breadcrumbButton.setProperty("studioClickable", bool(clickable and text))
            style = self.breadcrumbButton.style()
            style.unpolish(self.breadcrumbButton)
            style.polish(self.breadcrumbButton)
        self._elideBreadcrumb()
        self.updateColumnMinimums()

    def breadcrumbText(self):
        """The full (not elided) breadcrumb text."""
        return self._breadcrumbText

    def _elideBreadcrumb(self):
        """Elide the breadcrumb (in the middle) to the room that its column leaves."""
        button = self.breadcrumbButton
        text = self._breadcrumbText
        if not text:
            if button.text:
                button.setText("")
            return
        room = self.leftContainer.width - self.menuButton.width - self.leftContainer.layout().spacing
        if not self.projectMenuButton.isHidden():
            room -= self.projectMenuButton.width + self.leftContainer.layout().spacing
        if not self.saveStatusButton.isHidden():
            room -= self.saveStatusButton.sizeHint.width() + self.leftContainer.layout().spacing
        room = min(room, button.maximumWidth)
        room = max(room, BREADCRUMB_MIN_WIDTH)
        elided = qt.QFontMetrics(button.font).elidedText(text, qt.Qt.ElideMiddle, room - _BREADCRUMB_PADDING)
        # A QToolButton reads '&' as a mnemonic: double it so that a name like "T1 & T2" shows as typed and
        # registers no shortcut
        elided = elided.replace("&", "&&")
        if button.text != elided:
            button.setText(elided)

    def _sourceVolume(self):
        """The scan that the breadcrumb names: the Segment Editor source, else the first scan that is not a figure or a map."""
        editor = self._segmentEditor()
        volume = editor.sourceVolumeNode() if editor is not None else None
        if volume is not None:
            return volume
        scene = slicer.mrmlScene
        for index in range(scene.GetNumberOfNodesByClass("vtkMRMLScalarVolumeNode")):
            node = scene.GetNthNodeByClass(index, "vtkMRMLScalarVolumeNode")
            if node.GetAttribute(space.ROLE_ATTRIBUTE) not in (space.ROLE_FIGURE, space.ROLE_STATMAP):
                return node
        return None

    @staticmethod
    def _projects():
        """``StudioLib.projects`` when projects are on (A-09), else None."""
        try:
            from . import projects
        except ImportError:
            return None
        return projects if projects.isEnabled() else None

    def _currentProject(self):
        projects = self._projects()
        return projects.currentProject() if projects is not None else None

    def _updateProjectMenuButton(self):
        """The project menu is shown while projects are on and the title is shown (or a project is open)."""
        shown = self._projects() is not None and (bool(self._breadcrumbText) or self._currentProject() is not None)
        if self.projectMenuButton.isHidden() == shown:
            self.projectMenuButton.setVisible(shown)
            self._elideBreadcrumb()

    def _updateDefaultBreadcrumb(self, *args):
        if self._explicitBreadcrumb is not None:
            self._updateProjectMenuButton()
            return
        # The title is the open project (V-02), never a node name; without a project (projects off, or before the first
        # save) the scan that Draw edits and its lesion
        project = self._currentProject()
        if project is not None:
            self._showBreadcrumb(os.path.basename(os.path.normpath(project.path)), None, BREADCRUMB_SEPARATOR, clickable=True)
            self._updateProjectMenuButton()
            return
        volume = self._sourceVolume()
        secondary = None
        if volume is not None and self.shell.currentWorkspace() == "draw":
            editor = self._segmentEditor()
            segmentation = editor.segmentationNode() if editor is not None else None
            secondary = segmentation.GetName() if segmentation is not None else None
        self._showBreadcrumb(volume.GetName() if volume is not None else "", secondary, BREADCRUMB_SEPARATOR, clickable=volume is not None)
        self._updateProjectMenuButton()

    def _onProjectsChanged(self):
        """The open project or its name changed (``projects.addListener``)."""
        timer = getattr(self, "_breadcrumbTimer", None)
        if timer is not None:
            timer.start()

    def projectMenuActions(self):
        """``[(text, callback or submenu list, enabled)]`` of the project menu (V-02); a ``None`` text is a separator."""
        from . import projects, projects_ui

        project = projects.currentProject()
        manager = projects.fileManagerName()
        recent = []
        current = os.path.normcase(os.path.realpath(project.path)) if project is not None else None
        for info in projects.listProjects()[:8]:
            isCurrent = current is not None and os.path.normcase(os.path.realpath(info.path)) == current
            recent.append((info.name, lambda path=info.path: projects_ui.openProject(path), not isCurrent))
        return [
            (_("Rename…"), projects_ui.renameDialog, project is not None),
            (_("Show in {manager}").format(manager=manager), projects_ui.showProjectFolder, project is not None),
            (None, None, True),
            (_("New project"), projects_ui.newProject, True),
            (_("Recent projects"), recent, bool(recent)),
            (None, None, True),
            (_("Close project"), projects_ui.closeScene, project is not None),
        ]

    def _fillProjectMenu(self):
        menu = self.projectMenu
        menu.clear()
        try:
            entries = self.projectMenuActions()
        except Exception:
            logging.error(f"Studio: the project menu failed:\n{traceback.format_exc()}")
            return

        def add(target, text, callback, enabled):
            if text is None:
                target.addSeparator()
            elif isinstance(callback, list):
                submenu = target.addMenu(text)
                submenu.setObjectName("StudioRecentProjectsMenu")
                submenu.setEnabled(enabled)
                for itemText, itemCallback, itemEnabled in callback:
                    add(submenu, itemText, itemCallback, itemEnabled)
            else:
                action = target.addAction(text)
                action.setEnabled(enabled)
                action.connect("triggered()", lambda callback=callback: self._runProjectAction(callback))

        for text, callback, enabled in entries:
            add(menu, text, callback, enabled)

    def _runProjectAction(self, callback):
        try:
            callback()
        except Exception:
            logging.error(f"Studio: a project action failed:\n{traceback.format_exc()}")
        self._breadcrumbTimer.start()

    def _onBreadcrumbClicked(self):
        if self._explicitBreadcrumb is None:
            self.shell.switchTo("library")
            return
        if self._breadcrumbCallback is not None:
            try:
                self._breadcrumbCallback()
            except Exception:
                logging.error(f"Studio breadcrumb callback failed:\n{traceback.format_exc()}")

    def _connectEditor(self):
        """Follow the source volume and the segmentation of the Segment Editor (once its widget exists)."""
        editor = self._segmentEditor()
        if editor is None or editor == self._editor:
            return
        self._disconnectEditor()
        self._editor = editor
        editor.connect("sourceVolumeNodeChanged(vtkMRMLVolumeNode*)", self._onEditorChanged)
        editor.connect("segmentationNodeChanged(vtkMRMLSegmentationNode*)", self._onEditorChanged)

    def _disconnectEditor(self):
        editor, self._editor = self._editor, None
        if editor is None:
            return
        try:
            editor.disconnect("sourceVolumeNodeChanged(vtkMRMLVolumeNode*)", self._onEditorChanged)
            editor.disconnect("segmentationNodeChanged(vtkMRMLSegmentationNode*)", self._onEditorChanged)
        except Exception:
            # The editor widget was deleted
            pass

    def _onEditorChanged(self, *args):
        self._breadcrumbTimer.start()

    def _observeScene(self):
        self._breadcrumbTimer = qt.QTimer(self.toolBar)
        self._breadcrumbTimer.setSingleShot(True)
        self._breadcrumbTimer.setInterval(200)
        self._breadcrumbTimer.connect("timeout()", self._onBreadcrumbTimer)
        for event in (slicer.vtkMRMLScene.NodeAddedEvent, slicer.vtkMRMLScene.NodeRemovedEvent, slicer.vtkMRMLScene.EndCloseEvent, slicer.vtkMRMLScene.EndImportEvent):
            tag = slicer.mrmlScene.AddObserver(event, self._onSceneChanged)
            self._sceneObservations.append(tag)
        projects = self._projects()
        if projects is not None:
            projects.addListener(self._onProjectsChanged)

    def _onBreadcrumbTimer(self):
        self._connectEditor()
        self._updateDefaultBreadcrumb()

    def _onSceneChanged(self, caller, event):
        self._breadcrumbTimer.start()

    def setStatusChip(self, text=None, level="neutral"):
        if not text:
            self.statusChip.hide()
        else:
            self.statusChip.setText(text)
            ui.setChipLevel(self.statusChip, level)
            self.statusChip.show()
        self.updateSaveStatusCompact()
        self.updateColumnMinimums()

    #
    # Autosave status (A-01b)
    #

    def setSaveStatus(self, text=None, level="neutral", toolTip="", iconName=None, title=None):
        """Show the save status ("Autosaved · 2 min ago", or "Saved" in a project); level: neutral, busy, ok or warn. None
        hides it.

        ``iconName`` replaces the icon of the level; ``title`` names the status in the tooltip ("Autosave" by default). The
        text is kept in the tooltip when the button shows its icon only (``updateSaveStatusCompact()``).
        """
        self._saveStatusTitle = title
        if not text:
            self._saveStatus = None
            self.saveStatusButton.hide()
            self._elideBreadcrumb()
            self.updateColumnMinimums()
            return
        if level not in SAVE_STATUS_LEVELS:
            raise ValueError(f"Unknown save status level: {level}")
        status = (text, level, toolTip or "", iconName)
        if status == self._saveStatus and self.saveStatusButton.visible:
            return
        levelChanged = self._saveStatus is None or self._saveStatus[1] != level
        self._saveStatus = status
        self.saveStatusButton.setText(text)
        self._updateSaveStatusIcon()
        if levelChanged:
            ui.bindStyleSheet(self.saveStatusButton, self._saveStatusStyleSheet)
        self.saveStatusButton.show()
        self.updateSaveStatusCompact()
        self._elideBreadcrumb()
        self.updateColumnMinimums()

    def saveStatus(self):
        """``(text, level, toolTip, iconName)`` of the shown autosave status, or None."""
        return self._saveStatus

    def setSaveStatusMenu(self, menu=None):
        """The menu that the autosave status opens (None: no menu)."""
        self.saveStatusButton.setMenu(menu)

    def isSaveStatusCompact(self):
        return self.saveStatusButton.toolButtonStyle == qt.Qt.ToolButtonIconOnly

    def updateSaveStatusCompact(self):
        """Icon only (the text stays in the tooltip) in compact mode, below 1500 px, or below 1700 px with the status chip."""
        width = self.shell.mainWindow.width if self.shell is not None and self.shell.mainWindow is not None else 0
        compact = (self._compact or width < SAVE_STATUS_COMPACT_WIDTH
                   or (not self.statusChip.isHidden() and width < SAVE_STATUS_CHIP_COMPACT_WIDTH))
        style = qt.Qt.ToolButtonIconOnly if compact else qt.Qt.ToolButtonTextBesideIcon
        if self.saveStatusButton.toolButtonStyle != style:
            self.saveStatusButton.setToolButtonStyle(style)
            self._elideBreadcrumb()
            self.updateColumnMinimums()
        if self._saveStatus is not None:
            # "Autosave · <status, when only the icon is shown> <description>" (USABILITY 10)
            text, _level, toolTip, _iconName = self._saveStatus
            description = "\n".join(part for part in ((f"{text}." if compact else ""), toolTip) if part)
            self.saveStatusButton.setToolTip(ui.tooltip(getattr(self, "_saveStatusTitle", None) or _("Autosave"), None, description or None))
        return compact

    def _updateSaveStatusIcon(self):
        if self._saveStatus is None:
            return
        _text, level, _toolTip, iconName = self._saveStatus
        defaultIcon, color = SAVE_STATUS_LEVELS[level]
        self.saveStatusButton.setIcon(qt.QIcon(ui.pixmap(iconName or defaultIcon, SAVE_STATUS_ICON_SIZE, color)))

    def _saveStatusStyleSheet(self):
        level = self._saveStatus[1] if self._saveStatus is not None else "neutral"
        color = ui.token(SAVE_STATUS_TEXT[level]).name()
        return (f"QToolButton#StudioSaveStatus {{ color: {color}; padding: 0px 6px; margin-left: 2px; }}"
                "QToolButton#StudioSaveStatus::menu-indicator { image: none; width: 0px; }")

    #
    # Undo/redo of the Segment Editor
    #

    def setUndoRedoVisible(self, visible):
        # Hidden outside Draw: the switcher is centred by the grid, so their room is not kept
        self.undoButton.setVisible(visible)
        self.redoButton.setVisible(visible)
        if visible:
            self.updateUndoRedo()
            self.undoRedoTimer.start()
        else:
            self.undoRedoTimer.stop()
        self.updateColumnMinimums()

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

    def setLayoutFromPicker(self, key):
        """Apply a layout of the picker ("focus", "single", "fourup", "row", "focus3d" or "compare").

        "single" maximizes the target view inside the current layout (pressing it again restores); every other
        entry first restores a maximized view. The shell routes Alt+1..5 and the palette here.
        """
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        wasMaximized = layouts.isMaximized()
        self._userLayoutRequests += 1
        try:
            layouts.applyPickerEntry(key, self.shell.hoverTracker)
        finally:
            self._userLayoutRequests -= 1
        if key == "single" and not wasMaximized and layouts.isMaximized():
            self._announceLibraryLayout(layouts.PICKER_ENTRIES[1][1])
        if key != "single" and wasMaximized and not layouts.isMaximized() and self.shell.canvas() is not None:
            self._announceLibraryLayout(layouts.layoutName(layoutManager.layout))

    def _updateLayoutPicker(self):
        layoutManager = slicer.app.layoutManager()
        current = layoutManager.layout if layoutManager is not None else None
        maximized = layouts.isMaximized()
        showCompare = layouts.figureShown() or current == layouts.COMPARE_LAYOUT_ID
        self.layoutTiles["compare"].setVisible(showCompare)
        for key, tile in self.layoutTiles.items():
            if key == "single":
                tile.setChecked(maximized)
            else:
                # While a view is maximized the picker shows one state: Single
                tile.setChecked(not maximized and layouts.pickerLayoutId(key) == current)
        self.viewActions["crosshair"].setChecked(layouts.crosshairVisible())
        self.viewActions["intersections"].setChecked(layouts.intersectionsVisible())
        self.viewActions["link"].setChecked(layouts.viewsLinked())

    def _onLayoutTileClicked(self, key):
        self.layoutMenu.close()
        self.shell.setLayoutFromPicker(key)

    def _onViewAction(self, key, checked):
        if key == "crosshair":
            layouts.setCrosshairVisible(checked)
        elif key == "intersections":
            layouts.setIntersectionsVisible(checked)
        elif key == "link":
            layouts.setViewsLinked(checked)
        elif key == "reset":
            layouts.resetViews()

    def _onMoreLayouts(self):
        self.layoutMenu.close()
        menu = self.moreLayoutsMenu()
        menu.popup(self.layoutButton.mapToGlobal(qt.QPoint(0, self.layoutButton.height)))

    def moreLayoutsMenu(self):
        """The "More layouts…" menu: the Studio layouts, then the actions of the classic Layout menu (the same QAction
        objects: the Layout menu itself is not changed).
        """
        if self._moreLayoutsMenu is None:
            menu = qt.QMenu(self.layoutButton)
            menu.setObjectName("StudioMoreLayoutsMenu")
            self._moreLayoutsMenu = menu
            self._moreLayoutsStudioActions = []
            for layoutId, (title, description) in layouts.LAYOUTS.items():
                action = menu.addAction(title)
                action.setObjectName(f"StudioMoreLayoutsAction_{layoutId}")
                action.setCheckable(True)
                action.connect("triggered(bool)", lambda checked, layoutId=layoutId: self._setStudioLayout(layoutId))
                self._moreLayoutsStudioActions.append((layoutId, action))
            menu.addSeparator()
            layoutMenu = self.shell.mainWindow.findChild("QMenu", "LayoutMenu")
            if layoutMenu is not None:
                menu.addActions(layoutMenu.actions())
            menu.connect("aboutToShow()", self._updateMoreLayoutsMenu)
            menu.connect("aboutToHide()", self._onMoreLayoutsMenuHidden)
        return self._moreLayoutsMenu

    def _onMoreLayoutsMenuHidden(self):
        # Qt hides the menu before it triggers the chosen action: the layout it sets is still a user request
        qt.QTimer.singleShot(0, self._clearMoreLayoutsMenuOpen)

    def _clearMoreLayoutsMenuOpen(self):
        self._moreLayoutsMenuOpen = False

    def _setStudioLayout(self, layoutId):
        layouts.restoreMaximized()
        self.setLayoutByUser(layoutId)

    def setLayoutByUser(self, layoutId):
        """Set a layout that the user chose (Search, "More layouts…"): Library says that it is shown in Draw."""
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        if layouts.isStudioLayout(layoutId):
            layouts.register()
        self._userLayoutRequests += 1
        try:
            layoutManager.setLayout(layoutId)
        finally:
            self._userLayoutRequests -= 1

    def _updateMoreLayoutsMenu(self):
        self._moreLayoutsMenuOpen = True
        layoutManager = slicer.app.layoutManager()
        current = layoutManager.layout if layoutManager is not None else None
        for layoutId, action in self._moreLayoutsStudioActions:
            action.setChecked(layoutId == current)

    def _connectLayoutManager(self):
        layoutManager = self._layoutManager
        if layoutManager is None:
            return
        layoutManager.connect("layoutChanged(int)", self._onLayoutChanged)
        sliceWidget = layoutManager.sliceWidget("Red")
        self._redSliceNode = sliceWidget.mrmlSliceNode() if sliceWidget is not None else None
        if self._redSliceNode is not None:
            self._redSliceVisible = bool(self._redSliceNode.GetSliceVisible())
            self._redSliceObservation = self._redSliceNode.AddObserver(vtk.vtkCommand.ModifiedEvent, self._onRedSliceModified)

    def _onRedSliceModified(self, caller, event):
        visible = bool(caller.GetSliceVisible())
        if visible != self._redSliceVisible:
            self._redSliceVisible = visible
            if not self._threeDPrepared:
                self._redSliceTouched = True

    def _onLayoutChanged(self, layoutId=None):
        layoutManager = self._layoutManager
        if layoutManager is None:
            return
        if not self._threeDPrepared and layouts.threeDViewNamesInLayout() and self.shell.canvas() is None:
            # The 3D widget is laid out first (and the user sees it: the Library canvas covers the views)
            qt.QTimer.singleShot(0, self._prepareThreeD)
        userRequest = self._userLayoutRequests > 0 or self._moreLayoutsMenuOpen
        if userRequest and self.shell.canvas() is not None and layoutId is not None and layoutId > 0 and not self.shell.workspaces.isSwitching():
            self._announceLibraryLayout(layouts.layoutName(layoutId))

    def _announceLibraryLayout(self, name):
        """Library never switches layout: say that a layout chosen there is shown in Draw."""
        if self.shell.canvas() is not None:
            self.shell.toast(_("Layout: {name} (shown in Draw)").format(name=name))

    def _prepareThreeD(self):
        """The first 3D view of the session shows the slice in a 3/4 view: focal point reset, camera turned 30° and
        raised 20°, and the Red slice shown in 3D unless the user changed that.
        """
        layoutManager = self._layoutManager
        names = layouts.threeDViewNamesInLayout()
        if self._threeDPrepared or layoutManager is None or not names or self.shell.canvas() is not None:
            return
        self._threeDPrepared = True
        try:
            threeDWidget = layoutManager.threeDWidget(names[0])
            threeDView = threeDWidget.threeDView()
            threeDView.resetFocalPoint()
            # The reset also resets the rotation: turn the camera afterwards
            threeDView.resetCamera()
            cameraNode = threeDView.cameraNode()
            camera = cameraNode.GetCamera() if cameraNode is not None else None
            if camera is not None:
                camera.Azimuth(30)
                camera.Elevation(20)
                camera.OrthogonalizeViewUp()
            if self._redSliceNode is not None and not self._redSliceTouched:
                self._redSliceNode.SetSliceVisible(True)
        except Exception:
            logging.error(f"Studio: cannot prepare the 3D view:\n{traceback.format_exc()}")

    #
    # Error badge
    #

    def _connectErrorLog(self):
        self.errorLogModel = slicer.app.errorLogModel()
        self._scannedRows = 0
        self._errorRows = 0
        self._seenErrorRows = 0
        self._dicomFirstRunRow = None
        self._dicomFirstRunSqlErrors = 0
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
        for row in range(self._scannedRows, rowCount):
            isError = model.index(row, _LEVEL_COLUMN).data() in _ERROR_LEVEL_NAMES
            if not self._isExpectedFirstRunMessage(row, isError) and isError:
                self._errorRows += 1
        self._scannedRows = rowCount
        self._updateErrorBadge()

    def _isExpectedFirstRunMessage(self, row, isError):
        """True for the messages of the first DICOM use of a profile (``_DICOM_FIRST_RUN_NOTICE``), not counted."""
        description = self.errorLogModel.index(row, _DESCRIPTION_COLUMN).data() or ""
        if description.startswith(_DICOM_FIRST_RUN_NOTICE):
            self._dicomFirstRunRow = row
            self._dicomFirstRunSqlErrors = 0
            return True
        if not isError or self._dicomFirstRunRow is None or row - self._dicomFirstRunRow > _DICOM_FIRST_RUN_ROWS:
            return False
        if self._dicomFirstRunSqlErrors >= _DICOM_FIRST_RUN_MAX_SQL_ERRORS:
            return False
        lines = description.splitlines()
        if lines and lines[0].startswith("SQL failed:") and all(_DICOM_FIRST_RUN_SQL_LINES.match(line) for line in lines):
            self._dicomFirstRunRow = row  # the next query of the same burst
            self._dicomFirstRunSqlErrors += 1
            return True
        return False

    def _rescanErrors(self, *args):
        # The log was cleared or filtered
        self._scannedRows = 0
        self._errorRows = 0
        self._dicomFirstRunRow = None
        self._dicomFirstRunSqlErrors = 0
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
        # A hidden badge is not a focus stop
        self.errorBadge.setFocusPolicy(qt.Qt.TabFocus if count > 0 else qt.Qt.NoFocus)
        self.errorBadge.setVisible(count > 0)
        self.updateColumnMinimums()

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
        self.setSaveStatusMenu(None)
        ui.bindStyleSheet(self.saveStatusButton, None)
        self.undoRedoTimer.stop()
        self._breadcrumbTimer.stop()
        projects = self._projects()
        if projects is not None:
            projects.removeListener(self._onProjectsChanged)
        self._errorCountTimer.stop()
        if self._mapPopover is not None:
            self._mapPopover.close()
        # The shell method is used again
        self.shell.__dict__.pop("setLayoutFromPicker", None)
        self._disconnectEditor()
        if self._layoutManager is not None:
            self._layoutManager.disconnect("layoutChanged(int)", self._onLayoutChanged)
            self._layoutManager = None
        if self._redSliceNode is not None and self._redSliceObservation is not None:
            self._redSliceNode.RemoveObserver(self._redSliceObservation)
        self._redSliceNode = None
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

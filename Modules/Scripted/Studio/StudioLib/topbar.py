"""Top bar of Slicer Studio (SPEC 2.4, USABILITY 2, 6 and 10).

A 44 px toolbar ``StudioTopBar`` whose contents are a 3-column grid, so that the workspace switcher is centred
on the window whatever the other columns hold (no fixed widths, the window can still shrink):

- left: the app menu (``≡``) and the breadcrumb;
- centre: the workspace switcher [Library | Draw | Map] (``Ctrl+Alt+1/2/3``);
- right: the status chip, Undo/Redo (Draw only), the layout picker (``Alt+1..5``), the search pill (``Ctrl+K``)
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
SEARCH_PILL_WIDTH = 240
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
#: Where the README explains the Map workspace (the section "Lesion network mapping")
README_MAP_URL = "https://github.com/azhmeerjesani/Slicer-2#lesion-network-mapping"

_ERROR_LEVEL = 0x40  # ctkErrorLogLevel::Error
_FATAL_LEVEL = 0x100  # ctkErrorLogLevel::Fatal (Critical is 0x80, None is 0x200)
_ERROR_LEVEL_NAMES = ("Error", "Critical", "Fatal")

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
        leftLayout.addStretch(1)
        self._leftResizeFilter = _ResizeFilter(self._elideBreadcrumb)
        self.leftContainer.installEventFilter(self._leftResizeFilter)

        # Centre: workspace switcher
        self.switcherFrame, self.switcherGroup = ui.segmented([ws.title for ws in workspaces.WORKSPACES])
        self.switcherFrame.setObjectName("StudioWorkspaceSwitcher")
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
        return (
            f"QToolButton#StudioBreadcrumb {{ background-color: transparent; color: {ui.token('text2').name()}; border: none; border-radius: 6px; padding: 3px 6px; font-size: {size}px; }}"
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
        self.searchPlaceholder = qt.QLabel(_("Search") if hasPalette else _("Search modules"))
        self.searchPlaceholder.setObjectName("StudioSearchPlaceholder")
        self.pillLayout.addWidget(self.searchPlaceholder, 1)
        self.searchKey = ui.kbd(ui.keyText("Ctrl+K"))
        self.searchKey.setFixedHeight(20)
        self.pillLayout.addWidget(self.searchKey)
        for child in (self.searchIcon, self.searchPlaceholder, self.searchKey):
            child.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
        ui.bindStyleSheet(pill, self._searchPillStyleSheet)
        ui.installButtonKeys(pill)
        pill.connect("clicked()", self.shell.openPalette)
        return pill

    def _searchPillStyleSheet(self):
        size = ui.fontPx(13)
        keySize = ui.fontPx(11)
        # The key chip is inset: a darker well with a light edge, like a pressed key cap
        return (
            f"QPushButton#StudioSearchPill {{ background-color: {ui.token('base').name()}; border: 1px solid {ui.token('control-border').name()}; border-radius: 15px; }}"
            f"QPushButton#StudioSearchPill:hover {{ border-color: {ui.token('text3').name()}; }}"
            f"QPushButton#StudioSearchPill:focus {{ border: 2px solid {ui.token('accent-fg').name()}; }}"
            f"QLabel#StudioSearchPlaceholder {{ color: {ui.token('text3').name()}; font-size: {size}px; background: transparent; }}"
            f"QPushButton#StudioSearchPill QLabel#StudioKbd {{ background-color: {ui.token('control-pressed').name()}; color: {ui.token('text2').name()};"
            f" border: 1px solid {ui.token('border').name()}; border-top-color: {ui.token('hairline').name()}; border-radius: 5px; padding: 1px 6px; font-size: {keySize}px; }}")

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

    def refreshIcons(self):
        """Re-tint the icons for the current palette."""
        buttons = [self.menuButton, self.undoButton, self.redoButton, self.layoutButton, *self.workspaceButtons.values(), *self.layoutTiles.values()]
        for button in buttons:
            size = button.iconSize.width()
            button.setIcon(ui.icon(button.property("studioIcon"), size))
        for action in self.viewActions.values():
            action.setIcon(ui.icon(action.property("studioIcon"), 16))
        self.errorBadge.setIcon(ui.icon("circle-alert", ICON_SIZE, role="danger"))
        self.searchIcon.setPixmap(ui.pixmap("search", 14, "text3"))

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
            qt.QDesktopServices.openUrl(self.readmeUrl())
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

    def _updateDefaultBreadcrumb(self, *args):
        if self._explicitBreadcrumb is not None:
            return
        volume = self._sourceVolume()
        secondary = None
        if volume is not None and self.shell.currentWorkspace() == "draw":
            editor = self._segmentEditor()
            segmentation = editor.segmentationNode() if editor is not None else None
            secondary = segmentation.GetName() if segmentation is not None else None
        self._showBreadcrumb(volume.GetName() if volume is not None else "", secondary, BREADCRUMB_SEPARATOR, clickable=volume is not None)

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
        self.updateColumnMinimums()

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
        layouts.applyPickerEntry(key, self.shell.hoverTracker)
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
        return self._moreLayoutsMenu

    def _setStudioLayout(self, layoutId):
        layouts.restoreMaximized()
        layoutManager = slicer.app.layoutManager()
        layouts.register()
        layoutManager.setLayout(layoutId)

    def _updateMoreLayoutsMenu(self):
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
        if self.shell.canvas() is not None and layoutId is not None and not self.shell.workspaces.isSwitching():
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
        self.undoRedoTimer.stop()
        self._breadcrumbTimer.stop()
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

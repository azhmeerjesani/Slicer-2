r"""The keyboard shortcut overlay of Slicer Studio (``Ctrl+/``, ≡ > Keyboard shortcuts; USABILITY 10, SPEC 2.11).

``toggle(shell)`` shows or hides a translucent overlay over the view area: a search field and three columns,
Draw (the tools), Views, and App with Library. It is generated each time it opens from what is live:

- every Studio ``QShortcut`` of the main window (properties ``studioShortcut`` / ``studioDescription``), including the
  keys that a workspace installs when it is entered (``Shell.addWorkspaceShortcut()``), so the Draw keys are listed
  in any workspace;
- the main window actions that have a key (File, Edit, View and Help, and the actions attached to the window);
- the Segment Editor's live effect order: 1 to 0 and Shift+1 to Shift+0 pick the tools in the order of the Draw panel;
- the keys of the Segment Editor and of the slice views (SPEC 5.4 and Appendix A), including the Smart fill and
  brush keys, and the view gestures (wheel, Shift+move, double-click, right-drag, Ctrl+wheel, r, PageUp/PageDown...);
- the Esc ladder (``shell.ESCAPE_LADDER``), with the note that Library is outside it;
- platform notes: the ≡ menu (F10; on a Mac also the menu bar, Ctrl+F2), the two inspector keys, AltGr+digit.

It is not modal and never times out: Esc (the shell's Esc ladder, rung 0.5, or Esc in its search field), Ctrl+/ or its
✕ closes it. ``sections(shell)`` returns the same content as data (tests, documentation).
"""

import logging
import re
import sys
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import ui

#: Width of the columns and of the keys column of each row (keys that do not fit go on another line)
COLUMN_MIN_WIDTH = 330
COLUMN_SPACING = 32
KEYS_COLUMN_WIDTH = 150
KEY_SPACING = 3
#: Opacity of the black wash behind the card (the chrome of Search, VISUAL 4.6)
BACKGROUND_ALPHA = 0.5
#: Padding inside the card
MARGIN = 24
#: The card over the view area: its distance to the top and bottom, to the sides, and its widest
CARD_INSET_Y = 24
CARD_INSET_X = 32
CARD_MAX_WIDTH = 1120
CARD_RADIUS = 14
#: The search field of the header: its width, and the narrowest it gets before the hint is hidden
SEARCH_WIDTH = 240
SEARCH_MIN_WIDTH = 120
#: Space between the title, the hint, the search field and the close button
HEADER_SPACING = 12

#: Sections, in the order of the columns
SECTION_DRAW = "Draw"
SECTION_VIEWS = "Views"
SECTION_APP = "App"
SECTION_LIBRARY = "Library"
SECTION_ESCAPE = "Esc"
SECTION_PLATFORM = "Platform"

#: Studio keys of the views (layouts) rather than of the application
_VIEW_KEY_PATTERN = re.compile(r"^Alt\+[0-9]$")

_sheet = None


def _isAlive(qobject):
    if qobject is None:
        return False
    try:
        qobject.objectName
    except Exception:
        return False
    return True


def _shellModule():
    return sys.modules.get("StudioLib.shell")


def _isMacOS():
    return sys.platform == "darwin"


def _native(key):
    """A key ("Ctrl+K") as displayed on this platform ("⌘K"); descriptions of gestures ("Shift+wheel") and single
    characters ("v" is not "V") are kept.
    """
    if not key:
        return ""
    return key if len(key) == 1 else ui.keyText(key)


def _advance(metrics, text):
    advance = getattr(metrics, "horizontalAdvance", None)
    return advance(text) if advance is not None else metrics.width(text)


def _portable(sequence):
    return sequence.toString(qt.QKeySequence.PortableText) if hasattr(sequence, "toString") else str(sequence)


def _camelWords(name):
    return " ".join(re.findall(r"[A-Z][a-z]+|[a-z]+|[0-9]+", name))


def _shortcutDescription(shortcut):
    description = shortcut.property("studioDescription")
    if description:
        return str(description)
    name = str(shortcut.objectName or "")
    if name.startswith("Studio"):
        name = name[len("Studio"):]
    for prefix in ("Draw", "Library", "Map"):
        if name.startswith(prefix) and len(name) > len(prefix):
            name = name[len(prefix):]
    if name.endswith("Shortcut"):
        name = name[: -len("Shortcut")]
    return _camelWords(name).capitalize() if name else ""


def _shortcutWorkspace(shortcut, shell):
    """The workspace key ("draw"...) of a workspace shortcut of the shell, None for a global one."""
    installed = getattr(shell, "_installedWorkspaceShortcuts", None) or []
    if shortcut in installed:
        return shell.currentWorkspace()
    name = str(shortcut.objectName or "")
    for key, prefix in (("draw", "StudioDraw"), ("library", "StudioLibrary"), ("map", "StudioMap")):
        if name.startswith(prefix):
            return key
    return None


def _addRow(rows, keys, description):
    """Add ``(keys, description)``; a row with the same description gets the other key ("F11 / Ctrl+6")."""
    keys = [key for key in keys if key]
    if not keys or not description:
        return
    for row in rows:
        if row[1] == description:
            for key in keys:
                if key not in row[0]:
                    row[0].append(key)
            return
    rows.append([list(keys), description])


def _mergedRows(*rowLists):
    """One list of rows: a description listed by several sources (a live key and a static row) is shown once."""
    rows = []
    for rowList in rowLists:
        for keys, description in rowList:
            _addRow(rows, keys, description)
    return rows


def _studioShortcutRows(shell):
    """{section: rows} of the live Studio QShortcuts and of the registered workspace shortcuts."""
    result = {SECTION_DRAW: [], SECTION_VIEWS: [], SECTION_APP: [], SECTION_LIBRARY: []}
    mainWindow = shell.mainWindow
    seen = set()
    for shortcut in mainWindow.findChildren("QShortcut"):
        if not shortcut.isEnabled():
            continue
        name = str(shortcut.objectName or "")
        if not shortcut.property("studioShortcut") and not name.startswith("Studio"):
            continue
        key = _portable(shortcut.key)
        description = _shortcutDescription(shortcut)
        if not key or not description:
            continue
        workspace = _shortcutWorkspace(shortcut, shell)
        if workspace == "draw":
            section = SECTION_DRAW
        elif workspace == "library":
            section = SECTION_LIBRARY
        elif _VIEW_KEY_PATTERN.match(key):
            section = SECTION_VIEWS
        else:
            section = SECTION_APP
        seen.add((workspace, key))
        _addRow(result[section], [key], description)
    # Workspace keys that are registered but not installed now (another workspace is shown)
    registered = getattr(shell, "_workspaceShortcuts", None) or {}
    for workspace, entries in registered.items():
        section = {"draw": SECTION_DRAW, "library": SECTION_LIBRARY}.get(workspace, SECTION_APP)
        for keySequence, callback, description in entries:
            key = _portable(qt.QKeySequence(keySequence))
            if (workspace, key) in seen or not description:
                continue
            _addRow(result[section], [key], description)
    return result


#: Clearer descriptions of some main window actions (by objectName)
_ACTION_DESCRIPTIONS = {
    "PreviousModuleAction": _("Previous module"),
    "NextModuleIcon": _("Next module"),
    "FileSaveSceneAction": _("Save the scene (Save Data)"),
    "FileCloseSceneAction": _("Close the scene"),
    "EditApplicationSettingsAction": _("Settings"),
    "HelpDocumentationAction": _("Documentation"),
    "ViewExtensionsManagerAction": _("Extensions Manager"),
}

#: Menus whose actions with a key are listed (the classic menus, shown in ≡ in Studio)
_MENUS = ("FileMenu", "EditMenu", "ViewMenu", "HelpMenu", "AppearanceMenu", "LayoutMenu", "WindowToolBarsMenu")


def _actionRows(shell, takenKeys):
    """Rows of the main window actions that have a key and are in the classic menus or attached to the window."""
    shellModule = _shellModule()
    associated = getattr(shellModule, "associatedWidgets", None)
    mainWindow = shell.mainWindow
    rows = []
    for action in mainWindow.findChildren("QAction"):
        if action.isSeparator() or not action.isEnabled():
            continue
        shortcuts = [_portable(sequence) for sequence in action.shortcuts()]
        shortcuts = [key for key in shortcuts if key and key not in takenKeys]
        if not shortcuts:
            continue
        owners = associated(action) if associated is not None else []
        if not any(owner == mainWindow or str(owner.objectName) in _MENUS for owner in owners):
            continue
        text = _ACTION_DESCRIPTIONS.get(str(action.objectName)) or str(action.text).replace("&", "").replace("…", "").strip()
        if text:
            _addRow(rows, shortcuts, text)
    return rows


def _effectRows():
    """Rows of the Segment Editor tools in their live order (1 to 0, Shift+1 to Shift+0)."""
    widget = getattr(slicer.modules, "SegmentEditorWidget", None)
    editor = getattr(widget, "editor", None) if widget is not None else None
    if editor is None:
        return []
    palette = sys.modules.get("StudioLib.palette")
    studioNames = getattr(palette, "TOOL_NAMES", {}) if palette is not None else {}
    rows = []
    for index in range(1, 21):
        effect = editor.effectByIndex(index)
        if effect is None:
            continue
        name = str(effect.name)
        title = studioNames.get(name, (None,))[0] or str(getattr(effect, "title", "") or name)
        if title.lower() != name.lower():
            title = f"{title} ({name})"
        key = str(index % 10) if index <= 10 else f"Shift+{index % 10}"
        rows.append([[key], title])
    return rows


def _bracketKeysOn():
    """Whether [ and ] resize the brush in Draw: the user's choice, else on (in Studio's Draw, the studio presentation of
    the Segment Editor; qSlicerSegmentEditorPaintEffect has the same default, and Classic keeps 3D Slicer's keys).
    """
    return slicer.util.settingsValue("Studio/BracketKeysResizeBrush", True, converter=slicer.util.toBool)


def _drawStaticRows():
    rows = [
        [["Esc"], _("Deselect the tool (it stays recorded)")],
        [["Space"], _("Pause or resume the tool; while paused, drag in a view to adjust window/level")],
        [["Ctrl+Z", "z"], _("Undo")],
        [["Ctrl+Y", "Ctrl+Shift+Z", "y"], _("Redo")],
        [["q", "w"], _("Previous / next segment (also , . / * < >)")],
        [["i"], _("Paint only where the scan is in the intensity range (toggle)")],
        [["Shift+wheel", "+ - = _"], _("Brush: size")],
    ]
    if _bracketKeysOn():
        rows.append([["[ ]"], _("Brush: smaller / larger")])
    rows += [
        [["a", "Return", "right-click"], _("Outline: apply")],
        [["x"], _("Outline: remove the last point")],
        [["drag ↔", "Shift+wheel", "+ - = _"], _("Smart fill: tolerance")],
        [["Shift+click"], _("Smart fill: extend (click without moving)")],
        [["Ctrl+click" if not _isMacOS() else "⌘+click"], _("Smart fill: remove (click without moving)")],
        [["Return", "Enter"], _("Smart fill: apply")],
        [["Backspace"], _("Smart fill: discard the preview")],
    ]
    return rows


def _viewRows():
    rows = [
        [["wheel", "↑ ↓", "f b"], _("Next / previous slice")],
        [["PageUp", "PageDown"], _("Several slices at once (Studio)")],
        [["Home", "End"], _("First / last slice")],
        [["Shift+move"], _("Move the crosshair: the other views follow")],
        [["Ctrl+wheel", "right-drag"], _("Zoom")],
        [["middle-drag", "Shift+drag"], _("Pan")],
        [["left-drag"], _("Window/level, when no tool is active (Studio)")],
        [["Ctrl+left-drag"], _("Blend the foreground and background")],
        [["double-click"], _("Maximize the view; Esc restores")],
        [["r"], _("Reset the view (fit)")],
        [["g", "t"], _("Toggle label / foreground opacity")],
        [["[ ]", "{ }"], _("Previous / next background / foreground volume")],
        [["v", "V"], _("Show this slice / all slices in 3D")],
    ]
    return rows


def _libraryRows():
    rows = [
        [["Ctrl+O"], _("Open scans, lesion masks, figures or scenes")],
        [["drop"], _("Drop files or a folder anywhere on the window to open them")],
    ]
    return rows


def _escapeRows():
    shellModule = _shellModule()
    ladder = getattr(shellModule, "ESCAPE_LADDER", None) or []
    rows = [[["Esc"], f"{state} → {action}"] for state, action in ladder]
    rows.append([["—"], _("Library is not a state: it is outside the ladder (use Continue in Draw)")])
    return rows


def _platformRows(shell):
    inspectorText = _("Inspector: {keys}").format(keys=shell.inspectorShortcutText(alternate=True)) if hasattr(shell, "inspectorShortcutText") else _("Inspector: Ctrl+\\ or Ctrl+5")
    return [
        [["F10"], _("≡ menu: F10; on a Mac also the menu bar, Ctrl+F2")],
        [["Ctrl+\\", "Ctrl+5"], inspectorText],
        [["AltGr+digit"], _("AltGr+digit acts as Ctrl+Alt+digit in a view")],
        [["—"], _("The views are radiological: the patient's left is on the right of the screen")],
    ]


def sections(shell):
    """The content of the overlay: ``[(section, title, rows)]`` with rows ``[[keys], description]`` (portable keys)."""
    studioRows = _studioShortcutRows(shell)
    takenKeys = {key for rows in studioRows.values() for row in rows for key in row[0]}
    draw = _mergedRows(studioRows[SECTION_DRAW], _effectRows(), _drawStaticRows())
    views = _mergedRows(studioRows[SECTION_VIEWS], _viewRows())
    app = _mergedRows(studioRows[SECTION_APP], _actionRows(shell, takenKeys))
    library = _mergedRows(studioRows[SECTION_LIBRARY], _libraryRows())
    return [
        (SECTION_DRAW, _("Draw"), draw),
        (SECTION_VIEWS, _("Views"), views),
        (SECTION_LIBRARY, _("Library"), library),
        (SECTION_APP, _("App"), app),
        (SECTION_ESCAPE, _("Esc, one step back per press"), _escapeRows()),
        (SECTION_PLATFORM, _("Platform notes"), _platformRows(shell)),
    ]


#: Columns of the overlay: sections per column, for 3, 2 and 1 columns (narrow windows)
COLUMNS = ((SECTION_DRAW,), (SECTION_VIEWS, SECTION_LIBRARY), (SECTION_APP, SECTION_ESCAPE, SECTION_PLATFORM))
_COLUMN_LAYOUTS = {
    3: COLUMNS,
    2: ((SECTION_DRAW, SECTION_LIBRARY), (SECTION_VIEWS, SECTION_APP, SECTION_ESCAPE, SECTION_PLATFORM)),
    1: ((SECTION_DRAW, SECTION_VIEWS, SECTION_LIBRARY, SECTION_APP, SECTION_ESCAPE, SECTION_PLATFORM),),
}


def columnCount(width):
    """Columns that fit in an overlay ``width`` px wide (3 at most)."""
    available = width - 2 * MARGIN
    return max(1, min(3, (available + COLUMN_SPACING) // (COLUMN_MIN_WIDTH + COLUMN_SPACING)))


def cardRect(width, height):
    """The rectangle of the card in an overlay ``width`` x ``height`` px: centred, at most CARD_MAX_WIDTH wide."""
    cardWidth = max(0, min(CARD_MAX_WIDTH, width - 2 * CARD_INSET_X))
    return qt.QRect((width - cardWidth) // 2, CARD_INSET_Y, cardWidth, max(0, height - 2 * CARD_INSET_Y))


class _Background(qt.QWidget):
    """The overlay over the view area: a black wash and the card (the chrome of Search: ``card`` fill, a 1 px border and
    14 px corners) that holds the list.
    """

    def __init__(self, parent):
        qt.QWidget.__init__(self, parent)
        self.setObjectName("StudioCheatSheet")
        self.setAttribute(qt.Qt.WA_NoSystemBackground)
        self.setAutoFillBackground(False)

    def paintEvent(self, event):
        painter = qt.QPainter(self)
        try:
            painter.fillRect(self.rect, qt.QColor(0, 0, 0, round(255 * BACKGROUND_ALPHA)))
            painter.setRenderHint(qt.QPainter.Antialiasing, True)
            painter.setPen(qt.QPen(ui.token("border"), 1))
            painter.setBrush(qt.QBrush(ui.token("card")))
            card = qt.QRectF(cardRect(self.width, self.height)).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.drawRoundedRect(card, CARD_RADIUS, CARD_RADIUS)
        finally:
            painter.end()

    def mousePressEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.accept()


class _Filter(qt.QObject):
    """Esc in the search field closes the overlay; the overlay follows the size of the view area."""

    def __init__(self, sheet):
        qt.QObject.__init__(self)
        self.sheet = sheet

    def eventFilter(self, obj, event):
        try:
            eventType = event.type()
            if eventType == qt.QEvent.KeyPress and obj == self.sheet.field and event.key() == qt.Qt.Key_Escape:
                self.sheet.close()
                return True
            if eventType == qt.QEvent.ShortcutOverride and obj == self.sheet.field and event.key() == qt.Qt.Key_Escape:
                # Esc belongs to the overlay, not to a window-wide shortcut (the Segment Editor's)
                event.accept()
                return True
            if eventType in (qt.QEvent.Resize, qt.QEvent.Move) and obj != self.sheet.field:
                self.sheet.updateGeometry()
        except Exception:
            logging.error(f"Studio: shortcut overlay event failed:\n{traceback.format_exc()}")
        return False


class CheatSheet:
    """The overlay (one per main window, rebuilt on each open)."""

    def __init__(self, shell):
        self.shell = shell
        self.mainWindow = shell.mainWindow
        self.widget = _Background(self.mainWindow)
        self.widget.hide()
        self.rowWidgets = []
        self.sectionWidgets = {}
        self.content = []
        self._previousFocus = None
        self.columnCount = 0
        outer = qt.QVBoxLayout(self.widget)
        outer.setContentsMargins(MARGIN, MARGIN - 8, MARGIN, MARGIN - 8)
        outer.setSpacing(12)
        self.outerLayout = outer

        header = qt.QHBoxLayout()
        header.setSpacing(HEADER_SPACING)
        title = qt.QLabel(_("Keyboard shortcuts"))
        title.setObjectName("StudioCheatSheetTitle")
        header.addWidget(title)
        hint = qt.QLabel(_("{key} or Esc closes this list").format(key=_native("Ctrl+/")))
        hint.setObjectName("StudioCheatSheetHint")
        header.addWidget(hint)
        header.addStretch(1)
        field = qt.QLineEdit()
        field.setObjectName("StudioCheatSheetSearch")
        field.setPlaceholderText(_("Search keys"))
        field.setClearButtonEnabled(True)
        # 240 px when there is room, narrower (down to 120) in a narrow window (USABILITY 9.4)
        field.setMinimumWidth(SEARCH_MIN_WIDTH)
        field.setMaximumWidth(SEARCH_WIDTH)
        field.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        header.addWidget(field)
        closeButton = qt.QToolButton()
        closeButton.setObjectName("StudioCheatSheetClose")
        closeButton.setAutoRaise(True)
        closeButton.setToolTip(ui.tooltip(_("Close"), "Esc", _("Close the shortcut list")))
        closeButton.setIcon(ui.icon("x", 16))
        closeButton.connect("clicked()", self.close)
        ui.installButtonKeys(closeButton)
        header.addWidget(closeButton)
        outer.addLayout(header)
        self.title = title
        self.hint = hint
        self.field = field
        self.closeButton = closeButton

        scrollArea = qt.QScrollArea()
        scrollArea.setObjectName("StudioCheatSheetScrollArea")
        scrollArea.setWidgetResizable(True)
        scrollArea.setFrameShape(qt.QFrame.NoFrame)
        scrollArea.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        outer.addWidget(scrollArea, 1)
        self.scrollArea = scrollArea

        self.emptyLabel = qt.QLabel()
        self.emptyLabel.setObjectName("StudioCheatSheetEmpty")
        self.emptyLabel.hide()
        outer.addWidget(self.emptyLabel)

        self._filter = _Filter(self)
        field.installEventFilter(self._filter)
        # The window and the view area are only filtered while the overlay is shown (see _watchWindow)
        self._centralWidget = None
        self._watching = False
        field.connect("textChanged(QString)", self._applyFilter)
        self._styled = None

    def _style(self):
        key = slicer.app.palette().cacheKey()
        if key == self._styled:
            return
        self._styled = key
        text = ui.token("text").name()
        text2 = ui.token("text2").name()
        text3 = ui.token("text3").name()
        border = ui.token("border").name()
        # The chrome of Search (VISUAL 4.6): title 18/600, overline group headers, key chips, a canvas search field
        self.widget.setStyleSheet(
            f"QLabel#StudioCheatSheetTitle {{ color: {text}; font-size: {ui.fontPx(18)}px; font-weight: 600; }}"
            f"QLabel#StudioCheatSheetHint, QLabel#StudioCheatSheetEmpty {{ color: {text3}; font-size: {ui.fontPx(12)}px; }}"
            f"QLabel#StudioCheatSheetSection {{ color: {text3}; font-size: {ui.fontPx(11)}px; font-weight: 600; letter-spacing: 0.4px; }}"
            f"QLabel#StudioCheatSheetDescription {{ color: {text2}; font-size: {ui.fontPx(13)}px; }}"
            f"QLabel#StudioCheatSheetKey {{ color: {text2}; background-color: {ui.token('card').name()}; border: 1px solid {border}; border-radius: 5px;"
            f" padding: 0px 5px; font-size: {ui.fontPx(11)}px; }}"
            f"QLineEdit#StudioCheatSheetSearch {{ background-color: {ui.token('canvas').name()}; color: {text}; border: 1px solid {border};"
            f" border-radius: 8px; padding: 4px 10px; font-size: {ui.fontPx(13)}px; }}"
            f"QLineEdit#StudioCheatSheetSearch:focus {{ border-color: {ui.token('accent-fg').name()}; }}"
            "QScrollArea#StudioCheatSheetScrollArea, QWidget#StudioCheatSheetBody { background: transparent; }")
        self.closeButton.setIcon(ui.icon("x", 16))

    def _build(self):
        self.content = sections(self.shell)
        body = qt.QWidget()
        body.setObjectName("StudioCheatSheetBody")
        columns = qt.QHBoxLayout(body)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(COLUMN_SPACING)
        self.rowWidgets = []
        self.sectionWidgets = {}
        bySection = {section: (title, rows) for section, title, rows in self.content}
        self.columnCount = columnCount(self._overlayWidth())
        for sectionNames in _COLUMN_LAYOUTS[self.columnCount]:
            column = qt.QVBoxLayout()
            column.setSpacing(4)
            for section in sectionNames:
                title, rows = bySection.get(section, ("", []))
                if not rows:
                    continue
                label = qt.QLabel(title.upper())
                label.setObjectName("StudioCheatSheetSection")
                label.setContentsMargins(0, 10, 0, 2)
                column.addWidget(label)
                sectionRows = []
                for keys, description in rows:
                    rowWidget = self._rowWidget(keys, description)
                    column.addWidget(rowWidget)
                    searchText = " ".join([description] + keys + [_native(key) for key in keys]).lower()
                    self.rowWidgets.append((rowWidget, searchText, section))
                    sectionRows.append(rowWidget)
                self.sectionWidgets[section] = (label, sectionRows)
            column.addStretch(1)
            container = qt.QWidget()
            container.setLayout(column)
            columns.addWidget(container, 1)
        old = self.scrollArea.takeWidget()
        if old is not None:
            old.deleteLater()
        self.scrollArea.setWidget(body)
        self.scrollArea.viewport().setAutoFillBackground(False)
        body.setAutoFillBackground(False)

    @staticmethod
    def _rowWidget(keys, description):
        row = qt.QWidget()
        row.setObjectName("StudioCheatSheetRow")
        layout = qt.QHBoxLayout(row)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(4)
        keysBox = qt.QWidget()
        keysLayout = qt.QVBoxLayout(keysBox)
        keysLayout.setContentsMargins(0, 0, 0, 0)
        keysLayout.setSpacing(KEY_SPACING)
        chipFont = qt.QFont(keysBox.font)
        chipFont.setPixelSize(ui.fontPx(11))
        metrics = qt.QFontMetrics(chipFont)
        line = None
        lineWidth = 0
        for key in keys:
            text = _native(key)
            # Text, padding (2 x 5 px), border (2 x 1 px) and the rounding of the style sheet font
            width = _advance(metrics, text) + 18
            if line is None or (lineWidth > 0 and lineWidth + KEY_SPACING + width > KEYS_COLUMN_WIDTH):
                line = qt.QHBoxLayout()
                line.setContentsMargins(0, 0, 0, 0)
                line.setSpacing(KEY_SPACING)
                keysLayout.addLayout(line)
                lineWidth = 0
            chip = qt.QLabel(text)
            chip.setObjectName("StudioCheatSheetKey")
            chip.setTextFormat(qt.Qt.PlainText)
            chip.setSizePolicy(qt.QSizePolicy.Fixed, qt.QSizePolicy.Fixed)
            line.addWidget(chip)
            lineWidth += width + KEY_SPACING
        for index in range(keysLayout.count()):
            keysLayout.itemAt(index).layout().addStretch(1)
        keysBox.setFixedWidth(KEYS_COLUMN_WIDTH)
        keysBox.setSizePolicy(qt.QSizePolicy.Fixed, qt.QSizePolicy.Maximum)
        layout.addWidget(keysBox, 0, qt.Qt.AlignTop)
        label = qt.QLabel(description)
        label.setObjectName("StudioCheatSheetDescription")
        label.setTextFormat(qt.Qt.PlainText)
        label.setWordWrap(True)
        layout.addWidget(label, 1, qt.Qt.AlignTop)
        row.setToolTip(ui.tooltip(description, " / ".join(_native(key) for key in keys) if len(keys) > 1 else (keys[0] if keys else None)))
        return row

    def _overlayWidth(self):
        """Width of the card (the columns are laid out in it)."""
        centralWidget = self.mainWindow.centralWidget()
        width = centralWidget.width if centralWidget is not None else self.mainWindow.width
        height = centralWidget.height if centralWidget is not None else self.mainWindow.height
        return cardRect(width, height).width()

    def _placeContent(self, width, height):
        """The content inside the card: its padding added to the card's inset."""
        card = cardRect(width, height)
        self.outerLayout.setContentsMargins(card.left() + MARGIN, card.top() + MARGIN - 8, width - card.right() - 1 + MARGIN,
                                            height - card.bottom() - 1 + MARGIN - 8)
        # The title is never cut off: in a card too narrow for the title, the hint, the search field and the close button,
        # the hint ("Ctrl+/ or Esc closes this list", also the tool tip of the close button) is hidden
        needed = (self.title.sizeHint.width() + self.hint.sizeHint.width() + SEARCH_MIN_WIDTH + self.closeButton.sizeHint.width()
                  + 3 * HEADER_SPACING)
        self.hint.setVisible(card.width() - 2 * MARGIN >= needed)

    def updateGeometry(self):
        if not _isAlive(self.widget):
            return
        if self.widget.isVisible() and self.content and columnCount(self._overlayWidth()) != self.columnCount:
            # The window became narrower or wider: lay the sections out in another number of columns
            text = self.field.text
            self._build()
            self._applyFilter(text)
        centralWidget = self.mainWindow.centralWidget()
        if centralWidget is None:
            self.widget.setGeometry(self.mainWindow.rect)
            self._placeContent(self.mainWindow.width, self.mainWindow.height)
            return
        topLeft = centralWidget.mapTo(self.mainWindow, qt.QPoint(0, 0))
        self.widget.setGeometry(topLeft.x(), topLeft.y(), centralWidget.width, centralWidget.height)
        self._placeContent(centralWidget.width, centralWidget.height)

    def isVisible(self):
        return _isAlive(self.widget) and self.widget.isVisible()

    def _watchWindow(self, watch):
        """Follow the size of the view area (resize and move events of the window) while the overlay is shown."""
        if watch == self._watching:
            return
        self._watching = watch
        if watch:
            self._centralWidget = self.mainWindow.centralWidget() if _isAlive(self.mainWindow) else None
        for obj in (self.mainWindow, self._centralWidget):
            if not _isAlive(obj):
                continue
            if watch:
                obj.installEventFilter(self._filter)
            else:
                obj.removeEventFilter(self._filter)
        if not watch:
            self._centralWidget = None

    def open(self):
        self._style()
        self._build()
        self.field.blockSignals(True)
        self.field.clear()
        self.field.blockSignals(False)
        self._applyFilter("")
        self._watchWindow(True)
        self.updateGeometry()
        self._previousFocus = slicer.app.focusWidget()
        self.widget.show()
        self.widget.raise_()
        self.shell.registerEscapeOverlay(self.widget, self.close)
        self.field.setFocus(qt.Qt.ShortcutFocusReason)

    def close(self):
        if not _isAlive(self.widget):
            return
        self.shell.unregisterEscapeOverlay(self.widget)
        self._watchWindow(False)
        if not self.widget.isVisible():
            return
        self.widget.hide()
        previous = self._previousFocus
        self._previousFocus = None
        if _isAlive(previous) and previous.isVisible():
            previous.setFocus(qt.Qt.OtherFocusReason)

    def _applyFilter(self, text):
        words = [word for word in str(text or "").lower().split() if word]
        shown = 0
        visibleSections = set()
        for rowWidget, searchText, section in self.rowWidgets:
            visible = all(word in searchText for word in words)
            rowWidget.setVisible(visible)
            if visible:
                shown += 1
                visibleSections.add(section)
        for section, (label, rows) in self.sectionWidgets.items():
            label.setVisible(section in visibleSections)
        self.emptyLabel.setText(_("No key matches “{text}”.").format(text=str(text).strip()) if words and not shown else "")
        self.emptyLabel.setVisible(bool(words) and not shown)

    def visibleDescriptions(self):
        """Descriptions of the rows shown now (tests)."""
        return [searchText for rowWidget, searchText, section in self.rowWidgets if not rowWidget.isHidden()]

    def dispose(self):
        if not _isAlive(self.widget):
            return
        self.close()
        self._watchWindow(False)
        self.field.removeEventFilter(self._filter)
        self.widget.deleteLater()
        self.widget = None


def instance():
    """The overlay of the live shell, or None."""
    if _sheet is None or not _isAlive(_sheet.widget) or not _sheet.shell.isActive():
        return None
    return _sheet


def isVisible():
    sheet = instance()
    return sheet is not None and sheet.isVisible()


def show(shell):
    """Show the overlay (built from the live keys). Return True if it is shown."""
    global _sheet
    if shell is None or not shell.isActive():
        return False
    if _sheet is not None and (_sheet.shell is not shell or not _isAlive(_sheet.widget)):
        _sheet.dispose()
        _sheet = None
    if _sheet is None:
        _sheet = CheatSheet(shell)
    _sheet.open()
    return True


def hide():
    if _sheet is not None:
        _sheet.close()


def toggle(shell):
    """Show the overlay, or hide it if it is shown (``Ctrl+/``)."""
    if isVisible():
        hide()
        return False
    return show(shell)


def dispose():
    global _sheet
    if _sheet is not None:
        _sheet.dispose()
        _sheet = None

"""Library workspace of Slicer Studio (SPEC 3.1): the start page.

- ``LibraryCanvas``: the calm page shown over the paused views while Library is the current workspace.
  It has a drop zone, the Open…, Open folder… and Start on MNI template buttons, the recently loaded
  files and links to the sample data and to the Classic interface.
- ``LibraryPanel``: the content of the Studio module panel (the inspector in Studio). It also works in
  Classic and without main window.
- Hooks ``enter(shell)`` and ``leave(shell)`` (imported lazily by ``StudioLib.workspaces``). While nothing
  is loaded (no study, no volume or segmentation), the inspector is hidden and the canvas is full width.

Files are loaded by ``StudioLib.importer``.
"""

import logging
import os
import time
import traceback

import qt
import slicer
from slicer.i18n import tr as _
from slicer.util import VTKObservationMixin

from . import ui

COLUMN_WIDTH = 480
DROP_ZONE_HEIGHT = 220
BUTTON_HEIGHT = 36
RECENT_ROW_COUNT = 6
RECENT_ROW_HEIGHT = 40


def _shell():
    from . import shell

    return shell.instance()


def _importer():
    from . import importer

    return importer


def _isAlive(qobject):
    """False if the C++ object was deleted."""
    try:
        qobject.objectName
    except Exception:
        return False
    return True


_eventFilters = []  # [(watched object, filter object)]
_eventFiltersRemovedOnQuit = False


def _installEventFilter(watched, eventFilter):
    """Install a Python event filter, removed when the application quits.

    Python event filters must not run while the Python interpreter is finalized: the widgets owned by Python
    objects are deleted then, and their destruction sends events (ChildRemoved) to the filters.
    """
    global _eventFiltersRemovedOnQuit
    _eventFilters[:] = [(obj, filterObject) for obj, filterObject in _eventFilters if _isAlive(obj) and _isAlive(filterObject)]
    watched.installEventFilter(eventFilter)
    _eventFilters.append((watched, eventFilter))
    if not _eventFiltersRemovedOnQuit:
        slicer.app.connect("aboutToQuit()", _removeEventFilters)
        _eventFiltersRemovedOnQuit = True


def _removeEventFilters():
    for watched, eventFilter in _eventFilters:
        if _isAlive(watched) and _isAlive(eventFilter):
            watched.removeEventFilter(eventFilter)
    _eventFilters.clear()


def _setFont(widget, pixelSize, weight=None, letterSpacing=None):
    font = qt.QFont(widget.font)
    font.setPixelSize(pixelSize)
    if weight is not None:
        font.setWeight(weight)
    if letterSpacing is not None:
        font.setLetterSpacing(qt.QFont.AbsoluteSpacing, letterSpacing)
    widget.setFont(font)


def _buttonRow():
    """A row of the height of the Library buttons (36 px).

    The height is set on the row rather than on the buttons: the fallback stylesheet of the button role
    (``ui.setRole``) sets their minimum height whenever they are polished again.
    """
    row = qt.QWidget()
    row.setFixedHeight(BUTTON_HEIGHT)
    layout = qt.QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    return row


def _secondaryButton(text, objectName, callback):
    """A secondary button that fills the height of its ``_buttonRow()``."""
    button = qt.QPushButton(text)
    button.setObjectName(objectName)
    button.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Expanding)
    ui.setRole(button, "secondary")
    button.connect("clicked()", callback)
    return button


def _runAction(function, errorMessage):
    """Run a user action; errors are logged and shown in a message box."""
    try:
        return function()
    except Exception as e:
        logging.error(f"Studio Library: {errorMessage}\n{traceback.format_exc()}")
        if not slicer.app.testingEnabled():
            slicer.util.errorDisplay(f"{errorMessage}\n\n{e}")
    return None


def openFiles():
    """Open… (the Studio direct-load dialog)."""
    _runAction(lambda: _importer().openFilesDialog(_shell()), _("Failed to open the files."))


def openFolder():
    """Open folder…"""
    _runAction(lambda: _importer().openFolderDialog(_shell()), _("Failed to open the folder."))


def startOnTemplate():
    """Start on MNI template."""
    _runAction(lambda: _importer().startOnTemplate(shell=_shell()), _("Failed to load the MNI template."))


def openSampleData():
    slicer.util.selectModule("SampleData")


def switchToClassic():
    shell = _shell()
    if shell is not None:
        shell.classic()


#
# Recently loaded files
#


def relativeTime(timestamp, now=None):
    """Short relative time of a timestamp (seconds since the epoch): "just now", "5 min ago", "yesterday"..."""
    if now is None:
        now = time.time()
    seconds = max(0.0, now - timestamp)
    if seconds < 60:
        return _("just now")
    minutes = int(seconds // 60)
    if minutes < 60:
        return _("{count} min ago").format(count=minutes)
    hours = int(minutes // 60)
    if hours < 24:
        return _("{count} h ago").format(count=hours)
    days = int(hours // 24)
    if days == 1:
        return _("yesterday")
    if days < 7:
        return _("{count} days ago").format(count=days)
    date = qt.QDateTime.fromSecsSinceEpoch(int(timestamp)).date()
    return qt.QLocale().toString(date, qt.QLocale.ShortFormat)


class RecentEntry:
    """A recently loaded file: ``properties`` are its load properties (``fileName``, ``fileType``...).

    ``action`` is the "RecentlyLoadedMenu" action that loads it (None without main window).
    """

    def __init__(self, properties, action=None):
        self.properties = dict(properties)
        self.action = action
        self.path = str(self.properties.get("fileName", ""))
        self.name = os.path.basename(self.path.rstrip("/\\")) or self.path
        self.folder = os.path.dirname(self.path)
        try:
            self.modifiedTime = os.path.getmtime(self.path)
        except OSError:
            self.modifiedTime = None

    def exists(self):
        return self.modifiedTime is not None

    def load(self, shell=None):
        action = self.action if self.action is not None and _isAlive(self.action) else None
        return _importer().loadRecentFile(self.properties, action, shell)

    def __repr__(self):
        return f"RecentEntry({self.path!r})"


def recentlyLoadedMenu():
    mainWindow = slicer.util.mainWindow()
    return mainWindow.findChild("QMenu", "RecentlyLoadedMenu") if mainWindow is not None else None


def recentEntries(limit=RECENT_ROW_COUNT):
    """The most recently loaded files that still exist, most recent first.

    They come from the "RecentlyLoadedMenu" actions of the main window (property ``fileParameters``),
    or from the settings that the main window writes when there is no main window.
    """
    entries = []
    menu = recentlyLoadedMenu()
    if menu is not None:
        for action in menu.actions():
            properties = action.property("fileParameters")
            if not properties:
                continue
            entries.append(RecentEntry(properties, action))
    else:
        settings = qt.QSettings()
        count = settings.beginReadArray("RecentlyLoadedFiles/RecentFiles")
        for index in range(count):
            settings.setArrayIndex(index)
            properties = settings.value("file")
            if not properties:
                continue
            properties = dict(properties)
            properties["fileName"] = slicer.app.toSlicerHomeAbsolutePath(str(properties.get("fileName", "")))
            entries.append(RecentEntry(properties))
        settings.endArray()
        # Stored oldest first
        entries.reverse()
    return [entry for entry in entries if entry.path and entry.exists()][:limit]


class _RecentMenuWatcher(qt.QObject):
    """Notifies listeners (once per event loop iteration) when the recently loaded files menu changes."""

    def __init__(self, menu):
        qt.QObject.__init__(self)
        self.menu = menu
        self.listeners = []
        self.timer = qt.QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(0)
        self.timer.connect("timeout()", self._notify)
        _installEventFilter(menu, self)

    def eventFilter(self, obj, event):
        if event.type() in (qt.QEvent.ActionAdded, qt.QEvent.ActionRemoved, qt.QEvent.ActionChanged):
            self.timer.start()
        return False

    def _notify(self):
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                logging.error(f"Studio Library: recent files listener failed:\n{traceback.format_exc()}")


_recentMenuWatcher = None


def _watchRecentMenu(listener):
    global _recentMenuWatcher
    menu = recentlyLoadedMenu()
    if menu is None:
        return
    if _recentMenuWatcher is None or not _isAlive(_recentMenuWatcher.menu):
        _recentMenuWatcher = _RecentMenuWatcher(menu)
    if listener not in _recentMenuWatcher.listeners:
        _recentMenuWatcher.listeners.append(listener)


def _unwatchRecentMenu(listener):
    if _recentMenuWatcher is not None and listener in _recentMenuWatcher.listeners:
        _recentMenuWatcher.listeners.remove(listener)


class RecentList(qt.QObject):
    """Rows of 40 px for the recently loaded files: name, folder (text2) and relative time.

    Enter, Space or a click on a row loads the file; Up and Down move between the rows.
    The list follows the "RecentlyLoadedMenu" of the main window while it is visible.
    """

    def __init__(self, rowCount=RECENT_ROW_COUNT):
        qt.QObject.__init__(self)
        self.rowCount = rowCount
        self.entries = []
        #: (button, name label, folder label, time label) of the rows created so far (created when needed)
        self.rows = []
        self._dirty = True
        self.widget = qt.QFrame()
        self.widget.setObjectName("StudioRecentList")
        self.rowsLayout = qt.QVBoxLayout(self.widget)
        self.rowsLayout.setContentsMargins(0, 0, 0, 0)
        self.rowsLayout.setSpacing(0)
        self.emptyLabel = qt.QLabel(_("Files you open appear here."))
        self.emptyLabel.setObjectName("StudioRecentEmpty")
        _setFont(self.emptyLabel, 12)
        self.emptyLabel.setContentsMargins(10, 8, 10, 8)
        self.rowsLayout.addWidget(self.emptyLabel)
        ui.bindStyleSheet(self.widget, self._styleSheet)
        _installEventFilter(self.widget, self)
        _watchRecentMenu(self._onRecentFilesChanged)

    def _createRow(self):
        index = len(self.rows)
        row = qt.QPushButton()
        row.setObjectName("StudioRecentRow")
        row.setFixedHeight(RECENT_ROW_HEIGHT)
        row.setFocusPolicy(qt.Qt.StrongFocus)
        # Enter and Return click the focused row (outside dialogs, only auto default buttons do)
        row.setAutoDefault(True)
        row.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        rowLayout = qt.QHBoxLayout(row)
        rowLayout.setContentsMargins(10, 0, 10, 0)
        rowLayout.setSpacing(10)
        nameLabel = qt.QLabel()
        nameLabel.setObjectName("StudioRecentName")
        _setFont(nameLabel, 13)
        folderLabel = qt.QLabel()
        folderLabel.setObjectName("StudioRecentFolder")
        _setFont(folderLabel, 12)
        folderLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        timeLabel = qt.QLabel()
        timeLabel.setObjectName("StudioRecentTime")
        _setFont(timeLabel, 12)
        timeLabel.setAlignment(qt.Qt.AlignRight | qt.Qt.AlignVCenter)
        for label in (nameLabel, folderLabel, timeLabel):
            label.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
        rowLayout.addWidget(nameLabel)
        rowLayout.addWidget(folderLabel, 1)
        rowLayout.addWidget(timeLabel)
        row.connect("clicked()", lambda index=index: self.activate(index))
        _installEventFilter(row, self)
        _installEventFilter(folderLabel, self)
        # Rows are above the empty state label
        self.rowsLayout.insertWidget(index, row)
        self.rows.append((row, nameLabel, folderLabel, timeLabel))

    @staticmethod
    def _styleSheet():
        return (
            "QPushButton#StudioRecentRow { background-color: transparent; border: none; border-radius: 6px; padding: 0px; }"
            f"QPushButton#StudioRecentRow:hover {{ background-color: {ui.token('control-hover').name()}; }}"
            f"QPushButton#StudioRecentRow:focus {{ background-color: {ui.token('accent-subtle').name()}; }}"
            f"QPushButton#StudioRecentRow:pressed {{ background-color: {ui.token('control-pressed').name()}; }}"
            f"QLabel#StudioRecentName {{ color: {ui.token('text').name()}; background: transparent; }}"
            f"QLabel#StudioRecentFolder {{ color: {ui.token('text2').name()}; background: transparent; }}"
            f"QLabel#StudioRecentTime {{ color: {ui.token('text3').name()}; background: transparent; }}"
            f"QLabel#StudioRecentEmpty {{ color: {ui.token('text3').name()}; background: transparent; }}")

    def _onRecentFilesChanged(self):
        self._dirty = True
        if _isAlive(self.widget) and self.widget.isVisible():
            self.refresh()

    def refresh(self):
        """Read the recently loaded files again."""
        self._dirty = False
        self.entries = recentEntries(self.rowCount)
        while len(self.rows) < len(self.entries):
            self._createRow()
        now = time.time()
        for index, (row, nameLabel, folderLabel, timeLabel) in enumerate(self.rows):
            if index >= len(self.entries):
                row.hide()
                continue
            entry = self.entries[index]
            nameLabel.setProperty("fullText", entry.name)
            folderLabel.setProperty("fullText", entry.folder)
            timeLabel.setText(relativeTime(entry.modifiedTime, now))
            row.setToolTip(entry.path)
            row.setAccessibleName(entry.name)
            row.show()
            self._elide(index)
        self.emptyLabel.setVisible(not self.entries)

    def _elide(self, index):
        row, nameLabel, folderLabel, timeLabel = self.rows[index]
        name = str(nameLabel.property("fullText") or "")
        nameMetrics = qt.QFontMetrics(nameLabel.font)
        # The name takes what it needs, up to 55 % of the row; the folder gets the rest
        available = max(0, row.width - 20 - 20 - qt.QFontMetrics(timeLabel.font).horizontalAdvance(timeLabel.text))
        nameLabel.setText(nameMetrics.elidedText(name, qt.Qt.ElideMiddle, int(available * 0.55)))
        folder = str(folderLabel.property("fullText") or "")
        folderLabel.setText(qt.QFontMetrics(folderLabel.font).elidedText(folder, qt.Qt.ElideMiddle, max(0, folderLabel.width)))

    def eventFilter(self, obj, event):
        eventType = event.type()
        if eventType == qt.QEvent.Show and obj == self.widget:
            if self._dirty:
                self.refresh()
        elif eventType == qt.QEvent.Resize and obj != self.widget:
            for index, (row, nameLabel, folderLabel, timeLabel) in enumerate(self.rows):
                if obj in (row, folderLabel) and index < len(self.entries):
                    self._elide(index)
        elif eventType == qt.QEvent.KeyPress and event.key() in (qt.Qt.Key_Up, qt.Qt.Key_Down):
            visibleRows = [row for row, *labels in self.rows if not row.isHidden()]
            if obj in visibleRows:
                index = visibleRows.index(obj) + (1 if event.key() == qt.Qt.Key_Down else -1)
                if 0 <= index < len(visibleRows):
                    visibleRows[index].setFocus(qt.Qt.TabFocusReason)
                return True
        return False

    def focusFirst(self):
        """Give the keyboard focus to the first row (Enter loads it). Return False if the list is empty."""
        if not self.entries:
            return False
        self.rows[0][0].setFocus(qt.Qt.OtherFocusReason)
        return True

    def firstRow(self):
        return self.rows[0][0] if self.entries else None

    def activate(self, index):
        """Load the file of a row."""
        if index >= len(self.entries):
            return None
        entry = self.entries[index]
        return _runAction(lambda: entry.load(_shell()), _("Failed to load {name}.").format(name=entry.name))

    def dispose(self):
        _unwatchRecentMenu(self._onRecentFilesChanged)
        if _isAlive(self.widget):
            ui.bindStyleSheet(self.widget, None)


#
# Folder card
#


class FolderCard:
    """Card offered for a folder with several NIfTI files: "N files found", Load all, Create study (coming)."""

    def __init__(self, folder, files, onClose=None):
        self.folder = folder
        self.files = list(files)
        self.onClose = onClose
        self.widget, layout = ui.card()
        self.widget.setObjectName("StudioFolderCard")
        headerRow = qt.QHBoxLayout()
        headerRow.setSpacing(8)
        self.iconLabel = qt.QLabel()
        self.iconLabel.setFixedSize(18, 18)
        headerRow.addWidget(self.iconLabel)
        title = qt.QLabel(_("{count} NIfTI files found").format(count=len(self.files)))
        title.setObjectName("StudioFolderCardTitle")
        _setFont(title, 13, qt.QFont.DemiBold)
        headerRow.addWidget(title, 1)
        self.closeButton = qt.QToolButton()
        self.closeButton.setObjectName("StudioFolderCardCloseButton")
        self.closeButton.setFixedSize(24, 24)
        self.closeButton.setAutoRaise(True)
        self.closeButton.setToolTip(_("Close"))
        ui.setRole(self.closeButton, "ghost")
        self.closeButton.connect("clicked()", self.close)
        headerRow.addWidget(self.closeButton)
        layout.addLayout(headerRow)

        self.folderLabel = qt.QLabel()
        self.folderLabel.setObjectName("StudioFolderCardFolder")
        _setFont(self.folderLabel, 12)
        self.folderLabel.setToolTip(folder)
        self.folderLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        self.folderLabel.setText(qt.QFontMetrics(self.folderLabel.font).elidedText(folder, qt.Qt.ElideMiddle, COLUMN_WIDTH - 48))
        ui.bindStyleSheet(self.folderLabel, lambda: f"QLabel#StudioFolderCardFolder {{ color: {ui.token('text2').name()}; }}")
        layout.addWidget(self.folderLabel)

        buttonRow = qt.QHBoxLayout()
        buttonRow.setSpacing(8)
        buttonRow.addStretch(1)
        self.createStudyButton = qt.QPushButton(_("Create study (coming)"))
        self.createStudyButton.setObjectName("StudioFolderCardCreateStudyButton")
        self.createStudyButton.setEnabled(False)
        self.createStudyButton.setToolTip(_("Studies (lesions of many subjects, one at a time in the scene) come in a later version."))
        ui.setRole(self.createStudyButton, "secondary")
        buttonRow.addWidget(self.createStudyButton)
        self.loadAllButton = qt.QPushButton(_("Load all"))
        self.loadAllButton.setObjectName("StudioFolderCardLoadAllButton")
        ui.setRole(self.loadAllButton, "primary")
        importer = _importer()
        if len(self.files) > importer.MAX_LOAD_ALL_FILES:
            # A cohort is never loaded into the scene at once (SPEC 10)
            self.loadAllButton.setEnabled(False)
            self.loadAllButton.setToolTip(_("More than {count} files: open the files of one subject with Open….").format(count=importer.MAX_LOAD_ALL_FILES))
        else:
            self.loadAllButton.setToolTip("\n".join(os.path.basename(path) for path in self.files))
        self.loadAllButton.connect("clicked()", self.loadAll)
        buttonRow.addWidget(self.loadAllButton)
        layout.addLayout(buttonRow)

        ui.addPaletteListener(self._refreshIcons)
        self._refreshIcons()

    def _refreshIcons(self):
        if not _isAlive(self.widget):
            return
        self.iconLabel.setPixmap(ui.pixmap("folder-open", 18, "accent-fg"))
        self.closeButton.setIcon(ui.icon("x", 14))

    def loadAll(self):
        files = self.files
        self.close()
        return _runAction(lambda: _importer().loadFiles(files, _shell()), _("Failed to load the files."))

    def close(self):
        ui.removePaletteListener(self._refreshIcons)
        if _isAlive(self.widget):
            ui.bindStyleSheet(self.folderLabel, None)
            self.widget.hide()
            self.widget.deleteLater()
        if self.onClose is not None:
            onClose = self.onClose
            self.onClose = None
            onClose(self)


class _FolderCardSlot:
    """Place of the folder card in a page (at most one card)."""

    def __init__(self):
        self.layout = qt.QVBoxLayout()
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.card = None

    def show(self, folder, files):
        self.close()
        self.card = FolderCard(folder, files, onClose=self._onClosed)
        self.layout.addWidget(self.card.widget)
        return self.card

    def close(self):
        if self.card is not None:
            self.card.close()

    def _onClosed(self, card):
        if card == self.card:
            self.card = None


#
# Canvas
#


class LibraryCanvas(qt.QObject):
    """The Library page shown over the views (SPEC 3.1, empty state)."""

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget = qt.QFrame()
        self.widget.setObjectName("StudioLibraryCanvas")
        outerLayout = qt.QVBoxLayout(self.widget)
        outerLayout.setContentsMargins(0, 0, 0, 0)
        # Scrolls only when the window is too small for the page
        self.scrollArea = qt.QScrollArea()
        self.scrollArea.setObjectName("StudioLibraryScroll")
        self.scrollArea.setFrameShape(qt.QFrame.NoFrame)
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        outerLayout.addWidget(self.scrollArea)
        content = qt.QWidget()
        content.setObjectName("StudioLibraryContent")
        contentLayout = qt.QVBoxLayout(content)
        contentLayout.setContentsMargins(24, 24, 24, 24)
        contentLayout.setSpacing(0)
        contentLayout.addStretch(2)
        columnRow = qt.QHBoxLayout()
        columnRow.addStretch(1)
        column = qt.QWidget()
        column.setObjectName("StudioLibraryColumn")
        column.setFixedWidth(COLUMN_WIDTH)
        columnRow.addWidget(column)
        columnRow.addStretch(1)
        contentLayout.addLayout(columnRow)
        contentLayout.addStretch(3)
        self.scrollArea.setWidget(content)

        columnLayout = qt.QVBoxLayout(column)
        columnLayout.setContentsMargins(0, 0, 0, 0)
        columnLayout.setSpacing(0)

        # Drop zone (the files are dropped on the main window, see StudioLib.importer)
        self.dropZone = qt.QFrame()
        self.dropZone.setObjectName("StudioDropZone")
        self.dropZone.setFixedSize(COLUMN_WIDTH, DROP_ZONE_HEIGHT)
        self.dropZone.setProperty("dragActive", False)
        self.dropZone.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        self.dropZone.setToolTip(_("Drop files here, or click to open files"))
        _installEventFilter(self.dropZone, self)
        dropZoneLayout = qt.QVBoxLayout(self.dropZone)
        dropZoneLayout.setContentsMargins(24, 24, 24, 24)
        dropZoneLayout.setSpacing(10)
        dropZoneLayout.addStretch(1)
        self.dropZoneIcon = qt.QLabel()
        self.dropZoneIcon.setObjectName("StudioDropZoneIcon")
        self.dropZoneIcon.setAlignment(qt.Qt.AlignCenter)
        dropZoneLayout.addWidget(self.dropZoneIcon)
        dropZoneTitle = qt.QLabel(_("Drop scans, lesion masks or a folder"))
        dropZoneTitle.setObjectName("StudioDropZoneTitle")
        dropZoneTitle.setAlignment(qt.Qt.AlignCenter)
        _setFont(dropZoneTitle, 15, qt.QFont.Medium)
        dropZoneLayout.addWidget(dropZoneTitle)
        openShortcut = qt.QKeySequence("Ctrl+O").toString(qt.QKeySequence.NativeText)
        dropZoneHint = qt.QLabel(_("NIfTI, NRRD or DICOM folders · or press {shortcut}").format(shortcut=openShortcut))
        dropZoneHint.setObjectName("StudioDropZoneHint")
        dropZoneHint.setAlignment(qt.Qt.AlignCenter)
        _setFont(dropZoneHint, 12)
        dropZoneLayout.addWidget(dropZoneHint)
        dropZoneLayout.addStretch(1)
        for label in (self.dropZoneIcon, dropZoneTitle, dropZoneHint):
            label.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
        columnLayout.addWidget(self.dropZone)
        columnLayout.addSpacing(16)

        # Open buttons
        buttonRow = _buttonRow()
        self.openButton = self._button(_("Open…"), "StudioLibraryOpenButton", openFiles,
                                       _("Open scans, lesion masks or scenes ({shortcut})").format(shortcut=openShortcut))
        self.openFolderButton = self._button(_("Open folder…"), "StudioLibraryOpenFolderButton", openFolder,
                                             _("Open a DICOM folder or a folder of NIfTI files"))
        self.templateButton = self._button(_("Start on MNI template"), "StudioLibraryStartOnTemplateButton", startOnTemplate,
                                           _("Draw a lesion on the MNI152NLin2009cAsym template (works offline)"))
        for button in (self.openButton, self.openFolderButton, self.templateButton):
            buttonRow.layout().addWidget(button)
        columnLayout.addWidget(buttonRow)

        # Folder card (several NIfTI files dropped)
        self.folderCardSlot = _FolderCardSlot()
        self.folderCardSpacing = qt.QWidget()
        self.folderCardSpacing.setFixedHeight(16)
        self.folderCardSpacing.hide()
        columnLayout.addWidget(self.folderCardSpacing)
        columnLayout.addLayout(self.folderCardSlot.layout)
        columnLayout.addSpacing(28)

        # Recent
        recentTitle = qt.QLabel(_("Recent").upper())
        recentTitle.setObjectName("StudioLibrarySectionTitle")
        recentTitle.setContentsMargins(10, 0, 10, 0)
        _setFont(recentTitle, 11, qt.QFont.DemiBold, 0.6)
        columnLayout.addWidget(recentTitle)
        columnLayout.addSpacing(6)
        self.recentList = RecentList()
        columnLayout.addWidget(self.recentList.widget)
        columnLayout.addSpacing(20)

        # Footer links
        linkRow = qt.QHBoxLayout()
        linkRow.setSpacing(4)
        linkRow.addStretch(1)
        self.sampleDataLink = self._link(_("Load sample data"), "StudioLibrarySampleDataLink", openSampleData)
        linkRow.addWidget(self.sampleDataLink)
        separator = qt.QLabel("·")
        separator.setObjectName("StudioLibraryLinkSeparator")
        linkRow.addWidget(separator)
        self.classicLink = self._link(_("Classic interface"), "StudioLibraryClassicLink", switchToClassic)
        self.classicLink.setToolTip(_("Switch to the Classic interface (Ctrl+Alt+0)"))
        linkRow.addWidget(self.classicLink)
        linkRow.addStretch(1)
        columnLayout.addLayout(linkRow)

        ui.bindStyleSheet(self.widget, self._styleSheet)
        ui.addPaletteListener(self._refreshIcons)
        self._refreshIcons()

    @staticmethod
    def _button(text, objectName, callback, toolTip):
        button = _secondaryButton(text, objectName, callback)
        button.setAutoDefault(True)
        button.setToolTip(toolTip)
        return button

    @staticmethod
    def _link(text, objectName, callback):
        link = qt.QPushButton(text)
        link.setObjectName(objectName)
        link.setProperty("studioLibraryLink", True)
        link.setFlat(True)
        link.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        _setFont(link, 12)
        link.connect("clicked()", callback)
        return link

    @staticmethod
    def _styleSheet():
        t = {name: ui.token(name).name() for name in ("canvas", "text", "text2", "text3", "border", "accent-fg", "accent-subtle")}
        return (
            f"QFrame#StudioLibraryCanvas {{ background-color: {t['canvas']}; border: none; }}"
            "QScrollArea#StudioLibraryScroll, QWidget#StudioLibraryContent, QWidget#StudioLibraryColumn { background: transparent; border: none; }"
            "QScrollArea#StudioLibraryScroll > QWidget#qt_scrollarea_viewport { background: transparent; }"
            f"QFrame#StudioDropZone {{ background-color: transparent; border: 1.5px dashed {t['border']}; border-radius: 12px; }}"
            f"QFrame#StudioDropZone:hover {{ border-color: {t['text3']}; }}"
            f'QFrame#StudioDropZone[dragActive="true"] {{ background-color: {t["accent-subtle"]}; border-color: {t["accent-fg"]}; }}'
            f"QLabel#StudioDropZoneTitle {{ color: {t['text']}; background: transparent; border: none; }}"
            f"QLabel#StudioDropZoneHint {{ color: {t['text2']}; background: transparent; border: none; }}"
            "QLabel#StudioDropZoneIcon { background: transparent; border: none; }"
            f"QLabel#StudioLibrarySectionTitle {{ color: {t['text2']}; }}"
            f"QLabel#StudioLibraryLinkSeparator {{ color: {t['text3']}; }}"
            f"QPushButton[studioLibraryLink=\"true\"] {{ background: transparent; border: none; color: {t['accent-fg']}; padding: 2px 4px; }}"
            "QPushButton[studioLibraryLink=\"true\"]:hover { text-decoration: underline; }")

    def _refreshIcons(self):
        if not _isAlive(self.widget):
            return
        dragActive = bool(self.dropZone.property("dragActive"))
        self.dropZoneIcon.setPixmap(ui.pixmap("import", 32, "accent-fg" if dragActive else "text2"))
        for button, iconName in ((self.openButton, "import"), (self.openFolderButton, "folder-open"), (self.templateButton, "brain")):
            button.setIcon(ui.icon(iconName, 16))

    def eventFilter(self, obj, event):
        # A click on the drop zone opens files
        if obj == self.dropZone and event.type() in (qt.QEvent.MouseButtonPress, qt.QEvent.MouseButtonRelease):
            if event.button() != qt.Qt.LeftButton:
                return False
            if event.type() == qt.QEvent.MouseButtonRelease and self.dropZone.rect.contains(event.pos()):
                openFiles()
            return True
        return False

    def refresh(self):
        self.recentList.refresh()

    def focusFirst(self):
        """Focus the first recent file (Enter loads it), else the Open… button."""
        if not self.recentList.focusFirst():
            self.openButton.setFocus(qt.Qt.OtherFocusReason)

    def setDropHighlight(self, active):
        if bool(self.dropZone.property("dragActive")) == bool(active):
            return
        self.dropZone.setProperty("dragActive", bool(active))
        style = self.dropZone.style()
        style.unpolish(self.dropZone)
        style.polish(self.dropZone)
        self.dropZone.update()
        self._refreshIcons()

    def showFolderCard(self, folder, files):
        card = self.folderCardSlot.show(folder, files)
        self.folderCardSpacing.show()
        previousOnClose = card.onClose

        def onClose(closedCard):
            previousOnClose(closedCard)
            if self.folderCardSlot.card is None and _isAlive(self.folderCardSpacing):
                self.folderCardSpacing.hide()

        card.onClose = onClose
        card.loadAllButton.setFocus(qt.Qt.OtherFocusReason)
        return card

    def folderCard(self):
        return self.folderCardSlot.card

    def dispose(self):
        ui.removePaletteListener(self._refreshIcons)
        self.folderCardSlot.close()
        self.recentList.dispose()
        if _isAlive(self.widget):
            ui.bindStyleSheet(self.widget, None)
            self.widget.hide()
            self.widget.deleteLater()


#
# Module panel
#

_panels = []


class LibraryPanel(qt.QObject):
    """Content of the Studio module panel: Open buttons and the recently loaded files.

    It works in Studio (inspector), in Classic and without main window. The content is built when the panel
    is shown for the first time, except while Studio enters the Library empty state, which hides the inspector
    (at startup on a fresh profile, for example).
    """

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioLibraryPanel")
        self.openButton = None
        self.openFolderButton = None
        self.templateButton = None
        self.folderCardSlot = None
        self.recentList = None
        _installEventFilter(self.widget, self)
        _panels.append(self)

    def isBuilt(self):
        return self.recentList is not None

    def build(self):
        """Create the content (done when the panel is shown for the first time)."""
        if self.isBuilt():
            return
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        openCard, openLayout = ui.card(_("Open"))
        openCard.setObjectName("StudioLibraryOpenCard")
        self.openButton = _secondaryButton(_("Open…"), "StudioOpenButton", openFiles)
        self.openFolderButton = _secondaryButton(_("Open folder…"), "StudioOpenFolderButton", openFolder)
        self.templateButton = _secondaryButton(_("Start on MNI template"), "StudioStartOnTemplateButton", startOnTemplate)
        for button in (self.openButton, self.openFolderButton, self.templateButton):
            row = _buttonRow()
            row.layout().addWidget(button)
            openLayout.addWidget(row)
        layout.addWidget(openCard)

        self.folderCardSlot = _FolderCardSlot()
        layout.addLayout(self.folderCardSlot.layout)

        recentCard, recentLayout = ui.card(_("Recent"))
        recentCard.setObjectName("StudioLibraryRecentCard")
        recentLayout.setContentsMargins(4, 12, 4, 4)
        self.recentList = RecentList()
        recentLayout.addWidget(self.recentList.widget)
        layout.addWidget(recentCard)

        ui.addPaletteListener(self._refreshIcons)
        self._refreshIcons()
        # Built: the Show events are not needed anymore
        self.widget.removeEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Show and obj == self.widget and not self.isBuilt() and not _inspectorHiddenOnEnter():
            self.build()
        return False

    def _refreshIcons(self):
        if not _isAlive(self.widget):
            ui.removePaletteListener(self._refreshIcons)
            return
        for button, iconName in ((self.openButton, "import"), (self.openFolderButton, "folder-open"), (self.templateButton, "brain")):
            button.setIcon(ui.icon(iconName, 16))

    def showFolderCard(self, folder, files):
        self.build()
        return self.folderCardSlot.show(folder, files)


def _livePanel():
    for panel in list(_panels):
        if not _isAlive(panel.widget):
            _panels.remove(panel)
            if panel.recentList is not None:
                panel.recentList.dispose()
            continue
        return panel
    return None


#
# Workspace hooks
#

_canvas = None
_sceneWatcher = None


def canvas():
    """The Library canvas (created on first use)."""
    global _canvas
    if _canvas is None or not _isAlive(_canvas.widget):
        _canvas = LibraryCanvas()
    return _canvas


def hasData():
    """True if the scene has a volume or a segmentation that the user can see."""
    for className in ("vtkMRMLScalarVolumeNode", "vtkMRMLSegmentationNode"):
        for node in slicer.util.getNodesByClass(className):
            if not node.GetHideFromEditors():
                return True
    return False


def isEmptyState(shell=None):
    """True when the Library shows its empty state: no study and no data loaded (SPEC 2.5)."""
    if shell is not None and getattr(shell, "studySession", None) is not None:
        return False
    return not hasData()


def _inspectorHiddenOnEnter():
    """True while Studio switches to the Library empty state: ``enter()`` is about to hide the inspector."""
    shell = _shell()
    workspaces = getattr(shell, "workspaces", None) if shell is not None else None
    return workspaces is not None and workspaces.isSwitching() and isEmptyState(shell)


class _SceneWatcher(VTKObservationMixin):
    """While Library is shown: show the inspector when data is loaded, hide it when the scene is emptied."""

    def __init__(self, shell):
        VTKObservationMixin.__init__(self)
        self.shell = shell
        self.empty = isEmptyState(shell)
        self.timer = qt.QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(0)
        self.timer.connect("timeout()", self.update)
        scene = slicer.mrmlScene
        for event in (scene.NodeAddedEvent, scene.NodeRemovedEvent, scene.EndCloseEvent, scene.EndImportEvent):
            self.addObserver(scene, event, self._onSceneChanged)

    def _onSceneChanged(self, caller, event):
        self.timer.start()

    def update(self):
        if self.shell.currentWorkspace() != "library":
            return
        empty = isEmptyState(self.shell)
        if empty != self.empty:
            self.empty = empty
            self.shell.setInspectorVisible(not empty)

    def dispose(self):
        self.timer.stop()
        self.removeObservers()


def enter(shell):
    """Library becomes the current workspace: show the canvas (the views are paused) and focus Recent."""
    global _sceneWatcher
    libraryCanvas = canvas()
    shell.setInspectorVisible(not isEmptyState(shell))
    shell.setCanvas(libraryCanvas.widget)
    libraryCanvas.refresh()
    libraryCanvas.focusFirst()
    if _sceneWatcher is not None:
        _sceneWatcher.dispose()
    _sceneWatcher = _SceneWatcher(shell)


def leave(shell):
    """Library is left: hide the canvas (rendering resumes) and show the inspector."""
    global _sceneWatcher
    if _sceneWatcher is not None:
        _sceneWatcher.dispose()
        _sceneWatcher = None
    shell.setCanvas(None)
    shell.setInspectorVisible(True)


def setDropHighlight(active):
    """Highlight the drop zone while files are dragged over the main window."""
    if _canvas is not None and _isAlive(_canvas.widget):
        _canvas.setDropHighlight(active)


def showFolderCard(folder, files, shell=None):
    """Offer the NIfTI files of a folder in a card: on the canvas in Studio, else in the module panel.

    Return the card, or None if no page can show it.
    """
    if shell is not None:
        if shell.currentWorkspace() != "library":
            shell.switchTo("library")
        if shell.currentWorkspace() == "library":
            return canvas().showFolderCard(folder, files)
    panel = _livePanel()
    if panel is not None:
        return panel.showFolderCard(folder, files)
    return None

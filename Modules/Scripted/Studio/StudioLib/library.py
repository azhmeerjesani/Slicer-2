"""Library workspace of Slicer Studio (SPEC 3.1): the start page, and the place that answers "what is open".

- ``LibraryCanvas``: the calm page shown over the paused views while Library is the current workspace.
  It has a drop zone, the Open…, Open folder… and Start on MNI template buttons, the recently loaded
  files and links to the sample data and to the Classic interface. Once data is loaded (``hasData()``) the
  drop zone becomes a strip and the page shows the current work: the ``CurrentWorkCard`` (scans, lesions with their
  metrics and save state, Continue in Draw) and, for lesions loaded at once, the ``LoadedSetCard``.
- ``LibraryPanel``: the content of the Studio module panel (the inspector in Studio). It also works in
  Classic and without main window. With the canvas it lists what is open (``OpenNowCard``); without it
  (Classic) it keeps the Open buttons and Recent.
- Hooks ``enter(shell)`` and ``leave(shell)`` (imported lazily by ``StudioLib.workspaces``). While nothing
  is loaded (no study, no volume or segmentation), the inspector is hidden and the canvas is full width.
- ``setFooterLine()`` / ``clearFooterLine()``: one text line at the bottom of the canvas column (the
  research-use line of the first run).

"Close" is the only removal verb of Studio (Close scene, Close set, a row's Close); "Delete" stays the word of
the Data module. Files are loaded by ``StudioLib.importer``.
"""

import html
import logging
import os
import time
import traceback

import ctk
import qt
import slicer
import vtk
from slicer.i18n import tr as _
from slicer.util import VTKObservationMixin

from . import space, ui

#: The multiplication sign of "grid 91 x 109 x 91" (U+00D7)
TIMES = "\u00d7"
COLUMN_WIDTH = 480
DROP_ZONE_HEIGHT = 220
BUTTON_HEIGHT = 36
RECENT_ROW_COUNT = 6
RECENT_ROW_HEIGHT = 40
#: The last characters of a file name that Recent always keeps visible
RECENT_NAME_KEEP_CHARS = 24


def _shell():
    from . import shell

    return shell.instance()


def _importer():
    from . import importer

    return importer


def _eventPosition(event):
    """Position of a mouse event in its widget: ``position()`` in Qt 6 (``pos()`` is deprecated there), ``pos()`` in Qt 5."""
    return event.position().toPoint() if hasattr(event, "position") else event.pos()


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
    """Set the font size (pixels designed for a 13 px body, scaled with the application font: ``ui.fontPx``)."""
    font = qt.QFont(widget.font)
    font.setPixelSize(ui.fontPx(pixelSize))
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


def _controlBorder():
    """Color name of the border of drop zones and secondary buttons: ``control-border`` (3:1 on the canvas), else ``border``."""
    try:
        return ui.token("control-border").name()
    except ValueError:
        return ui.token("border").name()


def _exec(qobject):
    """``exec()`` of a menu or a dialog (``exec_`` in older PythonQt builds)."""
    function = getattr(qobject, "exec", None) or qobject.exec_
    return function()


def _execMenu(menu, point):
    """Open a context menu at a global ``point`` and return the chosen action (None if dismissed)."""
    function = getattr(menu, "exec", None) or menu.exec_
    return function(point)


def _discard(widget):
    """Delete a widget that Library built, once the current event is processed (it may be the button being clicked).

    Its stylesheet bindings are removed and the Python callbacks of its buttons and actions are disconnected, and it keeps
    its parent until it is deleted: ``ui.bindStyleSheet()`` holds the Python wrapper of a widget, and a widget without a
    parent that Python still holds would be deleted at interpreter exit, when its Python event filters can no longer run.
    """
    if widget is None or not _isAlive(widget):
        return
    for target in (widget, *widget.findChildren("QWidget")):
        ui.bindStyleSheet(target, None)
    for button in widget.findChildren("QAbstractButton"):
        button.disconnect("clicked()")
    for action in widget.findChildren("QAction"):
        action.disconnect("triggered()")
    widget.hide()
    widget.deleteLater()


def _clearLayout(layout):
    """Remove (and discard) every widget of a box layout; nested layouts are cleared as well."""
    while layout.count():
        item = layout.takeAt(0)
        child = item.widget()
        if child is not None:
            _discard(child)
        elif item.layout() is not None:
            _clearLayout(item.layout())


class _Elider(qt.QObject):
    """Keeps the text of labels elided to their width.

    A label with an Ignored horizontal size policy never widens its container; its full text is the ``fullText`` property
    and its elide mode the ``elideMode`` property ("middle" or "left"; with "left" the last ``keepTail`` characters stay
    visible, so that the entities at the end of a file name remain readable).
    """

    def eventFilter(self, obj, event):
        if event.type() in (qt.QEvent.Resize, qt.QEvent.Show):
            self.apply(obj)
        return False

    @staticmethod
    def apply(label):
        if not _isAlive(label):
            return
        text = str(label.property("fullText") or "")
        width = label.width
        if width <= 8:
            # Not laid out yet: the Resize event comes
            label.setText(text)
            return
        metrics = qt.QFontMetrics(label.font)
        if label.property("elideMode") == "left":
            keepTail = int(label.property("keepTail") or 0)
            width = max(width, metrics.horizontalAdvance(text[-keepTail:]) if keepTail else 0)
            label.setText(metrics.elidedText(text, qt.Qt.ElideLeft, width))
        else:
            label.setText(metrics.elidedText(text, qt.Qt.ElideMiddle, width))


_elider = None


def _elidedLabel(text="", pixelSize=12, objectName=None, mode="middle", keepTail=0):
    """A plain-text label whose text is elided to its width (``setElidedText()``); the full text is its tool tip."""
    global _elider
    if _elider is None:
        _elider = _Elider()
    label = qt.QLabel()
    if objectName:
        label.setObjectName(objectName)
    label.setTextFormat(qt.Qt.PlainText)
    label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    _setFont(label, pixelSize)
    label.setProperty("elideMode", mode)
    label.setProperty("keepTail", int(keepTail))
    _installEventFilter(label, _elider)
    setElidedText(label, text)
    return label


def setElidedText(label, text, toolTip=None):
    """Set the full text of a label made by ``_elidedLabel()`` (elided to its width, whole text in the tool tip)."""
    text = str(text or "")
    label.setProperty("fullText", text)
    label.setToolTip(text if toolTip is None else toolTip)
    _Elider.apply(label)


def _linkButton(text, objectName, callback):
    """A flat button that looks like a link (``studioLibraryLink``, styled by the canvas stylesheet)."""
    link = qt.QPushButton(text)
    link.setObjectName(objectName)
    link.setProperty("studioLibraryLink", True)
    link.setFlat(True)
    link.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
    _setFont(link, 12)
    link.connect("clicked()", callback)
    return link


def _roleButton(text, objectName, callback, role):
    """A button of the role ``primary`` or ``secondary`` that fills the height of its ``_buttonRow()``."""
    button = qt.QPushButton(text)
    button.setObjectName(objectName)
    button.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Expanding)
    ui.setRole(button, role)
    button.setAutoDefault(True)
    button.connect("clicked()", callback)
    return button


def _confirmClose(text, informativeText=""):
    """Ask before something with unsaved changes is closed. Return True to go on (Close), False to keep it."""
    box = ctk.ctkMessageBox(slicer.util.mainWindow())
    box.setWindowTitle(_("Close"))
    box.setText(text)
    if informativeText:
        box.setInformativeText(informativeText)
    closeButton = box.addButton(_("Close"), qt.QMessageBox.AcceptRole)
    box.addButton(qt.QMessageBox.Cancel)
    box.setIcon(qt.QMessageBox.Question)
    box.setDefaultButton(qt.QMessageBox.Cancel)
    _exec(box)
    return box.clickedButton() == closeButton


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

    ``action`` is the "RecentlyLoadedMenu" action that loads it (None without main window). ``openedTime`` is when it was
    opened in Studio (``Studio/RecentOpenTimes``), None when it was not; ``modifiedTime`` is its modification time.
    """

    kind = "file"

    def __init__(self, properties, action=None, openedTimes=None):
        self.properties = dict(properties)
        self.action = action
        self.path = str(self.properties.get("fileName", ""))
        self.name = os.path.basename(self.path.rstrip("/\\")) or self.path
        self.folder = os.path.dirname(self.path)
        try:
            self.modifiedTime = os.path.getmtime(self.path)
        except OSError:
            self.modifiedTime = None
        openedTimes = openedTimes if openedTimes is not None else {}
        self.openedTime = openedTimes.get(os.path.abspath(self.path)) if self.path else None

    def exists(self):
        return self.modifiedTime is not None

    def timeText(self, now=None):
        """"opened 2 min ago" (opened through Studio), else "modified 18 Oct" (the modification time of the file)."""
        if self.openedTime is not None:
            return _("opened {when}").format(when=relativeTime(self.openedTime, now))
        return _("modified {when}").format(when=relativeTime(self.modifiedTime, now))

    def load(self, shell=None):
        action = self.action if self.action is not None and _isAlive(self.action) else None
        return _importer().loadRecentFile(self.properties, action, shell)

    def __repr__(self):
        return f"RecentEntry({self.path!r})"


class RecentFolderEntry:
    """A folder opened in Studio (``Studio/RecentFolders``): "cohort · 20 NIfTI files"; it opens the folder card again."""

    kind = "folder"

    def __init__(self, record):
        self.path = str(record.get("path", ""))
        self.name = os.path.basename(self.path.rstrip("/\\")) or self.path
        self.count = int(record.get("count") or 0)
        self.recordKind = str(record.get("kind", ""))
        self.openedTime = record.get("time")
        self.properties = {}
        self.action = None
        if self.recordKind == "dicom":
            self.folder = _("DICOM folder")
        else:
            self.folder = _("{count} NIfTI files").format(count=self.count)

    def exists(self):
        return os.path.isdir(self.path)

    def timeText(self, now=None):
        return _("opened {when}").format(when=relativeTime(self.openedTime, now)) if self.openedTime else ""

    def load(self, shell=None):
        return _importer().loadFolder(self.path, shell)

    def __repr__(self):
        return f"RecentFolderEntry({self.path!r})"


def recentlyLoadedMenu():
    # The shell's main window when Studio is shown: slicer.util.mainWindow() looks it up among all top-level widgets
    shell = _shell()
    mainWindow = getattr(shell, "mainWindow", None) if shell is not None else None
    if mainWindow is None:
        mainWindow = slicer.util.mainWindow()
    return mainWindow.findChild("QMenu", "RecentlyLoadedMenu") if mainWindow is not None else None


RECENT_FOLDER_ROW_COUNT = 3


def recentFolderEntries(limit=RECENT_FOLDER_ROW_COUNT):
    """The folders opened in Studio that still exist, most recent first (``importer.recentFolders()``)."""
    entries = []
    for record in _importer().recentFolders():
        entry = RecentFolderEntry(record)
        if entry.exists():
            entries.append(entry)
            if len(entries) >= limit:
                break
    return entries


def recentEntries(limit=RECENT_ROW_COUNT):
    """The most recently loaded files that still exist, most recent first.

    They come from the "RecentlyLoadedMenu" actions of the main window (property ``fileParameters``),
    or from the settings that the main window writes when there is no main window. Files are examined
    (``os.stat``) only until ``limit`` existing files are found. Files in the cache or temporary folders (the templates and
    the sample data that Studio downloaded) are not the user's files and never listed (``isInternalPath()``).
    """
    candidates = []  # [(properties, action)], most recent first
    menu = recentlyLoadedMenu()
    if menu is not None:
        for action in menu.actions():
            properties = action.property("fileParameters")
            if properties:
                candidates.append((properties, action))
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
            candidates.append((properties, None))
        settings.endArray()
        # Stored oldest first
        candidates.reverse()
    openedTimes = _importer().recentOpenTimes()
    internal = _internalFolders()
    entries = []
    for properties, action in candidates:
        if len(entries) >= limit:
            break
        path = str(properties.get("fileName", ""))
        if not path or isInternalPath(path, internal):
            continue
        entry = RecentEntry(properties, action, openedTimes)
        if entry.exists():
            entries.append(entry)
    return entries


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
        self._shownKey = None
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
        # File names are never rich text (a name may start with "<")
        for label in (nameLabel, folderLabel):
            label.setTextFormat(qt.Qt.PlainText)
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

    @staticmethod
    def _settingsKey():
        """What the rows show besides the menu: the times and the folders that Studio recorded."""
        settings = qt.QSettings()
        return (str(settings.value("Studio/RecentOpenTimes") or ""), str(settings.value("Studio/RecentFolders") or ""))

    def refresh(self, force=True):
        """Read the recently loaded files again: the folders opened in Studio first, then the files.

        Without ``force`` (the page is shown again) nothing is read when the menu and the recorded times and folders did not change.
        """
        key = self._settingsKey()
        # An empty list is not read again either (a new recent file changes the menu, which sets _dirty)
        if not force and not self._dirty and key == self._shownKey:
            return
        self._shownKey = key
        self._dirty = False
        folders = recentFolderEntries(min(RECENT_FOLDER_ROW_COUNT, self.rowCount))
        self.entries = folders + recentEntries(self.rowCount - len(folders))
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
            timeLabel.setText(entry.timeText(now))
            row.setToolTip(entry.path)
            row.setAccessibleName(entry.name)
            row.setProperty("recentKind", entry.kind)
            row.show()
            self._elide(index)
        self.emptyLabel.setVisible(not self.entries)

    def _elide(self, index):
        row, nameLabel, folderLabel, timeLabel = self.rows[index]
        name = str(nameLabel.property("fullText") or "")
        nameMetrics = qt.QFontMetrics(nameLabel.font)
        # The name takes what it needs, up to 55 % of the row; the folder gets the rest. A file name is elided at its left
        # only, and its last characters stay: the entities at the end (desc-lesion, T1w, AvgR) are what tells files apart.
        available = max(0, row.width - 20 - 20 - qt.QFontMetrics(timeLabel.font).horizontalAdvance(timeLabel.text))
        nameWidth = max(int(available * 0.55), nameMetrics.horizontalAdvance("…" + name[-RECENT_NAME_KEEP_CHARS:]) + 6)
        nameLabel.setText(nameMetrics.elidedText(name, qt.Qt.ElideLeft, nameWidth))
        folder = str(folderLabel.property("fullText") or "")
        folderLabel.setText(qt.QFontMetrics(folderLabel.font).elidedText(folder, qt.Qt.ElideMiddle, max(0, folderLabel.width)))

    def eventFilter(self, obj, event):
        eventType = event.type()
        if eventType == qt.QEvent.Show and obj == self.widget:
            self.refresh(force=False)
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
# What is open: the scene as Library shows it
#

#: Scans shown in the Current work card (more are counted: "+N more")
MAX_SCAN_ROWS = 4
#: Rows of the Open now card (more are counted)
MAX_OPEN_NOW_ROWS = 12
#: The subject list of the loaded set card shows this many rows (it scrolls)
LOADED_SET_LIST_ROWS = 6
LOADED_SET_ROW_HEIGHT = 28


def _draw():
    from . import draw

    return draw


def _segmentEditor():
    """The Segment Editor widget of the editor module if it was created (it is never created here), else None."""
    widget = getattr(slicer.modules, "SegmentEditorWidget", None)
    return widget.editor if widget is not None else None


def _sameNode(node, other):
    return node is not None and other is not None and node.GetID() == other.GetID()


def _hasReferenceGeometry(segmentationNode):
    parameterName = slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName()
    return bool(segmentationNode.GetSegmentation().GetConversionParameter(parameterName))


def isStraySegmentation(segmentationNode):
    """True for the empty segmentation that the classic Segment Editor creates by itself: no segment with voxels, no
    reference geometry and no source volume bound in the editor. It is not data (Library keeps its empty state).
    """
    if segmentationNode is None or _hasReferenceGeometry(segmentationNode):
        return False
    editor = _segmentEditor()
    if editor is not None and _sameNode(editor.segmentationNode(), segmentationNode) and editor.sourceVolumeNode() is not None:
        return False
    return _importer().isSegmentationEmpty(segmentationNode)


def _isHidden(node):
    return bool(node.GetHideFromEditors())


def lesionNodes():
    """The segmentations that are lesions of the scene: not hidden from the editors, not a stray empty segmentation."""
    return [node for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode") if not _isHidden(node) and not isStraySegmentation(node)]


def volumeKind(node):
    """"figure", "statmap", "template", "labelmap", "scan" (a scalar volume) or "volume" (any other volume)."""
    role = node.GetAttribute(space.ROLE_ATTRIBUTE)
    if role in (space.ROLE_FIGURE, space.ROLE_STATMAP, space.ROLE_TEMPLATE):
        return role
    if node.IsA("vtkMRMLLabelMapVolumeNode"):
        return "labelmap"
    if node.IsA("vtkMRMLScalarVolumeNode"):
        # A template of a scene saved before the tag existed
        return "template" if node.GetName().startswith(space.TEMPLATE_SPACE) else "scan"
    return "volume"


def volumeNodes():
    """The volumes of the scene that are not hidden from the editors."""
    return [node for node in slicer.util.getNodesByClass("vtkMRMLVolumeNode") if not _isHidden(node)]


def scanNodes():
    """The scans of the scene (scalar volumes that are not label maps, figures or statistical maps; templates included)."""
    return [node for node in volumeNodes() if volumeKind(node) in ("scan", "template")]


def isTemplateNode(node):
    return node is not None and volumeKind(node) == "template"


def hasData():
    """True if the scene has a volume or a segmentation that the user can see.

    A figure is not data (it is shown next to the template), and neither is the empty segmentation that the classic
    Segment Editor creates by itself (``isStraySegmentation()``).
    """
    for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        if not _isHidden(node) and node.GetAttribute(space.ROLE_ATTRIBUTE) != space.ROLE_FIGURE:
            return True
    for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
        if not _isHidden(node) and not isStraySegmentation(node):
            return True
    return False


def lesionScan(segmentationNode):
    """The scan that a lesion is bound to (its reference image geometry), else the source volume that Draw edits it on, else None."""
    role = slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole()
    reference = segmentationNode.GetNodeReference(role)
    if reference is not None and reference.IsA("vtkMRMLVolumeNode"):
        return reference
    editor = _segmentEditor()
    if editor is not None and _sameNode(editor.segmentationNode(), segmentationNode):
        return editor.sourceVolumeNode()
    return None


def lesionTitle(segmentationNode):
    """Name shown for a lesion: its segment ("Lesion") when it has one, else the name of the segmentation."""
    segmentation = segmentationNode.GetSegmentation()
    if segmentation.GetNumberOfSegments() == 1:
        return segmentation.GetNthSegment(0).GetName() or segmentationNode.GetName()
    return segmentationNode.GetName()


def nodeFile(node):
    """The file of a node (its storage node), None if it has none."""
    storageNode = node.GetStorageNode() if hasattr(node, "GetStorageNode") else None
    fileName = storageNode.GetFileName() if storageNode is not None else None
    return str(fileName) if fileName else None


def _internalFolders():
    """Real paths of the application's cache and temporary folders and of the sample data cache."""
    folders = []
    for attribute in ("cachePath", "temporaryPath"):
        value = getattr(slicer.app, attribute, None)
        if isinstance(value, str):
            folders.append(value)
    try:
        folders.append(slicer.mrmlScene.GetCacheManager().GetRemoteCacheDirectory())
    except Exception:
        pass
    try:
        from . import downloads

        folders.append(downloads.cacheDirectory())
    except Exception:
        pass
    return [os.path.normcase(os.path.realpath(folder)) for folder in folders if folder]


def isInternalPath(path, folders=None):
    """True for a file in the application's cache or temporary folders or in the sample data cache (the templates and the
    samples that Studio downloads): not a file of the user, so Recent never lists it. ``folders``: ``_internalFolders()``.
    """
    if not path:
        return False
    try:
        real = os.path.normcase(os.path.realpath(path))
        for base in (folders if folders is not None else _internalFolders()):
            if real == base or real.startswith(base.rstrip(os.sep) + os.sep):
                return True
    except (OSError, ValueError):
        return False
    return False


def lesionSummary(segmentationNode, compute=False):
    """What the Current work card says about a lesion: ``{"text", "side", "metrics"}``.

    ``text`` is "Left · 2.27 mL · 1 part" (the side word, "Left", "Right" or "Both hemispheres", only when the lesion grid is
    a known MNI grid), "Empty" for a lesion without voxels, "…" while the metrics are not computed yet (the cache of
    ``draw.lesionMetrics()`` is cold; ``compute`` computes them now) and "Nothing drawn yet" without a segment.
    """
    if segmentationNode.GetSegmentation().GetNumberOfSegments() == 0:
        return {"text": _("Nothing drawn yet"), "side": None, "metrics": None}
    draw = _draw()
    metrics = draw.lesionMetrics(segmentationNode, compute=compute)
    if metrics is None:
        return {"text": "…", "side": None, "metrics": None}
    if not metrics["voxels"]:
        return {"text": _("Empty"), "side": None, "metrics": metrics}
    side = space.hemisphereWord(metrics["centroidRas"][0], segmentationNode, partsSpanBoth=bool(metrics.get("spans_both")))
    volume = metrics["mL"]
    parts = [side] if side else []
    parts.append(_("<0.01 mL") if volume < 0.005 else _("{volume:.2f} mL").format(volume=volume))
    count = metrics["parts"]
    parts.append(_("{count} part").format(count=count) if count == 1 else _("{count} parts").format(count=count))
    return {"text": " · ".join(parts), "side": side, "metrics": metrics}


def lesionSaveInfo(segmentationNode):
    """``(state, text)`` of the save state of a lesion (``draw.lesionSaveState()``): state in "saved", "unsaved",
    "previous" and "never"; text such as "Saved · sub-01_lesion.nii.gz · 2 min ago", "Unsaved changes · …",
    "Not saved yet" or "Last saved as sub-01_lesion.nii.gz · 18 Oct".
    """
    draw = _draw()
    state = draw.lesionSaveState(segmentationNode)[0]
    return state, draw.saveStateText(segmentationNode)[0]


def isLesionUnsaved(segmentationNode):
    """True when closing the lesion would lose voxels: it has voxels and is not saved (or changed since it was saved)."""
    if lesionSaveInfo(segmentationNode)[0] == "saved":
        return False
    return not _importer().isSegmentationEmpty(segmentationNode)


class _Model:
    """The scene as the Current work card shows it."""

    def __init__(self):
        importer = _importer()
        loaded = importer.loadedSet()
        self.setIds = {node.GetID() for node in loaded.nodes()} if loaded is not None and 2 <= len(loaded) <= importer.MAX_LOADED_SET_LESIONS else set()
        scans = scanNodes()
        editor = _segmentEditor()
        source = editor.sourceVolumeNode() if editor is not None else None
        # The scan that Draw edits comes first
        scans.sort(key=lambda node: 0 if _sameNode(node, source) else 1)
        self.moreScans = max(0, len(scans) - MAX_SCAN_ROWS)
        self.scans = scans[:MAX_SCAN_ROWS]
        self.others = [node for node in volumeNodes() if volumeKind(node) in ("labelmap", "statmap", "volume")]
        self.lesions = [node for node in lesionNodes() if node.GetID() not in self.setIds]
        shown = {node.GetID() for node in self.scans}
        self.lesionsOf = {nodeId: [] for nodeId in shown}
        self.unattached = []
        self.scanOf = {}
        for lesion in self.lesions:
            scan = lesionScan(lesion)
            self.scanOf[lesion.GetID()] = scan
            if scan is not None and scan.GetID() in shown:
                self.lesionsOf[scan.GetID()].append(lesion)
            else:
                self.unattached.append(lesion)

    def signature(self):
        """Changes when rows must be created again (the nodes or their names changed)."""
        def key(node):
            return node.GetID(), node.GetName(), lesionTitle(node) if node.IsA("vtkMRMLSegmentationNode") else ""
        return (tuple(key(n) for n in self.scans), self.moreScans, tuple(key(n) for n in self.others),
                tuple((scanId, tuple(key(n) for n in group)) for scanId, group in self.lesionsOf.items()),
                tuple(key(n) for n in self.unattached))

    def isEmpty(self):
        return not (self.scans or self.others or self.lesions)

    def currentLesion(self):
        """The lesion that Draw edits when it is listed, else the first lesion of the first scan (or an unattached one)."""
        editor = _segmentEditor()
        edited = editor.segmentationNode() if editor is not None else None
        for lesion in self.lesions:
            if _sameNode(lesion, edited):
                return lesion
        for scan in self.scans:
            if self.lesionsOf.get(scan.GetID()):
                return self.lesionsOf[scan.GetID()][0]
        return self.unattached[0] if self.unattached else None

    def templateLesions(self):
        return [lesion for lesion in self.lesions if isTemplateNode(self.scanOf.get(lesion.GetID()))]


#
# Actions (module level: the cards look them up when a button is clicked)
#


def _figureShown():
    controller = _importer().figureController()
    return controller is not None and controller.isShown()


def _switchToDraw(shell):
    """Draw, with the brush armed: the user asked to trace (Continue in Draw, New lesion; D-U2)."""
    if shell is not None:
        _draw().requestArm(shell)
        shell.switchTo("draw", keepLayout=_figureShown())
    elif slicer.util.mainWindow() is not None and hasattr(slicer.modules, "segmenteditor"):
        slicer.util.selectModule("SegmentEditor")


def openScanInDraw(volumeNode, shell=None):
    """Edit a scan in Draw: its lesion if it has one, else a new empty one bound to it."""
    shell = shell if shell is not None else _shell()
    importer = _importer()
    for lesion in lesionNodes():
        if _sameNode(lesionScan(lesion), volumeNode):
            return continueInDraw(shell, lesion, volumeNode)
    if shell is not None:
        importer.setViewLayers(background=volumeNode, fit=True)
    name = importer.LESION_SEGMENTATION_NAME if isTemplateNode(volumeNode) else importer.scanLesionSegmentationName(volumeNode)
    lesion = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", name)
    lesion.CreateDefaultDisplayNodes()
    lesion.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
    importer.selectInSegmentEditor(lesion, volumeNode)
    _switchToDraw(shell)
    return lesion


def continueInDraw(shell=None, segmentationNode=None, volumeNode=None):
    """"Continue in Draw →": edit the lesion that Draw edited (or ``segmentationNode``) on its scan, with the brush armed.

    The segment that was selected stays selected when the editor already edits that lesion. Without a lesion the first scan
    is opened in Draw. Return the lesion (None if there is none).
    """
    shell = shell if shell is not None else _shell()
    model = _Model()
    lesion = segmentationNode if segmentationNode is not None else model.currentLesion()
    if lesion is None:
        if model.scans:
            return openScanInDraw(model.scans[0], shell)
        _switchToDraw(shell)
        return None
    scan = volumeNode if volumeNode is not None else lesionScan(lesion)
    editor = _segmentEditor()
    editsIt = editor is not None and _sameNode(editor.segmentationNode(), lesion) and (scan is None or _sameNode(editor.sourceVolumeNode(), scan))
    if not editsIt:
        _importer().selectInSegmentEditor(lesion, scan)
    _switchToDraw(shell)
    return lesion


def newLesionOnTemplate(shell=None):
    """"New lesion on template": a new empty lesion on the template (the plain "Start on MNI template" continues the existing
    one), shown in Draw. Return it (None without a template).
    """
    shell = shell if shell is not None else _shell()
    templates = [node for node in scanNodes() if isTemplateNode(node)]
    if not templates:
        return None
    lesion = _importer().newLesionSegmentation(templates[0], shell=shell)
    if lesion is not None:
        _switchToDraw(shell)
    return lesion


def closeScene():
    """Close the scene (File > Close scene, which asks first when something is unsaved)."""
    mainWindow = slicer.util.mainWindow()
    action = mainWindow.findChild("QAction", "FileCloseSceneAction") if mainWindow is not None else None
    if action is not None:
        action.trigger()
    else:
        slicer.mrmlScene.Clear(False)


def showInDataModule(node=None):
    """Show a node in the Data module (the transient module of Studio)."""
    slicer.util.selectModule("Data")


def _removeWithRelated(node):
    scene = slicer.mrmlScene
    related = [node.GetNthDisplayNode(i) for i in range(node.GetNumberOfDisplayNodes())] if hasattr(node, "GetNumberOfDisplayNodes") else []
    if hasattr(node, "GetNumberOfStorageNodes"):
        related.extend(node.GetNthStorageNode(i) for i in range(node.GetNumberOfStorageNodes()))
    scene.RemoveNode(node)
    for other in related:
        if other is not None and scene.IsNodePresent(other):
            scene.RemoveNode(other)


def closeNode(node, confirm=True):
    """Close one row: remove the node with its display and storage nodes. Return False when the user kept it.

    A lesion with unsaved voxels asks first (``confirm``). The Segment Editor lets go of the node before it is removed.
    """
    if node is None or not slicer.mrmlScene.IsNodePresent(node):
        return False
    if confirm and node.IsA("vtkMRMLSegmentationNode") and isLesionUnsaved(node):
        if not _confirmClose(_("{name} has changes that are not saved.").format(name=node.GetName()),
                             _("Save the lesion in Draw first (Ctrl+Shift+S), or close it and lose the changes.")):
            return False
    editor = _segmentEditor()
    if editor is not None:
        if _sameNode(editor.segmentationNode(), node):
            editor.setSegmentationNode(None)
        if _sameNode(editor.sourceVolumeNode(), node):
            editor.setSourceVolumeNode(None)
    loaded = _importer().loadedSet()
    if loaded is not None and node.IsA("vtkMRMLSegmentationNode") and loaded.contains(node):
        loaded.forget()
    _removeWithRelated(node)
    return True


def closeLoadedSet(confirm=True):
    """Close the loaded set: remove exactly the nodes of that load. Asks first when one of its lesions has unsaved edits.
    Return False when the user kept the set.
    """
    loaded = _importer().loadedSet()
    if loaded is None:
        return False
    unsaved = [node for node in loaded.nodes() if isLesionUnsaved(node)]
    if confirm and unsaved:
        if not _confirmClose(_("{count} lesions of this set have changes that are not saved.").format(count=len(unsaved)),
                             _("Save them in Draw first (Ctrl+Shift+S), or close the set and lose the changes.")):
            return False
    loaded.close()
    return True


def reviewLoadedSet(shell=None, index=None):
    """"Review in Draw": show one lesion of the loaded set (the one the set is at, else the first) and open Draw."""
    shell = shell if shell is not None else _shell()
    loaded = _importer().loadedSet()
    if loaded is None:
        return None
    lesion = loaded.select(max(loaded.index, 0) if index is None else index)
    _switchToDraw(shell)
    return lesion


def _askName(current):
    """Ask for a new name (an input dialog). Return the text, None when canceled."""
    dialog = qt.QInputDialog(slicer.util.mainWindow())
    dialog.setWindowTitle(_("Rename"))
    dialog.setLabelText(_("Name"))
    dialog.setTextValue(current)
    if _exec(dialog) != qt.QDialog.Accepted:
        return None
    return str(dialog.textValue()).strip() or None


def renameNode(node, name=None):
    """Rename a node (and the single segment of a lesion, which Draw shows as its name). Return the new name or None."""
    if node is None:
        return None
    name = name if name is not None else _askName(node.GetName())
    if not name:
        return None
    node.SetName(name)
    if node.IsA("vtkMRMLSegmentationNode") and node.GetSegmentation().GetNumberOfSegments() == 1:
        node.GetSegmentation().GetNthSegment(0).SetName(name)
    return name


def _studyUi():
    """``StudioLib.study_ui`` (W3-07) when this build has it, else None."""
    import importlib

    try:
        return importlib.import_module("StudioLib.study_ui")
    except ModuleNotFoundError as e:
        if e.name != "StudioLib.study_ui":
            raise
    return None


def _pickFiles(folder):
    """Ask for the files of one subject, starting in ``folder``. Return the paths (empty when canceled)."""
    paths = qt.QFileDialog.getOpenFileNames(slicer.util.mainWindow(), _("Open one subject"), folder, _importer().FILE_DIALOG_FILTERS)
    return [str(path) for path in paths] if paths else []


class _SceneRefresher(VTKObservationMixin):
    """Calls ``callback()`` once per event loop iteration after nodes are added or removed, or the scene is closed or imported."""

    def __init__(self, callback):
        VTKObservationMixin.__init__(self)
        self.callback = callback
        self.enabled = False
        self.timer = qt.QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(0)
        self.timer.connect("timeout()", self._run)

    def setEnabled(self, enabled):
        if bool(enabled) == self.enabled:
            return
        self.enabled = bool(enabled)
        if self.enabled:
            scene = slicer.mrmlScene
            for event in (scene.NodeAddedEvent, scene.NodeRemovedEvent, scene.EndCloseEvent, scene.EndImportEvent):
                self.addObserver(scene, event, self._onSceneChanged)
        else:
            self.timer.stop()
            self.removeObservers()

    def _onSceneChanged(self, caller, event):
        self.timer.start()

    def _run(self):
        try:
            self.callback()
        except Exception:
            logging.error(f"Studio Library: refresh failed:\n{traceback.format_exc()}")

    def dispose(self):
        self.setEnabled(False)
        self.timer.disconnect("timeout()")


#
# Current work card (USABILITY 2, "Library, data loaded, no study")
#


class _OfferLine:
    """The 1 mm template offer as the last line of the Current work card.

    The offer itself is a notice of the inspector (``importer.templateOfferWidget()``), which that slot owns: this line
    mirrors its text and buttons (a click presses the offer's button), so that it is also visible over the Library page.
    """

    def __init__(self, layout):
        self.layout = layout
        self.line = None
        self.signature = None

    @staticmethod
    def _banner():
        try:
            return _importer().templateBanner()
        except Exception:
            return None

    def isOffered(self):
        return self._banner() is not None

    def update(self):
        banner = self._banner()
        if banner is None:
            self.clear()
            return False
        buttons = [button for button in (banner.primaryButton, banner.secondaryButton, getattr(banner, "detailsButton", None))
                   if button is not None and _isAlive(button) and not button.isHidden()]
        text = str(banner.textLabel.text)
        signature = (text, tuple(str(button.text) for button in buttons))
        if signature != self.signature:
            self.clear()
            self.signature = signature
            self.line = ui.noticeLine(text, [(str(button.text), lambda button=button: button.click()) for button in buttons])
            self.line.setObjectName("StudioCurrentWorkOffer")
            self.layout.addWidget(self.line)
        return True

    def clear(self):
        if self.line is not None:
            _discard(self.line)
            self.line = None
        self.signature = None


class _LesionBlock:
    """Widgets of the lesion rows of the Current work card: name, metrics and save state."""

    def __init__(self, segmentationNode, indent):
        self.nodeId = segmentationNode.GetID()
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioCurrentWorkLesion")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(indent, 0, 0, 0)
        layout.setSpacing(1)
        self.nameLabel = _elidedLabel("", 13, "StudioCurrentWorkLesionName")
        self.metricsLabel = qt.QLabel()
        self.metricsLabel.setObjectName("StudioCurrentWorkLesionMetrics")
        self.stateLabel = qt.QLabel()
        self.stateLabel.setObjectName("StudioCurrentWorkLesionState")
        for label in (self.metricsLabel, self.stateLabel):
            label.setTextFormat(qt.Qt.RichText)
            label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
            label.setWordWrap(False)
            _setFont(label, 12)
        for label in (self.nameLabel, self.metricsLabel, self.stateLabel):
            layout.addWidget(label)

    def node(self):
        node = slicer.mrmlScene.GetNodeByID(self.nodeId)
        return node if node is not None and node.IsA("vtkMRMLSegmentationNode") else None


class CurrentWorkCard(qt.QObject):
    """"Current work": what is open and what to do next (USABILITY 2). One row per scan (at most 4), its lesions with their
    metrics and save state, the primary "Continue in Draw →", "Close", "New lesion on template" and "Show in Data module".

    Lesions of a loaded set are listed by the ``LoadedSetCard``. ``refresh()`` creates the rows when the scene changed and
    updates their texts; metrics that are not cached yet read "…" and are computed once the switch is done.
    """

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget, layout = ui.card(_("Current work"))
        self.widget.setObjectName("StudioCurrentWorkCard")
        self.rowsLayout = qt.QVBoxLayout()
        self.rowsLayout.setSpacing(10)
        layout.addLayout(self.rowsLayout)

        buttonRow = _buttonRow()
        self.continueButton = _roleButton(_("Continue in Draw →"), "StudioCurrentWorkContinueButton", lambda: continueInDraw(_shell()), "primary")
        self.continueButton.setToolTip(_("Edit the lesion in Draw (Ctrl+Alt+2)"))
        self.closeButton = _roleButton(_("Close"), "StudioCurrentWorkCloseButton", lambda: closeScene(), "secondary")  # noqa: PLW0108 (looked up when clicked)
        self.closeButton.setToolTip(_("Close the scene"))
        buttonRow.layout().addWidget(self.continueButton, 3)
        buttonRow.layout().addWidget(self.closeButton, 2)
        layout.addWidget(buttonRow)

        self.newLesionRow = _buttonRow()
        self.newLesionButton = _roleButton(_("New lesion on template"), "StudioCurrentWorkNewLesionButton", lambda: newLesionOnTemplate(_shell()), "secondary")
        self.newLesionButton.setToolTip(_("Start another lesion on the MNI template; the one you drew stays (hidden)"))
        self.newLesionRow.layout().addWidget(self.newLesionButton)
        layout.addWidget(self.newLesionRow)
        self.newLesionRow.hide()

        linkRow = qt.QHBoxLayout()
        linkRow.setContentsMargins(0, 0, 0, 0)
        self.dataLink = _linkButton(_("Show in Data module"), "StudioCurrentWorkDataLink", lambda: showInDataModule())  # noqa: PLW0108
        linkRow.addWidget(self.dataLink)
        linkRow.addStretch(1)
        layout.addLayout(linkRow)

        self.offerLayout = qt.QVBoxLayout()
        self.offerLayout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.offerLayout)
        self.offer = _OfferLine(self.offerLayout)
        self.offerTimer = qt.QTimer()
        self.offerTimer.setInterval(500)
        self.offerTimer.connect("timeout()", self._pollOffer)

        self._signature = None
        self._scanRows = []
        self._blocks = {}
        self._pendingMetrics = []
        self._metricsTimer = qt.QTimer()
        self._metricsTimer.setSingleShot(True)
        self._metricsTimer.setInterval(0)
        self._metricsTimer.connect("timeout()", self._computeNext)
        self._model = None
        ui.bindStyleSheet(self.widget, self._styleSheet)
        ui.addPaletteListener(self._onPalette)

    @staticmethod
    def _styleSheet():
        return (
            f"QLabel#StudioCurrentWorkScanFolder, QLabel#StudioCurrentWorkLesionMetrics, QLabel#StudioCurrentWorkLesionState, "
            f"QLabel#StudioCurrentWorkMore {{ color: {ui.token('text2').name()}; }}")

    def _onPalette(self):
        if not _isAlive(self.widget):
            ui.removePaletteListener(self._onPalette)
            return
        self._updateTexts()

    #
    # Rows
    #

    def _rebuild(self, model):
        _clearLayout(self.rowsLayout)
        self._blocks = {}
        self._scanRows = []

        def addScan(scan):
            row = qt.QWidget()
            row.setObjectName("StudioCurrentWorkScan")
            rowLayout = qt.QVBoxLayout(row)
            rowLayout.setContentsMargins(0, 0, 0, 0)
            rowLayout.setSpacing(1)
            name = _elidedLabel(scan.GetName(), 13, "StudioCurrentWorkScanName")
            font = qt.QFont(name.font)
            font.setWeight(qt.QFont.DemiBold)
            name.setFont(font)
            folder = _elidedLabel("", 12, "StudioCurrentWorkScanFolder")
            rowLayout.addWidget(name)
            rowLayout.addWidget(folder)
            self.rowsLayout.addWidget(row)
            self._scanRows.append((scan.GetID(), folder))
            for lesion in model.lesionsOf.get(scan.GetID(), []):
                self._addBlock(lesion, 14)

        for scan in model.scans:
            addScan(scan)
        if model.moreScans:
            more = qt.QLabel(_("+{count} more").format(count=model.moreScans))
            more.setObjectName("StudioCurrentWorkMore")
            _setFont(more, 12)
            self.rowsLayout.addWidget(more)
        for lesion in model.unattached:
            self._addBlock(lesion, 0)
        for node in model.others:
            row = _elidedLabel(f"{node.GetName()} · {_kindText(node)}", 12, "StudioCurrentWorkOther")
            self.rowsLayout.addWidget(row)

    def _addBlock(self, lesion, indent):
        block = _LesionBlock(lesion, indent)
        self._blocks[lesion.GetID()] = block
        self.rowsLayout.addWidget(block.widget)

    def _updateTexts(self, queue=True):
        """Set the texts of the rows. ``queue``: lesions whose metrics are not cached are queued for ``_computeNext()``."""
        model = self._model
        if model is None:
            return
        warning = ui.token("warning").name()
        for scanId, folderLabel in self._scanRows:
            scan = slicer.mrmlScene.GetNodeByID(scanId)
            if scan is None:
                continue
            path = nodeFile(scan)
            if isTemplateNode(scan):
                spaceText = space.spaceLabel(space.spaceOf(scan))
                text = _("MNI template · {space}").format(space=spaceText) if spaceText else _("MNI template")
                toolTip = text
            elif not path:
                text = _("Not saved to a file")
                toolTip = text
            elif isInternalPath(path):
                text, toolTip = _("Sample data"), path
            else:
                text = toolTip = os.path.dirname(path)
            setElidedText(folderLabel, text, toolTip)
        for lesionId, block in self._blocks.items():
            lesion = block.node()
            if lesion is None:
                continue
            title = lesionTitle(lesion)
            displayNode = lesion.GetDisplayNode()
            if displayNode is not None and not (displayNode.GetVisibility() and displayNode.GetVisibility2D()):
                title += " · " + _("hidden")
            setElidedText(block.nameLabel, title, lesion.GetName())
            summary = lesionSummary(lesion)
            plain = summary["text"]
            block.metricsLabel.setProperty("plainText", plain)
            block.metricsLabel.setToolTip(self._metricsToolTip(summary))
            if summary["side"] == "Both hemispheres":
                block.metricsLabel.setText(f'<span style="color:{warning}">{html.escape(plain)}</span>')
            else:
                block.metricsLabel.setText(html.escape(plain))
            if queue and summary["metrics"] is None and lesion.GetSegmentation().GetNumberOfSegments() and lesionId not in self._pendingMetrics:
                self._pendingMetrics.append(lesionId)
            state, text = lesionSaveInfo(lesion)
            block.stateLabel.setProperty("plainText", text)
            block.stateLabel.setProperty("saveState", state)
            block.stateLabel.setToolTip(_draw().saveStateText(lesion)[1] or "")
            if state == "unsaved":
                block.stateLabel.setText(f'<span style="color:{warning}">{html.escape(text)}</span>')
            else:
                block.stateLabel.setText(html.escape(text))
        if queue and self._pendingMetrics:
            self._metricsTimer.start()

    @staticmethod
    def _metricsToolTip(summary):
        metrics = summary["metrics"]
        if not metrics or not metrics["voxels"]:
            return ""
        return _("{voxels} voxels · {parts} parts · {gaps} gaps").format(voxels=metrics["voxels"], parts=metrics["parts"], gaps=len(metrics["gaps"]))

    def _computeNext(self):
        """Compute the metrics of the next lesion with a cold cache (one per event loop iteration)."""
        if not _isAlive(self.widget):
            return
        while self._pendingMetrics:
            lesionId = self._pendingMetrics.pop(0)
            lesion = slicer.mrmlScene.GetNodeByID(lesionId)
            if lesion is None or lesionId not in self._blocks:
                continue
            try:
                lesionSummary(lesion, compute=True)
            except Exception:
                logging.error(f"Studio Library: lesion metrics failed:\n{traceback.format_exc()}")
                continue
            self._updateTexts(queue=False)
            if self._pendingMetrics:
                self._metricsTimer.start()
            return

    #
    # Buttons
    #

    def _updateButtons(self, model):
        unsaved = [lesion for lesion in model.lesions if isLesionUnsaved(lesion)]
        self.closeButton.setText(_("Close… (unsaved)") if unsaved else _("Close"))
        self.closeButton.setToolTip(
            _("Close the scene; you are asked first, because {count} lesion(s) are not saved").format(count=len(unsaved)) if unsaved
            else _("Close the scene"))
        importer = _importer()
        templateLesions = model.templateLesions()
        offerNew = any(not importer.isSegmentationEmpty(lesion) for lesion in templateLesions)
        self.newLesionRow.setVisible(offerNew)
        self.continueButton.setEnabled(bool(model.scans or model.lesions))

    def _pollOffer(self):
        if not _isAlive(self.widget) or not self.widget.isVisible():
            self.offerTimer.stop()
            return
        if not self.offer.update():
            self.offerTimer.stop()

    #
    # Refresh
    #

    def refresh(self):
        """Read the scene again. Return True when there is something to show (the card is then visible)."""
        model = _Model()
        self._model = model
        if model.isEmpty():
            self.widget.hide()
            return False
        signature = model.signature()
        if signature != self._signature:
            self._signature = signature
            self._rebuild(model)
        self._updateTexts()
        self._updateButtons(model)
        if self.offer.update():
            self.offerTimer.start()
        else:
            self.offerTimer.stop()
        self.widget.show()
        return True

    def lesionBlock(self, segmentationNode):
        return self._blocks.get(segmentationNode.GetID())

    def dispose(self):
        ui.removePaletteListener(self._onPalette)
        self.offerTimer.stop()
        self._metricsTimer.stop()
        self.offer.clear()
        if _isAlive(self.widget):
            _clearLayout(self.rowsLayout)
            ui.bindStyleSheet(self.widget, None)
            _discard(self.widget)


def _kindText(node):
    """Short kind of a node for the lists of what is open."""
    if node.IsA("vtkMRMLSegmentationNode"):
        return _("Lesion")
    return {"scan": _("Scan"), "template": _("Template"), "figure": _("Figure"), "statmap": _("Map"),
            "labelmap": _("Label map")}.get(volumeKind(node), _("Volume"))


#
# Loaded set card (USABILITY 2, "Library, loaded set")
#


def _gridText(node):
    """"grid 91x109x91 · MNI152NLin6Asym 2 mm", or "grid 91x109x91, 2 mm, space unknown" (with the multiplication sign)."""
    grid = space.gridOf(node)
    if grid is None:
        return ""
    dims, matrix = grid
    dimsText = TIMES.join(str(int(n)) for n in dims)
    named = space.spaceOf(node)
    if named is not None:
        return _("grid {dims} · {space}").format(dims=dimsText, space=space.spaceLabel(named))
    import numpy as np

    steps = [float(np.linalg.norm(matrix[:3, axis])) for axis in range(3)]
    spacing = f"{steps[0]:g}" if max(steps) - min(steps) < 0.01 else TIMES.join(f"{step:.2g}" for step in steps)
    return _("grid {dims}, {spacing} mm, space unknown").format(dims=dimsText, spacing=spacing)


def participantsTable(folder, levels=3):
    """The ``participants.tsv`` (or .csv) of a folder or of its parent folders (``levels`` folders in all), else None."""
    for _step in range(levels):
        for name in ("participants.tsv", "participants.csv"):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                return path
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return None


def participantsLine(path):
    """"participants.tsv · 20 rows · group, age, sex, symptom_score" (the header and the number of rows), None if unreadable."""
    header = _importer().readTableHeader(path)
    if header is None:
        return None
    columns, rowCount = header
    shown = [column for column in columns if column != "participant_id"][:4]
    more = "…" if len([c for c in columns if c != "participant_id"]) > len(shown) else ""
    text = _("{name} · {count} rows").format(name=os.path.basename(path), count=rowCount)
    return text + (" · " + ", ".join(shown) + more if shown else "")


class LoadedSetCard(qt.QObject):
    """"Loaded set": the lesions loaded at once (2 to 60), reviewed one at a time in Draw.

    It names their number, grid and space, the ``participants.tsv`` found next to them, and lists the subjects with the
    current one marked. Buttons: Review in Draw, Close set and (with the study page) Create study….
    """

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget, layout = ui.card(_("Loaded set"))
        self.widget.setObjectName("StudioLoadedSetCard")
        self.summaryLabel = qt.QLabel()
        self.summaryLabel.setObjectName("StudioLoadedSetSummary")
        self.tableLabel = qt.QLabel()
        self.tableLabel.setObjectName("StudioLoadedSetTable")
        for label in (self.summaryLabel, self.tableLabel):
            label.setTextFormat(qt.Qt.PlainText)
            label.setWordWrap(True)
            label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
            _setFont(label, 13 if label is self.summaryLabel else 12)
            layout.addWidget(label)
        self.list = qt.QListWidget()
        self.list.setObjectName("StudioLoadedSetList")
        self.list.setUniformItemSizes(True)
        self.list.setFocusPolicy(qt.Qt.StrongFocus)
        self.list.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        self.list.connect("itemDoubleClicked(QListWidgetItem*)", self._onDoubleClicked)
        self.list.connect("itemActivated(QListWidgetItem*)", self._onDoubleClicked)
        self.list.connect("itemClicked(QListWidgetItem*)", self._onClicked)
        layout.addWidget(self.list)

        buttonRow = _buttonRow()
        self.reviewButton = _roleButton(_("Review in Draw"), "StudioLoadedSetReviewButton", lambda: reviewLoadedSet(_shell(), self._chosenIndex()), "primary")
        self.reviewButton.setToolTip(_("Edit one lesion at a time in Draw; Alt+← and Alt+→ step through the set"))
        self.closeButton = _roleButton(_("Close set"), "StudioLoadedSetCloseButton", lambda: closeLoadedSet(), "secondary")  # noqa: PLW0108
        self.closeButton.setToolTip(_("Remove these lesions from the scene (files are never deleted)"))
        buttonRow.layout().addWidget(self.reviewButton, 3)
        buttonRow.layout().addWidget(self.closeButton, 2)
        self.createStudyButton = None
        if _studyUi() is not None:
            self.createStudyButton = _roleButton(_("Create study…"), "StudioLoadedSetCreateStudyButton", self._createStudy, "secondary")
            buttonRow.layout().addWidget(self.createStudyButton, 2)
        layout.addWidget(buttonRow)
        self._signature = None
        self._userRow = None
        self._lastActivation = 0.0
        ui.bindStyleSheet(self.widget, self._styleSheet)

    @staticmethod
    def _styleSheet():
        return (
            f"QLabel#StudioLoadedSetSummary {{ color: {ui.token('text').name()}; }}"
            f"QLabel#StudioLoadedSetTable {{ color: {ui.token('text2').name()}; }}"
            f"QListWidget#StudioLoadedSetList {{ background: transparent; border: 1px solid {ui.token('hairline').name()}; border-radius: 6px; "
            f"color: {ui.token('text').name()}; outline: none; }}"
            f"QListWidget#StudioLoadedSetList::item {{ padding: 0px 8px; }}"
            f"QListWidget#StudioLoadedSetList::item:selected {{ background-color: {ui.token('accent-subtle').name()}; color: {ui.token('text').name()}; }}")

    def _loaded(self):
        importer = _importer()
        loaded = importer.loadedSet()
        if loaded is None or not 2 <= len(loaded) <= importer.MAX_LOADED_SET_LESIONS:
            return None
        return loaded

    def _chosenIndex(self):
        """The row the user picked in the list, else None (the set is then reviewed from where it is)."""
        return self._userRow

    def _onDoubleClicked(self, item):
        # A double click emits itemDoubleClicked and itemActivated: one review
        now = time.monotonic()
        if now - self._lastActivation < 0.4:
            return
        self._lastActivation = now
        loaded = self._loaded()
        if loaded is not None:
            reviewLoadedSet(_shell(), self.list.row(item))

    def _createStudy(self):
        studyUi = _studyUi()
        loaded = self._loaded()
        handler = getattr(studyUi, "createStudy", None) if studyUi is not None else None
        if handler is not None and loaded is not None:
            handler([nodeFile(node) for node in loaded.nodes() if nodeFile(node)], _shell())

    def refresh(self):
        """Show or hide the card. Return True when it is shown."""
        loaded = self._loaded()
        if loaded is None:
            self.widget.hide()
            self._signature = None
            return False
        nodes = loaded.nodes()
        subjects = [loaded.subject(node) for node in nodes]
        grid = _gridText(nodes[0])
        signature = (tuple(node.GetID() for node in nodes), tuple(subjects), loaded.index, grid)
        if signature != self._signature:
            self._signature = signature
            self._userRow = None
            files = [nodeFile(node) for node in nodes if nodeFile(node)]
            parts = [_("{count} lesion masks").format(count=len(nodes))]
            if grid:
                parts.append(grid)
            self.summaryLabel.setText(" · ".join(parts))
            table = participantsTable(os.path.commonpath([os.path.dirname(path) for path in files])) if files else None
            line = participantsLine(table) if table else None
            self.tableLabel.setText(line or "")
            self.tableLabel.setVisible(bool(line))
            self.list.clear()
            for index, subject in enumerate(subjects):
                marker = "●  " if index == loaded.index else "    "
                self.list.addItem(marker + subject)
                item = self.list.item(index)
                item.setSizeHint(qt.QSize(0, LOADED_SET_ROW_HEIGHT))
                item.setToolTip(nodes[index].GetName())
                if index == loaded.index:
                    font = qt.QFont(item.font())
                    font.setWeight(qt.QFont.DemiBold)
                    item.setFont(font)
            rows = min(len(nodes), LOADED_SET_LIST_ROWS)
            self.list.setFixedHeight(rows * LOADED_SET_ROW_HEIGHT + 2)
            if 0 <= loaded.index < len(nodes):
                self.list.setCurrentRow(loaded.index)
        self.widget.show()
        return True

    def _onClicked(self, item):
        self._userRow = self.list.row(item)

    def subjects(self):
        """The text of the rows of the subject list (the current one starts with "●")."""
        return [str(self.list.item(index).text()) for index in range(self.list.count)]

    def dispose(self):
        if _isAlive(self.widget):
            ui.bindStyleSheet(self.widget, None)
            _discard(self.widget)


#
# Open now card (the inspector of Library with data: what is open)
#

OPEN_NOW_ROW_HEIGHT = 28


def openNowItems():
    """What is open, for the Open now list: ``[{"kind": "volume" | "lesion" | "set", "node", "name", "kindText"}]``.

    Scans and templates first, then the other volumes (figures, maps, label maps), then the lesions. The lesions of a loaded
    set are one row (closing it closes the set).
    """
    importer = _importer()
    loaded = importer.loadedSet()
    setNodes = loaded.nodes() if loaded is not None and 2 <= len(loaded) <= importer.MAX_LOADED_SET_LESIONS else []
    setIds = {node.GetID() for node in setNodes}
    volumes = volumeNodes()
    volumes.sort(key=lambda node: 0 if volumeKind(node) in ("scan", "template") else 1)
    items = [{"kind": "volume", "node": node, "name": node.GetName(), "kindText": _kindText(node)} for node in volumes]
    for lesion in lesionNodes():
        if lesion.GetID() not in setIds:
            items.append({"kind": "lesion", "node": lesion, "name": lesion.GetName(), "kindText": _kindText(lesion)})
    if setNodes:
        items.append({"kind": "set", "node": setNodes[0], "name": _("{count} lesion masks").format(count=len(setNodes)), "kindText": _("Loaded set")})
    return items


class _DisplayObserver(VTKObservationMixin):
    """Calls ``callback()`` when an observed display node changes (the eye toggles follow what other parts of Studio hide)."""

    def __init__(self, callback):
        VTKObservationMixin.__init__(self)
        self.callback = callback

    def watch(self, nodes):
        self.removeObservers()
        for node in nodes:
            self.addObserver(node, vtk.vtkCommand.ModifiedEvent, self._onModified)

    def _onModified(self, caller, event):
        try:
            self.callback()
        except Exception:
            logging.error(f"Studio Library: Open now update failed:\n{traceback.format_exc()}")


class OpenNowCard(qt.QObject):
    """"Open now": the volumes and lesions of the scene in 28 px rows (the same name as the section of the canvas).

    Each row has the kind in text2 and a ⋯ menu: Rename…, Open in Draw, Show in Data module and Close. A lesion also has an
    eye that shows or hides it in the views (its display node). A volume has no eye: the display node of a volume does not
    decide what the slice views show. "Close" is the only removal verb of Studio; "Delete" stays the word of the Data module.
    """

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget, layout = ui.card(_("Open now"))
        self.widget.setObjectName("StudioOpenNowCard")
        self.rowsLayout = qt.QVBoxLayout()
        self.rowsLayout.setSpacing(0)
        layout.addLayout(self.rowsLayout)
        self.emptyLabel = qt.QLabel(_("Nothing is open yet. Drop files on the window, or use Open…"))
        self.emptyLabel.setObjectName("StudioOpenNowEmpty")
        self.emptyLabel.setWordWrap(True)
        self.emptyLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        _setFont(self.emptyLabel, 12)
        layout.addWidget(self.emptyLabel)
        self.moreLink = _linkButton("", "StudioOpenNowMore", lambda: showInDataModule())  # noqa: PLW0108
        self.moreLink.hide()
        layout.addWidget(self.moreLink)
        self._signature = None
        self._rows = []  # [(row widget, eye button or None, item)]
        self._observer = _DisplayObserver(self._updateEyes)
        ui.bindStyleSheet(self.widget, self._styleSheet)
        ui.addPaletteListener(self._refreshIcons)

    @staticmethod
    def _styleSheet():
        return (
            f"QLabel#StudioOpenNowKind, QLabel#StudioOpenNowEmpty {{ color: {ui.token('text2').name()}; }}"
            f"QLabel#StudioOpenNowName {{ color: {ui.token('text').name()}; }}")

    def _refreshIcons(self):
        if not _isAlive(self.widget):
            ui.removePaletteListener(self._refreshIcons)
            return
        for row, eye, item in self._rows:
            moreButton = row.findChild("QToolButton", "StudioOpenNowMenuButton")
            if moreButton is not None:
                moreButton.setIcon(ui.icon("ellipsis", 16))
        self._updateEyes()

    @staticmethod
    def isShown(node):
        """True when a lesion is shown in the views: its display node is visible in 2D and 3D."""
        displayNode = node.GetDisplayNode()
        return displayNode is not None and bool(displayNode.GetVisibility()) and bool(displayNode.GetVisibility2D())

    @staticmethod
    def setShown(node, shown):
        displayNode = node.GetDisplayNode()
        if displayNode is None:
            return
        if shown:
            displayNode.SetVisibility(True)
            displayNode.SetVisibility2D(True)
        else:
            displayNode.SetVisibility(False)

    def _updateEyes(self):
        for row, eye, item in self._rows:
            if eye is None or not _isAlive(eye):
                continue
            node = slicer.mrmlScene.GetNodeByID(item["id"])
            if node is None:
                continue
            shown = self.isShown(node)
            eye.setProperty("shown", shown)
            eye.setIcon(ui.icon("eye" if shown else "eye-off", 16))
            eye.setToolTip(_("Hide in the views") if shown else _("Show in the views"))
            eye.setAccessibleName(_("Hide {name}").format(name=item["name"]) if shown else _("Show {name}").format(name=item["name"]))

    def _toggleEye(self, nodeId):
        node = slicer.mrmlScene.GetNodeByID(nodeId)
        if node is not None:
            self.setShown(node, not self.isShown(node))
            self._updateEyes()

    def _addRow(self, item):
        row = qt.QWidget()
        row.setObjectName("StudioOpenNowRow")
        row.setFixedHeight(OPEN_NOW_ROW_HEIGHT)
        layout = qt.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        eye = None
        if item["kind"] == "lesion":
            eye = qt.QToolButton()
            eye.setObjectName("StudioOpenNowEye")
            eye.setAutoRaise(True)
            eye.setFixedSize(24, 24)
            ui.setRole(eye, "ghost")
            ui.installButtonKeys(eye)
            eye.connect("clicked()", lambda nodeId=item["id"]: self._toggleEye(nodeId))
            layout.addWidget(eye)
        else:
            spacer = qt.QWidget()
            spacer.setFixedSize(24, 24)
            layout.addWidget(spacer)
        name = _elidedLabel(item["name"], 13, "StudioOpenNowName")
        layout.addWidget(name, 1)
        kind = qt.QLabel(item["kindText"])
        kind.setObjectName("StudioOpenNowKind")
        _setFont(kind, 12)
        layout.addWidget(kind)
        more = qt.QToolButton()
        more.setObjectName("StudioOpenNowMenuButton")
        more.setAutoRaise(True)
        more.setFixedSize(24, 24)
        more.setToolTip(_("More"))
        more.setAccessibleName(_("More about {name}").format(name=item["name"]))
        more.setPopupMode(qt.QToolButton.InstantPopup)
        ui.setRole(more, "ghost")
        ui.installButtonKeys(more)
        menu = qt.QMenu(more)
        menu.setObjectName("StudioOpenNowMenu")
        self._fillMenu(menu, item)
        more.setMenu(menu)
        layout.addWidget(more)
        self.rowsLayout.addWidget(row)
        self._rows.append((row, eye, item))

    def _fillMenu(self, menu, item):
        kind = item["kind"]
        nodeId = item["id"]

        def node():
            return slicer.mrmlScene.GetNodeByID(nodeId)

        def addAction(text, objectName, callback, enabled=True):
            action = menu.addAction(text)
            action.setObjectName(objectName)
            action.setEnabled(enabled)
            action.connect("triggered()", callback)
            return action

        if kind != "set":
            addAction(_("Rename…"), "StudioOpenNowRename", lambda: (renameNode(node()), self.refresh()))
        canDraw = kind == "lesion" or (kind == "volume" and item["volumeKind"] in ("scan", "template"))
        if kind == "set":
            addAction(_("Open in Draw"), "StudioOpenNowOpenInDraw", lambda: reviewLoadedSet(_shell()))
        elif kind == "lesion":
            addAction(_("Open in Draw"), "StudioOpenNowOpenInDraw", lambda: continueInDraw(_shell(), node()))
        else:
            addAction(_("Open in Draw"), "StudioOpenNowOpenInDraw", lambda: openScanInDraw(node(), _shell()), enabled=canDraw)
        addAction(_("Show in Data module"), "StudioOpenNowShowInData", lambda: showInDataModule(node()))
        if kind == "set":
            addAction(_("Close set"), "StudioOpenNowClose", lambda: closeLoadedSet())  # noqa: PLW0108
        else:
            addAction(_("Close"), "StudioOpenNowClose", lambda: closeNode(node()))

    def refresh(self):
        """Read the scene again: rows are created when the nodes or their names changed, the eyes are updated in place."""
        items = openNowItems()
        shown = items[:MAX_OPEN_NOW_ROWS]
        for item in items:
            node = item["node"]
            item["id"] = node.GetID()
            item["volumeKind"] = volumeKind(node) if item["kind"] == "volume" else None
        signature = tuple((item["kind"], item["id"], item["name"], item["kindText"]) for item in shown)
        if signature != self._signature:
            self._signature = signature
            _clearLayout(self.rowsLayout)
            self._rows = []
            for item in shown:
                self._addRow(item)
            self._refreshIcons()
        self._observer.watch([item["node"].GetDisplayNode() for item in shown if item["kind"] == "lesion" and item["node"].GetDisplayNode() is not None])
        self._updateEyes()
        self.emptyLabel.setVisible(not items)
        extra = len(items) - len(shown)
        self.moreLink.setVisible(extra > 0)
        if extra > 0:
            self.moreLink.setText(_("+{count} more · Show in Data module").format(count=extra))
        return len(items)

    def rowNames(self):
        """The names of the rows (for tests)."""
        return [item["name"] for row, eye, item in self._rows]

    def dispose(self):
        ui.removePaletteListener(self._refreshIcons)
        self._observer.removeObservers()
        if _isAlive(self.widget):
            _clearLayout(self.rowsLayout)
            ui.bindStyleSheet(self.widget, None)
            _discard(self.widget)


#
# Folder card
#


class FolderCard:
    """Card offered for a folder with several NIfTI files: "N NIfTI files found", the reason when there are too many to
    load at once, and an enabled way on: Load all (up to ``MAX_LOAD_ALL_FILES`` files), Open one subject…, and
    Create study… when the study page (W3-07) is part of this build.
    """

    def __init__(self, folder, files, onClose=None):
        self.folder = folder
        self.files = list(files)
        self.onClose = onClose
        importer = _importer()
        self.tooMany = len(self.files) > importer.MAX_LOAD_ALL_FILES
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

        self.folderLabel = _elidedLabel(folder, 12, "StudioFolderCardFolder")
        ui.bindStyleSheet(self.folderLabel, lambda: f"QLabel#StudioFolderCardFolder {{ color: {ui.token('text2').name()}; }}")
        layout.addWidget(self.folderLabel)

        # The participants table next to the files (BIDS)
        table = participantsTable(folder, levels=2)
        line = participantsLine(table) if table else None
        self.participantsLabel = None
        if line:
            self.participantsLabel = self._textLabel(line, "StudioFolderCardParticipants")
            layout.addWidget(self.participantsLabel)

        # The reason is in the card, not in a tool tip
        self.studyAvailable = _studyUi() is not None
        self.reasonLabel = None
        if self.tooMany:
            if self.studyAvailable:
                reason = _("{count} files: open one subject at a time, or create a study").format(count=len(self.files))
            else:
                reason = _("{count} files: open one subject at a time, or create a study when available").format(count=len(self.files))
            self.reasonLabel = self._textLabel(reason, "StudioFolderCardReason")
            layout.addWidget(self.reasonLabel)

        buttonRow = qt.QHBoxLayout()
        buttonRow.setSpacing(8)
        buttonRow.addStretch(1)
        self.createStudyButton = None
        if self.studyAvailable:
            # The study is the way on for a folder of subjects
            self.createStudyButton = qt.QPushButton(_("Create study…"))
            self.createStudyButton.setObjectName("StudioFolderCardCreateStudyButton")
            self.createStudyButton.connect("clicked()", self.createStudy)
        self.openOneButton = qt.QPushButton(_("Open one subject…"))
        self.openOneButton.setObjectName("StudioFolderCardOpenOneButton")
        self.openOneButton.setToolTip(_("Pick the files of one subject in this folder"))
        self.openOneButton.connect("clicked()", self.openOneSubject)
        self.loadAllButton = qt.QPushButton(_("Load all"))
        self.loadAllButton.setObjectName("StudioFolderCardLoadAllButton")
        if self.tooMany:
            # A cohort is never loaded into the scene at once (SPEC 10)
            self.loadAllButton.setEnabled(False)
            self.loadAllButton.setToolTip(_("More than {count} files: open the files of one subject").format(count=importer.MAX_LOAD_ALL_FILES))
        else:
            self.loadAllButton.setToolTip("\n".join(os.path.basename(path) for path in self.files))
        self.loadAllButton.connect("clicked()", self.loadAll)
        # The rightmost button is the primary one: Create study…, else the way on (Open one subject… for a large folder, else Load all)
        order = [self.loadAllButton, self.openOneButton] if self.tooMany else [self.openOneButton, self.loadAllButton]
        if self.createStudyButton is not None:
            order.append(self.createStudyButton)
        primary = order[-1] if order[-1].isEnabled() else order[-2]
        for button in order:
            ui.setRole(button, "primary" if button is primary else "secondary")
            buttonRow.addWidget(button)
        layout.addLayout(buttonRow)

        ui.addPaletteListener(self._refreshIcons)
        self._refreshIcons()

    @staticmethod
    def _textLabel(text, objectName):
        label = qt.QLabel(text)
        label.setObjectName(objectName)
        label.setTextFormat(qt.Qt.PlainText)
        label.setWordWrap(True)
        label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        _setFont(label, 12)
        ui.bindStyleSheet(label, lambda objectName=objectName: f"QLabel#{objectName} {{ color: {ui.token('text2').name()}; }}")
        return label

    def _refreshIcons(self):
        if not _isAlive(self.widget):
            return
        self.iconLabel.setPixmap(ui.pixmap("folder-open", 18, "accent-fg"))
        self.closeButton.setIcon(ui.icon("x", 14))

    def loadAll(self):
        files = self.files
        self.close()
        return _runAction(lambda: _importer().loadFiles(files, _shell()), _("Failed to load the files."))

    def openOneSubject(self):
        """Pick the files of one subject (a file dialog in this folder) and load them."""
        paths = _pickFiles(self.folder)
        if not paths:
            return None
        self.close()
        return _runAction(lambda: _importer().loadFiles(paths, _shell()), _("Failed to load the files."))

    def createStudy(self):
        studyUi = _studyUi()
        handler = getattr(studyUi, "createStudy", None) if studyUi is not None else None
        if handler is not None:
            return _runAction(lambda: handler(self.files, _shell()), _("Failed to create the study."))
        return None

    def close(self):
        ui.removePaletteListener(self._refreshIcons)
        if _isAlive(self.widget):
            _discard(self.widget)
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


#: Height of the compact drop strip that replaces the drop zone once data is loaded
DROP_STRIP_HEIGHT = 44
FOOTER_LINE_HEIGHT = 32


class LibraryCanvas(qt.QObject):
    """The Library page shown over the views (SPEC 3.1): the empty state, or the current work once data is loaded."""

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
        self.contentLayout = qt.QVBoxLayout(content)
        self.contentLayout.setContentsMargins(24, 24, 24, 24)
        self.contentLayout.setSpacing(0)
        # The empty state is centred; the current work starts at the top
        self.contentLayout.addStretch(2)
        columnRow = qt.QHBoxLayout()
        columnRow.addStretch(1)
        column = qt.QWidget()
        column.setObjectName("StudioLibraryColumn")
        column.setFixedWidth(COLUMN_WIDTH)
        columnRow.addWidget(column)
        columnRow.addStretch(1)
        self.contentLayout.addLayout(columnRow)
        self.contentLayout.addStretch(3)
        self.scrollArea.setWidget(content)

        columnLayout = qt.QVBoxLayout(column)
        columnLayout.setContentsMargins(0, 0, 0, 0)
        columnLayout.setSpacing(0)

        # What is open (shown once data is loaded): the current work and the loaded set
        self.dataSection = qt.QWidget()
        self.dataSection.setObjectName("StudioLibraryDataSection")
        dataLayout = qt.QVBoxLayout(self.dataSection)
        dataLayout.setContentsMargins(0, 0, 0, 16)
        dataLayout.setSpacing(16)
        # The cards are created when data is loaded for the first time (Library is created at startup)
        self._dataLayout = dataLayout
        self._currentWork = None
        self._loadedSet = None
        self.dataSection.hide()
        columnLayout.addWidget(self.dataSection)

        # Drop zone (the files are dropped on the main window, see StudioLib.importer). Once data is loaded it is a strip.
        self.dropZone = qt.QFrame()
        self.dropZone.setObjectName("StudioDropZone")
        self.dropZone.setFixedSize(COLUMN_WIDTH, DROP_ZONE_HEIGHT)
        self.dropZone.setProperty("dragActive", False)
        self.dropZone.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        self.dropZone.setToolTip(_("Drop files here, or click to open files"))
        _installEventFilter(self.dropZone, self)
        dropZoneOuter = qt.QVBoxLayout(self.dropZone)
        dropZoneOuter.setContentsMargins(0, 0, 0, 0)
        dropZoneOuter.setSpacing(0)
        self.dropFull = qt.QWidget()
        self.dropFull.setObjectName("StudioDropZoneFull")
        dropZoneLayout = qt.QVBoxLayout(self.dropFull)
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
        dropZoneOuter.addWidget(self.dropFull)
        self.dropCompact = qt.QWidget()
        self.dropCompact.setObjectName("StudioDropZoneCompact")
        compactLayout = qt.QHBoxLayout(self.dropCompact)
        compactLayout.setContentsMargins(16, 0, 16, 0)
        compactLayout.setSpacing(8)
        compactLayout.addStretch(1)
        self.dropStripIcon = qt.QLabel()
        self.dropStripIcon.setObjectName("StudioDropStripIcon")
        compactLayout.addWidget(self.dropStripIcon)
        self.dropStripText = qt.QLabel(_("Drop more files · {shortcut}").format(shortcut=openShortcut))
        self.dropStripText.setObjectName("StudioDropStripText")
        _setFont(self.dropStripText, 13)
        compactLayout.addWidget(self.dropStripText)
        compactLayout.addStretch(1)
        for label in (self.dropStripIcon, self.dropStripText):
            label.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
        dropZoneOuter.addWidget(self.dropCompact)
        self.dropCompact.hide()
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

        # Footer line (the research-use line of the first run): one 32 px line, nothing while no one sets it
        self.footerFrame = qt.QFrame()
        self.footerFrame.setObjectName("StudioLibraryFooterLine")
        self.footerFrame.setMinimumHeight(FOOTER_LINE_HEIGHT + 8)
        self.footerLayout = qt.QHBoxLayout(self.footerFrame)
        self.footerLayout.setContentsMargins(10, 12, 10, 4)
        self.footerLayout.setSpacing(4)
        self.footerText = qt.QLabel()
        self.footerText.setObjectName("StudioLibraryFooterText")
        self.footerText.setWordWrap(True)
        self.footerText.setTextFormat(qt.Qt.PlainText)
        self.footerText.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        _setFont(self.footerText, 12)
        self.footerLayout.addWidget(self.footerText, 1)
        self.footerActionsLayout = qt.QHBoxLayout()
        self.footerActionsLayout.setSpacing(0)
        self.footerLayout.addLayout(self.footerActionsLayout)
        self.footerFrame.hide()
        columnLayout.addWidget(self.footerFrame)
        #: key -> (text, actions): the most recently set one is shown
        self._footerLines = {}

        self._dataShown = None
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
        return _linkButton(text, objectName, callback)

    @staticmethod
    def _styleSheet():
        t = {name: ui.token(name).name() for name in ("canvas", "text", "text2", "text3", "border", "accent-fg", "accent-subtle")}
        t["control-border"] = _controlBorder()
        return (
            f"QFrame#StudioLibraryCanvas {{ background-color: {t['canvas']}; border: none; }}"
            "QScrollArea#StudioLibraryScroll, QWidget#StudioLibraryContent, QWidget#StudioLibraryColumn { background: transparent; border: none; }"
            "QScrollArea#StudioLibraryScroll > QWidget#qt_scrollarea_viewport { background: transparent; }"
            "QWidget#StudioLibraryDataSection, QWidget#StudioDropZoneFull, QWidget#StudioDropZoneCompact { background: transparent; border: none; }"
            f"QFrame#StudioDropZone {{ background-color: transparent; border: 1.5px dashed {t['control-border']}; border-radius: 12px; }}"
            f"QFrame#StudioDropZone:hover {{ border-color: {t['text3']}; }}"
            f'QFrame#StudioDropZone[dragActive="true"] {{ background-color: {t["accent-subtle"]}; border-color: {t["accent-fg"]}; }}'
            f"QLabel#StudioDropZoneTitle {{ color: {t['text']}; background: transparent; border: none; }}"
            f"QLabel#StudioDropZoneHint {{ color: {t['text2']}; background: transparent; border: none; }}"
            f"QLabel#StudioDropStripText {{ color: {t['text2']}; background: transparent; border: none; }}"
            "QLabel#StudioDropZoneIcon, QLabel#StudioDropStripIcon { background: transparent; border: none; }"
            f"QLabel#StudioLibrarySectionTitle {{ color: {t['text2']}; }}"
            f"QLabel#StudioLibraryLinkSeparator {{ color: {t['text3']}; }}"
            f"QLabel#StudioLibraryFooterText {{ color: {t['text2']}; }}"
            f"QPushButton[studioLibraryLink=\"true\"] {{ background: transparent; border: none; color: {t['accent-fg']}; padding: 2px 4px; }}"
            "QPushButton[studioLibraryLink=\"true\"]:hover { text-decoration: underline; }")

    def _refreshIcons(self):
        if not _isAlive(self.widget):
            return
        dragActive = bool(self.dropZone.property("dragActive"))
        color = "accent-fg" if dragActive else "text2"
        self.dropZoneIcon.setPixmap(ui.pixmap("import", 32, color))
        self.dropStripIcon.setPixmap(ui.pixmap("import", 16, color))
        for button, iconName in ((self.openButton, "import"), (self.openFolderButton, "folder-open"), (self.templateButton, "brain")):
            button.setIcon(ui.icon(iconName, 16))

    def eventFilter(self, obj, event):
        # A click on the drop zone opens files
        if obj == self.dropZone and event.type() in (qt.QEvent.MouseButtonPress, qt.QEvent.MouseButtonRelease):
            if event.button() != qt.Qt.LeftButton:
                return False
            if event.type() == qt.QEvent.MouseButtonRelease and self.dropZone.rect.contains(_eventPosition(event)):
                openFiles()
            return True
        return False

    #
    # What is open
    #

    @property
    def currentWork(self):
        """The Current work card (created when first needed)."""
        if self._currentWork is None:
            self._currentWork = CurrentWorkCard()
            self._currentWork.widget.hide()
            self._dataLayout.insertWidget(0, self._currentWork.widget)
        return self._currentWork

    @property
    def loadedSet(self):
        """The Loaded set card (created when first needed)."""
        if self._loadedSet is None:
            self._loadedSet = LoadedSetCard()
            self._loadedSet.widget.hide()
            self._dataLayout.addWidget(self._loadedSet.widget)
        return self._loadedSet

    def hasData(self):
        """True if the scene has data to show (``library.hasData()``): the page then shows the current work."""
        return hasData()

    def refreshData(self):
        """Show the current work (and the loaded set) when data is loaded, else the empty state."""
        shell = _shell()
        study = shell is not None and getattr(shell, "studySession", None) is not None
        data = hasData() and not study
        shown = False
        if data:
            # A card that fails never keeps the page from showing
            for card in (self.currentWork, self.loadedSet):
                try:
                    shown = card.refresh() or shown
                except Exception:
                    logging.error(f"Studio Library: {type(card).__name__} failed:\n{traceback.format_exc()}")
                    card.widget.hide()
        else:
            for card in (self._currentWork, self._loadedSet):
                if card is not None:
                    card.widget.hide()
        self.dataSection.setVisible(shown)
        self._setStrip(shown)
        return shown

    def _setStrip(self, strip):
        """The drop zone is a 44 px strip while the current work is shown, else the 480x220 zone of the empty state."""
        if self._dataShown == strip:
            return
        self._dataShown = strip
        self.dropFull.setVisible(not strip)
        self.dropCompact.setVisible(strip)
        self.dropZone.setFixedSize(COLUMN_WIDTH, DROP_STRIP_HEIGHT if strip else DROP_ZONE_HEIGHT)
        self.dropZone.setToolTip(_("Drop more files here, or click to open files") if strip else _("Drop files here, or click to open files"))
        # The empty state is centred, the current work starts at the top of the page
        self.contentLayout.setStretch(0, 0 if strip else 2)

    def isStrip(self):
        return bool(self._dataShown)

    def refresh(self):
        self.refreshData()
        self.recentList.refresh(force=False)

    def focusFirst(self):
        """Focus "Continue in Draw" when the current work is shown, else the first recent file (Enter loads it), else Open…."""
        if self.isStrip() and self.currentWork.widget.isVisible() and self.currentWork.continueButton.isEnabled():
            self.currentWork.continueButton.setFocus(qt.Qt.OtherFocusReason)
        elif self.isStrip() and self.loadedSet.widget.isVisible():
            self.loadedSet.reviewButton.setFocus(qt.Qt.OtherFocusReason)
        elif not self.recentList.focusFirst():
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
        primary = card.createStudyButton if card.createStudyButton is not None else (card.openOneButton if card.tooMany else card.loadAllButton)
        primary.setFocus(qt.Qt.OtherFocusReason)
        return card

    def folderCard(self):
        return self.folderCardSlot.card

    #
    # Footer line
    #

    def setFooterLine(self, key, text, actions=()):
        """Show one line of text (word-wrapped, text2) at the bottom of the page, with link-style actions
        (``[(text, callback)]``, for example "Don't show again"). A line set later hides the earlier ones until it is cleared.
        """
        self._footerLines.pop(key, None)
        self._footerLines[key] = (str(text), list(actions))
        self._showFooterLine()

    def clearFooterLine(self, key):
        self._footerLines.pop(key, None)
        self._showFooterLine()

    def footerLineKey(self):
        """Key of the footer line that is shown, None if there is none."""
        return next(reversed(self._footerLines), None) if self._footerLines else None

    def _showFooterLine(self):
        _clearLayout(self.footerActionsLayout)
        key = self.footerLineKey()
        if key is None:
            self.footerText.setText("")
            self.footerFrame.hide()
            return
        text, actions = self._footerLines[key]
        self.footerText.setText(text)
        for index, (actionText, callback) in enumerate(actions):
            link = _linkButton(actionText, "StudioLibraryFooterAction", callback)
            self.footerActionsLayout.addWidget(link)
        self.footerFrame.show()

    def dispose(self):
        ui.removePaletteListener(self._refreshIcons)
        self.folderCardSlot.close()
        self.recentList.dispose()
        for card in (self._currentWork, self._loadedSet):
            if card is not None:
                card.dispose()
        _clearLayout(self.footerActionsLayout)
        if _isAlive(self.widget):
            ui.bindStyleSheet(self.widget, None)
            self.widget.hide()
            self.widget.deleteLater()


#
# Module panel
#

_panels = []


class LibraryPanel(qt.QObject):
    """Content of the Studio module panel: what is open, or the Open buttons and the recently loaded files.

    It works in Studio (inspector), in Classic and without main window. With the Library canvas (Studio) the inspector lists
    what is open (``OpenNowCard``): the Open buttons and Recent are on the canvas, not twice. Without the canvas (Classic) it
    keeps the Open buttons and Recent, and lists what is open above Recent once data is loaded. The content is built when
    the panel is shown for the first time, except while Studio enters the Library empty state, which hides the inspector
    (at startup on a fresh profile, for example).
    """

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioLibraryPanel")
        self.openCard = None
        self.openButton = None
        self.openFolderButton = None
        self.templateButton = None
        self.openNowCard = None
        self.folderCardSlot = None
        self.recentCard = None
        self.recentList = None
        self._sceneRefresher = _SceneRefresher(self.refresh)
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

        self.openCard, openLayout = ui.card(_("Open"))
        self.openCard.setObjectName("StudioLibraryOpenCard")
        self.openButton = _secondaryButton(_("Open…"), "StudioOpenButton", openFiles)
        self.openFolderButton = _secondaryButton(_("Open folder…"), "StudioOpenFolderButton", openFolder)
        self.templateButton = _secondaryButton(_("Start on MNI template"), "StudioStartOnTemplateButton", startOnTemplate)
        for button in (self.openButton, self.openFolderButton, self.templateButton):
            row = _buttonRow()
            row.layout().addWidget(button)
            openLayout.addWidget(row)
        layout.addWidget(self.openCard)

        self.openNowCard = OpenNowCard()
        layout.addWidget(self.openNowCard.widget)

        self.folderCardSlot = _FolderCardSlot()
        layout.addLayout(self.folderCardSlot.layout)

        self.recentCard, recentLayout = ui.card(_("Recent"))
        self.recentCard.setObjectName("StudioLibraryRecentCard")
        recentLayout.setContentsMargins(4, 12, 4, 4)
        self.recentList = RecentList()
        recentLayout.addWidget(self.recentList.widget)
        layout.addWidget(self.recentCard)

        ui.addPaletteListener(self._refreshIcons)
        self._refreshIcons()
        self.refresh()

    @staticmethod
    def withCanvas():
        """True when the Library canvas is the page (Studio): the Open buttons and Recent are there."""
        return _shell() is not None

    def refresh(self):
        """Show the cards that fit: with the canvas, what is open; without it, the Open buttons, Recent and what is open."""
        if not self.isBuilt():
            return
        withCanvas = self.withCanvas()
        data = hasData()
        self.openCard.setVisible(not withCanvas)
        self.recentCard.setVisible(not withCanvas)
        self.openNowCard.widget.setVisible(withCanvas or data)
        if self.openNowCard.widget.isVisible():
            self.openNowCard.refresh()

    def eventFilter(self, obj, event):
        if obj == self.widget:
            eventType = event.type()
            if eventType == qt.QEvent.Show:
                if not self.isBuilt() and not _inspectorHiddenOnEnter():
                    self.build()
                if self.isBuilt():
                    self.refresh()
                    self._sceneRefresher.setEnabled(True)
            elif eventType == qt.QEvent.Hide:
                self._sceneRefresher.setEnabled(False)
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
            panel._sceneRefresher.dispose()
            if panel.openNowCard is not None:
                panel.openNowCard.dispose()
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
        for key, (text, actions) in _footerLines.items():
            _canvas.setFooterLine(key, text, actions)
    return _canvas


#: Footer lines that were set: key -> (text, actions), applied again when the canvas is created again
_footerLines = {}


def setFooterLine(key, text, actions=()):
    """Show one line of text at the bottom of the Library canvas (word-wrapped, text2, 32 px), with link-style actions
    (``[(text, callback)]``, for example "Don't show again"). Nothing is shown by Library itself: the research-use line of the
    first run (W3-03, U-03) uses this. The line is kept while the canvas is created again.
    """
    _footerLines.pop(key, None)
    _footerLines[key] = (str(text), list(actions))
    if _canvas is not None and _isAlive(_canvas.widget):
        _canvas.setFooterLine(key, text, actions)


def clearFooterLine(key):
    """Remove the footer line ``key``."""
    _footerLines.pop(key, None)
    if _canvas is not None and _isAlive(_canvas.widget):
        _canvas.clearFooterLine(key)


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
        # The canvas also follows the scene by itself while it is shown; this covers a change that came with the inspector
        if _canvas is not None and _isAlive(_canvas.widget):
            _canvas.refreshData()

    def dispose(self):
        self.timer.stop()
        self.removeObservers()


def enter(shell):
    """Library becomes the current workspace: show the canvas (the views are paused) and focus Recent."""
    global _sceneWatcher
    libraryCanvas = canvas()
    # The canvas covers the views first: the views that the inspector change resizes are paused (no render), and the
    # edge handle of a hidden inspector is not shown for a moment
    shell.setCanvas(libraryCanvas.widget)
    shell.setInspectorVisible(not isEmptyState(shell))
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
    # The inspector is shown while the canvas still covers the views: they are resized once, paused, and render
    # when the canvas is hidden
    shell.setInspectorVisible(True)
    shell.setCanvas(None)


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

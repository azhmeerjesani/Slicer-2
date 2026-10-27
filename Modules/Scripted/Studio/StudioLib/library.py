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
- ``showNotice(key, widget)`` / ``closeNotice(key)`` / ``notices()``: the keyed notice slot at the top of the canvas,
  above the drop zone, the current work and the study page (A-01b: the autosave recovery card). Notices are never
  shown over a view (USABILITY 6.4). Entering Library clears the inspector notice of the same key, which points here.
- With a study, the canvas lists the subjects (``StudyList``) as rows or as a grid of cards (``StudyGrid``, W4-02) with an
  axial lesion thumbnail computed off the main thread (``StudioLib.thumbnails``); both share the model, the selection,
  the keys and the context menu.

"Close" is the only removal verb of Studio (Close scene, Close set, a row's Close); "Delete" stays the word of
the Data module. Files are loaded by ``StudioLib.importer``.
"""

import html
import logging
import os
import re
import sys
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


def newStudy():
    """New study… (``study_ui.newStudyDialog``)."""
    studyUi = _studyUi()
    if studyUi is not None:
        _runAction(lambda: studyUi.newStudyDialog(_shell()), _("Failed to create the study."))


def openStudy():
    """Open study… (``study_ui.openStudyDialog``)."""
    studyUi = _studyUi()
    if studyUi is not None:
        _runAction(lambda: studyUi.openStudyDialog(_shell()), _("Failed to open the study."))


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
            self.folder = (_("1 NIfTI file") if self.count == 1 else _("{count} NIfTI files").format(count=self.count))

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


def recentEntries(limit=RECENT_ROW_COUNT, outsideRootOnly=False):
    """The most recently loaded files that still exist, most recent first.

    They come from the "RecentlyLoadedMenu" actions of the main window (property ``fileParameters``),
    or from the settings that the main window writes when there is no main window. Files are examined
    (``os.stat``) only until ``limit`` existing files are found. Files in the cache or temporary folders (the templates and
    the sample data that Studio downloaded) are not the user's files and never listed (``isInternalPath()``). Files of the
    projects of the Slicer Studio folder are listed in Projects instead (A-09): Recent keeps the files opened from elsewhere.
    ``outsideRootOnly`` also leaves out every file of the Slicer Studio folder (its studies and LNM folder; the Library page).
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
    projectsFolder = _rootFolder() if outsideRootOnly else _projectsFolder()
    entries = []
    for properties, action in candidates:
        if len(entries) >= limit:
            break
        path = str(properties.get("fileName", ""))
        if not path or isInternalPath(path, internal):
            continue
        if projectsFolder and isInternalPath(path, [projectsFolder]):
            continue
        entry = RecentEntry(properties, action, openedTimes)
        if entry.exists():
            entries.append(entry)
    return entries


def _projectsFolder():
    """<Slicer Studio folder>/Projects (real path), or None when projects are off (A-09)."""
    try:
        from . import projects
    except ImportError:
        return None
    if not projects.isEnabled():
        return None
    return os.path.normcase(os.path.realpath(projects.projectsFolder()))


def _rootFolder():
    """The Slicer Studio folder (real path), or None when projects are off (V-02: the page lists only files from elsewhere)."""
    try:
        from . import projects
    except ImportError:
        return None
    if not projects.isEnabled():
        return None
    return os.path.normcase(os.path.realpath(projects.root()))


def _shortFolder(path):
    """A folder for display: the home folder written "~" (``projects.shortPath``)."""
    try:
        from . import projects

        return projects.shortPath(path)
    except ImportError:
        return path


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

    def __init__(self, rowCount=RECENT_ROW_COUNT, includeStudies=True, outsideRootOnly=False):
        qt.QObject.__init__(self)
        self.rowCount = rowCount
        #: The Library page lists the studies in Studies, and only what was opened from outside the Slicer Studio folder
        self.includeStudies = includeStudies
        self.outsideRootOnly = outsideRootOnly
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
        return (str(settings.value("Studio/RecentOpenTimes") or ""), str(settings.value("Studio/RecentFolders") or ""),
                str(settings.value("Studio/RecentStudies") or ""))

    def refresh(self, force=True):
        """Read the recently loaded files again: the studies first (``Studio/RecentStudies``), then the folders opened in Studio,
        then the files.

        Without ``force`` (the page is shown again) nothing is read when the menu and the recorded times and folders did not change.
        """
        key = self._settingsKey()
        # An empty list is not read again either (a new recent file changes the menu, which sets _dirty)
        if not force and not self._dirty and key == self._shownKey:
            return
        self._shownKey = key
        self._dirty = False
        studies = recentStudyEntries(min(RECENT_STUDY_ROW_COUNT, self.rowCount)) if self.includeStudies else []
        folders = recentFolderEntries(min(RECENT_FOLDER_ROW_COUNT, self.rowCount - len(studies)))
        if self.outsideRootOnly:
            root = _rootFolder()
            folders = [entry for entry in folders if not root or not isInternalPath(entry.path, [root])]
        self.entries = studies + folders + recentEntries(self.rowCount - len(studies) - len(folders), outsideRootOnly=self.outsideRootOnly)
        while len(self.rows) < len(self.entries):
            self._createRow()
        now = time.time()
        for index, (row, nameLabel, folderLabel, timeLabel) in enumerate(self.rows):
            if index >= len(self.entries):
                row.hide()
                continue
            entry = self.entries[index]
            nameLabel.setProperty("fullText", entry.name)
            folderLabel.setProperty("fullText", _shortFolder(entry.folder) if entry.kind == "file" else entry.folder)
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
    a known MNI grid; the enclosed volume for a closed surface source), "Empty" for a lesion without voxels, "…" while the metrics are not computed yet (the cache of
    ``draw.lesionMetrics()`` is cold; ``compute`` computes them now) and "Nothing drawn yet" without a segment.
    """
    if segmentationNode.GetSegmentation().GetNumberOfSegments() == 0:
        return {"text": _("Nothing drawn yet"), "side": None, "metrics": None}
    draw = _draw()
    metrics = draw.lesionMetrics(segmentationNode, compute=compute)
    if metrics is None:
        return {"text": "…", "side": None, "metrics": None}
    if not metrics["voxels"]:
        surface = draw.surfaceMetrics(segmentationNode)
        if surface is None:
            return {"text": _("Empty"), "side": None, "metrics": metrics}
        # A closed surface source (official 3D Slicer can save one): its enclosed volume and pieces
        metrics = dict(metrics, mL=surface["mL"], parts=surface["parts"], centroidRas=surface["centroidRas"],
                       spans_both=surface["spans_both"])
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


def _projectService():
    """The autosave service while the scene is a project (A-09), else None."""
    try:
        from . import autosave
    except ImportError:
        return None
    service = autosave.instance() if hasattr(autosave, "instance") else None
    if service is None or not hasattr(service, "currentProject") or service.currentProject() is None:
        return None
    return service


def closeScene():
    """Close the scene (File > Close scene, which asks first when something is unsaved). In a project (A-09) the project is
    saved first, so nothing is lost and nothing is asked (as New project).
    """
    service = _projectService()
    if service is not None:
        service.newProject()
        return
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
        # One line (VISUAL 4.2): swatch, name, metrics, save state chip
        layout = qt.QHBoxLayout(self.widget)
        layout.setContentsMargins(indent, 0, 0, 0)
        layout.setSpacing(8)
        self.swatch = qt.QLabel()
        self.swatch.setObjectName("StudioCurrentWorkLesionSwatch")
        self.swatch.setFixedSize(10, 10)
        layout.addWidget(self.swatch, 0, qt.Qt.AlignVCenter)
        self.nameLabel = _elidedLabel("", 13, "StudioCurrentWorkLesionName")
        font = qt.QFont(self.nameLabel.font)
        font.setWeight(qt.QFont.Medium)
        self.nameLabel.setFont(font)
        self.nameLabel.setSizePolicy(qt.QSizePolicy.Preferred, qt.QSizePolicy.Preferred)
        self.nameLabel.setMinimumWidth(40)
        self.nameLabel.setMaximumWidth(220)
        self.metricsLabel = qt.QLabel()
        self.metricsLabel.setObjectName("StudioCurrentWorkLesionMetrics")
        self.stateLabel = qt.QLabel()
        self.stateLabel.setObjectName("StudioCurrentWorkLesionState")
        for label in (self.metricsLabel, self.stateLabel):
            label.setTextFormat(qt.Qt.RichText)
            label.setWordWrap(False)
            _setFont(label, 12)
        self.metricsLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        self.stateLabel.setSizePolicy(qt.QSizePolicy.Maximum, qt.QSizePolicy.Fixed)
        self.stateLabel.setFixedHeight(20)
        layout.addWidget(self.nameLabel)
        layout.addWidget(self.metricsLabel, 1)
        layout.addWidget(self.stateLabel)

    def node(self):
        node = slicer.mrmlScene.GetNodeByID(self.nodeId)
        return node if node is not None and node.IsA("vtkMRMLSegmentationNode") else None


class CurrentWorkCard(qt.QObject):
    """"Continue" (VISUAL 4.2, USABILITY 2): what is open and what to do next. A card on the canvas with the project's
    thumbnail, the project name and when it was saved, one row per scan (at most 4) with its lesions (metrics and save
    state), and the actions: the primary "Continue in Draw →", "Close", "New lesion on template" and "Show in Data module".

    Lesions of a loaded set are listed by the ``LoadedSetCard``. ``refresh()`` creates the rows when the scene changed and
    updates their texts; metrics that are not cached yet read "…" and are computed once the switch is done.
    """

    THUMBNAIL_SIZE = (196, 148)
    #: The coronal and sagittal tiles of the 3-view strip (V-02)
    SIDE_THUMBNAIL_SIZE = (96, 72)
    ACTIONS_WIDTH = 200

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget = qt.QFrame()
        self.widget.setObjectName("StudioCurrentWorkCard")
        ui.setRole(self.widget, "cardLow")
        outerLayout = qt.QVBoxLayout(self.widget)
        outerLayout.setContentsMargins(ui.CARD_PADDING, ui.CARD_PADDING, ui.CARD_PADDING + 2, ui.CARD_PADDING)
        outerLayout.setSpacing(10)
        cardLayout = qt.QHBoxLayout()
        cardLayout.setSpacing(20)
        outerLayout.addLayout(cardLayout)
        # The project's thumbnails, on black (the image is the hero): the axial view, and the coronal and sagittal views
        # beside it (V-02)
        strip = qt.QWidget()
        strip.setObjectName("StudioCurrentWorkThumbnails")
        stripLayout = qt.QHBoxLayout(strip)
        stripLayout.setContentsMargins(0, 0, 0, 0)
        stripLayout.setSpacing(4)
        self.thumbnail = qt.QLabel()
        self.thumbnail.setObjectName("StudioCurrentWorkThumbnail")
        self.thumbnail.setFixedSize(*self.THUMBNAIL_SIZE)
        self.thumbnail.setAlignment(qt.Qt.AlignCenter)
        stripLayout.addWidget(self.thumbnail)
        sideColumn = qt.QVBoxLayout()
        sideColumn.setSpacing(4)
        self.sideThumbnails = {}
        for view in ("coronal", "sagittal"):
            label = qt.QLabel()
            label.setObjectName("StudioCurrentWorkThumbnailSide")
            label.setProperty("studioView", view)
            label.setFixedSize(*self.SIDE_THUMBNAIL_SIZE)
            label.setAlignment(qt.Qt.AlignCenter)
            label.hide()
            sideColumn.addWidget(label)
            self.sideThumbnails[view] = label
        sideColumn.addStretch(1)
        stripLayout.addLayout(sideColumn)
        cardLayout.addWidget(strip, 0, qt.Qt.AlignTop)
        middle = qt.QVBoxLayout()
        middle.setSpacing(4)
        self.overline = ui.overline(_("Continue"), "StudioCurrentWorkOverline")
        middle.addWidget(self.overline)
        self.titleLabel = _elidedLabel("", 18, "StudioCurrentWorkTitle")
        ui.setText(self.titleLabel, "title")
        middle.addWidget(self.titleLabel)
        self.metaLabel = _elidedLabel("", 12, "StudioCurrentWorkMeta")
        middle.addWidget(self.metaLabel)
        middle.addSpacing(8)
        self.rowsLayout = qt.QVBoxLayout()
        self.rowsLayout.setSpacing(8)
        middle.addLayout(self.rowsLayout)
        middle.addStretch(1)
        cardLayout.addLayout(middle, 1)

        actions = qt.QWidget()
        actions.setObjectName("StudioCurrentWorkActions")
        actions.setFixedWidth(self.ACTIONS_WIDTH)
        actionsLayout = qt.QVBoxLayout(actions)
        actionsLayout.setContentsMargins(0, 0, 0, 0)
        actionsLayout.setSpacing(8)
        actionsLayout.addStretch(1)
        self.continueButton = _roleButton(_("Continue in Draw →"), "StudioCurrentWorkContinueButton", lambda: continueInDraw(_shell()), "primary")
        self.continueButton.setToolTip(_("Edit the lesion in Draw (Ctrl+Alt+2)"))
        self.continueButton.setFixedHeight(36)
        self.closeButton = _roleButton(_("Close"), "StudioCurrentWorkCloseButton", lambda: closeScene(), "secondary")  # noqa: PLW0108 (looked up when clicked)
        self.closeButton.setToolTip(_("Close the scene"))
        self.closeButton.setFixedHeight(ui.BUTTON_HEIGHT)
        self.newLesionButton = _roleButton(_("New lesion on template"), "StudioCurrentWorkNewLesionButton", lambda: newLesionOnTemplate(_shell()), "secondary")
        self.newLesionButton.setToolTip(_("Start another lesion on the MNI template; the one you drew stays (hidden)"))
        self.newLesionButton.setFixedHeight(ui.BUTTON_HEIGHT)
        self.newLesionRow = self.newLesionButton
        self.newLesionRow.hide()
        for button in (self.continueButton, self.closeButton, self.newLesionButton):
            actionsLayout.addWidget(button)
        self.dataLink = _linkButton(_("Show in Data module"), "StudioCurrentWorkDataLink", lambda: showInDataModule())  # noqa: PLW0108
        actionsLayout.addWidget(self.dataLink, 0, qt.Qt.AlignHCenter)
        actionsLayout.addStretch(1)
        cardLayout.addWidget(actions)
        # The 1 mm template offer: the last line of the card, across its width
        self.offerLayout = qt.QVBoxLayout()
        self.offerLayout.setContentsMargins(0, 0, 0, 0)
        outerLayout.addLayout(self.offerLayout)

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
        self._project = None
        self._thumbnailKey = None
        ui.bindStyleSheet(self.widget, self._styleSheet)
        ui.addPaletteListener(self._onPalette)
        self._showProject()

    @staticmethod
    def _styleSheet():
        t = {name: ui.token(name).name() for name in ("text", "text2", "text3", "success", "warning", "control-hover")}
        tint = {name: ui._rgba(ui.token(name), 0.14) for name in ("success", "warning")}
        return (
            "QLabel#StudioCurrentWorkThumbnail, QLabel#StudioCurrentWorkThumbnailSide { background-color: #000000; border: none; border-radius: 8px; }"
            f"QLabel#StudioCurrentWorkScanFolder, QLabel#StudioCurrentWorkMeta, QLabel#StudioCurrentWorkMore {{ color: {t['text3']}; }}"
            f"QLabel#StudioCurrentWorkLesionMetrics {{ color: {t['text2']}; }}"
            f"QLabel#StudioCurrentWorkLesionState {{ color: {t['text2']}; background-color: {t['control-hover']}; border-radius: 10px; padding: 0px 8px; }}"
            f'QLabel#StudioCurrentWorkLesionState[saveState="saved"] {{ color: {t["success"]}; background-color: {tint["success"]}; }}'
            f'QLabel#StudioCurrentWorkLesionState[saveState="unsaved"] {{ color: {t["warning"]}; background-color: {tint["warning"]}; }}'
            "QFrame#StudioCurrentWorkCard QLabel { border: none; }")

    def setProject(self, info):
        """The current project (``projects.ProjectInfo``) of the Slicer Studio folder, or None: its name, when it was saved
        and its thumbnail.
        """
        self._project = info
        if _isAlive(self.widget):
            self._showProject()

    def _showProject(self):
        info = self._project
        if info is not None:
            setElidedText(self.titleLabel, info.name)
            when = relativeTime(info.modified) if info.modified else ""
            meta = " · ".join(part for part in ((_("Edited {when}").format(when=when) if when else ""),
                                                 _("Projects \u203a {name}").format(name=info.name)) if part)
            setElidedText(self.metaLabel, meta)
        else:
            setElidedText(self.titleLabel, _("Open now"))
            setElidedText(self.metaLabel, _("Not in a project yet"))
        views = dict(getattr(info, "views", None) or ({"axial": info.thumbnail} if info is not None and info.thumbnail else {}))

        def mtime(path):
            try:
                return os.path.getmtime(path)
            except (OSError, TypeError):
                return None

        key = (tuple(sorted((view, path, mtime(path)) for view, path in views.items())), ui.isDark())
        if key == self._thumbnailKey:
            return
        self._thumbnailKey = key
        path = views.get("axial")
        pixmap = qt.QPixmap(path) if path else qt.QPixmap()
        if pixmap.isNull():
            self.thumbnail.setPixmap(ui.pixmap("brain", 40, "#55555c"))
        else:
            width, height = self.THUMBNAIL_SIZE
            self.thumbnail.setPixmap(pixmap.scaled(width, height, qt.Qt.KeepAspectRatio, qt.Qt.SmoothTransformation))
        for view, label in self.sideThumbnails.items():
            sidePixmap = qt.QPixmap(views[view]) if views.get(view) else qt.QPixmap()
            label.setVisible(not sidePixmap.isNull())
            if not sidePixmap.isNull():
                width, height = self.SIDE_THUMBNAIL_SIZE
                label.setPixmap(sidePixmap.scaled(width, height, qt.Qt.KeepAspectRatio, qt.Qt.SmoothTransformation))

    def _onPalette(self):
        if not _isAlive(self.widget):
            ui.removePaletteListener(self._onPalette)
            return
        self._thumbnailKey = None
        self._showProject()
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
            font.setWeight(qt.QFont.Medium)
            name.setFont(font)
            folder = _elidedLabel("", 12, "StudioCurrentWorkScanFolder")
            rowLayout.addWidget(name)
            rowLayout.addWidget(folder)
            self.rowsLayout.addWidget(row)
            self._scanRows.append((scan.GetID(), folder))
            for lesion in model.lesionsOf.get(scan.GetID(), []):
                self._addBlock(lesion, 0)

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
            segmentation = lesion.GetSegmentation()
            segment = segmentation.GetNthSegment(0) if segmentation.GetNumberOfSegments() else None
            color = qt.QColor.fromRgbF(*segment.GetColor()[:3]).name() if segment is not None else ui.token("lesion").name()
            swatchStyle = f"QLabel {{ background-color: {color}; border: none; border-radius: 2px; }}"
            if block.swatch.styleSheet != swatchStyle:
                block.swatch.setStyleSheet(swatchStyle)
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
            if block.stateLabel.property("saveState") != state:
                # The chip color follows the state (the card's stylesheet)
                block.stateLabel.setProperty("saveState", state)
                block.stateLabel.style().unpolish(block.stateLabel)
                block.stateLabel.style().polish(block.stateLabel)
            block.stateLabel.setToolTip(" · ".join(part for part in (text, _draw().saveStateText(lesion)[1] or "") if part))
            # The chip says the state; the full text (file, time) is its tooltip
            chipText = text.split(" · ")[0] if text else ""
            block.stateLabel.setText(html.escape(chipText))
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
        # In a project (A-09) closing saves the project first: nothing is unsaved
        unsaved = [] if _projectService() is not None else [lesion for lesion in model.lesions if isLesionUnsaved(lesion)]
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
    current one marked. Buttons: Review in Draw, Close set and (with the study page) Create study.
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
            self.createStudyButton = _roleButton(_("Create study"), "StudioLoadedSetCreateStudyButton", self._createStudy, "secondary")
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
        """Create a study of the files of the set; the set's nodes are closed (only the current subject stays in the scene)."""
        studyUi = _studyUi()
        loaded = self._loaded()
        if studyUi is None or loaded is None:
            return None
        files = [nodeFile(node) for node in loaded.nodes() if nodeFile(node)]
        problem = studyUi.studyFolderProblem(studyUi._commonFolder(files)) if files else None
        if problem:
            # Checked before the set is closed: Studio's own folders (a project's Images/, ...) never become a study
            shell = _shell()
            if shell is not None:
                shell.toast(problem, ms=8000, level="warning")
            return None
        if not closeLoadedSet(confirm=True):
            return None
        return _runAction(lambda: studyUi.createStudy(files, _shell()), _("Failed to create the study."))

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

OPEN_NOW_ROW_HEIGHT = 32


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

    Each row has the kind in text2 and a ⋯ menu: Rename…, Open in Draw, Export as NIfTI…, Show in Data module and Close. A lesion also has an
    eye that shows or hides it in the views (its display node). A volume has no eye: the display node of a volume does not
    decide what the slice views show. "Close" is the only removal verb of Studio; "Delete" stays the word of the Data module.
    """

    def __init__(self):
        qt.QObject.__init__(self)
        # A section of the inspector (VISUAL 4.2): the label, then 32 px rows on the panel
        self.widget = qt.QFrame()
        self.widget.setObjectName("StudioOpenNowCard")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(ui.INSPECTOR_PADDING - 4, 4, ui.INSPECTOR_PADDING - 4, 4)
        layout.setSpacing(6)
        title = ui.sectionLabel(_("Open now"), "StudioCardTitle")
        title.setContentsMargins(4, 0, 0, 0)
        layout.addWidget(title)
        self.rowsLayout = qt.QVBoxLayout()
        self.rowsLayout.setSpacing(2)
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
            "QFrame#StudioOpenNowCard { background: transparent; border: none; }"
            f"QLabel#StudioOpenNowKind, QLabel#StudioOpenNowEmpty {{ color: {ui.token('text3').name()}; }}"
            f"QLabel#StudioOpenNowName {{ color: {ui.token('text').name()}; }}"
            "QWidget#StudioOpenNowRow { background: transparent; border-radius: 7px; }"
            f"QWidget#StudioOpenNowRow:hover {{ background-color: {ui.token('card').name()}; }}")

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
        row.setAttribute(qt.Qt.WA_StyledBackground, True)
        row.setAttribute(qt.Qt.WA_Hover, True)
        row.setFixedHeight(OPEN_NOW_ROW_HEIGHT)
        layout = qt.QHBoxLayout(row)
        layout.setContentsMargins(8, 0, 2, 0)
        layout.setSpacing(8)
        # What it is: the lesion's color, or the scan icon
        marker = qt.QLabel()
        marker.setObjectName("StudioOpenNowMarker")
        marker.setFixedSize(16, 16)
        marker.setAlignment(qt.Qt.AlignCenter)
        node = item["node"]
        if item["kind"] in ("lesion", "set") and node is not None and node.IsA("vtkMRMLSegmentationNode"):
            segmentation = node.GetSegmentation()
            segment = segmentation.GetNthSegment(0) if segmentation.GetNumberOfSegments() else None
            color = qt.QColor.fromRgbF(*segment.GetColor()[:3]) if segment is not None else ui.token("lesion")
            swatch = qt.QPixmap(10, 10)
            swatch.fill(color)
            marker.setPixmap(swatch)
        else:
            marker.setPixmap(ui.pixmap("scan", 16, "text3"))
        layout.addWidget(marker)
        name = _elidedLabel(item["name"], 13, "StudioOpenNowName")
        layout.addWidget(name, 1)
        kind = qt.QLabel(item["kindText"])
        kind.setObjectName("StudioOpenNowKind")
        _setFont(kind, 12)
        layout.addWidget(kind)
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
        # The row keeps the node ID, not the node: a removed scan or a closed scene is freed even while the row lives
        self._rows.append((row, eye, {key: value for key, value in item.items() if key != "node"}))

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
        self._addExportAction(menu, item, node)
        addAction(_("Show in Data module"), "StudioOpenNowShowInData", lambda: showInDataModule(node()))
        if kind == "set":
            addAction(_("Close set"), "StudioOpenNowClose", lambda: closeLoadedSet())  # noqa: PLW0108
        else:
            addAction(_("Close"), "StudioOpenNowClose", lambda: closeNode(node()))

    @staticmethod
    def _addExportAction(menu, item, node):
        """"Export as NIfTI…" (A-11): the dialog lists every image with this one checked (others can be checked too); a loaded
        set checks all its lesions. Disabled, with the reason as tooltip, for what NIfTI cannot hold (a figure).
        """
        try:
            from . import niftiexport, niftiexport_ui
        except ImportError:
            return
        isSet = item["kind"] == "set"  # the closures below do not keep the item (it holds the node)
        if isSet:
            importer = _importer()
            loaded = importer.loadedSet()

            def nodes():
                return loaded.nodes() if loaded is not None else []
        else:

            def nodes():
                return [node()]
        menu.setToolTipsVisible(True)
        action = menu.addAction(_("Export as NIfTI…"))
        action.setObjectName("StudioOpenNowExportNifti")
        action.connect("triggered()", lambda: niftiexport_ui.exportNodesDialog(nodes(), _shell()))

        def update():
            # A lesion that was empty when the row was made may have voxels now: checked each time the menu opens
            reason = niftiexport.unavailableReason(node()) if not isSet else ""
            action.setEnabled(not reason)
            action.setToolTip(reason or _("Write it as a NIfTI file (check others to export them at once) for FSL, SPM, AFNI or nibabel"))

        update()
        menu.connect("aboutToShow()", update)

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
# Studies (W3-07): the libcard registry, the subject list and the inspector cards
#

#: Rows of the subject list and the height of its filter row (SPEC 3.1)
STUDY_ROW_HEIGHT = 32
FILTER_ROW_HEIGHT = 44
#: The study page of the canvas is at most this wide (the list has more columns than the 480 px column)
STUDY_PAGE_MAX_WIDTH = 1100
#: Prefix of the Library extension modules (LNM addendum 6.2): StudioLib/libcard_<name>.py
LIBCARD_PREFIX = "libcard_"
#: Columns of the subject list: (row key, header)
STUDY_COLUMNS = (("participant_id", "ID"), ("group", "Group"), ("volume_ml", "Lesion mL"), ("space", "Space"), ("status", "Status"), ("qc", "QC"))
#: Sort orders of the list: (key, label)
STUDY_SORTS = (("table", "Table order"), ("id", "ID"), ("volume", "Lesion mL"), ("group", "Group"), ("status", "Status"), ("qc", "QC"))
#: Keys of the list (scoped to the list: they never reach the views), for the shortcut list: (key, description)
STUDY_LIST_KEYS = (
    ("Return", "Open the subject in Draw"),
    ("P", "QC pass"),
    ("X", "QC fail"),
    ("U", "QC not reviewed"),
    ("Delete", "Remove from the study (files are kept; Undo in the message)"),
    ("Ctrl+A", "Select all subjects"),
    ("/", "Search the subjects"),
)


def _studyUi():
    """``StudioLib.study_ui`` (W3-07) when this build has it and the study library is importable, else None."""
    import importlib

    try:
        module = importlib.import_module("StudioLib.study_ui")
    except ModuleNotFoundError as e:
        if e.name != "StudioLib.study_ui":
            raise
        return None
    return module if module.isAvailable() else None


def _studySession():
    studyUi = _studyUi()
    return studyUi.session() if studyUi is not None else None


def _studyModule():
    import importlib

    return importlib.import_module("LesionNetworkMappingLib.study")


#
# Library extension registry (StudioLib/libcard_*.py)
#

_libcards = None


def libcardModules(reload=False):
    """The Library extension modules ``StudioLib/libcard_*.py``, imported once (sorted by name).

    Each may expose ``CARD`` (an inspector card: a dict ``{"section": "study" | "batch" | "import", "order", "create": fn(session)
    -> QWidget, "refresh": fn(widget, session, selectedIds)}`` or a callable ``fn(session) -> QWidget``), ``SUBJECT_SECTION``
    (``{"order", "create": fn(session) -> QWidget, "refresh": fn(widget, session, sid)}``), ``COLUMNS`` (list columns: dicts
    ``{"key", "header", "valueFn": fn(row) -> (text, level)}`` or tuples ``(header, valueFn)``), ``FILTERS`` and ``QUEUES``
    (chips and Done → next queues: dicts ``{"key", "label", "predicate": fn(row) -> bool, "qcColumn": optional}`` or tuples
    ``(label, predicate)``). A module that fails to import is skipped and logged.
    """
    global _libcards
    if _libcards is not None and not reload:
        return _libcards
    import importlib
    import pkgutil
    import sys

    modules = []
    folder = os.path.dirname(os.path.abspath(__file__))
    names = sorted(info.name for info in pkgutil.iter_modules([folder]) if info.name.startswith(LIBCARD_PREFIX))
    names += sorted(name.split(".", 1)[1] for name in sys.modules if name.startswith("StudioLib." + LIBCARD_PREFIX) and name.split(".", 1)[1] not in names)
    for name in names:
        try:
            modules.append(importlib.import_module("StudioLib." + name))
        except Exception:
            logging.error(f"Studio Library: the extension {name} is skipped:\n{traceback.format_exc()}")
    _libcards = modules
    return modules


def _entries(attribute, fields):
    """Normalized entries of a libcard attribute: dicts, or tuples in the order of ``fields``."""
    out = []
    for module in libcardModules():
        for index, entry in enumerate(getattr(module, attribute, None) or ()):
            if isinstance(entry, dict):
                item = dict(entry)
            elif isinstance(entry, (tuple, list)):
                item = dict(zip(fields, entry, strict=False))
            else:
                continue
            item.setdefault("key", f"{module.__name__.rsplit('.', 1)[-1]}.{index}")
            item["module"] = module.__name__
            out.append(item)
    return out


def libcardColumns():
    """List columns of the extensions: ``[{"key", "header", "valueFn"}]``."""
    return [entry for entry in _entries("COLUMNS", ("header", "valueFn")) if callable(entry.get("valueFn"))]


def libcardFilters():
    """Filter chips of the extensions: ``[{"key", "label", "predicate", "qcColumn"}]``."""
    return [entry for entry in _entries("FILTERS", ("label", "predicate", "qcColumn")) if callable(entry.get("predicate"))]


def libcardQueues():
    """Done → next queues of the extensions: ``[{"key", "label", "predicate"}]`` (``StudySession.next(queue=key)``)."""
    return [entry for entry in _entries("QUEUES", ("label", "predicate")) if callable(entry.get("predicate"))]


def libcardCards(section):
    """Inspector cards of the extensions for a section ("study", "batch" or "import"), by their order."""
    cards = []
    for module in libcardModules():
        card = getattr(module, "CARD", None)
        if card is None:
            continue
        entry = dict(card) if isinstance(card, dict) else {"create": card} if callable(card) else None
        if entry is None or not callable(entry.get("create")):
            continue
        entry.setdefault("section", "study")
        entry.setdefault("order", 50)
        entry["key"] = module.__name__
        if str(entry["section"]).lower() == section:
            cards.append(entry)
    return sorted(cards, key=lambda entry: entry["order"])


def libcardSubjectSections():
    """SUBJECT card sections of the extensions, by their order."""
    sections = []
    for module in libcardModules():
        section = getattr(module, "SUBJECT_SECTION", None)
        entry = dict(section) if isinstance(section, dict) else {"create": section} if callable(section) else None
        if entry is None or not callable(entry.get("create")):
            continue
        entry.setdefault("order", 50)
        entry["key"] = module.__name__
        sections.append(entry)
    return sorted(sections, key=lambda entry: entry["order"])


#
# Rows as the list shows them
#


def _missing(value):
    return value is None or value == "" or value == "n/a"


def spaceText(row):
    """The space cell: "NLin6Asym 2 mm"... ("Native" for scan space; "MNI152NLin2009cAsym · normalized" for normalized rows)."""
    value = row.get("space")
    if _missing(value):
        return "—", "neutral"
    if not _missing(row.get("norm_method")):
        return _("{space} · normalized").format(space=value), "neutral"
    if value == "native":
        return _("Native"), "muted"
    return str(value).replace("MNI152", ""), "neutral"


def studyCell(row, key):
    """``(text, level)`` of a base column of the list."""
    value = row.get(key)
    if key == "participant_id":
        return str(value), "neutral"
    if key == "group":
        return {"case": _("Case"), "control": _("Control")}.get(value, "—"), "neutral"
    if key == "volume_ml":
        if _missing(value):
            return "—", "neutral"
        try:
            return f"{float(value):.2f}", "neutral"
        except ValueError:
            return str(value), "neutral"
    if key == "space":
        return spaceText(row)
    if key == "status":
        return {"untraced": (_("Untraced"), "muted"), "inprogress": (_("In progress"), "neutral"), "traced": (_("Traced"), "neutral"),
                "mapped": (_("Mapped"), "ok")}.get(value, (str(value), "neutral"))
    if key == "qc":
        return {"pass": (_("Pass"), "ok"), "fail": (_("Fail"), "danger")}.get(value, ("—", "muted"))
    return ("—" if _missing(value) else str(value)), "neutral"


def _levelColor(level):
    token = {"ok": "success", "warn": "warning", "danger": "danger", "muted": "text3"}.get(level)
    return ui.token(token) if token else None


def baseFilters():
    """The chips of the list: [(key, label, predicate)]."""
    return [
        ("all", _("All"), None),
        ("cases", _("Cases"), lambda row: row.get("group") == "case"),
        ("controls", _("Controls"), lambda row: row.get("group") == "control"),
        ("untraced", _("Untraced"), lambda row: row.get("status") == "untraced"),
        ("qcfail", _("QC fail"), lambda row: row.get("qc") == "fail"),
    ]


def _sortKey(sortKey):
    def number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("-inf")

    order = {"untraced": 0, "inprogress": 1, "traced": 2, "mapped": 3}
    qcOrder = {"fail": 0, "unreviewed": 1, "pass": 2}
    if sortKey == "id":
        return lambda item: item[1]["participant_id"]
    if sortKey == "volume":
        return lambda item: -number(item[1].get("volume_ml"))
    if sortKey == "group":
        return lambda item: (str(item[1].get("group")), item[0])
    if sortKey == "status":
        return lambda item: (order.get(item[1].get("status"), 9), item[0])
    if sortKey == "qc":
        return lambda item: (qcOrder.get(item[1].get("qc"), 9), item[0])
    return lambda item: item[0]


class StudyTableModel(qt.QAbstractTableModel):
    """The rows of the subject list (ID · Group · Lesion mL · Space · Status · QC, then the extension columns)."""

    def __init__(self):
        qt.QAbstractTableModel.__init__(self)
        self.rows = []
        self.columns = [{"key": key, "header": header, "valueFn": None} for key, header in STUDY_COLUMNS]
        self.current = None
        #: Short headers (the module panel): "mL" for "Lesion mL"
        self.compact = False

    def setColumns(self, extra):
        self.beginResetModel()
        self.columns = [{"key": key, "header": header, "valueFn": None} for key, header in STUDY_COLUMNS] + list(extra)
        self.endResetModel()

    def setRows(self, rows, current=None):
        self.beginResetModel()
        self.rows = list(rows)
        self.current = current
        self.endResetModel()

    def rowCount(self, parent=qt.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=qt.QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def cell(self, row, column):
        entry = self.columns[column]
        if entry.get("valueFn") is None:
            return studyCell(row, entry["key"])
        try:
            value = entry["valueFn"](row)
        except Exception:
            logging.error(f"Studio Library: the column {entry.get('header')} failed:\n{traceback.format_exc()}")
            return "?", "warn"
        if isinstance(value, (tuple, list)):
            return str(value[0]), (value[1] if len(value) > 1 else "neutral")
        return str(value), "neutral"

    def data(self, index, role=qt.Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == qt.Qt.DisplayRole:
            return self.cell(row, index.column())[0]
        if role == qt.Qt.ForegroundRole:
            color = _levelColor(self.cell(row, index.column())[1])
            return qt.QBrush(color) if color is not None else None
        if role == qt.Qt.FontRole and row["participant_id"] == self.current:
            font = qt.QFont()
            font.setWeight(qt.QFont.DemiBold)
            return font
        if role == qt.Qt.ToolTipRole:
            parts = [row["participant_id"]]
            for key in ("scan", "lesion"):
                if not _missing(row.get(key)):
                    parts.append(f"{key}: {row[key]}")
            if not _missing(row.get("notes")):
                parts.append(str(row["notes"]))
            return "\n".join(parts)
        if role == qt.Qt.TextAlignmentRole and self.columns[index.column()]["key"] == "volume_ml":
            return int(qt.Qt.AlignRight | qt.Qt.AlignVCenter)
        if role == qt.Qt.UserRole:
            return row["participant_id"]
        return None

    def headerData(self, section, orientation, role=qt.Qt.DisplayRole):
        if orientation == qt.Qt.Horizontal and role == qt.Qt.DisplayRole and 0 <= section < len(self.columns):
            if self.compact and self.columns[section]["key"] == "volume_ml":
                return _("mL")
            return _(self.columns[section]["header"])
        return None

    def sid(self, row):
        return self.rows[row]["participant_id"] if 0 <= row < len(self.rows) else None

    def rowOf(self, sid):
        return next((index for index, row in enumerate(self.rows) if row["participant_id"] == sid), -1)


#: Selected subjects of the list (shared by the canvas list and the inspector cards)
_studySelection = []
_studyViews = []
_studyListening = [False]


def studySelection():
    """participant_ids selected in the subject list."""
    return list(_studySelection)


def _setStudySelection(sids, source=None):
    if list(sids) == _studySelection:
        return
    _studySelection[:] = list(sids)
    for view in list(_studyViews):
        if view is not source:
            try:
                view.onSelectionChanged()
            except Exception:
                logging.error(f"Studio Library: study selection update failed:\n{traceback.format_exc()}")


def _listenToStudies():
    if _studyListening[0]:
        return
    studyUi = _studyUi()
    if studyUi is not None:
        studyUi.addListener(_onStudyChanged)
        _studyListening[0] = True


def _onStudyChanged(studySession):
    if studySession is None:
        stopThumbnails()
        _studySelection[:] = []
    else:
        _studySelection[:] = [sid for sid in _studySelection if sid in studySession.study]
    refreshStudy()


def refreshStudy():
    """Show the open study (or its absence) in the canvas and in the module panel."""
    for view in list(_studyViews):
        if not _isAlive(view.widget):
            _studyViews.remove(view)
            continue
        try:
            view.refresh()
        except Exception:
            logging.error(f"Studio Library: study view refresh failed:\n{traceback.format_exc()}")
    if _canvas is not None and _isAlive(_canvas.widget):
        _canvas.refreshData()
    shell = _shell()
    if shell is not None and shell.currentWorkspace() == "library":
        # A study shows its cards in the inspector (also when it was opened while Library showed its empty state)
        empty = isEmptyState(shell)
        if _sceneWatcher is not None:
            _sceneWatcher.empty = empty
        shell.setInspectorVisible(not empty)
    panel = _livePanel()
    if panel is not None:
        panel.refresh()


class _ResizeWatcher(qt.QObject):
    """Calls a function when the watched widget is resized."""

    def __init__(self, callback):
        qt.QObject.__init__(self)
        self.callback = callback

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Resize:
            try:
                self.callback()
            except Exception:
                logging.error(f"Studio Library: resize handler failed:\n{traceback.format_exc()}")
        return False


class _ListKeys(qt.QObject):
    """Keys of a subject list that are not shortcuts: Enter on the table opens the subject (Qt's activation differs per platform)."""

    def __init__(self, studyList):
        qt.QObject.__init__(self)
        self.studyList = studyList

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.KeyPress and event.key() in (qt.Qt.Key_Return, qt.Qt.Key_Enter) and not ui.hasKeyModifiers(event):
            self.studyList.openSelected()
            return True
        return False


class StudyList(qt.QObject):
    """The subject list of a study: a 44 px filter row (search, chips, sort) over a table of 32 px rows.

    Keys (scoped to the list): Enter opens in Draw, P / X / U set QC pass / fail / not reviewed, Delete removes from the
    study (files are kept; Undo in the message), Ctrl+A selects all, / goes to the search field. ``compact`` puts the chips
    on a second row (the module panel).
    """

    def __init__(self, compact=False):
        qt.QObject.__init__(self)
        _listenToStudies()
        self.compact = compact
        self.widget = qt.QFrame()
        self.widget.setObjectName("StudioStudyList")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0 if not compact else 6)
        self.filterRow = qt.QWidget()
        self.filterRow.setObjectName("StudioStudyFilterRow")
        rowLayout = qt.QHBoxLayout(self.filterRow)
        rowLayout.setContentsMargins(0, 6, 0, 6)
        rowLayout.setSpacing(6)
        self.search = qt.QLineEdit()
        self.search.setObjectName("StudioStudySearch")
        self.search.setPlaceholderText(_("Search subjects"))
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip(ui.tooltip(_("Search subjects"), "/", _("filter the list by id, group, status or notes")))
        self.search.connect("textChanged(QString)", lambda text: self.refresh())
        self.search.setMinimumWidth(140)
        rowLayout.addWidget(self.search, 1 if compact else 0)
        self.chipGroup = qt.QButtonGroup(self.widget)
        self.chipGroup.setExclusive(True)
        # The chips wrap onto more lines rather than being elided (V-02): ``chipLines`` holds one row layout per line, the
        # first of which is ``chipLayout``
        self.chipLayout = qt.QHBoxLayout()
        self.chipLayout.setSpacing(4)
        # Each line ends with a stretch: the chips stay at its start
        self.chipLayout.addStretch(1)
        self.chipLines = qt.QVBoxLayout()
        self.chipLines.setContentsMargins(0, 0, 0, 0)
        self.chipLines.setSpacing(4)
        self.chipLines.addLayout(self.chipLayout)
        self._chipLineLayouts = [self.chipLayout]
        self.chips = {}
        self._filterKey = "all"
        if compact:
            self.filterRow.setFixedHeight(FILTER_ROW_HEIGHT)
            layout.addWidget(self.filterRow)
            chipRow = qt.QWidget()
            chipRow.setObjectName("StudioStudyChipRow")
            chipRow.setLayout(self.chipLines)
            self.chipLayout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(chipRow)
            self.chipRow = None
            self._compactChipRow = chipRow
        else:
            # The chips stay in the filter row while they fit, else they go to a row of their own below it (_placeChips)
            self.chipRow = qt.QWidget()
            self.chipRow.setObjectName("StudioStudyChipRow")
            chipRowLayout = qt.QHBoxLayout(self.chipRow)
            chipRowLayout.setContentsMargins(0, 0, 0, 0)
            chipRowLayout.addLayout(self.chipLines)
            chipRowLayout.addStretch(1)
            # Ignored: the chips never make the page wider than the canvas (they move below instead)
            self.chipRow.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
            rowLayout.addWidget(self.chipRow, 1)
        self._chipsInRow = not compact
        rowLayout.addStretch(1)
        self.sortLabel = qt.QLabel(_("Sort"))
        self.sortLabel.setObjectName("StudioStudySortLabel")
        _setFont(self.sortLabel, 12)
        rowLayout.addWidget(self.sortLabel)
        self.sortCombo = qt.QComboBox()
        self.sortCombo.setObjectName("StudioStudySort")
        for key, label in STUDY_SORTS:
            self.sortCombo.addItem(_(label), key)
        self.sortCombo.connect("currentIndexChanged(int)", lambda index: self.refresh())
        rowLayout.addWidget(self.sortCombo)
        # Grid / list toggle and card size (W4-02; the canvas list only)
        self.grid = None
        self.viewButtons = {}
        self.sizeSlider = None
        if not compact:
            self._addViewControls(rowLayout)
            self.filterRow.setFixedHeight(FILTER_ROW_HEIGHT)
            layout.addWidget(self.filterRow)
        self.model = StudyTableModel()
        self.model.compact = compact
        self.table = qt.QTableView()
        self.table.setObjectName("StudioStudyTable")
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(qt.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setFocusPolicy(qt.Qt.StrongFocus)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(STUDY_ROW_HEIGHT)
        self.table.verticalHeader().setMinimumSectionSize(STUDY_ROW_HEIGHT)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setHighlightSections(False)
        header.setDefaultAlignment(qt.Qt.AlignLeft | qt.Qt.AlignVCenter)
        header.connect("sectionClicked(int)", self._onHeaderClicked)
        self.table.connect("doubleClicked(QModelIndex)", lambda index: self.openSelected())
        self.table.selectionModel().connect("selectionChanged(QItemSelection,QItemSelection)", self._onTableSelection)
        self._keys = _ListKeys(self)
        _installEventFilter(self.table, self._keys)
        self.table.setContextMenuPolicy(qt.Qt.CustomContextMenu)
        self.table.connect("customContextMenuRequested(QPoint)", lambda point: self._showContextMenu(self.table, point))
        # The list and the grid share the model, the selection, the keys (shortcuts on this stack) and the context menu
        self.views = qt.QStackedWidget()
        self.views.setObjectName("StudioStudyViews")
        self.views.addWidget(self.table)
        layout.addWidget(self.views, 1)
        self.emptyLabel = qt.QLabel()
        self.emptyLabel.setObjectName("StudioStudyEmpty")
        self.emptyLabel.setWordWrap(True)
        _setFont(self.emptyLabel, 12)
        self.emptyLabel.setContentsMargins(4, 8, 4, 8)
        self.emptyLabel.hide()
        layout.addWidget(self.emptyLabel)
        self.shortcuts = []
        self._addShortcut("P", lambda: self.setQc("pass"), "LibraryQcPass", _("Subject list: QC pass"))
        self._addShortcut("X", lambda: self.setQc("fail"), "LibraryQcFail", _("Subject list: QC fail"))
        self._addShortcut("U", lambda: self.setQc("unreviewed"), "LibraryQcClear", _("Subject list: QC not reviewed"))
        self._addShortcut("Delete", self.removeSelected, "LibraryRemoveFromStudy", _("Subject list: remove from the study (files are kept)"))
        if sys.platform == "darwin":
            self._addShortcut("Backspace", self.removeSelected, "LibraryRemoveFromStudyMac", _("Subject list: remove from the study (files are kept)"))
        self._addShortcut("Ctrl+A", self.table.selectAll, "LibrarySelectAllSubjects", _("Subject list: select all"))
        self._addShortcut("/", self.focusSearch, "LibrarySearchSubjects", _("Subject list: search"), parent=self.widget)
        self._addShortcut("Return", self.openSelected, "LibraryOpenSubject", _("Subject list: open in Draw"))
        self._columnSignature = None
        self._signature = None
        self._settingSelection = False
        self.onSelection = None
        ui.bindStyleSheet(self.widget, self._styleSheet)
        _studyViews.append(self)
        if not compact and _savedStudyView() == "grid":
            self.setViewMode("grid")
        # The chips are placed again when the list is resized (also in the narrow inspector, where they wrap)
        self._resizeWatcher = _ResizeWatcher(self._placeChips)
        _installEventFilter(self.widget, self._resizeWatcher)

    def _addShortcut(self, key, callback, name, description, parent=None):
        shortcut = qt.QShortcut(qt.QKeySequence(key), parent or self.views)
        shortcut.setObjectName("Studio" + name + "Shortcut")
        shortcut.setContext(qt.Qt.WidgetWithChildrenShortcut)
        # Listed in the shortcut list (Library section); not a window-wide Studio shortcut, so it never reaches the views
        shortcut.setProperty("studioDescription", description)
        shortcut.connect("activated()", callback)
        self.shortcuts.append(shortcut)
        return shortcut

    @staticmethod
    def _styleSheet():
        t = {name: ui.token(name).name() for name in ("card", "text", "text2", "text3", "hairline", "accent-subtle", "accent-fg", "border")}
        return (
            f"QFrame#StudioStudyList {{ background: transparent; border: none; }}"
            f"QTableView#StudioStudyTable {{ background-color: {t['card']}; border: 1px solid {t['hairline']}; border-radius: 8px; "
            f"color: {t['text']}; selection-background-color: {t['accent-subtle']}; selection-color: {t['text']}; outline: none; }}"
            f"QTableView#StudioStudyTable::item {{ padding: 0px 8px; border-bottom: 1px solid {t['hairline']}; }}"
            f"QTableView#StudioStudyTable::item:focus {{ border: 1px solid {t['accent-fg']}; }}"
            f"QTableView#StudioStudyTable:focus {{ border: 2px solid {t['accent-fg']}; }}"
            f"QHeaderView::section {{ background-color: {t['card']}; color: {t['text2']}; border: none; border-bottom: 1px solid {t['border']}; "
            "padding: 4px 8px; }"
            f"QLabel#StudioStudySortLabel, QLabel#StudioStudyEmpty {{ color: {t['text2']}; }}"
            f"QToolButton[studioChip=\"true\"] {{ border: 1px solid {t['border']}; border-radius: 12px; padding: 2px 10px; color: {t['text2']}; "
            "background: transparent; }"
            f"QToolButton[studioChip=\"true\"]:checked {{ background-color: {t['accent-subtle']}; border-color: {t['accent-fg']}; color: {t['text']}; }}"
            f"QToolButton[studioChip=\"true\"]:focus {{ border: 2px solid {t['accent-fg']}; }}")

    # Filters

    def _filters(self):
        filters = [{"key": key, "label": label, "predicate": predicate, "qcColumn": None} for key, label, predicate in baseFilters()]
        return filters + libcardFilters()

    def _rebuildChips(self):
        filters = self._filters()
        signature = tuple(entry["key"] for entry in filters)
        if signature == tuple(self.chips):
            return
        for button in list(self.chips.values()):
            self.chipGroup.removeButton(button)
            _discard(button)
        self.chips = {}
        for entry in filters:
            button = qt.QToolButton()
            button.setObjectName("StudioStudyFilterChip")
            button.setProperty("studioChip", True)
            button.setText(entry["label"])
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setFocusPolicy(qt.Qt.TabFocus)
            button.setChecked(entry["key"] == self._filterKey)
            button.connect("clicked()", lambda key=entry["key"]: self.setFilter(key))
            self.chipGroup.addButton(button)
            self.chipLayout.insertWidget(max(0, self.chipLayout.count() - 1), button)
            self.chips[entry["key"]] = button
        if self._filterKey not in self.chips:
            self.setFilter("all")
        self._chipFlowWidth = None
        self._placeChips()

    def _flowChips(self, available):
        """Lay the chips out on as many lines as ``available`` px need: a chip is never elided (V-02)."""
        available = int(available)
        if available <= 0 or getattr(self, "_chipFlowWidth", None) == available:
            return
        self._chipFlowWidth = available
        buttons = [button for button in self.chips.values() if _isAlive(button)]
        for line in self._chipLineLayouts:
            for button in buttons:
                line.removeWidget(button)
        spacing = self.chipLayout.spacing
        lines = [[]]
        width = 0
        for button in buttons:
            buttonWidth = button.sizeHint.width()
            if lines[-1] and width + spacing + buttonWidth > available:
                lines.append([])
                width = 0
            width += (spacing if lines[-1] else 0) + buttonWidth
            lines[-1].append(button)
        while len(self._chipLineLayouts) < len(lines):
            line = qt.QHBoxLayout()
            line.setSpacing(spacing)
            line.setContentsMargins(0, 0, 0, 0)
            line.addStretch(1)
            self.chipLines.addLayout(line)
            self._chipLineLayouts.append(line)
        for index, line in enumerate(lines):
            lineLayout = self._chipLineLayouts[index]
            for button in line:
                # Before the line's stretch
                lineLayout.insertWidget(max(0, lineLayout.count() - 1), button)
                button.show()

    def _placeChips(self):
        """Chips in the filter row when the row is wide enough for all of them, else on rows of their own below it."""
        if not _isAlive(self.widget):
            return
        if self.compact or self.chipRow is None:
            self._flowChips(self.widget.width if self.widget.width > 0 else 320)
            return
        rowLayout = self.filterRow.layout()
        others = [self.search, self.sortLabel, self.sortCombo, *self.viewButtons.values()]
        if self.sizeSlider is not None and not self.sizeSlider.isHidden():
            others.append(self.sizeSlider)
        spacing = rowLayout.spacing
        fixed = sum(max(widget.minimumWidth, widget.sizeHint.width()) for widget in others) + spacing * (len(others) + 1)
        buttons = list(self.chips.values())
        chips = sum(button.sizeHint.width() for button in buttons) + self.chipLayout.spacing * max(0, len(buttons) - 1)
        inRow = fixed + chips <= self.widget.width
        self._flowChips(chips + 1 if inRow else self.widget.width)
        if inRow == self._chipsInRow:
            return
        self._chipsInRow = inRow
        listLayout = self.widget.layout()
        if inRow:
            listLayout.removeWidget(self.chipRow)
            rowLayout.insertWidget(1, self.chipRow, 1)
        else:
            rowLayout.removeWidget(self.chipRow)
            listLayout.insertWidget(listLayout.indexOf(self.filterRow) + 1, self.chipRow)
        self.chipRow.show()

    def chipsInFilterRow(self):
        """True while the chips are in the filter row (False: on their own row below it)."""
        return self._chipsInRow

    def setFilter(self, key):
        """Show the rows of a chip ("all", "cases", "controls", "untraced", "qcfail" or an extension filter key)."""
        self._filterKey = key
        button = self.chips.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        self.refresh()

    def filterKey(self):
        return self._filterKey

    def _activeFilter(self):
        return next((entry for entry in self._filters() if entry["key"] == self._filterKey), None)

    def qcColumn(self):
        """The column that P / X / U write: the active filter's ``qcColumn`` (for example norm_qc), else qc."""
        entry = self._activeFilter()
        return (entry or {}).get("qcColumn") or "qc"

    def sortKey(self):
        return str(self.sortCombo.itemData(self.sortCombo.currentIndex) or "table")

    def setSort(self, key):
        index = self.sortCombo.findData(key)
        if index >= 0:
            self.sortCombo.setCurrentIndex(index)

    def _onHeaderClicked(self, section):
        key = self.model.columns[section]["key"] if 0 <= section < len(self.model.columns) else None
        sortKey = {"participant_id": "id", "volume_ml": "volume", "group": "group", "status": "status", "qc": "qc"}.get(key)
        if sortKey is not None:
            self.setSort("table" if self.sortKey() == sortKey else sortKey)

    # Content

    def visibleRows(self, studySession):
        """The rows that the filter, the search and the sort show, in their order."""
        rows = studySession.study.subjects
        entry = self._activeFilter()
        predicate = entry.get("predicate") if entry else None
        query = str(self.search.text).strip().lower()
        shown = []
        for index, row in enumerate(rows):
            if predicate is not None:
                try:
                    if not predicate(row):
                        continue
                except Exception:
                    logging.error(f"Studio Library: the filter {entry.get('label')} failed:\n{traceback.format_exc()}")
                    continue
            if query:
                haystack = " ".join(str(row.get(key, "")) for key in ("participant_id", "group", "status", "qc", "notes", "source_id")).lower()
                if query not in haystack:
                    continue
            shown.append((index, row))
        shown.sort(key=_sortKey(self.sortKey()))
        return [row for _index, row in shown]

    def refresh(self):
        studySession = _studySession()
        if studySession is None:
            self.model.setRows([])
            for shortcut in self.shortcuts:
                shortcut.setEnabled(False)
            return
        for shortcut in self.shortcuts:
            shortcut.setEnabled(True)
        self._rebuildChips()
        extra = libcardColumns()
        signature = tuple(entry["key"] for entry in extra)
        if signature != self._columnSignature:
            self._columnSignature = signature
            self.model.setColumns(extra)
        rows = self.visibleRows(studySession)
        selected = studySelection()
        # A model reset scrolls to the top: keep the place of the user (a QC key, a thumbnail or a save refreshes the rows)
        scrolls = [(view, view.verticalScrollBar().value) for view in self._itemViews()]
        # It also clears the keyboard cursor (the current index): without it the next arrow key starts at the first row
        currentIndex = self.table.currentIndex()
        currentId = self.model.sid(currentIndex.row()) if currentIndex.isValid() else None
        self._settingSelection = True
        try:
            self.model.setRows(rows, studySession.current())
            selectionModel = self.table.selectionModel()
            for sid in selected:
                row = self.model.rowOf(sid)
                if row >= 0:
                    selectionModel.select(self.model.index(row, 0), qt.QItemSelectionModel.Select | qt.QItemSelectionModel.Rows)
            self._restoreCurrent(currentId)
        finally:
            self._settingSelection = False
        self._sizeColumns()
        if self.grid is not None and self.viewMode() == "grid":
            self.grid.prepare(studySession, rows)
        for view, value in scrolls:
            if value:
                view.doItemsLayout()
                view.verticalScrollBar().setValue(value)
        total = len(studySession.study)
        if not rows:
            self.emptyLabel.setText(_("No subject matches. Choose All or clear the search.") if total else
                                    _("This study has no subjects yet. Add files in the Import card, or drop a folder."))
        self.emptyLabel.setVisible(not rows)

    def _sizeColumns(self):
        header = self.table.horizontalHeader()
        widths = {"participant_id": 140, "group": 90, "volume_ml": 90, "space": 170, "status": 100, "qc": 70}
        if self.compact:
            widths = {"participant_id": 96, "group": 64, "volume_ml": 64, "space": 96, "status": 80, "qc": 48}
        for index, entry in enumerate(self.model.columns):
            header.resizeSection(index, widths.get(entry["key"], 110))

    # Selection

    def selectedIds(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [self.model.sid(row) for row in rows if self.model.sid(row)]

    def _onTableSelection(self, selected=None, deselected=None):
        if self._settingSelection:
            return
        _setStudySelection(self.selectedIds(), source=self)
        if self.onSelection is not None:
            self.onSelection()

    def onSelectionChanged(self):
        """The shared selection changed elsewhere: follow it."""
        self._settingSelection = True
        try:
            selectionModel = self.table.selectionModel()
            selectionModel.clearSelection()
            for sid in studySelection():
                row = self.model.rowOf(sid)
                if row >= 0:
                    selectionModel.select(self.model.index(row, 0), qt.QItemSelectionModel.Select | qt.QItemSelectionModel.Rows)
            currentIndex = self.table.currentIndex()
            self._restoreCurrent(self.model.sid(currentIndex.row()) if currentIndex.isValid() else None)
        finally:
            self._settingSelection = False
        if self.onSelection is not None:
            self.onSelection()

    def _restoreCurrent(self, sid):
        """Put the keyboard cursor (the current index, shared by the list and the grid) on ``sid`` when it is selected or
        nothing is, else on the first selected row; the selection is not changed (the arrow keys go on from there).
        """
        selectionModel = self.table.selectionModel()
        selectedRows = sorted({index.row() for index in selectionModel.selectedRows()})
        row = self.model.rowOf(sid) if sid else -1
        if row < 0 or (selectedRows and row not in selectedRows):
            row = selectedRows[0] if selectedRows else -1
        if row < 0:
            return
        current = self.table.currentIndex()
        if current.isValid() and current.row() == row:
            return
        selectionModel.setCurrentIndex(self.model.index(row, max(0, current.column())), qt.QItemSelectionModel.NoUpdate)

    def selectIds(self, sids):
        _setStudySelection(list(sids))
        self.onSelectionChanged()

    def focusSearch(self):
        self.search.setFocus(qt.Qt.ShortcutFocusReason)
        self.search.selectAll()

    def focusTable(self):
        """Focus the subject list, or the grid when it is shown (selecting the current subject when nothing is selected)."""
        view = self.currentView()
        view.setFocus(qt.Qt.OtherFocusReason)
        if not self.table.selectionModel().selectedRows() and self.model.rowCount() > 0:
            studySession = _studySession()
            row = max(0, self.model.rowOf(studySession.current())) if studySession is not None else 0
            self.table.selectRow(row)

    # Grid / list (W4-02)

    def _addViewControls(self, rowLayout):
        """The list / grid toggle and the card size slider (shown in grid mode), at the end of the filter row."""
        group = qt.QButtonGroup(self.widget)
        group.setExclusive(True)
        for mode, iconName, label in (("list", "list", _("List")), ("grid", "layout-grid", _("Grid"))):
            button = qt.QToolButton()
            button.setObjectName("StudioStudyListViewButton" if mode == "list" else "StudioStudyGridViewButton")
            button.setProperty("studioChip", True)
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setChecked(mode == "list")
            button.setFocusPolicy(qt.Qt.TabFocus)
            button.setIcon(ui.icon(iconName, 16))
            button.setToolTip(ui.tooltip(_("Show as a list") if mode == "list" else _("Show as a grid"), None,
                                         _("rows with every column") if mode == "list" else _("cards with an axial view of each lesion")))
            button.setAccessibleName(label)
            button.connect("clicked()", lambda mode=mode: self.setViewMode(mode))
            group.addButton(button)
            rowLayout.addWidget(button)
            self.viewButtons[mode] = button
        self.sizeSlider = qt.QSlider(qt.Qt.Horizontal)
        self.sizeSlider.setObjectName("StudioStudyGridSizeSlider")
        self.sizeSlider.setRange(GRID_SIZE_MIN, GRID_SIZE_MAX)
        self.sizeSlider.setSingleStep(10)
        self.sizeSlider.setPageStep(20)
        self.sizeSlider.setValue(_savedGridSize())
        self.sizeSlider.setFixedWidth(80)
        self.sizeSlider.setToolTip(_("Card size"))
        self.sizeSlider.setAccessibleName(_("Card size"))
        self.sizeSlider.connect("valueChanged(int)", self._onSizeChanged)
        self.sizeSlider.hide()
        rowLayout.addWidget(self.sizeSlider)

    def viewMode(self):
        """Return "grid" when the cards are shown, else "list"."""
        return "grid" if self.grid is not None and self.views.currentWidget() is self.grid.view else "list"

    def currentView(self):
        return self.grid.view if self.viewMode() == "grid" else self.table

    def _itemViews(self):
        return [self.table] + ([self.grid.view] if self.grid is not None else [])

    def setViewMode(self, mode):
        """Show the subjects as a list ("list") or as cards with lesion thumbnails ("grid"; not in the module panel)."""
        if self.compact or mode not in ("list", "grid") or mode == self.viewMode():
            return
        hadFocus = self.currentView().hasFocus()
        if mode == "grid":
            if self.grid is None:
                self.grid = StudyGrid(self)
                self.views.addWidget(self.grid.view)
            self.views.setCurrentWidget(self.grid.view)
            studySession = _studySession()
            if studySession is not None:
                self.grid.prepare(studySession, self.model.rows)
        else:
            self.views.setCurrentWidget(self.table)
        for key, button in self.viewButtons.items():
            if button.isChecked() != (key == mode):
                button.setChecked(key == mode)
        if self.sizeSlider is not None:
            self.sizeSlider.setVisible(mode == "grid")
        self._placeChips()
        current = self.table.currentIndex()
        if current.isValid():
            self.currentView().scrollTo(current)
        if hadFocus:
            self.currentView().setFocus(qt.Qt.OtherFocusReason)
        _saveSetting(STUDY_VIEW_KEY, mode)

    def _onSizeChanged(self, value):
        if self.grid is not None:
            self.grid.setThumbSize(value)
        _saveSetting(GRID_SIZE_KEY, int(value))

    def _showContextMenu(self, view, point):
        """Right-click on a row or a card: the actions of the list keys (shared by the list and the grid)."""
        index = view.indexAt(point)
        if index.isValid() and not self.table.selectionModel().isSelected(index):
            self.selectIds([self.model.sid(index.row())])
        sids = self.selectedIds()
        if not sids:
            return
        menu = qt.QMenu(view)
        menu.setObjectName("StudioStudyContextMenu")
        # The keys are shown after a tab (a hint, not a second shortcut: the list's own shortcuts handle them)
        openAction = menu.addAction(_("Open in Draw") + "\t" + ui.keyText("Return"))
        openAction.setEnabled(len(sids) == 1)
        openAction.connect("triggered()", self.openSelected)
        menu.addSeparator()
        for key, value, label in (("P", "pass", _("QC pass")), ("X", "fail", _("QC fail")), ("U", "unreviewed", _("QC not reviewed"))):
            action = menu.addAction(label + "\t" + key)
            action.connect("triggered()", lambda value=value: self.setQc(value))
        menu.addSeparator()
        removeAction = menu.addAction(_("Remove from study (files are kept)") + "\t" + ui.keyText("Delete"))
        removeAction.connect("triggered()", self.removeSelected)
        _execMenu(menu, view.viewport().mapToGlobal(point))
        menu.deleteLater()

    # Actions

    def openSelected(self):
        """Open the selected (else the current) subject in Draw."""
        studySession = _studySession()
        if studySession is None:
            return False
        sids = self.selectedIds()
        sid = sids[0] if sids else self.model.sid(self.table.currentIndex().row()) if self.table.currentIndex().isValid() else None
        if sid is None:
            return False
        return _runAction(lambda: studySession.open(sid), _("Failed to open {sid}.").format(sid=sid))

    def setQc(self, value):
        """P / X / U on the selected subjects."""
        studySession = _studySession()
        sids = self.selectedIds()
        if studySession is None or not sids:
            return
        column = self.qcColumn()
        studySession.setQc(sids, value, column)
        shell = _shell()
        if shell is not None:
            label = {"pass": _("pass"), "fail": _("fail"), "unreviewed": _("not reviewed")}.get(value, value)
            shell.toast(_("{column} {value}: {n} subjects").format(column=column.upper() if column == "qc" else column, value=label, n=len(sids))
                        if len(sids) > 1 else _("{sid}: {column} {value}").format(sid=sids[0], column=column.upper() if column == "qc" else column, value=label))

    def removeSelected(self):
        """Delete: remove the selected subjects from the study (files are kept; Undo in the message)."""
        studySession = _studySession()
        sids = self.selectedIds()
        if studySession is None or not sids:
            return 0
        count = studySession.removeSubjects(sids)
        _setStudySelection([])
        shell = _shell()
        if shell is not None and count:
            shell.toast(_("Removed {n} from the study (files are kept)").format(n=sids[0] if count == 1 else _("{n} subjects").format(n=count)),
                        action=studySession.undoRemove, actionText=_("Undo"), level="consequence")
        return count

    def dispose(self):
        if self in _studyViews:
            _studyViews.remove(self)
        for shortcut in self.shortcuts:
            with _suppress():
                shortcut.setEnabled(False)
                shortcut.disconnect("activated()")
        self.shortcuts = []
        if self.grid is not None:
            self.grid.dispose()
        if _isAlive(self.widget):
            ui.bindStyleSheet(self.widget, None)


#
# Grid of the study page (W4-02): cards with an axial lesion thumbnail
#

#: Fork-only settings: the subject view of the canvas ("list" or "grid") and the thumbnail size of the cards
STUDY_VIEW_KEY = "Studio/Library/StudyView"
GRID_SIZE_KEY = "Studio/Library/GridThumbSize"
#: Thumbnail side on a card: the slider range and the default (a 168 x 196 card)
GRID_SIZE_MIN = 120
GRID_SIZE_MAX = 220
GRID_SIZE_DEFAULT = 160
#: Card padding, footer (id, QC mark, status dot) and the gap between cards
CARD_PAD = 4
CARD_FOOTER = 32
CARD_GAP = 8
CARD_RADIUS = 8
#: Main-thread budgets (ms): new cards rendered inside one paint event, and per pre-render tick
RENDER_BUDGET_MS = 4.0
PRERENDER_BUDGET_MS = 10.0
#: Colours drawn over a thumbnail (always on the dark thumbnail backdrop, in both themes)
THUMB_BACKDROP = "#0e0e10"
THUMB_CHIP_COLORS = {"ok": "#3fb950", "warn": "#e3b341", "danger": "#f85149", "neutral": "#ececf0", "muted": "#b4b4bc", "busy": "#9ec1ff"}
#: Patient directions drawn at the left and right edges of a thumbnail (radiological, like the Red axial view)
LATERALITY_CUES = ("R", "L")
#: Status dots: grey untraced, blue traced (a ring while in progress), green mapped
STATUS_DOT_TOKENS = {"untraced": "text3", "inprogress": "accent-fg", "traced": "accent-fg", "mapped": "success"}


def _readSetting(key, default):
    try:
        return qt.QSettings().value(key, default)
    except Exception:
        return default


def _saveSetting(key, value):
    if slicer.app.testingEnabled():
        return  # tests never change the saved choice of the user
    try:
        qt.QSettings().setValue(key, value)
    except Exception:
        logging.warning(f"Studio Library: could not save {key}")


def _savedStudyView():
    value = str(_readSetting(STUDY_VIEW_KEY, "list") or "list")
    return value if value in ("list", "grid") and not slicer.app.testingEnabled() else "list"


def _savedGridSize():
    try:
        value = int(_readSetting(GRID_SIZE_KEY, GRID_SIZE_DEFAULT))
    except (TypeError, ValueError):
        value = GRID_SIZE_DEFAULT
    return min(GRID_SIZE_MAX, max(GRID_SIZE_MIN, value))


def _thumbnailsModule():
    from . import thumbnails

    return thumbnails


#: The thumbnail service of the open study: {"root": study folder, "service": ThumbnailService, "template": file}
_thumbs = {"root": None, "service": None, "template": None}


def thumbnailService(studySession=None, create=True):
    """The ``thumbnails.ThumbnailService`` of the open study (one worker thread per study), else None.

    Opening another study, or closing it, stops the previous worker.
    """
    root = studySession.root if studySession is not None else None
    if _thumbs["service"] is not None and _thumbs["root"] != root:
        _thumbs["service"].stop()
        _thumbs.update(root=None, service=None, template=None)
    if root is None or not create:
        return _thumbs["service"]
    if _thumbs["service"] is None:
        thumbnails = _thumbnailsModule()
        _thumbs.update(root=root, service=thumbnails.ThumbnailService(root), template=_studyTemplateFile(studySession))
    return _thumbs["service"]


def stopThumbnails():
    """Stop the thumbnail worker (the study closed)."""
    thumbnailService(None)


def _studyTemplateFile(studySession):
    """The study template's NIfTI file (the thumbnails' background), else None."""
    studyUi = _studyUi()
    try:
        record = studySession.templateRecord() or {}
    except Exception:
        return None
    source = str(record.get("source") or "")
    if not source.startswith("sampledata:"):
        path = studySession.study.abspath(source) if source else None
        return path if path and os.path.isfile(path) else None
    if studyUi is None:
        return None
    path = studyUi.sampleFile(source[len("sampledata:"):])
    return path or studyUi.sampleFile(studyUi.DEFAULT_TEMPLATE_SAMPLE)


def _hiddenColumns(studyObject):
    """Columns hidden from tracers when the study sets ``blind_tracing``: group, scores and symptoms (LNM addendum 7.1)."""
    if not getattr(studyObject, "blind_tracing", False):
        return set()
    hidden = {"group"}
    try:
        hidden.update(name for name, _confirmed in studyObject.score_columns())
    except Exception:
        pass
    try:
        hidden.update(studyObject.group_columns())
    except Exception:
        pass
    return hidden


class _CardInfo:
    """What a card shows, computed once per refresh (painting reads it): id, status, QC, chips and the thumbnail request."""

    __slots__ = ("bottomChips", "current", "note", "qc", "request", "sid", "signature", "status", "thumbKey", "topChips")

    def __init__(self, row, studySession, extraColumns, hidden, service, template):
        self.sid = row["participant_id"]
        self.status = row.get("status") or "untraced"
        self.qc = row.get("qc") or "unreviewed"
        self.current = self.sid == studySession.current()
        spaceValue = row.get("space")
        bottom = []
        if spaceValue == "native":
            bottom.append((_("Native"), "warn"))
        elif not _missing(spaceValue):
            bottom.append(("MNI", "neutral"))
        volume = row.get("volume_ml")
        if not _missing(volume):
            try:
                bottom.append((_("{ml} mL").format(ml=f"{float(volume):.2f}"), "neutral"))
            except ValueError:
                pass
        self.bottomChips = tuple(bottom)
        top = []
        for entry in extraColumns:
            if entry.get("key") in hidden:
                continue
            try:
                value = entry["valueFn"](row)
            except Exception:
                logging.error(f"Studio Library: the column {entry.get('header')} failed:\n{traceback.format_exc()}")
                continue
            text, level = (str(value[0]), value[1] if len(value) > 1 else "neutral") if isinstance(value, (tuple, list)) else (str(value), "neutral")
            if _missing(text) or text == "—" or level == "muted":
                continue
            top.append((f"{_(entry.get('header') or '')}: {text}" if entry.get("key") == "norm" else text, level))
        self.topChips = tuple(top)
        # The thumbnail: participant id and file paths only (never a hidden column: blind tracing)
        studyObject = studySession.study
        lesion = None if _missing(row.get("lesion")) else studyObject.abspath(row.get("lesion"))
        scan = None if _missing(row.get("scan")) else studyObject.abspath(row.get("scan"))
        normalized = not _missing(row.get("norm_method"))
        if normalized and scan:
            background = scan  # the template-space scan of a normalized row (A-03c)
        elif spaceValue == "native" or (_missing(spaceValue) and scan):
            background = scan
        else:
            background = template
        self.request = None
        hasLesion = bool(lesion) and os.path.isfile(lesion)
        lesionMissing = bool(lesion) and not hasLesion
        if service is not None and not lesionMissing:
            sid = self.sid if hasLesion or background != template else None
            self.request = service.requestFor(sid, lesion if hasLesion else None, background)
        self.thumbKey = self.request.key if self.request is not None else None
        if lesionMissing:
            self.note = _("Lesion file missing")  # never a background alone, which would read as an empty lesion
        else:
            self.note = _("No lesion yet") if self.status == "untraced" else _("No preview")
        self.signature = (self.status, self.qc, self.current, self.bottomChips, self.topChips, self.thumbKey)


class _GridView(qt.QListView):
    """The card grid (a ``QListView`` in icon mode over the list's model).

    Each paint event is timed: ``paintMs`` (the whole event) and ``delegateMs`` (the time spent in the card delegate).
    """

    def __init__(self):
        qt.QListView.__init__(self)
        self.paintMs = []
        self.delegateMs = []
        self.delegateS = 0.0
        self.paintCount = 0
        self.paintStart = 0.0

    def paintEvent(self, event):
        start = self.paintStart = time.perf_counter()
        self.delegateS = 0.0
        qt.QListView.paintEvent(self, event)
        self.paintCount += 1
        if len(self.paintMs) >= 4000:
            del self.paintMs[:2000]
            del self.delegateMs[:2000]
        self.paintMs.append((time.perf_counter() - start) * 1000.0)
        self.delegateMs.append(self.delegateS * 1000.0)


class _CardDelegate(qt.QStyledItemDelegate):
    """Paints a card through the grid (cached per card state) and gives the card size."""

    def __init__(self, grid):
        qt.QStyledItemDelegate.__init__(self, grid.view)
        self.grid = grid

    def paint(self, painter, option, index):
        start = time.perf_counter()
        try:
            self.grid.paintCard(painter, option, index)
        except Exception:
            logging.error(f"Studio Library: painting a card failed:\n{traceback.format_exc()}")
        self.grid.view.delegateS += time.perf_counter() - start

    def sizeHint(self, option, index):
        return self.grid.cardSize()


class _GridHover(qt.QObject):
    """Clears the hovered card when the mouse leaves the grid."""

    def __init__(self, grid):
        qt.QObject.__init__(self)
        self.grid = grid

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Leave:
            self.grid.setHoverRow(-1)
        return False


class StudyGrid(qt.QObject):
    """Cards of the study's subjects (SPEC 3.1, W4-02): an axial lesion MIP over the template (``thumbnails``), the id,
    the space and volume chips, the extension columns' chips (Flags, Norm, Review...), the QC mark and a status dot.

    It shares the subject list's model, selection, keys and context menu; only the cards on screen are painted, and
    each card is drawn once per state into a cached pixmap.
    """

    def __init__(self, studyList):
        qt.QObject.__init__(self)
        self.list = studyList
        self.thumbSize = _savedGridSize()
        self.infos = {}
        self.hoverRow = -1
        self._cards = {}
        self._cardKeys = {}
        self._colors = None
        self._service = None
        #: Card render times (ms), for the verification
        self.renderMs = []
        self._paintContext_ = (None,)
        self._prerenderTimer = qt.QTimer()
        self._prerenderTimer.setSingleShot(True)
        self._prerenderTimer.setInterval(0)
        self._prerenderTimer.connect("timeout()", self._prerender)
        view = _GridView()
        view.setObjectName("StudioStudyGrid")
        view.setModel(studyList.model)
        view.setModelColumn(0)
        view.setSelectionModel(studyList.table.selectionModel())
        view.setViewMode(qt.QListView.IconMode)
        view.setMovement(qt.QListView.Static)
        view.setResizeMode(qt.QListView.Adjust)
        view.setFlow(qt.QListView.LeftToRight)
        view.setWrapping(True)
        view.setUniformItemSizes(True)
        view.setSpacing(0)
        view.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        view.setSelectionMode(qt.QAbstractItemView.ExtendedSelection)
        view.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        view.setVerticalScrollMode(qt.QAbstractItemView.ScrollPerPixel)
        view.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        view.setFocusPolicy(qt.Qt.StrongFocus)
        view.setMouseTracking(True)
        view.setFrameShape(qt.QFrame.NoFrame)
        view.setAccessibleName(_("Subjects (grid)"))
        self.view = view
        self.delegate = _CardDelegate(self)
        view.setItemDelegate(self.delegate)
        self._applySize()
        view.connect("doubleClicked(QModelIndex)", lambda index: studyList.openSelected())
        view.connect("entered(QModelIndex)", lambda index: self.setHoverRow(index.row()))
        _installEventFilter(view, studyList._keys)
        self._hover = _GridHover(self)
        _installEventFilter(view.viewport(), self._hover)
        view.setContextMenuPolicy(qt.Qt.CustomContextMenu)
        view.connect("customContextMenuRequested(QPoint)", lambda point: studyList._showContextMenu(view, point))
        view.verticalScrollBar().setSingleStep(24)
        ui.addPaletteListener(self._onPalette)

    # Geometry

    def cardSize(self):
        """168 x 196 for a 160 px thumbnail."""
        return qt.QSize(self.thumbSize + 2 * CARD_PAD, self.thumbSize + CARD_PAD + CARD_FOOTER)

    def _applySize(self):
        size = self.cardSize()
        self.view.setGridSize(qt.QSize(size.width() + CARD_GAP, size.height() + CARD_GAP))
        self.view.verticalScrollBar().setSingleStep(max(24, size.height() // 4))

    def setThumbSize(self, value):
        """Card size from the slider (120-220 px thumbnails); the selection and the current card stay in view."""
        value = min(GRID_SIZE_MAX, max(GRID_SIZE_MIN, int(value)))
        if value == self.thumbSize:
            return
        self.thumbSize = value
        self._cards.clear()
        self._cardKeys.clear()
        self._applySize()
        self.view.doItemsLayout()
        current = self.view.currentIndex()
        if current.isValid():
            self.view.scrollTo(current)
        self.view.viewport().update()

    # Content

    def prepare(self, studySession, rows):
        """Compute the cards of the rows (a refresh) and queue their thumbnails in the list order."""
        service = thumbnailService(studySession)
        if service is not self._service:
            if self._service is not None and self._onThumbnails in self._service.listeners:
                self._service.listeners.remove(self._onThumbnails)
            self._service = service
            self._cards.clear()
            self._cardKeys.clear()
            if service is not None:
                service.listeners.append(self._onThumbnails)
        # Re-read on every refresh: switching the study template changes the background (and the thumbnail names)
        template = _studyTemplateFile(studySession) if service is not None else None
        _thumbs["template"] = template
        extra = libcardColumns()
        hidden = _hiddenColumns(studySession.study)
        infos = {}
        for row in rows:
            try:
                infos[row["participant_id"]] = _CardInfo(row, studySession, extra, hidden, service, template)
            except Exception:
                logging.error(f"Studio Library: card of {row.get('participant_id')} failed:\n{traceback.format_exc()}")
        self.infos = infos
        stale = [sid for sid in self._cardKeys if sid not in infos]
        for sid in stale:
            self._cards.pop(self._cardKeys.pop(sid), None)
        if service is not None:
            for info in infos.values():
                service.request(info.request)
        self.view.viewport().update()

    def _onThumbnails(self, keys):
        """Thumbnails arrived (main thread): repaint the cards on screen that show them."""
        if not _isAlive(self.view) or not self.view.isVisible():
            return
        keys = set(keys)
        viewport = self.view.viewport()
        visible = viewport.rect
        for row, entry in enumerate(self.list.model.rows):
            info = self.infos.get(entry["participant_id"])
            if info is None or info.thumbKey not in keys:
                continue
            rect = self.view.visualRect(self.list.model.index(row, 0))
            if rect.intersects(visible):
                viewport.update(rect)
        self._schedulePrerender()

    def _schedulePrerender(self):
        if not self._prerenderTimer.isActive():
            self._prerenderTimer.start()

    def _prerender(self):
        """Render the cards on screen and one screen ahead and behind (main thread, about 10 ms per tick)."""
        if not _isAlive(self.view) or not self.view.isVisible():
            return
        start = time.perf_counter()
        viewport = self.view.viewport()
        visible = viewport.rect
        ahead = visible.adjusted(0, -visible.height(), 0, visible.height())
        pending = False
        for row, entry in enumerate(self.list.model.rows):
            rect = self.view.visualRect(self.list.model.index(row, 0))
            if not rect.intersects(ahead):
                continue
            sid = entry["participant_id"]
            info = self.infos.get(sid)
            if info is None or self._card(sid, info, False) is not None:
                continue
            if (time.perf_counter() - start) * 1000.0 > PRERENDER_BUDGET_MS:
                pending = True
                break
            self._card(sid, info, True)
            if rect.intersects(visible):
                viewport.update(rect)
        if pending:
            self._prerenderTimer.start()

    def setHoverRow(self, row):
        if row == self.hoverRow:
            return
        viewport = self.view.viewport()
        for old in (self.hoverRow, row):
            if old >= 0:
                viewport.update(self.view.visualRect(self.list.model.index(old, 0)))
        self.hoverRow = row

    def _onPalette(self):
        self._colors = None
        self._cards.clear()
        self._cardKeys.clear()
        if _isAlive(self.view):
            self.view.viewport().update()

    def colors(self):
        if self._colors is None:
            names = ("card", "hairline", "text", "text2", "text3", "accent-fg", "success", "warning", "danger", "control-border")
            self._colors = {name: ui.token(name) for name in names}
            self._colors["backdrop"] = qt.QColor(THUMB_BACKDROP)
        return self._colors

    # Painting

    def _card(self, sid, info, render, front=False, dpr=None):
        """The cached card pixmap of a subject in its current state; rendered when ``render`` (else None if not cached)."""
        service = self._service
        pixmap = service.pixmap(info.thumbKey) if service is not None and info.thumbKey else None
        if pixmap is None and service is not None and info.request is not None:
            service.request(info.request, front=front)
        state = "ready" if pixmap is not None else "failed" if (service is None or info.request is None or service.failed(info.thumbKey)) else "pending"
        if dpr is None:
            dpr = float(self.view.devicePixelRatioF()) if hasattr(self.view, "devicePixelRatioF") else 1.0
        key = (sid, info.signature, self.thumbSize, state, dpr)
        card = self._cards.get(key)
        if card is None and render:
            start = time.perf_counter()
            card = self._renderCard(info, pixmap, state, dpr)
            self.renderMs.append((time.perf_counter() - start) * 1000.0)
            if len(self.renderMs) > 4000:
                del self.renderMs[:2000]
            previous = self._cardKeys.get(sid)
            if previous is not None:
                self._cards.pop(previous, None)
            self._cards[key] = card
            self._cardKeys[sid] = key
        return card

    def _paintContext(self):
        """What the cards of one paint event share: device pixel ratio, card size, selected subjects and the focused row."""
        if self._paintContext_[0] != self.view.paintStart:
            view = self.view
            dpr = float(view.devicePixelRatioF()) if hasattr(view, "devicePixelRatioF") else 1.0
            size = self.cardSize()
            selected = set(_studySelection)  # the shared selection (kept equal to the views' selection model)
            focusedRow = view.currentIndex().row() if view.hasFocus() else -2
            self._paintContext_ = (view.paintStart, dpr, size.width(), size.height(), selected, focusedRow)
        return self._paintContext_

    def paintCard(self, painter, option, index):
        row = index.row()
        sid = self.list.model.sid(row)
        info = self.infos.get(sid)
        if info is None:
            return
        _stamp, dpr, width, height, selectedIds, focusedRow = self._paintContext()
        # A paint event renders new cards only within its budget; the others show their previous state (or a blank card)
        # and are rendered right after by the pre-renderer, so scrolling never waits for many new cards at once
        withinBudget = (time.perf_counter() - self.view.paintStart) * 1000.0 < RENDER_BUDGET_MS
        card = self._card(sid, info, withinBudget, front=True, dpr=dpr)
        if card is None:
            previous = self._cardKeys.get(sid)
            card = self._cards.get(previous) if previous is not None else None
            self._schedulePrerender()
        rect = option.rect
        x = rect.x() + (rect.width() - width) // 2
        y = rect.y() + (rect.height() - height) // 2
        size = qt.QSize(width, height)
        if card is not None:
            painter.drawPixmap(x, y, card)
        else:
            colors = self.colors()
            painter.save()
            painter.setRenderHint(qt.QPainter.Antialiasing, True)
            painter.setPen(qt.QPen(colors["hairline"], 1))
            painter.setBrush(qt.QBrush(colors["card"]))
            painter.drawRoundedRect(qt.QRectF(x + 0.5, y + 0.5, size.width() - 1, size.height() - 1), CARD_RADIUS, CARD_RADIUS)
            painter.restore()
        selected = sid in selectedIds
        focused = row == focusedRow
        if selected or focused or row == self.hoverRow:
            colors = self.colors()
            painter.save()
            painter.setRenderHint(qt.QPainter.Antialiasing, True)
            painter.setBrush(qt.QBrush(qt.Qt.NoBrush))
            frame = qt.QRectF(x + 1, y + 1, size.width() - 2, size.height() - 2)
            if selected:
                painter.setPen(qt.QPen(colors["accent-fg"], 2))
                painter.drawRoundedRect(frame, CARD_RADIUS, CARD_RADIUS)
            elif row == self.hoverRow:
                painter.setPen(qt.QPen(colors["control-border"], 1))
                painter.drawRoundedRect(frame, CARD_RADIUS, CARD_RADIUS)
            if focused:
                pen = qt.QPen(colors["accent-fg"], 1)
                pen.setStyle(qt.Qt.DashLine)
                painter.setPen(pen)
                painter.drawRoundedRect(frame.adjusted(3, 3, -3, -3), CARD_RADIUS - 2, CARD_RADIUS - 2)
            painter.restore()

    def _font(self, pixelSize, weight=None):
        font = qt.QFont(self.view.font)
        font.setPixelSize(ui.fontPx(pixelSize))
        if weight is not None:
            font.setWeight(weight)
        return font

    def _chip(self, painter, x, y, text, level, maxRight):
        """A chip over the thumbnail at (x, y); returns its width (0 when it does not fit)."""
        metrics = painter.fontMetrics()
        width = min(metrics.horizontalAdvance(text) if hasattr(metrics, "horizontalAdvance") else metrics.width(text), 999) + 10
        if x + width > maxRight:
            width = maxRight - x
            if width < 28:
                return 0
            text = metrics.elidedText(text, qt.Qt.ElideRight, width - 10)
        rect = qt.QRectF(x, y, width, 18)
        painter.setPen(qt.Qt.NoPen)
        painter.setBrush(qt.QBrush(qt.QColor(0, 0, 0, 160)))
        painter.drawRoundedRect(rect, 4, 4)
        painter.setPen(qt.QColor(THUMB_CHIP_COLORS.get(level, THUMB_CHIP_COLORS["neutral"])))
        painter.drawText(rect, int(qt.Qt.AlignCenter), text)
        return width

    def _lateralityCues(self, painter, thumbRect):
        """Patient directions of the axial thumbnail: R at the left edge, L at the right (radiological, as the Red view).

        Drawn at the vertical middle, clear of the chips at the top and the bottom.
        """
        painter.save()
        painter.setFont(self._font(10, qt.QFont.DemiBold))
        side = 16.0
        top = thumbRect.center().y() - side / 2.0
        for text, left in ((LATERALITY_CUES[0], thumbRect.left() + 3), (LATERALITY_CUES[1], thumbRect.right() - 3 - side)):
            rect = qt.QRectF(left, top, side, side)
            painter.setPen(qt.Qt.NoPen)
            painter.setBrush(qt.QBrush(qt.QColor(0, 0, 0, 160)))
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(qt.QColor(THUMB_CHIP_COLORS["muted"]))
            painter.drawText(rect, int(qt.Qt.AlignCenter), text)
        painter.restore()

    def _renderCard(self, info, pixmap, state, dpr):
        colors = self.colors()
        size = self.cardSize()
        width, height = size.width(), size.height()
        card = qt.QPixmap(round(width * dpr), round(height * dpr))
        card.setDevicePixelRatio(dpr)
        card.fill(qt.QColor(0, 0, 0, 0))
        painter = qt.QPainter(card)
        try:
            painter.setRenderHint(qt.QPainter.Antialiasing, True)
            painter.setRenderHint(qt.QPainter.SmoothPixmapTransform, True)
            painter.setPen(qt.QPen(colors["hairline"], 1))
            painter.setBrush(qt.QBrush(colors["card"]))
            painter.drawRoundedRect(qt.QRectF(0.5, 0.5, width - 1, height - 1), CARD_RADIUS, CARD_RADIUS)
            side = self.thumbSize
            thumbRect = qt.QRectF(CARD_PAD, CARD_PAD, side, side)
            clip = qt.QPainterPath()
            clip.addRoundedRect(thumbRect, CARD_RADIUS - 2, CARD_RADIUS - 2)
            painter.fillPath(clip, qt.QBrush(colors["backdrop"]))
            if pixmap is not None:
                painter.save()
                painter.setClipPath(clip)
                painter.drawPixmap(thumbRect.toRect(), pixmap)
                painter.restore()
                self._lateralityCues(painter, thumbRect)
            else:
                painter.setFont(self._font(11))
                painter.setPen(qt.QColor(THUMB_CHIP_COLORS["muted"]))
                note = _("Making preview…") if state == "pending" else info.note
                painter.drawText(thumbRect, int(qt.Qt.AlignCenter), note)
            # Chips: extension columns at the top, space and volume at the bottom of the thumbnail
            painter.setFont(self._font(11))
            right = CARD_PAD + side - 6
            x = CARD_PAD + 6
            for text, level in info.topChips:
                used = self._chip(painter, x, CARD_PAD + 6, text, level, right)
                if not used:
                    break
                x += used + 4
            x = CARD_PAD + 6
            for text, level in info.bottomChips:
                used = self._chip(painter, x, CARD_PAD + side - 24, text, level, right)
                if not used:
                    break
                x += used + 4
            # Footer: id, QC mark, status dot
            footerTop = CARD_PAD + side
            centerY = footerTop + CARD_FOOTER / 2.0
            dotX = width - 14
            dotColor = colors.get(STATUS_DOT_TOKENS.get(info.status, "text3"), colors["text3"])
            if info.status == "inprogress":
                painter.setPen(qt.QPen(dotColor, 2))
                painter.setBrush(qt.QBrush(qt.Qt.NoBrush))
                painter.drawEllipse(qt.QPointF(dotX, centerY), 4, 4)
            else:
                painter.setPen(qt.Qt.NoPen)
                painter.setBrush(qt.QBrush(dotColor))
                painter.drawEllipse(qt.QPointF(dotX, centerY), 5, 5)
            textRight = dotX - 10
            if info.qc in ("pass", "fail"):
                markX = dotX - 18
                pen = qt.QPen(colors["success"] if info.qc == "pass" else colors["danger"], 2)
                pen.setCapStyle(qt.Qt.RoundCap)
                pen.setJoinStyle(qt.Qt.RoundJoin)
                painter.setPen(pen)
                painter.setBrush(qt.QBrush(qt.Qt.NoBrush))
                mark = qt.QPainterPath()
                if info.qc == "pass":
                    mark.moveTo(markX - 5, centerY)
                    mark.lineTo(markX - 1.5, centerY + 3.5)
                    mark.lineTo(markX + 5, centerY - 4)
                else:
                    mark.moveTo(markX - 4, centerY - 4)
                    mark.lineTo(markX + 4, centerY + 4)
                    mark.moveTo(markX + 4, centerY - 4)
                    mark.lineTo(markX - 4, centerY + 4)
                painter.drawPath(mark)
                textRight = markX - 10
            painter.setFont(self._font(13, qt.QFont.DemiBold))
            painter.setPen(colors["accent-fg"] if info.current else colors["text"])
            textRect = qt.QRectF(CARD_PAD + 4, footerTop, max(0, textRight - CARD_PAD - 4), CARD_FOOTER)
            painter.drawText(textRect, int(qt.Qt.AlignLeft | qt.Qt.AlignVCenter),
                             painter.fontMetrics().elidedText(info.sid, qt.Qt.ElideMiddle, int(textRect.width())))
        finally:
            painter.end()
        return card

    def dispose(self):
        ui.removePaletteListener(self._onPalette)
        self._prerenderTimer.stop()
        if self._service is not None and self._onThumbnails in self._service.listeners:
            self._service.listeners.remove(self._onThumbnails)
        self._service = None
        self._cards.clear()
        self._cardKeys.clear()


class _suppress:
    """``contextlib.suppress(Exception)`` (a deleted Qt object raises from PythonQt)."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return True


#
# Study page of the canvas
#


class StudyPage(qt.QObject):
    """The canvas with a study (SPEC 3.1): the study's name and counts, the subject list, and Open in Draw."""

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioStudyPage")
        self.widget.setMaximumWidth(STUDY_PAGE_MAX_WIDTH)
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        headerRow = qt.QHBoxLayout()
        headerRow.setSpacing(8)
        titleColumn = qt.QVBoxLayout()
        titleColumn.setSpacing(2)
        self.titleLabel = qt.QLabel()
        self.titleLabel.setObjectName("StudioStudyPageTitle")
        self.titleLabel.setTextFormat(qt.Qt.PlainText)
        _setFont(self.titleLabel, 16, qt.QFont.DemiBold)
        titleColumn.addWidget(self.titleLabel)
        self.countsLabel = qt.QLabel()
        self.countsLabel.setObjectName("StudioStudyPageCounts")
        self.countsLabel.setTextFormat(qt.Qt.PlainText)
        _setFont(self.countsLabel, 12)
        titleColumn.addWidget(self.countsLabel)
        headerRow.addLayout(titleColumn, 1)
        buttonRow = _buttonRow()
        self.newCaseButton = _roleButton(_("New case on template"), "StudioStudyNewCaseButton", self._newCase, "secondary")
        self.newCaseButton.setToolTip(_("Add a case without a scan and draw its lesion on the study template"))
        self.openButton = _roleButton(_("Open in Draw →"), "StudioStudyOpenButton", self._open, "primary")
        self.openButton.setToolTip(ui.tooltip(_("Open in Draw"), "Return", _("the selected subject, else the next untraced one")))
        buttonRow.layout().addWidget(self.newCaseButton)
        buttonRow.layout().addWidget(self.openButton)
        buttonRow.setSizePolicy(qt.QSizePolicy.Maximum, qt.QSizePolicy.Fixed)
        headerRow.addWidget(buttonRow)
        layout.addLayout(headerRow)
        self.mismatchLabel = qt.QLabel()
        self.mismatchLabel.setObjectName("StudioStudyPageMismatch")
        self.mismatchLabel.setWordWrap(True)
        _setFont(self.mismatchLabel, 12)
        self.mismatchLabel.hide()
        layout.addWidget(self.mismatchLabel)
        self.list = StudyList()
        layout.addWidget(self.list.widget, 1)
        self.hintLabel = qt.QLabel(_("Enter opens in Draw · P / X / U set QC · Delete removes from the study (files are kept) · / searches"))
        self.hintLabel.setObjectName("StudioStudyPageHint")
        self.hintLabel.setWordWrap(True)
        _setFont(self.hintLabel, 12)
        layout.addWidget(self.hintLabel)
        ui.bindStyleSheet(self.widget, lambda: (
            "QWidget#StudioStudyPage { background: transparent; }"
            f"QLabel#StudioStudyPageTitle {{ color: {ui.token('text').name()}; }}"
            f"QLabel#StudioStudyPageCounts, QLabel#StudioStudyPageHint {{ color: {ui.token('text2').name()}; }}"
            f"QLabel#StudioStudyPageMismatch {{ color: {ui.token('warning').name()}; }}"))

    def refresh(self):
        studySession = _studySession()
        if studySession is None:
            return
        self.titleLabel.setText(studySession.study.name)
        self.countsLabel.setText(studyCounts(studySession.study))
        studyUi = _studyUi()
        mismatch = studyUi.templateMismatch(studySession.study) if studyUi is not None else None
        self.mismatchLabel.setText(mismatch or "")
        self.mismatchLabel.setVisible(bool(mismatch))
        self.list.refresh()

    def _open(self):
        studySession = _studySession()
        if studySession is None:
            return None
        sids = self.list.selectedIds()
        sid = sids[0] if sids else studySession.current() or studySession.study.next_subject(None, status="untraced") or (
            studySession.study.subject_ids[0] if len(studySession.study) else None)
        if sid is None:
            return None
        return _runAction(lambda: studySession.open(sid), _("Failed to open {sid}.").format(sid=sid))

    def _newCase(self):
        studySession = _studySession()
        if studySession is not None:
            _runAction(studySession.newCase, _("Failed to add a case."))

    def focusFirst(self):
        self.list.focusTable()

    def dispose(self):
        self.list.dispose()
        if _isAlive(self.widget):
            ui.bindStyleSheet(self.widget, None)


def studyCounts(studyObject):
    """"42 subjects · 40 traced · 30 cases / 12 controls"."""
    rows = studyObject.subjects
    traced = sum(1 for row in rows if row.get("status") in ("traced", "mapped"))
    cases = sum(1 for row in rows if row.get("group") == "case")
    controls = sum(1 for row in rows if row.get("group") == "control")
    text = (_("1 subject") if len(rows) == 1 else _("{n} subjects").format(n=len(rows))) + " · " + _("{traced} traced").format(traced=traced)
    if cases or controls:
        text += " · " + (_("1 case") if cases == 1 else _("{n} cases").format(n=cases)) + " / " + (
            _("1 control") if controls == 1 else _("{n} controls").format(n=controls))
    return text


#
# Inspector cards of a study (STUDY, SUBJECT, BATCH, IMPORT)
#


def _cardLabel(objectName, pixelSize=12, color="text2"):
    label = qt.QLabel()
    label.setObjectName(objectName)
    label.setWordWrap(True)
    label.setTextFormat(qt.Qt.PlainText)
    label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    _setFont(label, pixelSize)
    ui.bindStyleSheet(label, lambda: f"QLabel#{objectName} {{ color: {ui.token(color).name()}; }}")
    return label


def _rowButton(text, objectName, callback, role="secondary"):
    row = _buttonRow()
    button = _roleButton(text, objectName, callback, role)
    row.layout().addWidget(button)
    return row, button


class StudyCard(qt.QObject):
    """STUDY: name, folder, counts, save status, tracing template, grid questions, and the study actions."""

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget, layout = ui.card(_("Study"))
        self.widget.setObjectName("StudioStudyCard")
        self.nameLabel = _cardLabel("StudioStudyCardName", 14, "text")
        _setFont(self.nameLabel, 14, qt.QFont.DemiBold)
        layout.addWidget(self.nameLabel)
        self.folderLink = _linkButton("", "StudioStudyCardFolder", self._showFolder)
        self.folderLink.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        ui.bindStyleSheet(self.folderLink, lambda: (
            f"QPushButton#StudioStudyCardFolder {{ background: transparent; border: none; color: {ui.token('accent-fg').name()}; "
            "padding: 0px; text-align: left; }"))
        layout.addWidget(self.folderLink)
        self.countsLabel = _cardLabel("StudioStudyCardCounts")
        layout.addWidget(self.countsLabel)
        self.saveLabel = _cardLabel("StudioStudyCardSaved")
        layout.addWidget(self.saveLabel)
        self.templateLabel = _cardLabel("StudioStudyCardTemplate")
        layout.addWidget(self.templateLabel)
        self.mismatchLabel = _cardLabel("StudioStudyCardMismatch", 12, "warning")
        layout.addWidget(self.mismatchLabel)
        self.templateRow, self.templateButton = _rowButton(_("Get MNI152NLin6Asym 1 mm template…"), "StudioStudyNlin6Button", self._getNlin6)
        self.templateButton.setToolTip(_("FSL licence terms apply (non-commercial); downloaded on request"))
        layout.addWidget(self.templateRow)
        self.questionsLayout = qt.QVBoxLayout()
        self.questionsLayout.setSpacing(6)
        layout.addLayout(self.questionsLayout)
        self.extraLayout = qt.QVBoxLayout()
        self.extraLayout.setSpacing(6)
        layout.addLayout(self.extraLayout)
        actions = _buttonRow()
        self.mergeButton = _roleButton(_("Merge job results"), "StudioStudyMergeButton", self._merge, "secondary")
        self.mergeButton.setToolTip(_("Merge the values that background jobs posted (.studio/inbox)"))
        self.closeButton = _roleButton(_("Close study"), "StudioStudyCloseButton", self._close, "secondary")
        actions.layout().addWidget(self.mergeButton)
        actions.layout().addWidget(self.closeButton)
        layout.addWidget(actions)
        self._extras = []
        self._questionsSignature = None

    def refresh(self):
        studySession = _studySession()
        if studySession is None:
            return
        studyObject = studySession.study
        self.nameLabel.setText(studyObject.name)
        self.folderLink.setText(_elideMiddle(studyObject.path, 46))
        self.folderLink.setToolTip(studyObject.path)
        self.countsLabel.setText(studyCounts(studyObject))
        text, when = studySession.saveStatus()
        self.saveLabel.setText(text + (" · " + relativeTime(when) if when else ""))
        template = studyObject.template or {}
        self.templateLabel.setText(_("Tracing template: {id} {res} mm").format(id=template.get("id", "—"), res=template.get("res", "")))
        studyUi = _studyUi()
        mismatch = studyUi.templateMismatch(studyObject) if studyUi is not None else None
        self.mismatchLabel.setText(mismatch or "")
        self.mismatchLabel.setVisible(bool(mismatch))
        self.templateRow.setVisible(studyObject.trace_policy.get("space") == "MNI152NLin6Asym" and template.get("id") != "MNI152NLin6Asym")
        self._refreshQuestions(studySession)
        self._refreshExtras(studySession)

    def _refreshQuestions(self, studySession):
        questions = studySession.gridQuestions()
        signature = tuple((gridId, tuple(candidates), count) for gridId, candidates, count in questions)
        if signature == self._questionsSignature:
            return
        self._questionsSignature = signature
        _clearLayout(self.questionsLayout)
        for gridId, candidates, count in questions:
            label = _cardLabel("StudioStudyGridQuestion", 12, "text")
            label.setText(_("{n} files are on the {grid} grid, which several templates share. Which space are they in?").format(n=count, grid=gridId))
            self.questionsLayout.addWidget(label)
            row = qt.QWidget()
            rowLayout = qt.QHBoxLayout(row)
            rowLayout.setContentsMargins(0, 0, 0, 0)
            rowLayout.setSpacing(4)
            for candidate in candidates:
                button = qt.QPushButton(candidate.replace("MNI152", ""))
                button.setObjectName("StudioStudyGridAnswer")
                button.setToolTip(candidate)
                ui.setRole(button, "secondary")
                button.connect("clicked()", lambda gridId=gridId, candidate=candidate: studySession.answerGrid(gridId, candidate))
                rowLayout.addWidget(button)
            self.questionsLayout.addWidget(row)

    def _refreshExtras(self, studySession):
        cards = libcardCards("study")
        if [card["key"] for card in cards] != [key for key, _widget, _card in self._extras]:
            for _key, widget, _card in self._extras:
                _discard(widget)
            self._extras = []
            for card in cards:
                try:
                    widget = card["create"](studySession)
                except Exception:
                    logging.error(f"Studio Library: the card {card['key']} failed:\n{traceback.format_exc()}")
                    continue
                if widget is not None:
                    self.extraLayout.addWidget(widget)
                    self._extras.append((card["key"], widget, card))
        for _key, widget, card in self._extras:
            refresh = card.get("refresh")
            if callable(refresh):
                try:
                    refresh(widget, studySession, studySelection())
                except Exception:
                    logging.error(f"Studio Library: the card {card['key']} failed:\n{traceback.format_exc()}")

    def _showFolder(self):
        studySession = _studySession()
        if studySession is not None:
            from . import projects

            projects.showInFileManager(studySession.study.path)

    def _merge(self):
        studySession = _studySession()
        if studySession is not None:
            applied = studySession.applyInbox()
            shell = _shell()
            if shell is not None:
                shell.toast(_("Merged {n} job results").format(n=len(applied)) if applied else _("No job results to merge"))

    def _close(self):
        studyUi = _studyUi()
        if studyUi is not None:
            _runAction(studyUi.closeStudy, _("Failed to close the study."))

    def _getNlin6(self):
        studySession = _studySession()
        if studySession is None:
            return
        if not slicer.util.confirmOkCancelDisplay(_("MNI152NLin6Asym is distributed under the FSL licence terms (non-commercial use). "
                                                    "Download the 1 mm template (about 3 MB) and trace on it?"), windowTitle=_("Tracing template")):
            return
        studyUi = _studyUi()

        def download():
            import SampleData

            SampleData.SampleDataLogic().downloadSample(studyUi.NLIN6_1MM_SAMPLE)
            studyObject = studySession.study
            studyObject.metadata["template"] = {"id": "MNI152NLin6Asym", "res": 1, "source": "sampledata:" + studyUi.NLIN6_1MM_SAMPLE}
            policy = studyObject.trace_policy
            policy["res"] = 1
            studyObject.trace_policy = policy
            studySession.flush()
            refreshStudy()

        _runAction(download, _("Failed to download the MNI152NLin6Asym template."))

    def dispose(self):
        for _key, widget, _card in self._extras:
            _discard(widget)
        self._extras = []


_resolutions = {}


def _gridResolution(path):
    """"1 mm", "2 mm" or "1.1 mm" of a NIfTI file (its header, cached by modification time), None when unknown."""
    if not path or not os.path.isfile(path):
        return None
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    if key not in _resolutions:
        try:
            import importlib

            nifti = importlib.import_module("LesionNetworkMappingLib.nifti")
            affine = nifti.read_nifti_header(path)["affine"]
            sizes = sorted({round(float((affine[0][axis] ** 2 + affine[1][axis] ** 2 + affine[2][axis] ** 2) ** 0.5), 2) for axis in range(3)})
            _resolutions[key] = (f"{sizes[0]:g} mm" if len(sizes) == 1 else TIMES.join(f"{size:g}" for size in sizes) + " mm")
        except Exception:
            _resolutions[key] = None
    return _resolutions[key]


def _elideMiddle(text, length):
    text = str(text)
    if len(text) <= length:
        return text
    half = (length - 1) // 2
    return text[:half] + "…" + text[-(length - 1 - half):]


class SubjectCard(qt.QObject):
    """SUBJECT: id, scan and lesion, space chip, volume, group, scores, notes and Open in Draw (the selected or current subject)."""

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget, layout = ui.card(_("Subject"))
        self.widget.setObjectName("StudioSubjectCard")
        self.sid = None
        idRow = qt.QHBoxLayout()
        idRow.setSpacing(8)
        self.idLabel = _cardLabel("StudioSubjectCardId", 14, "text")
        _setFont(self.idLabel, 14, qt.QFont.DemiBold)
        idRow.addWidget(self.idLabel, 1)
        self.spaceChip = ui.chip("", "neutral")
        self.spaceChip.setObjectName("StudioSubjectSpaceChip")
        idRow.addWidget(self.spaceChip)
        layout.addLayout(idRow)
        self.filesLabel = _cardLabel("StudioSubjectCardFiles")
        layout.addWidget(self.filesLabel)
        self.metricsLabel = _cardLabel("StudioSubjectCardMetrics", 12, "text")
        layout.addWidget(self.metricsLabel)
        groupRow = qt.QHBoxLayout()
        groupLabel = _cardLabel("StudioSubjectCardGroupLabel")
        groupLabel.setText(_("Group"))
        groupRow.addWidget(groupLabel)
        self.groupFrame, self.groupButtons = ui.segmented([_("Case"), _("Control"), "—"])
        self.groupFrame.setObjectName("StudioSubjectGroup")
        # QButtonGroup.buttonClicked(int) is gone in Qt 6; idClicked(int) exists since Qt 5.15
        self.groupButtons.connect("idClicked(int)" if hasattr(self.groupButtons, "idClicked") else "buttonClicked(int)", self._onGroup)
        groupRow.addWidget(self.groupFrame)
        groupRow.addStretch(1)
        layout.addLayout(groupRow)
        self.scoresLabel = _cardLabel("StudioSubjectCardScores", 12, "text")
        layout.addWidget(self.scoresLabel)
        self.warpRow, self.warpButton = _rowButton(_("Warp to MNI152NLin6Asym…"), "StudioSubjectWarpButton", self._warp)
        self.warpButton.setToolTip(_("Resample the 2009c lesion onto the connectome's FSL MNI152 grid (the original is kept)"))
        layout.addWidget(self.warpRow)
        self.notesEdit = qt.QLineEdit()
        self.notesEdit.setObjectName("StudioSubjectNotes")
        self.notesEdit.setPlaceholderText(_("Notes"))
        self.notesEdit.connect("editingFinished()", self._onNotes)
        layout.addWidget(self.notesEdit)
        self.recoverRow, self.recoverButton = _rowButton(_("Recover unsaved tracing"), "StudioSubjectRecoverButton", self._recover)
        layout.addWidget(self.recoverRow)
        self.extraLayout = qt.QVBoxLayout()
        layout.addLayout(self.extraLayout)
        self._extras = []
        self.openRow, self.openButton = _rowButton(_("Open in Draw"), "StudioSubjectOpenButton", self._open, "primary")
        layout.addWidget(self.openRow)

    def refresh(self, sid):
        studySession = _studySession()
        self.sid = sid
        if studySession is None or sid is None or sid not in studySession.study:
            self.widget.hide()
            return
        self.widget.show()
        row = studySession.study.subject(sid)
        current = studySession.current() == sid
        self.idLabel.setText(sid + ("  ·  " + _("open in Draw") if current else ""))
        text, level = spaceText(row)
        studyUi = _studyUi()
        target = studySession.study.trace_policy.get("space")
        if not _missing(row.get("space")) and target and row.get("space") not in (target, "native") and _missing(row.get("norm_method")):
            text, level = _("{space} ≠ connectome").format(space=str(row["space"]).replace("MNI152", "")), "warn"
        resolution = _gridResolution(studySession.study.abspath(row.get("lesion")) or studySession.study.abspath(row.get("scan")))
        if resolution and level != "warn" and row.get("space") != "native":
            text = f"{text} {resolution}"
        self.spaceChip.setText(text)
        # Scan space is amber on the chip (Map needs MNI lesions), plain in the list
        ui.setChipLevel(self.spaceChip, "warn" if level == "warn" or row.get("space") == "native" else "neutral")
        files = []
        for key, label in (("scan", _("Scan")), ("lesion", _("Lesion"))):
            files.append(f"{label}: {row.get(key) if not _missing(row.get(key)) else '—'}")
        self.filesLabel.setText("\n".join(files))
        metrics = [studyCell(row, "volume_ml")[0] + " mL"]
        if not _missing(row.get("n_parts")):
            metrics.append(_("{n} parts").format(n=row["n_parts"]) if row["n_parts"] != "1" else _("1 part"))
        if current and studyUi is not None:
            node = studySession.segmentationNode()
            draw = _draw()
            lesion = draw.lesionMetrics(node, compute=False) if draw is not None and node is not None else None
            if lesion and lesion.get("sides"):
                metrics.insert(0, lesion["sides"])
        metrics.append(studyCell(row, "status")[0])
        self.metricsLabel.setText(" · ".join(metrics))
        group = row.get("group")
        index = {"case": 0, "control": 1}.get(group, 2)
        button = self.groupButtons.button(index)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        scores = []
        for column, confirmed in studySession.study.score_columns():
            value = row.get(column)
            if not _missing(value):
                scores.append(f"{column}: {value}" + ("" if confirmed else " ?"))
        self.scoresLabel.setText("  ·  ".join(scores))
        self.scoresLabel.setVisible(bool(scores))
        notes = "" if _missing(row.get("notes")) else str(row["notes"])
        if not self.notesEdit.hasFocus() and str(self.notesEdit.text) != notes:
            self.notesEdit.setText(notes)
        journal = getattr(studySession, "_journal", None)
        self.recoverRow.setVisible(bool(current and journal is not None and journal.pendingRecovery(sid) is not None))
        self.warpRow.setVisible(bool(studyUi is not None and studyUi.canWarpToConnectomeSpace(studySession, sid)))
        self.openButton.setText(_("Open in Draw") if not current else _("Continue in Draw →"))
        self._refreshExtras(studySession, sid)

    def _refreshExtras(self, studySession, sid):
        sections = libcardSubjectSections()
        if [section["key"] for section in sections] != [key for key, _widget, _section in self._extras]:
            for _key, widget, _section in self._extras:
                _discard(widget)
            self._extras = []
            for section in sections:
                try:
                    widget = section["create"](studySession)
                except Exception:
                    logging.error(f"Studio Library: the subject section {section['key']} failed:\n{traceback.format_exc()}")
                    continue
                if widget is not None:
                    self.extraLayout.addWidget(widget)
                    self._extras.append((section["key"], widget, section))
        for _key, widget, section in self._extras:
            refresh = section.get("refresh")
            if callable(refresh):
                try:
                    refresh(widget, studySession, sid)
                except Exception:
                    logging.error(f"Studio Library: the subject section {section['key']} failed:\n{traceback.format_exc()}")

    def _onGroup(self, index):
        studySession = _studySession()
        if studySession is None or self.sid is None:
            return
        group = ("case", "control", "n/a")[index]
        if studySession.study.subject(self.sid).get("group") != group:
            studySession.setValues([self.sid], {"group": group})

    def _onNotes(self):
        studySession = _studySession()
        if studySession is None or self.sid is None or self.sid not in studySession.study:
            return
        text = str(self.notesEdit.text).strip()
        current = studySession.study.subject(self.sid).get("notes")
        if text != ("" if _missing(current) else current):
            studySession.setValues([self.sid], {"notes": text or None})

    def _open(self):
        studySession = _studySession()
        if studySession is not None and self.sid is not None:
            _runAction(lambda: studySession.open(self.sid), _("Failed to open {sid}.").format(sid=self.sid))

    def _warp(self):
        studyUi = _studyUi()
        if studyUi is not None and self.sid is not None:
            _runAction(lambda: studyUi.warpToConnectomeSpace(self.sid, shell=_shell()), _("Failed to warp {sid}.").format(sid=self.sid))

    def _recover(self):
        studySession = _studySession()
        journal = getattr(studySession, "_journal", None) if studySession is not None else None
        if journal is not None:
            journal.recover(self.sid)
            refreshStudy()

    def dispose(self):
        for _key, widget, _section in self._extras:
            _discard(widget)
        self._extras = []


class BatchCard(qt.QObject):
    """BATCH (several subjects selected): set the group, set a score column, remove from the study (files are kept)."""

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget, layout = ui.card(_("Batch"))
        self.widget.setObjectName("StudioBatchCard")
        self.countLabel = _cardLabel("StudioBatchCount", 12, "text")
        layout.addWidget(self.countLabel)
        groupRow = _buttonRow()
        for text, group, objectName in ((_("Case"), "case", "StudioBatchCaseButton"), (_("Control"), "control", "StudioBatchControlButton"),
                                        (_("No group"), "n/a", "StudioBatchNoGroupButton")):
            groupRow.layout().addWidget(_roleButton(text, objectName, lambda group=group: self._setValue("group", group), "secondary"))
        layout.addWidget(groupRow)
        scoreRow = qt.QHBoxLayout()
        scoreRow.setSpacing(6)
        self.scoreColumn = qt.QComboBox()
        self.scoreColumn.setObjectName("StudioBatchScoreColumn")
        self.scoreColumn.setEditable(True)
        self.scoreColumn.setToolTip(_("A score column (choose one, or type a new name)"))
        self.scoreColumn.lineEdit().setPlaceholderText(_("Score column"))
        scoreRow.addWidget(self.scoreColumn, 1)
        self.scoreValue = qt.QLineEdit()
        self.scoreValue.setObjectName("StudioBatchScoreValue")
        self.scoreValue.setPlaceholderText(_("Value"))
        scoreRow.addWidget(self.scoreValue, 1)
        self.scoreButton = qt.QPushButton(_("Set"))
        self.scoreButton.setObjectName("StudioBatchScoreButton")
        ui.setRole(self.scoreButton, "secondary")
        self.scoreButton.connect("clicked()", self._setScore)
        scoreRow.addWidget(self.scoreButton)
        layout.addLayout(scoreRow)
        self.removeRow, self.removeButton = _rowButton(_("Remove from study"), "StudioBatchRemoveButton", self._remove)
        self.removeButton.setToolTip(_("The subjects leave the study; their files are never deleted (Delete in the list; Undo in the message)"))
        layout.addWidget(self.removeRow)
        self.extraLayout = qt.QVBoxLayout()
        layout.addLayout(self.extraLayout)
        self._extras = []

    def refresh(self, sids):
        studySession = _studySession()
        if studySession is None or len(sids) < 2:
            self.widget.hide()
            return
        self.widget.show()
        self.countLabel.setText(_("{n} subjects selected").format(n=len(sids)))
        columns = [column for column, _confirmed in studySession.study.score_columns()]
        current = str(self.scoreColumn.currentText)
        if [str(self.scoreColumn.itemText(index)) for index in range(self.scoreColumn.count)] != columns:
            self.scoreColumn.clear()
            for column in columns:
                self.scoreColumn.addItem(column)
            if current:
                self.scoreColumn.setEditText(current)
        cards = libcardCards("batch")
        if [card["key"] for card in cards] != [key for key, _widget, _card in self._extras]:
            for _key, widget, _card in self._extras:
                _discard(widget)
            self._extras = []
            for card in cards:
                try:
                    widget = card["create"](studySession)
                except Exception:
                    logging.error(f"Studio Library: the card {card['key']} failed:\n{traceback.format_exc()}")
                    continue
                if widget is not None:
                    self.extraLayout.addWidget(widget)
                    self._extras.append((card["key"], widget, card))
        for _key, widget, card in self._extras:
            if callable(card.get("refresh")):
                try:
                    card["refresh"](widget, studySession, list(sids))
                except Exception:
                    logging.error(f"Studio Library: the card {card['key']} failed:\n{traceback.format_exc()}")

    def _setValue(self, column, value):
        studySession = _studySession()
        sids = studySelection()
        if studySession is not None and sids:
            _runAction(lambda: studySession.setValues(sids, {column: value}), _("Failed to set {column}.").format(column=column))

    def _setScore(self):
        column = re.sub(r"\s+", "_", str(self.scoreColumn.currentText).strip())
        if not column:
            return
        if column in _studyModule().STANDARD_COLUMNS:
            slicer.util.errorDisplay(_("{column} is a standard column of the study, not a score").format(column=column))
            return
        self._setValue(column, str(self.scoreValue.text).strip() or None)

    def _remove(self):
        canvasList = _canvas.studyPage.list if _canvas is not None and _canvas._studyPage is not None else None
        target = canvasList if canvasList is not None and _isAlive(canvasList.widget) else next(iter(_studyViews), None)
        if target is not None:
            target.removeSelected()

    def dispose(self):
        for _key, widget, _card in self._extras:
            _discard(widget)
        self._extras = []


class ImportCard(qt.QObject):
    """IMPORT (collapsed): the lesion and id rules, Add files…, Add folder…, Import participants.tsv…."""

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget = ctk.ctkCollapsibleButton()
        self.widget.setObjectName("StudioImportCard")
        self.widget.text = _("Import")
        self.widget.collapsed = True
        layout = qt.QFormLayout(self.widget)
        layout.setSpacing(6)
        self.lesionRule = qt.QLineEdit()
        self.lesionRule.setObjectName("StudioImportLesionRule")
        self.lesionRule.setToolTip(_("File names with one of these whole words are lesion masks (a regular expression)"))
        self.lesionRule.connect("editingFinished()", self._saveRules)
        layout.addRow(_("Lesion rule"), self.lesionRule)
        self.idRule = qt.QLineEdit()
        self.idRule.setObjectName("StudioImportIdRule")
        self.idRule.setToolTip(_("Subject ids in file and folder names (a regular expression)"))
        self.idRule.connect("editingFinished()", self._saveRules)
        layout.addRow(_("ID rule"), self.idRule)
        for text, objectName, callback in ((_("Add files…"), "StudioImportAddFilesButton", self._addFiles),
                                           (_("Add folder…"), "StudioImportAddFolderButton", self._addFolder),
                                           (_("Import participants.tsv…"), "StudioImportTableButton", self._importTable)):
            row, _button = _rowButton(text, objectName, callback)
            layout.addRow(row)
        self.extraLayout = qt.QVBoxLayout()
        layout.addRow(self.extraLayout)
        self._extras = []

    def refresh(self):
        studySession = _studySession()
        if studySession is None:
            return
        rules = studySession.study.rules
        for edit, key in ((self.lesionRule, "lesion"), (self.idRule, "id")):
            if not edit.hasFocus() and str(edit.text) != rules[key]:
                edit.setText(rules[key])
        cards = libcardCards("import")
        if [card["key"] for card in cards] != [key for key, _widget, _card in self._extras]:
            for _key, widget, _card in self._extras:
                _discard(widget)
            self._extras = []
            for card in cards:
                try:
                    widget = card["create"](studySession)
                except Exception:
                    logging.error(f"Studio Library: the card {card['key']} failed:\n{traceback.format_exc()}")
                    continue
                if widget is not None:
                    self.extraLayout.addWidget(widget)
                    self._extras.append((card["key"], widget, card))

    def _saveRules(self):
        studySession = _studySession()
        if studySession is None:
            return
        rules = {"lesion": str(self.lesionRule.text).strip(), "id": str(self.idRule.text).strip()}
        for key, value in rules.items():
            try:
                re.compile(value)
            except re.error as e:
                slicer.util.errorDisplay(_("The {key} rule is not a valid regular expression: {error}").format(key=key, error=e))
                return
        if rules != {key: studySession.study.rules[key] for key in ("lesion", "id")}:
            studySession.study.metadata["rules"] = rules
            studySession.flush()

    def _addFiles(self):
        studySession = _studySession()
        if studySession is None:
            return
        paths = qt.QFileDialog.getOpenFileNames(slicer.util.mainWindow(), _("Add files to the study"), studySession.study.path, "NIfTI (*.nii *.nii.gz)")
        if paths:
            self._report(studySession, _runAction(lambda: studySession.addFiles([str(path) for path in paths]), _("Failed to add the files.")))

    def _addFolder(self):
        studySession = _studySession()
        if studySession is None:
            return
        folder = qt.QFileDialog.getExistingDirectory(slicer.util.mainWindow(), _("Add a folder to the study"), studySession.study.path)
        if folder:
            self._report(studySession, _runAction(lambda: studySession.addFiles([str(folder)]), _("Failed to add the folder.")))

    def _importTable(self):
        studySession = _studySession()
        if studySession is None:
            return
        path = qt.QFileDialog.getOpenFileName(slicer.util.mainWindow(), _("Import participants table"), studySession.study.path,
                                              _("Participants table") + " (*.tsv *.csv)")
        if path:
            result = _runAction(lambda: studySession.importTable(str(path)), _("Failed to import the table."))
            shell = _shell()
            if result and shell is not None:
                shell.toast(_("{added} added, {updated} updated").format(added=len(result["added"]), updated=len(result["updated"])))

    @staticmethod
    def _report(studySession, report):
        shell = _shell()
        if report and shell is not None:
            shell.toast(_("{added} subjects added, {updated} updated, {skipped} files skipped").format(
                added=len(report["added"]), updated=len(report["updated"]), skipped=len(report["skipped"])), ms=4000)

    def dispose(self):
        for _key, widget, _card in self._extras:
            _discard(widget)
        self._extras = []


class StudyCards(qt.QObject):
    """The inspector of Library with a study: STUDY, SUBJECT (one selected or the current subject), BATCH (several selected),
    IMPORT, and in Classic the subject list itself (the canvas is not used there).
    """

    def __init__(self, withList=False):
        qt.QObject.__init__(self)
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioStudyCards")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.studyCard = StudyCard()
        layout.addWidget(self.studyCard.widget)
        self.list = None
        if withList:
            self.list = StudyList(compact=True)
            self.list.widget.setMinimumHeight(260)
            layout.addWidget(self.list.widget)
        self.subjectCard = SubjectCard()
        layout.addWidget(self.subjectCard.widget)
        self.batchCard = BatchCard()
        layout.addWidget(self.batchCard.widget)
        self.importCard = ImportCard()
        layout.addWidget(self.importCard.widget)
        self.view = _CardsView(self)
        _studyViews.append(self.view)

    def refresh(self):
        studySession = _studySession()
        if studySession is None:
            self.widget.hide()
            return
        self.widget.show()
        self.studyCard.refresh()
        if self.list is not None:
            self.list.refresh()
        self.refreshSelection()
        self.importCard.refresh()

    def refreshSelection(self):
        studySession = _studySession()
        if studySession is None:
            return
        sids = studySelection()
        self.subjectCard.refresh(sids[0] if len(sids) == 1 else (studySession.current() if not sids else None))
        self.batchCard.refresh(sids)

    def dispose(self):
        if self.view in _studyViews:
            _studyViews.remove(self.view)
        for card in (self.studyCard, self.subjectCard, self.batchCard, self.importCard):
            card.dispose()
        if self.list is not None:
            self.list.dispose()


class _CardsView:
    """Registers the inspector cards as a study view (selection changes refresh the SUBJECT and BATCH cards)."""

    def __init__(self, cards):
        self.cards = cards
        self.widget = cards.widget

    def refresh(self):
        self.cards.refresh()

    def onSelectionChanged(self):
        self.cards.refreshSelection()


#
# Recent studies (Studio/RecentStudies, listed first in Recent)
#


class RecentStudyEntry:
    """A study opened before (``Studio/RecentStudies``): "Study · <folder>"; it opens the study again."""

    kind = "study"

    def __init__(self, record):
        self.path = str(record.get("path", ""))
        self.name = str(record.get("name") or os.path.basename(self.path.rstrip("/\\")))
        self.openedTime = record.get("time")
        self.folder = _("Study · {folder}").format(folder=self.path)
        self.properties = {}
        self.action = None

    def exists(self):
        return os.path.isfile(os.path.join(self.path, "study.json"))

    def timeText(self, now=None):
        return _("opened {when}").format(when=relativeTime(self.openedTime, now)) if self.openedTime else ""

    def load(self, shell=None):
        studyUi = _studyUi()
        return studyUi.openStudy(self.path, shell) if studyUi is not None else None

    def __repr__(self):
        return f"RecentStudyEntry({self.path!r})"


RECENT_STUDY_ROW_COUNT = 3


def recentStudyEntries(limit=RECENT_STUDY_ROW_COUNT):
    """The studies opened before that still exist, most recent first."""
    studyUi = _studyUi()
    if studyUi is None:
        return []
    entries = [RecentStudyEntry(record) for record in studyUi.recentStudies()]
    return [entry for entry in entries if entry.exists()][:limit]


#
# Folder card
#


class FolderCard:
    """Card offered for a folder with several NIfTI files: "N NIfTI files found", the reason when there are too many to
    load at once, and an enabled way on: Load all (up to ``MAX_LOAD_ALL_FILES`` files), Open one subject…, and
    Create study when the study page (W3-07) is part of this build: the primary button for any number of files, with the
    group mapping of a participants table whose groups are not case / control. Studio's own folders (Studies/, Projects/,
    a project, the Slicer Studio folder: ``study_ui.studyFolderProblem``) show why instead of Create study.
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
        title = qt.QLabel(_("1 NIfTI file found") if len(self.files) == 1 else _("{count} NIfTI files found").format(count=len(self.files)))
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

        self.participantsPath = table
        # The reason is in the card, not in a tool tip
        self.studyAvailable = _studyUi() is not None
        # Studio's own folders (Studies/, Projects/, a project, ...) never become a study: the reason replaces the plan
        self.studyProblem = _studyUi().studyFolderProblem(folder) if self.studyAvailable else None
        self.groupColumn = None
        self.groupCombos = {}
        if self.studyProblem:
            self.planLabel = self._textLabel(self.studyProblem, "StudioFolderCardStudyPlan")
            layout.addWidget(self.planLabel)
        elif self.studyAvailable:
            self._addStudyPlan(layout, folder, table)
        self.reasonLabel = None
        if self.tooMany:
            if self.studyProblem:
                reason = _("{count} files: open one subject at a time").format(count=len(self.files))
            elif self.studyAvailable:
                reason = _("{count} files: open one subject at a time, or create a study").format(count=len(self.files))
            else:
                reason = _("{count} files: open one subject at a time, or create a study when available").format(count=len(self.files))
            self.reasonLabel = self._textLabel(reason, "StudioFolderCardReason")
            layout.addWidget(self.reasonLabel)

        buttonRow = qt.QHBoxLayout()
        buttonRow.setSpacing(8)
        buttonRow.addStretch(1)
        self.createStudyButton = None
        if self.studyAvailable and not self.studyProblem:
            # The study is the way on for a folder of subjects (any number of files)
            self.createStudyButton = qt.QPushButton(_("Create study"))
            self.createStudyButton.setToolTip(_("Make a study of this folder: study.json, participants.tsv and lesions/ are written here; "
                                                "your scans and masks are never changed"))
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
        # The rightmost button is the primary one: Create study, else the way on (Open one subject… for a large folder, else Load all)
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

    def _addStudyPlan(self, layout, folder, table):
        """Where the study goes and, for a participants table whose groups are not case / control, how they map (L-01)."""
        studyUi = _studyUi()
        self.planLabel = self._textLabel(_("Create study writes study.json and lesions/ in this folder; your files are not changed."),
                                         "StudioFolderCardStudyPlan")
        layout.addWidget(self.planLabel)
        if not table:
            return
        try:
            columns, rows = studyUi.readTable(table)
            proposal = studyUi.groupMappingProposal(columns, rows)
        except Exception:
            logging.warning(f"Studio Library: {table} could not be read:\n{traceback.format_exc()}")
            return
        if proposal is None:
            return
        self.groupColumn, mapping = proposal
        label = self._textLabel(_("Groups in column '{column}':").format(column=self.groupColumn), "StudioFolderCardGroupTitle")
        layout.addWidget(label)
        targets = ((_("Case"), "case"), (_("Control"), "control"), (_("Exclude"), "exclude"), ("—", None))
        for value, target in list(mapping.items())[:8]:
            row = qt.QHBoxLayout()
            row.setSpacing(8)
            valueLabel = self._textLabel(f"{value}  →", "StudioFolderCardGroupValue")
            row.addWidget(valueLabel, 1)
            combo = qt.QComboBox()
            combo.setObjectName("StudioFolderCardGroupCombo")
            for text, data in targets:
                combo.addItem(text, data)
            combo.setCurrentIndex(next(index for index, (_text, data) in enumerate(targets) if data == target))
            row.addWidget(combo)
            layout.addLayout(row)
            self.groupCombos[value] = combo

    def groupMapping(self):
        """{value: "case" | "control" | "exclude" | None} as chosen in the card (None without a group column to map)."""
        if not self.groupCombos:
            return None
        return {value: combo.itemData(combo.currentIndex) for value, combo in self.groupCombos.items()}

    def createStudy(self):
        """Create the study of this folder (and open its subject list): one click, the group mapping as shown."""
        studyUi = _studyUi()
        if studyUi is None:
            return None
        files, folder, table = self.files, self.folder, self.participantsPath
        groupColumn, mapping = self.groupColumn, self.groupMapping()

        def create():
            inFolder = table is not None and os.path.normcase(os.path.dirname(os.path.abspath(table))) == os.path.normcase(os.path.abspath(folder))
            studySession = studyUi.createStudy(files, _shell(), folder=folder, groupColumn=groupColumn if inFolder else None,
                                               groupMapping=mapping if inFolder else None)
            if table is not None and not inFolder:
                # A participants table of the parent folder (BIDS): imported into the new study
                studySession.importTable(table, groupColumn, mapping)
            return studySession

        studySession = _runAction(create, _("Failed to create the study."))
        if studySession is not None:
            self.close()
        return studySession

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
#: The page (VISUAL 4.2): at most 1100 px of content, 40 px on the sides and 28 px at the top
PAGE_MAX_WIDTH = 1100
PAGE_SIDE_PADDING = 40
PAGE_TOP_PADDING = 28
#: The dashed frame of the empty state around the drop zone
DROP_FRAME_SIZE = (620, 340)


def _fileManagerName():
    try:
        from . import projects

        return projects.fileManagerName()
    except Exception:
        return _("Files")


def _showRootFolder():
    try:
        from . import projects_ui

        projects_ui.showRootFolder()
    except Exception:
        logging.error(f"Studio Library: the folder could not be shown:\n{traceback.format_exc()}")


class LibraryCanvas(qt.QObject):
    """The Library page shown over the views (SPEC 3.1, VISUAL 4.2): a full-width page (at most ``PAGE_MAX_WIDTH`` px of
    content) with the "Projects" header and its buttons, the Continue card of what is open, the projects of the Slicer
    Studio folder as a grid of cards, the recent files and the links. Nothing loaded: a large drop zone under the header,
    with the buttons in it; once data is loaded the drop zone is a 44 px strip under Recent.
    """

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
        self.contentLayout.setContentsMargins(PAGE_SIDE_PADDING, PAGE_TOP_PADDING, PAGE_SIDE_PADDING, PAGE_TOP_PADDING)
        self.contentLayout.setSpacing(0)
        # The page starts at the top (stretch 0); the stretch at the end takes the rest
        self.contentLayout.addStretch(0)
        # Keyed notices (A-01b, ``showNotice()``), above everything else of the page; the 16 px below them is part
        # of the container, which is hidden while there is no notice
        self.noticeContainer = qt.QWidget()
        self.noticeContainer.setObjectName("StudioLibraryNotices")
        noticeRow = qt.QHBoxLayout(self.noticeContainer)
        noticeRow.setContentsMargins(0, 0, 0, NOTICE_SPACING_BELOW)
        noticeRow.addStretch(1)
        noticeColumn = qt.QWidget()
        noticeColumn.setMaximumWidth(PAGE_MAX_WIDTH)
        noticeColumn.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Preferred)
        self.noticeLayout = qt.QVBoxLayout(noticeColumn)
        self.noticeLayout.setContentsMargins(0, 0, 0, 0)
        self.noticeLayout.setSpacing(8)
        noticeRow.addWidget(noticeColumn, 100)
        noticeRow.addStretch(1)
        self.noticeContainer.hide()
        self.contentLayout.addWidget(self.noticeContainer)
        # With a study (W3-07): the study page, as wide as the window allows (up to STUDY_PAGE_MAX_WIDTH)
        self.studyContainer = qt.QWidget()
        self.studyContainer.setObjectName("StudioStudyContainer")
        studyRow = qt.QHBoxLayout(self.studyContainer)
        studyRow.setContentsMargins(0, 0, 0, 0)
        self.studyRow = studyRow
        self.studyContainer.hide()
        self.contentLayout.addWidget(self.studyContainer)
        self._studyPage = None
        self._studyShown = False
        columnRow = qt.QHBoxLayout()
        columnRow.addStretch(1)
        column = qt.QWidget()
        column.setObjectName("StudioLibraryColumn")
        column.setMaximumWidth(PAGE_MAX_WIDTH)
        column.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Maximum)
        self.column = column
        columnRow.addWidget(column, 100)
        columnRow.addStretch(1)
        self.contentLayout.addLayout(columnRow)
        self.contentLayout.addStretch(1)
        self.scrollArea.setWidget(content)

        columnLayout = qt.QVBoxLayout(column)
        columnLayout.setContentsMargins(0, 0, 0, 0)
        columnLayout.setSpacing(0)
        openShortcut = qt.QKeySequence("Ctrl+O").toString(qt.QKeySequence.NativeText)

        # Header: "Projects", where they are saved, and the buttons (never wrapped)
        self.header = qt.QWidget()
        self.header.setObjectName("StudioLibraryHeader")
        headerLayout = qt.QHBoxLayout(self.header)
        headerLayout.setContentsMargins(0, 0, 0, 0)
        headerLayout.setSpacing(16)
        titleColumn = qt.QVBoxLayout()
        titleColumn.setSpacing(4)
        self.pageTitle = ui.label(_("Projects"), "display", "text", "StudioLibraryTitle")
        titleColumn.addWidget(self.pageTitle)
        captionRow = qt.QHBoxLayout()
        captionRow.setSpacing(6)
        self.captionIcon = qt.QLabel()
        self.captionIcon.setObjectName("StudioLibraryCaptionIcon")
        self.captionIcon.setFixedSize(14, 14)
        captionRow.addWidget(self.captionIcon)
        self.captionLabel = ui.label("", "caption", "text3", "StudioLibraryCaption")
        captionRow.addWidget(self.captionLabel)
        self.captionSeparator = ui.label("·", "caption", "text3", "StudioLibraryLinkSeparator")
        captionRow.addWidget(self.captionSeparator)
        self.captionLink = self._link(_("Show in {manager}").format(manager=_fileManagerName()), "StudioLibraryCaptionLink", _showRootFolder)
        captionRow.addWidget(self.captionLink)
        captionRow.addStretch(1)
        titleColumn.addLayout(captionRow)
        # Once data is loaded the page has no drop zone: a second caption line says that files can be dropped anywhere
        self.captionDropHint = ui.label(_("Drop scans, lesion masks or folders anywhere on the window to add them"), "caption", "text3",
                                        "StudioLibraryDropHint")
        self.captionDropHint.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        self.captionDropHint.hide()
        titleColumn.addWidget(self.captionDropHint)
        headerLayout.addLayout(titleColumn, 1)
        self.headerButtons = qt.QWidget()
        self.headerButtons.setObjectName("StudioLibraryHeaderButtons")
        headerButtonsLayout = qt.QHBoxLayout(self.headerButtons)
        headerButtonsLayout.setContentsMargins(0, 0, 0, 0)
        headerButtonsLayout.setSpacing(8)
        headerLayout.addWidget(self.headerButtons, 0, qt.Qt.AlignBottom)
        columnLayout.addWidget(self.header)
        columnLayout.addSpacing(24)

        # The buttons: in the header, or in the drop zone of the empty state (``_setStrip()``)
        self.buttonRow = _buttonRow()
        self.buttonRow.setObjectName("StudioLibraryButtons")
        self.buttonRow.setSizePolicy(qt.QSizePolicy.Fixed, qt.QSizePolicy.Fixed)
        self.openButton = self._button(_("Open…"), "StudioLibraryOpenButton", openFiles,
                                       _("Open scans, lesion masks or scenes ({shortcut})").format(shortcut=openShortcut), "primary")
        self.openFolderButton = self._button(_("Open folder…"), "StudioLibraryOpenFolderButton", openFolder,
                                             _("Open a DICOM folder or a folder of NIfTI files"))
        self.templateButton = self._button(_("Start on template"), "StudioLibraryStartOnTemplateButton", startOnTemplate,
                                           _("Draw a lesion on your default MNI template (the bundled MNI152NLin2009cAsym works offline)"))
        # "Start on template ▾" (A-10): the arrow lists every template, Add your own template… and Manage templates…
        self.templateMenuButton = qt.QToolButton()
        self.templateMenuButton.setObjectName("StudioLibraryTemplateMenuButton")
        self.templateMenuButton.setText("")
        self.templateMenuButton.setToolButtonStyle(qt.Qt.ToolButtonIconOnly)
        self.templateMenuButton.setToolTip(_("Choose a template, or add your own MNI template"))
        self.templateMenuButton.setAccessibleName(_("Templates"))
        self.templateMenuButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.templateMenuButton.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        self.templateMenuButton.setSizePolicy(qt.QSizePolicy.Fixed, qt.QSizePolicy.Expanding)
        self.templateMenuButton.setFixedWidth(ui.BUTTON_HEIGHT)
        ui.setRole(self.templateMenuButton, "secondary")
        self.templateMenu = qt.QMenu(self.templateMenuButton)
        self.templateMenu.setObjectName("studioTemplateMenu")
        self.templateMenu.connect("aboutToShow()", self._fillTemplateMenu)
        self.templateMenuButton.setMenu(self.templateMenu)
        templateSplit = qt.QWidget()
        templateSplit.setObjectName("StudioLibraryTemplateSplit")
        splitLayout = qt.QHBoxLayout(templateSplit)
        splitLayout.setContentsMargins(0, 0, 0, 0)
        splitLayout.setSpacing(2)
        splitLayout.addWidget(self.templateButton)
        splitLayout.addWidget(self.templateMenuButton)
        for button in (templateSplit, self.openFolderButton, self.openButton):
            self.buttonRow.layout().addWidget(button)
        headerButtonsLayout.addWidget(self.buttonRow)

        # What is open (shown once data is loaded): the Continue card and the loaded set
        self.dataSection = qt.QWidget()
        self.dataSection.setObjectName("StudioLibraryDataSection")
        dataLayout = qt.QVBoxLayout(self.dataSection)
        dataLayout.setContentsMargins(0, 0, 0, 24)
        dataLayout.setSpacing(16)
        # The cards are created when data is loaded for the first time (Library is created at startup)
        self._dataLayout = dataLayout
        self._currentWork = None
        self._loadedSet = None
        self.dataSection.hide()
        columnLayout.addWidget(self.dataSection)

        # Drop zone (the files are dropped on the main window, see StudioLib.importer): the dashed frame of the empty state
        # (``StudioDropZoneFrame``, VISUAL 4.2) around the 480 x 220 zone, or the 44 px strip once data is loaded
        self.dropFrame = qt.QFrame()
        self.dropFrame.setObjectName("StudioDropZoneFrame")
        self.dropFrame.setProperty("dragActive", False)
        self.dropFrame.setProperty("studioStrip", False)
        self.dropFrame.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        dropFrameLayout = qt.QVBoxLayout(self.dropFrame)
        dropFrameLayout.setContentsMargins(0, 0, 0, 0)
        dropFrameLayout.setSpacing(0)
        self.dropZone = qt.QFrame()
        self.dropZone.setObjectName("StudioDropZone")
        self.dropZone.setFixedSize(COLUMN_WIDTH, DROP_ZONE_HEIGHT)
        self.dropZone.setProperty("dragActive", False)
        self.dropZone.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        self.dropZone.setToolTip(_("Drop files here, or click to open files"))
        _installEventFilter(self.dropZone, self)
        _installEventFilter(self.dropFrame, self)
        dropFrameLayout.addWidget(self.dropZone, 0, qt.Qt.AlignCenter)
        dropZoneOuter = qt.QVBoxLayout(self.dropZone)
        dropZoneOuter.setContentsMargins(0, 0, 0, 0)
        dropZoneOuter.setSpacing(0)
        self.dropFull = qt.QWidget()
        self.dropFull.setObjectName("StudioDropZoneFull")
        dropZoneLayout = qt.QVBoxLayout(self.dropFull)
        dropZoneLayout.setContentsMargins(0, 0, 0, 0)
        dropZoneLayout.setSpacing(8)
        dropZoneLayout.addStretch(1)
        self.dropZoneIcon = ui.iconTile("import", 48, 24, "StudioDropZoneIcon")
        dropZoneLayout.addWidget(self.dropZoneIcon, 0, qt.Qt.AlignHCenter)
        dropZoneLayout.addSpacing(6)
        dropZoneTitle = ui.label(_("Drop scans, DICOM folders or Slicer scenes"), "title", "text", "StudioDropZoneTitle")
        dropZoneTitle.setAlignment(qt.Qt.AlignCenter)
        dropZoneLayout.addWidget(dropZoneTitle)
        dropZoneHint = ui.label(_("NIfTI, NRRD, DICOM folders, lesion masks and .mrb scenes · or press {shortcut}").format(shortcut=openShortcut),
                                "caption", "text3", "StudioDropZoneHint")
        dropZoneHint.setAlignment(qt.Qt.AlignCenter)
        dropZoneLayout.addWidget(dropZoneHint)
        dropZoneLayout.addSpacing(10)
        self.dropButtonsSlot = qt.QHBoxLayout()
        self.dropButtonsSlot.setContentsMargins(0, 0, 0, 0)
        dropZoneLayout.addLayout(self.dropButtonsSlot)
        dropZoneLayout.addStretch(1)
        for label in (dropZoneTitle, dropZoneHint):
            label.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
        self.dropZoneIcon.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
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
        self.dropStripText = ui.label(_("Drop more files · {shortcut}").format(shortcut=openShortcut), "caption", "text3", "StudioDropStripText")
        compactLayout.addWidget(self.dropStripText)
        compactLayout.addStretch(1)
        for label in (self.dropStripIcon, self.dropStripText):
            label.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
        dropZoneOuter.addWidget(self.dropCompact)
        self.dropCompact.hide()
        # Where the drop zone goes: under the header (empty state) or under Recent (strip)
        self.dropTopSlot = qt.QVBoxLayout()
        self.dropTopSlot.setContentsMargins(0, 0, 0, 0)
        columnLayout.addLayout(self.dropTopSlot)
        self.dropTopSlot.addWidget(self.dropFrame)
        self.dropTopSpacing = qt.QWidget()
        self.dropTopSpacing.setFixedHeight(32)
        columnLayout.addWidget(self.dropTopSpacing)

        # Folder card (several NIfTI files dropped)
        self.folderCardSlot = _FolderCardSlot()
        self.folderCardSpacing = qt.QWidget()
        self.folderCardSpacing.setFixedHeight(16)
        self.folderCardSpacing.hide()
        columnLayout.addLayout(self.folderCardSlot.layout)
        columnLayout.addWidget(self.folderCardSpacing)

        # Projects of the Slicer Studio folder (A-09)
        self.projectsSection = None
        try:
            from . import projects_ui

            self.projectsSection = projects_ui.ProjectsSection()
            columnLayout.addWidget(self.projectsSection.widget)
            # The Continue card names the open project: the section lists the projects again when one is created (a scan
            # was loaded) or changes, with no workspace switch
            self.projectsSection.onRefreshed = self._syncCurrentProject
        except Exception:
            logging.error(f"Studio Library: the Projects section failed:\n{traceback.format_exc()}")

        # Studies (V-02): the studies of the folder and those opened from elsewhere, as row cards
        self.studiesSection = None
        try:
            from . import projects_ui

            self.studiesSection = projects_ui.StudiesSection()
            columnLayout.addWidget(self.studiesSection.widget)
        except Exception:
            logging.error(f"Studio Library: the Studies section failed:\n{traceback.format_exc()}")

        # A compact row of ghost buttons under Studies: New study…, Open study…, Load sample data, Classic interface
        linkRow = qt.QHBoxLayout()
        linkRow.setSpacing(4)
        self.newStudyLink = None
        self.openStudyLink = None
        if _studyUi() is not None:
            self.newStudyLink = self._ghost(_("New study…"), "StudioLibraryNewStudyLink", newStudy, "users")
            self.newStudyLink.setToolTip(_("Make a study of a folder of scans and lesion masks"))
            self.openStudyLink = self._ghost(_("Open study…"), "StudioLibraryOpenStudyLink", openStudy, "folder-open")
            self.openStudyLink.setToolTip(_("Open a study folder (study.json)"))
            for link in (self.newStudyLink, self.openStudyLink):
                linkRow.addWidget(link)
        self.sampleDataLink = self._ghost(_("Load sample data"), "StudioLibrarySampleDataLink", openSampleData, "download")
        self.sampleDataLink.setToolTip(_("Load a sample scan (downloaded from the 3D Slicer sample data)"))
        linkRow.addWidget(self.sampleDataLink)
        self.classicLink = self._ghost(_("Classic interface"), "StudioLibraryClassicLink", switchToClassic, "monitor")
        self.classicLink.setToolTip(_("Switch to the Classic interface (Ctrl+Alt+0)"))
        linkRow.addWidget(self.classicLink)
        linkRow.addStretch(1)
        columnLayout.addLayout(linkRow)
        columnLayout.addSpacing(28)

        # Opened from elsewhere: the folders and files opened from outside the Slicer Studio folder (the studies are listed
        # in Studies, the projects in All projects)
        recentTitle = ui.sectionLabel(_("Opened from elsewhere"), "StudioLibrarySectionTitle")
        recentTitle.setContentsMargins(0, 0, 0, 0)
        ui.setText(recentTitle, "section")
        self.recentTitle = recentTitle
        columnLayout.addWidget(recentTitle)
        columnLayout.addSpacing(8)
        self.recentList = RecentList(includeStudies=False, outsideRootOnly=True)
        columnLayout.addWidget(self.recentList.widget)
        columnLayout.addSpacing(16)
        # The drop strip of earlier versions went here; files can be dropped anywhere on the window (the header says so)
        self.dropBottomSlot = qt.QVBoxLayout()
        self.dropBottomSlot.setContentsMargins(0, 0, 0, 0)
        columnLayout.addLayout(self.dropBottomSlot)

        # Footer line (the research-use line of the first run): one 32 px line, nothing while no one sets it
        self.footerFrame = qt.QFrame()
        self.footerFrame.setObjectName("StudioLibraryFooterLine")
        self.footerFrame.setMinimumHeight(FOOTER_LINE_HEIGHT + 8)
        self.footerLayout = qt.QHBoxLayout(self.footerFrame)
        self.footerLayout.setContentsMargins(0, 12, 0, 4)
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
        self._setStrip(False)
        ui.bindStyleSheet(self.widget, self._styleSheet)
        ui.addPaletteListener(self._refreshIcons)
        self._refreshIcons()
        self._refreshCaption()

    def _fillTemplateMenu(self):
        """The "Start on template ▾" menu (``templates_ui.populateTemplateMenu``)."""
        try:
            from . import templates_ui

            templates_ui.populateTemplateMenu(self.templateMenu)
        except Exception:
            logging.error(f"Studio Library: the template menu failed:\n{traceback.format_exc()}")

    @staticmethod
    def _button(text, objectName, callback, toolTip, role="secondary"):
        button = _roleButton(text, objectName, callback, role) if role != "secondary" else _secondaryButton(text, objectName, callback)
        button.setAutoDefault(True)
        button.setToolTip(toolTip)
        # Buttons never wrap or shrink below their text
        button.setSizePolicy(qt.QSizePolicy.Minimum, qt.QSizePolicy.Expanding)
        return button

    @staticmethod
    def _link(text, objectName, callback):
        return _linkButton(text, objectName, callback)

    @staticmethod
    def _ghost(text, objectName, callback, iconName):
        """A compact ghost button (28 px, text2 with a 16 px icon) of the row under Studies."""
        button = qt.QPushButton(text)
        button.setObjectName(objectName)
        button.setProperty("studioIconName", iconName)
        button.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        button.setFixedHeight(ui.COMPACT_HEIGHT)
        button.setIconSize(qt.QSize(16, 16))
        button.setSizePolicy(qt.QSizePolicy.Minimum, qt.QSizePolicy.Fixed)
        ui.setText(button, "body")
        ui.setRole(button, "ghost")
        button.connect("clicked()", callback)
        ui.installButtonKeys(button)
        return button

    @staticmethod
    def _styleSheet():
        t = {name: ui.token(name).name() for name in ("canvas", "text", "text2", "text3", "border", "hairline", "accent-fg", "accent-subtle")}
        t["control-border"] = _controlBorder()
        return (
            f"QFrame#StudioLibraryCanvas {{ background-color: {t['canvas']}; border: none; }}"
            "QScrollArea#StudioLibraryScroll, QWidget#StudioLibraryContent, QWidget#StudioLibraryColumn { background: transparent; border: none; }"
            "QScrollArea#StudioLibraryScroll > QWidget#qt_scrollarea_viewport { background: transparent; }"
            "QWidget#StudioLibraryDataSection, QWidget#StudioDropZoneFull, QWidget#StudioDropZoneCompact { background: transparent; border: none; }"
            # The dashed drop zone (1.5 px, border color, radius 16); the inner zone has no frame of its own
            f"QFrame#StudioDropZoneFrame {{ background-color: transparent; border: 1.5px dashed {t['control-border']}; border-radius: 16px; }}"
            f'QFrame#StudioDropZoneFrame[studioStrip="true"] {{ border: 1px dashed {t["border"]}; border-radius: 10px; }}'
            f"QFrame#StudioDropZoneFrame:hover {{ border-color: {t['text3']}; }}"
            f'QFrame#StudioDropZoneFrame[dragActive="true"] {{ background-color: {t["accent-subtle"]}; border-color: {t["accent-fg"]}; }}'
            "QFrame#StudioDropZone { background-color: transparent; border: none; }"
            "QLabel#StudioDropStripIcon { background: transparent; border: none; }"
            f"QLabel#StudioLibraryLinkSeparator {{ color: {t['text3']}; }}"
            f"QLabel#StudioLibraryFooterText {{ color: {t['text3']}; }}"
            f"QPushButton[studioLibraryLink=\"true\"] {{ background: transparent; border: none; color: {t['accent-fg']}; padding: 2px 4px; }}"
            "QPushButton[studioLibraryLink=\"true\"]:hover { text-decoration: underline; }")

    def _refreshIcons(self):
        if not _isAlive(self.widget):
            return
        dragActive = bool(self.dropFrame.property("dragActive"))
        color = "accent-fg" if dragActive else "text3"
        self.dropStripIcon.setPixmap(ui.pixmap("import", 16, color))
        self.captionIcon.setPixmap(ui.pixmap("folder", 14, "text3"))
        for button, iconName in ((self.openButton, "import"), (self.openFolderButton, "folder-open"), (self.templateButton, "globe")):
            button.setIcon(ui.icon(iconName, 16, "onAccent" if button is self.openButton else "normal"))
        self.templateMenuButton.setIcon(ui.icon("chevron-down", 16))
        for button in (self.newStudyLink, self.openStudyLink, self.sampleDataLink, self.classicLink):
            if button is not None:
                button.setIcon(ui.icon(str(button.property("studioIconName")), 16))

    def _refreshCaption(self):
        """The caption under the title: where the projects are saved (A-09), else what Library is for."""
        try:
            from . import projects

            enabled = projects.isEnabled()
            folder = projects.shortPath(projects.root())
            folder = folder[2:] if folder.startswith("~/") else folder
            text = _("Saved automatically in {folder}").format(folder=folder.replace("/", " \u203a ")) if enabled else ""
            toolTip = projects.root() if enabled else ""
        except Exception:
            enabled, text, toolTip = False, "", ""
        self.pageTitle.setText(_("Projects") if enabled else _("Library"))
        if not enabled:
            text = _("Open scans and lesion masks, or start on the MNI template")
        self.captionLabel.setText(text)
        self.captionLabel.setToolTip(toolTip)
        self.captionLink.setVisible(enabled)
        self.captionSeparator.setVisible(enabled)
        self.captionIcon.setVisible(enabled)

    def eventFilter(self, obj, event):
        # A click on the drop zone (or on its frame) opens files
        if obj in (self.dropZone, self.dropFrame) and event.type() in (qt.QEvent.MouseButtonPress, qt.QEvent.MouseButtonRelease):
            if event.button() != qt.Qt.LeftButton:
                return False
            if event.type() == qt.QEvent.MouseButtonRelease and obj.rect.contains(_eventPosition(event)):
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

    @property
    def studyPage(self):
        """The study page (created when a study is open for the first time)."""
        if self._studyPage is None:
            self._studyPage = StudyPage()
            self.studyRow.addStretch(1)
            self.studyRow.addWidget(self._studyPage.widget, 40)
            self.studyRow.addStretch(1)
        return self._studyPage

    def _showStudy(self, shown):
        """The study page replaces the column (drop zone, buttons, Recent) while a study is open (R7)."""
        if shown == self._studyShown:
            return
        self._studyShown = shown
        self.studyContainer.setVisible(shown)
        self.column.setVisible(not shown)
        # Content stretches: top, notices, study page, column row, bottom
        self.contentLayout.setStretch(2, 1 if shown else 0)
        self.contentLayout.setStretch(4, 0 if shown else 1)

    def refreshData(self):
        """Show the open study, else the current work (and the loaded set) when data is loaded, else the empty state."""
        studySession = _studySession()
        if studySession is not None:
            self._showStudy(True)
            try:
                self.studyPage.refresh()
            except Exception:
                logging.error(f"Studio Library: the study page failed:\n{traceback.format_exc()}")
            for card in (self._currentWork, self._loadedSet):
                if card is not None:
                    card.widget.hide()
            self.dataSection.hide()
            return True
        if self._studyPage is not None:
            self._showStudy(False)
        data = hasData()
        shown = False
        if data:
            # A card that fails never keeps the page from showing
            for card in (self.currentWork, self.loadedSet):
                try:
                    shown = card.refresh() or shown
                except Exception:
                    logging.error(f"Studio Library: {type(card).__name__} failed:\n{traceback.format_exc()}")
                    card.widget.hide()
            # A card made now (a scan was loaded while Library showed) starts with the open project, if there is one
            self._syncCurrentProject()
        else:
            for card in (self._currentWork, self._loadedSet):
                if card is not None:
                    card.widget.hide()
        self.dataSection.setVisible(shown)
        self._setStrip(shown)
        return shown

    def _setStrip(self, strip):
        """While the current work is shown the page has no drop zone (V-02: the header caption says that files can be
        dropped anywhere) and the Library buttons are in the header; else the 480 x 220 zone of the empty state in its
        620 x 340 dashed frame under the header, with the Library buttons in it (VISUAL 4.2).
        """
        if self._dataShown == strip:
            return
        self._dataShown = strip
        self.dropFull.setVisible(not strip)
        self.dropCompact.setVisible(strip)
        self.dropFrame.setProperty("studioStrip", bool(strip))
        if strip:
            # With data the page has no drop strip (V-02): files are dropped anywhere on the window, as the header says
            self.dropZone.setMinimumWidth(0)
            self.dropZone.setMaximumWidth(16777215)
            self.dropZone.setFixedHeight(DROP_STRIP_HEIGHT)
            self.dropZone.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
            self.dropFrame.setMinimumSize(0, 0)
            self.dropFrame.setMaximumSize(16777215, DROP_STRIP_HEIGHT)
            self.dropFrame.layout().removeWidget(self.dropZone)
            self.dropFrame.layout().addWidget(self.dropZone)
            self.dropBottomSlot.addWidget(self.dropFrame)
            self.headerButtons.layout().addWidget(self.buttonRow)
        else:
            self.dropZone.setFixedSize(COLUMN_WIDTH, DROP_ZONE_HEIGHT)
            self.dropZone.setSizePolicy(qt.QSizePolicy.Fixed, qt.QSizePolicy.Fixed)
            self.dropFrame.setFixedHeight(DROP_FRAME_SIZE[1])
            self.dropFrame.setMinimumWidth(min(DROP_FRAME_SIZE[0], COLUMN_WIDTH + 40))
            self.dropFrame.setMaximumWidth(DROP_FRAME_SIZE[0])
            self.dropFrame.layout().removeWidget(self.dropZone)
            self.dropFrame.layout().addWidget(self.dropZone, 0, qt.Qt.AlignCenter)
            self.dropTopSlot.addWidget(self.dropFrame, 0, qt.Qt.AlignHCenter)
            self.dropButtonsSlot.addWidget(self.buttonRow, 0, qt.Qt.AlignHCenter)
        self.dropTopSpacing.setVisible(not strip)
        self.headerButtons.setVisible(strip)
        self.dropFrame.setVisible(not strip)
        self.captionDropHint.setVisible(bool(strip))
        style = self.dropFrame.style()
        style.unpolish(self.dropFrame)
        style.polish(self.dropFrame)
        self.dropZone.setToolTip(_("Drop more files here, or click to open files") if strip else _("Drop files here, or click to open files"))

    def isStrip(self):
        return bool(self._dataShown)

    def _syncCurrentProject(self):
        """Show the open project (as the Projects section lists it) on the Continue card."""
        if self._currentWork is not None and self.projectsSection is not None and hasattr(self.projectsSection, "currentInfo"):
            self._currentWork.setProject(self.projectsSection.currentInfo())

    def refresh(self):
        self.refreshData()
        if self.projectsSection is not None:
            self.projectsSection.refresh()
            self._syncCurrentProject()
        if self.studiesSection is not None:
            try:
                self.studiesSection.refresh()
            except Exception:
                logging.error(f"Studio Library: the Studies section failed:\n{traceback.format_exc()}")
        self._refreshCaption()
        self.recentList.refresh(force=False)

    def focusFirst(self):
        """Focus the subject list of an open study, else "Continue in Draw" when the current work is shown, else the first recent
        file (Enter loads it), else Open….
        """
        if self._studyPage is not None and self._studyShown:
            self._studyPage.focusFirst()
        elif self.isStrip() and self.currentWork.widget.isVisible() and self.currentWork.continueButton.isEnabled():
            self.currentWork.continueButton.setFocus(qt.Qt.OtherFocusReason)
        elif self.isStrip() and self.loadedSet.widget.isVisible():
            self.loadedSet.reviewButton.setFocus(qt.Qt.OtherFocusReason)
        elif not self.recentList.focusFirst():
            self.openButton.setFocus(qt.Qt.OtherFocusReason)

    def setDropHighlight(self, active):
        if bool(self.dropZone.property("dragActive")) == bool(active):
            return
        for widget in (self.dropZone, self.dropFrame):
            widget.setProperty("dragActive", bool(active))
            style = widget.style()
            style.unpolish(widget)
            style.polish(widget)
            widget.update()
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

    def addNotice(self, widget):
        """Show ``widget`` in the notice slot (``showNotice()``), after the notices already shown."""
        self.noticeLayout.addWidget(widget)
        widget.show()
        self.noticeContainer.show()

    def removeNotice(self, widget):
        """Take ``widget`` out of the notice slot without deleting it."""
        try:
            self.noticeLayout.removeWidget(widget)
            widget.hide()
            widget.setParent(None)
        except Exception:
            # Deleted by its owner
            pass
        self.noticeContainer.setVisible(self.noticeLayout.count() > 0)

    def dispose(self):
        ui.removePaletteListener(self._refreshIcons)
        # The notices belong to their owners: kept for the next canvas (``canvas()``)
        for widget in list(_notices.values()):
            self.removeNotice(widget)
        self.folderCardSlot.close()
        if self.projectsSection is not None:
            self.projectsSection.dispose()
        if self.studiesSection is not None:
            self.studiesSection.dispose()
        self.recentList.dispose()
        for card in (self._currentWork, self._loadedSet, self._studyPage):
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


class ProjectOverviewCard(qt.QObject):
    """The open project in the Library inspector (VISUAL 4.2): its thumbnail on black, "In this project" (images, lesions,
    maps and exports as 2 x 2 tiles), where it is and when it was created, and "Show in Files" and "Rename…".
    Hidden without a project (A-09).
    """

    THUMBNAIL_HEIGHT = 168
    STATS = (("scans", "Images"), ("lesions", "Lesions"), ("maps", "Maps"), ("exports", "Exports"))

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget = qt.QFrame()
        self.widget.setObjectName("StudioProjectOverview")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(ui.INSPECTOR_PADDING, 4, ui.INSPECTOR_PADDING, 8)
        layout.setSpacing(8)
        self.thumbnail = qt.QLabel()
        self.thumbnail.setObjectName("StudioProjectOverviewThumbnail")
        self.thumbnail.setFixedHeight(self.THUMBNAIL_HEIGHT)
        self.thumbnail.setAlignment(qt.Qt.AlignCenter)
        self.thumbnail.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        # The thumbnail heads the inspector, above "Open now" (VISUAL 4.2): a widget of its own that the panel places
        self.header = qt.QFrame()
        self.header.setObjectName("StudioProjectOverviewHeader")
        headerLayout = qt.QVBoxLayout(self.header)
        headerLayout.setContentsMargins(ui.INSPECTOR_PADDING, 4, ui.INSPECTOR_PADDING, 4)
        headerLayout.addWidget(self.thumbnail)
        self.header.hide()
        layout.addWidget(ui.sectionLabel(_("In this project"), "StudioProjectOverviewTitle"))
        grid = qt.QGridLayout()
        grid.setSpacing(8)
        self.statValues = {}
        for index, (key, caption) in enumerate(self.STATS):
            tile = qt.QFrame()
            tile.setObjectName("StudioProjectOverviewTile")
            tileLayout = qt.QVBoxLayout(tile)
            tileLayout.setContentsMargins(12, 8, 12, 8)
            tileLayout.setSpacing(0)
            value = ui.label("0", "number", "text", "StudioProjectOverviewValue")
            ui.setText(value, "title")
            tileLayout.addWidget(value)
            tileLayout.addWidget(ui.label(_(caption), "caption", "text3", "StudioProjectOverviewCaption"))
            grid.addWidget(tile, index // 2, index % 2)
            self.statValues[key] = value
        layout.addLayout(grid)
        layout.addSpacing(4)
        self.pathLabel = _elidedLabel("", 12, "StudioProjectOverviewPath")
        self.createdLabel = _elidedLabel("", 12, "StudioProjectOverviewCreated")
        layout.addWidget(self.pathLabel)
        layout.addWidget(self.createdLabel)
        buttons = qt.QHBoxLayout()
        buttons.setSpacing(8)
        self.showButton = _secondaryButton(_("Show in {manager}").format(manager=_fileManagerName()), "StudioProjectOverviewShowButton", self._show)
        self.renameButton = _secondaryButton(_("Rename…"), "StudioProjectOverviewRenameButton", self._rename)
        for button in (self.showButton, self.renameButton):
            button.setFixedHeight(ui.BUTTON_HEIGHT)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self._key = None
        ui.bindStyleSheet(self.widget, lambda: (
            "QFrame#StudioProjectOverview, QFrame#StudioProjectOverviewHeader { background: transparent; border: none; }"
            "QLabel#StudioProjectOverviewThumbnail { background-color: #000000; border: none; border-radius: 10px; }"
            f"QFrame#StudioProjectOverviewTile {{ background-color: {ui.token('card').name()}; border: none; border-radius: 10px; }}"
            "QFrame#StudioProjectOverviewTile QLabel { background: transparent; border: none; }"
            f"QLabel#StudioProjectOverviewPath, QLabel#StudioProjectOverviewCreated {{ color: {ui.token('text3').name()}; }}"))
        self.widget.hide()

    @staticmethod
    def _projects():
        try:
            from . import projects

            return projects
        except Exception:
            return None

    def _show(self):
        try:
            from . import projects_ui

            projects_ui.showProjectFolder()
        except Exception:
            logging.error(f"Studio Library: the project folder could not be shown:\n{traceback.format_exc()}")

    def _rename(self):
        try:
            from . import projects_ui

            projects_ui.renameDialog()
        except Exception:
            logging.error(f"Studio Library: the project could not be renamed:\n{traceback.format_exc()}")
        self._key = None
        self.refresh()

    def refresh(self):
        """Show the open project (hidden without one). Return True when it is shown."""
        projects = self._projects()
        project = projects.currentProject() if projects is not None else None
        if project is None or not os.path.isdir(project.path):
            self.widget.hide()
            self.header.hide()
            self._key = None
            return False
        info = projects.ProjectInfo(project.path)
        exportsFolder = os.path.join(project.path, "Exports")
        try:
            exports = len([name for name in os.listdir(exportsFolder) if not name.startswith(".")])
        except OSError:
            exports = 0
        counts = dict(projects.counts())
        counts["exports"] = exports
        key = (info.path, info.thumbnail, tuple(sorted(counts.items())), info.created, ui.isDark())
        if key != self._key:
            self._key = key
            for name, value in self.statValues.items():
                value.setText(str(int(counts.get(name) or 0)))
            pixmap = qt.QPixmap(info.thumbnail) if info.thumbnail else qt.QPixmap()
            if pixmap.isNull():
                self.thumbnail.setPixmap(ui.pixmap("brain", 40, "#55555c"))
            else:
                self.thumbnail.setPixmap(pixmap.scaled(300, self.THUMBNAIL_HEIGHT - 8, qt.Qt.KeepAspectRatio, qt.Qt.SmoothTransformation))
            setElidedText(self.pathLabel, _("Projects \u203a {name}").format(name=info.name), info.path)
            created = time.strftime("%d %b %Y", time.localtime(info.created)) if info.created else ""
            setElidedText(self.createdLabel, _("Created {date}").format(date=created) if created else "")
            self.createdLabel.setVisible(bool(created))
        self.widget.show()
        self.header.show()
        return True

    def dispose(self):
        if _isAlive(self.header):
            _discard(self.header)
        if _isAlive(self.widget):
            ui.bindStyleSheet(self.widget, None)
            _discard(self.widget)


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
        self.overviewCard = None
        self.folderCardSlot = None
        self.recentCard = None
        self.recentList = None
        self.studyCards = None
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
        _listenToStudies()

        self.openCard, openLayout = ui.card(_("Open"))
        self.openCard.setObjectName("StudioLibraryOpenCard")
        self.openButton = _secondaryButton(_("Open…"), "StudioOpenButton", openFiles)
        self.openFolderButton = _secondaryButton(_("Open folder…"), "StudioOpenFolderButton", openFolder)
        self.templateButton = _secondaryButton(_("Start on MNI template"), "StudioStartOnTemplateButton", startOnTemplate)
        buttons = [self.openButton, self.openFolderButton, self.templateButton]
        self.openStudyButton = None
        if _studyUi() is not None:
            self.openStudyButton = _secondaryButton(_("Open study…"), "StudioOpenStudyButton", openStudy)
            buttons.append(self.openStudyButton)
        for button in buttons:
            row = _buttonRow()
            row.layout().addWidget(button)
            openLayout.addWidget(row)
        layout.addWidget(self.openCard)

        # The project's thumbnail, "Open now", then "In this project" (VISUAL 4.2)
        self.overviewCard = ProjectOverviewCard()
        layout.addWidget(self.overviewCard.header)
        self.openNowCard = OpenNowCard()
        layout.addWidget(self.openNowCard.widget)
        layout.addWidget(self.overviewCard.widget)

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
        """Show the cards that fit: with a study, its cards (STUDY, SUBJECT, BATCH, IMPORT, and in Classic the subject list);
        with the canvas, what is open; without it, the Open buttons, Recent and what is open.
        """
        if not self.isBuilt():
            return
        withCanvas = self.withCanvas()
        studySession = _studySession()
        if studySession is not None:
            self._ensureStudyCards(withList=not withCanvas)
            for card in (self.openCard, self.recentCard, self.openNowCard.widget, self.overviewCard.widget, self.overviewCard.header):
                card.hide()
            self.studyCards.refresh()
            return
        if self.studyCards is not None:
            self.studyCards.widget.hide()
        data = hasData()
        self.openCard.setVisible(not withCanvas)
        self.recentCard.setVisible(not withCanvas)
        self.openNowCard.widget.setVisible(withCanvas or data)
        if self.openNowCard.widget.isVisible():
            self.openNowCard.refresh()
        if withCanvas and data:
            self.overviewCard.refresh()
        else:
            self.overviewCard.widget.hide()
            self.overviewCard.header.hide()

    def _ensureStudyCards(self, withList):
        """The study cards (created when a study is first shown; created again when Studio and Classic swap the list)."""
        if self.studyCards is not None and (self.studyCards.list is not None) == withList:
            return
        if self.studyCards is not None:
            self.studyCards.dispose()
            _discard(self.studyCards.widget)
        self.studyCards = StudyCards(withList=withList)
        self.widget.layout().insertWidget(0, self.studyCards.widget)

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
            if panel.studyCards is not None:
                panel.studyCards.dispose()
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
        for key, widget in list(_notices.items()):
            if _isAlive(widget):
                _canvas.addNotice(widget)
            else:
                _notices.pop(key, None)
    return _canvas


#: Notices of the canvas: key -> widget (``showNotice()``), shown again when the canvas is created again
_notices = {}
NOTICE_SPACING_BELOW = 16


def showNotice(key, widget):
    """Show ``widget`` at the top of the Library canvas, above the drop zone, the current work and the study page.

    A notice with the same ``key`` is replaced (its widget is taken out, not deleted). The widget belongs to the
    caller, who deletes it after ``closeNotice(key)``; it is at most ``COLUMN_WIDTH`` wide. The Library inspector
    and Classic have no notice slot. Return the canvas.
    """
    previous = _notices.get(key)
    if previous is not None and previous != widget:
        closeNotice(key)
    libraryCanvas = canvas()
    _notices[key] = widget
    if previous != widget or not _isAlive(previous):
        libraryCanvas.addNotice(widget)
    return libraryCanvas


def closeNotice(key):
    """Take the notice ``key`` out of the canvas (not deleted). Return its widget, or None if there is none."""
    widget = _notices.pop(key, None)
    if widget is not None and _canvas is not None and _isAlive(_canvas.widget):
        _canvas.removeNotice(widget)
    elif widget is not None and _isAlive(widget):
        widget.hide()
    return widget


def notices():
    """The notices of the canvas: {key: widget}."""
    return dict(_notices)


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
    if _studySession() is not None or (shell is not None and getattr(shell, "studySession", None) is not None):
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
    _listenToStudies()
    studySession = _studySession()
    if studySession is not None:
        # The values that jobs posted while another workspace was shown (LNM addendum: merged when Library is shown)
        studySession.applyInbox()
    libraryCanvas = canvas()
    # An inspector notice with the key of a canvas notice points to it ("Show"): the user is here now
    for key in _notices:
        shell.clearInspectorNotice(key)
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

"""Ranking of the module finder in Slicer Studio, until the command palette (StudioLib/palette.py) exists.

The search pill, Ctrl+K, Ctrl+F and "All tools..." open the classic module finder
(``qSlicerModuleFinderDialog``). It matches substrings, lists the matches alphabetically and Enter opens
the first row, so natural partial names opened the wrong module ("vol" opened Add Scalar Volumes,
"mark" Landmark Registration, "trans" Transform Convert (BRAINS)).

In Studio, this plug-in ranks the matches like a person expects (``rankModules()``):

1. exact title or name ("volumes", "segment editor", "segmenteditor");
2. synonym ("fiducial", "point", "landmark" -> Markups; "window level", "contrast" -> Volumes;
   "paint", "brush", "draw", "lesion" -> Segment Editor; see ``SYNONYMS``);
3. title or name prefix ("vol" -> Volumes, Volume Rendering...);
4. prefix of a synonym, for queries of 3 characters or more ("fid" -> Markups, after "Fiducial Registration");
5. word starts: every word of the query starts a word of the title, in order ("seg ed", "stat");
6. substring of the title or name;
7. with "Search in full text" checked, the help, acknowledgment and contributors text.

Within a rank, the core modules (``CORE_MODULES``, in that order) come first, then the other modules,
then the command-line (CLI), BRAINS, registration, DICOM plug-in and developer modules; then shorter
titles. The best match is selected, so Enter opens it.

The ranking uses the filter model of the dialog: ``qSlicerModuleFactoryFilterModel.setShowModules()``
shows only the listed modules, in the order of the list. When the filter text is empty the dialog's
own filter is restored. ``uninstall(shell)`` disconnects everything and restores the dialog, so Classic
is unchanged.
"""

import logging
import re
import traceback

import qt
import slicer
from slicer.i18n import tr as _

#: Core modules, boosted above the others in this order
CORE_MODULES = ("Data", "Volumes", "Models", "Transforms", "Markups", "SegmentEditor", "Segmentations",
                "SegmentStatistics", "VolumeRendering")

#: Words people use for what a module does (lowercase query -> module names)
SYNONYMS = {
    "fiducial": ("Markups",),
    "fiducials": ("Markups",),
    "point": ("Markups",),
    "points": ("Markups",),
    "point list": ("Markups",),
    "landmark": ("Markups",),
    "landmarks": ("Markups",),
    "ruler": ("Markups",),
    "measure": ("Markups", "SegmentStatistics"),
    "measurement": ("Markups", "SegmentStatistics"),
    "distance": ("Markups",),
    "angle": ("Markups",),
    "curve": ("Markups",),
    "window level": ("Volumes",),
    "window/level": ("Volumes",),
    "window": ("Volumes",),
    "level": ("Volumes",),
    "w/l": ("Volumes",),
    "wl": ("Volumes",),
    "contrast": ("Volumes",),
    "brightness": ("Volumes",),
    "image": ("Volumes",),
    "scan": ("Volumes",),
    "paint": ("SegmentEditor",),
    "brush": ("SegmentEditor",),
    "draw": ("SegmentEditor",),
    "erase": ("SegmentEditor",),
    "eraser": ("SegmentEditor",),
    "lesion": ("SegmentEditor", "Segmentations"),
    "mask": ("SegmentEditor", "Segmentations"),
    "label": ("SegmentEditor", "Segmentations"),
    "labelmap": ("SegmentEditor", "Segmentations"),
    "threshold": ("SegmentEditor",),
    "contour": ("SegmentEditor",),
    "segment": ("SegmentEditor",),
    "segmentation": ("SegmentEditor", "Segmentations"),
    "lesion volume": ("SegmentStatistics",),
    "statistics": ("SegmentStatistics",),
    "stats": ("SegmentStatistics",),
    "matrix": ("Transforms",),
    "rotate": ("Transforms",),
    "translate": ("Transforms",),
    "move": ("Transforms",),
    "mesh": ("Models",),
    "surface": ("Models",),
    "3d": ("VolumeRendering", "Models"),
    "render": ("VolumeRendering",),
    "scene": ("Data",),
    "hierarchy": ("Data",),
    "files": ("Data",),
}

#: Ranks of ``rankModules()`` (lower is better)
RANK_EXACT = 0
RANK_SYNONYM = 1
RANK_PREFIX = 2
RANK_SYNONYM_PREFIX = 3
RANK_WORD_START = 4
RANK_SUBSTRING = 5
RANK_FULL_TEXT = 6

#: Minimum query length for prefixes of synonyms
SYNONYM_PREFIX_MINIMUM_LENGTH = 3

#: Module groups after the rank: core, other, demoted
_GROUP_CORE = 0
_GROUP_OTHER = 1
_GROUP_DEMOTED = 2

_DEMOTED_CATEGORIES = ("Registration", "Legacy", "Developer Tools", "Testing")
_DEMOTED_TITLE_PATTERN = re.compile(r"(?i)\(BRAINS\)|\bregistration\b|\bplugin\b")

_WORD_PATTERN = re.compile(r"[A-Za-z0-9]+")
_CAMEL_CASE_PATTERN = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")

_FILTER_ROLE_MODULE_NAME = qt.Qt.UserRole
_FILTER_ROLE_FULL_TEXT = qt.Qt.UserRole + 5

_finder = None


def _normalized(text):
    return " ".join(str(text or "").lower().split())


def _compact(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


class ModuleEntry:
    """What the ranking needs to know about one module of the finder."""

    def __init__(self, name, title, group=_GROUP_OTHER, coreIndex=None, fullText=""):
        self.name = name
        self.title = title or name
        self.group = group
        self.coreIndex = coreIndex
        self.fullText = (fullText or "").lower()
        self.titleLower = _normalized(self.title)
        self.nameLower = name.lower()
        self.titleWords = [word.lower() for word in _WORD_PATTERN.findall(self.title)]
        self.nameWords = [word.lower() for word in _CAMEL_CASE_PATTERN.findall(name)]

    def sortKey(self, rank):
        coreIndex = self.coreIndex if self.coreIndex is not None else len(CORE_MODULES)
        return (rank, self.group, coreIndex, len(self.title), self.titleLower)


def _wordsStartInOrder(queryWords, words):
    """True if each query word starts a word of ``words``, in order."""
    position = 0
    for queryWord in queryWords:
        while position < len(words) and not words[position].startswith(queryWord):
            position += 1
        if position == len(words):
            return False
        position += 1
    return True


def _synonymRanks(query):
    """{module name: rank} of the synonyms matching the query."""
    ranks = {}
    for synonym, moduleNames in SYNONYMS.items():
        if synonym == query:
            rank = RANK_SYNONYM
        elif len(query) >= SYNONYM_PREFIX_MINIMUM_LENGTH and synonym.startswith(query):
            rank = RANK_SYNONYM_PREFIX
        else:
            continue
        for moduleName in moduleNames:
            ranks[moduleName] = min(rank, ranks.get(moduleName, rank))
    return ranks


def moduleRank(entry, query, synonymRanks=None, fullText=False):
    """Rank of a module for a normalized (lowercase) query, None if it does not match."""
    if not query:
        return None
    compactQuery = _compact(query)
    ranks = []
    if query in (entry.titleLower, entry.nameLower) or (compactQuery and compactQuery in (_compact(entry.title), entry.nameLower)):
        ranks.append(RANK_EXACT)
    synonymRank = (synonymRanks or {}).get(entry.name)
    if synonymRank is not None:
        ranks.append(synonymRank)
    if entry.titleLower.startswith(query) or entry.nameLower.startswith(compactQuery or query):
        ranks.append(RANK_PREFIX)
    queryWords = _WORD_PATTERN.findall(query)
    if queryWords and (_wordsStartInOrder(queryWords, entry.titleWords) or _wordsStartInOrder(queryWords, entry.nameWords)):
        ranks.append(RANK_WORD_START)
    if query in entry.titleLower or (compactQuery and compactQuery in entry.nameLower):
        ranks.append(RANK_SUBSTRING)
    if fullText and not ranks and queryWords and all(word in entry.fullText for word in queryWords):
        ranks.append(RANK_FULL_TEXT)
    return min(ranks) if ranks else None


def rankModules(query, entries, fullText=False):
    """Return the names of the modules matching ``query``, best first (see the module docstring)."""
    query = _normalized(query)
    if not query:
        return []
    synonymRanks = _synonymRanks(query)
    ranked = []
    for entry in entries:
        rank = moduleRank(entry, query, synonymRanks, fullText)
        if rank is not None:
            ranked.append((entry.sortKey(rank), entry.name))
    ranked.sort()
    return [name for key, name in ranked]


def _moduleGroup(moduleName, title):
    """Core, other or demoted (CLI, BRAINS, registration, DICOM plug-ins, developer tools)."""
    if moduleName in CORE_MODULES:
        return _GROUP_CORE
    if moduleName.startswith("BRAINS") or _DEMOTED_TITLE_PATTERN.search(title or ""):
        return _GROUP_DEMOTED
    module = None
    try:
        moduleManager = slicer.app.moduleManager()
        if moduleManager.factoryManager().isLoaded(moduleName):
            module = moduleManager.module(moduleName)
    except Exception:
        module = None
    if module is not None:
        if module.inherits("qSlicerCLIModule"):
            return _GROUP_DEMOTED
        categories = module.categories if not callable(module.categories) else module.categories()
        for category in categories or []:
            if str(category).split(".")[0] in _DEMOTED_CATEGORIES:
                return _GROUP_DEMOTED
    return _GROUP_OTHER


def _property(qobject, name):
    """A Qt property or getter, wrapped either as an attribute or as a method by PythonQt."""
    value = getattr(qobject, name)
    return value() if callable(value) else value


def _isAlive(qobject):
    """False if the C++ object was deleted."""
    try:
        qobject.objectName
    except Exception:
        return False
    return True


class _FinderRanking:
    """Ranks the rows of the module finder dialog while Studio is shown."""

    def __init__(self, dialog):
        self.dialog = dialog
        self.searchBox = dialog.findChild("QWidget", "FilterTitleSearchBox")
        self.listView = dialog.findChild("QWidget", "ModuleListView")
        self.fullTextCheckBox = dialog.findChild("QCheckBox", "SearchInAllTextCheckBox")
        if self.searchBox is None or self.listView is None:
            raise RuntimeError("module finder widgets not found")
        self.filterModel = self.listView.filterModel()
        self.entries = {}
        self.ranked = []
        self.classicWindowTitle = dialog.windowTitle
        self.classicHideAll = bool(_property(self.filterModel, "hideAllWhenShowModulesIsEmpty"))
        # setShowModules() sets a regular expression, which also sets the case sensitivity of the filter
        self.classicCaseSensitivity = _property(self.filterModel, "filterCaseSensitivity")
        self.classicSortColumn = self.filterModel.sortColumn()
        self.classicSortOrder = self.filterModel.sortOrder()
        self.connections = []
        # Connected after the dialog's own slots, so the ranking runs after the dialog's filter
        self._connect(self.searchBox, "textChanged(QString)", self._onFilterChanged)
        if self.fullTextCheckBox is not None:
            self._connect(self.fullTextCheckBox, "toggled(bool)", self._onFilterChanged)
        dialog.setWindowTitle(_("Search modules"))
        self._onFilterChanged()

    def _connect(self, sender, signal, slot):
        sender.connect(signal, slot)
        self.connections.append((sender, signal, slot))

    def _sourceModel(self):
        return _property(self.filterModel, "sourceModel")

    def moduleEntries(self):
        """The modules of the finder's model (cached by module name)."""
        sourceModel = self._sourceModel()
        entries = []
        for row in range(sourceModel.rowCount()):
            index = sourceModel.index(row, 0)
            name = sourceModel.data(index, _FILTER_ROLE_MODULE_NAME)
            if not name:
                continue
            title = sourceModel.data(index, qt.Qt.DisplayRole) or name
            entry = self.entries.get(name)
            if entry is None or entry.title != title:
                coreIndex = CORE_MODULES.index(name) if name in CORE_MODULES else None
                entry = ModuleEntry(name, title, _moduleGroup(name, title), coreIndex,
                                    sourceModel.data(index, _FILTER_ROLE_FULL_TEXT) or "")
                self.entries[name] = entry
            entries.append(entry)
        return entries

    def _fullTextSearch(self):
        return self.fullTextCheckBox is not None and self.fullTextCheckBox.checked

    def _onFilterChanged(self, *args):
        try:
            self.applyRanking()
        except Exception:
            logging.error(f"Studio: module finder ranking failed:\n{traceback.format_exc()}")

    def applyRanking(self):
        """Show the matches of the current filter text, best first, and select the best one."""
        text = self.searchBox.text
        if callable(text):
            text = text()
        if not _normalized(text):
            self.ranked = []
            self.restoreClassicFilter()
            return
        self.ranked = rankModules(text, self.moduleEntries(), self._fullTextSearch())
        self.filterModel.hideAllWhenShowModulesIsEmpty = True
        # setShowModules() returns early for an unchanged list, but the dialog has just replaced the
        # filter with the typed text: set the list again
        self.filterModel.setShowModules([])
        self.filterModel.setShowModules(self.ranked)
        self.selectBestMatch()

    def selectBestMatch(self):
        """Select the first row (the best match), so that Enter opens it."""
        if self.filterModel.rowCount() > 0:
            first = self.filterModel.index(0, 0)
            self.listView.setCurrentIndex(first)
            self.listView.scrollTo(first)
        else:
            self.listView.selectionModel().clearSelection()

    def restoreClassicFilter(self):
        """Restore the dialog's own filter: the typed text on the title (or full text), in model order."""
        self.filterModel.hideAllWhenShowModulesIsEmpty = self.classicHideAll
        self.filterModel.setShowModules([])
        self.dialog.setSearchInAllText(self._fullTextSearch())
        self.filterModel.filterCaseSensitivity = self.classicCaseSensitivity
        text = self.searchBox.text
        self.filterModel.setFilterFixedString(text() if callable(text) else text)
        self.filterModel.sort(self.classicSortColumn, self.classicSortOrder)

    def dispose(self):
        for sender, signal, slot in reversed(self.connections):
            try:
                sender.disconnect(signal, slot)
            except Exception:
                pass
        self.connections = []
        if _isAlive(self.dialog):
            self.restoreClassicFilter()
            self.dialog.setWindowTitle(self.classicWindowTitle)


def finderDialog(mainWindow=None):
    """The module finder dialog of the module selector toolbar (None if not found)."""
    mainWindow = mainWindow or slicer.util.mainWindow()
    if mainWindow is None or not hasattr(mainWindow, "moduleSelector"):
        return None
    moduleSelector = mainWindow.moduleSelector()
    if moduleSelector is None:
        return None
    dialogs = moduleSelector.findChildren("qSlicerModuleFinderDialog")
    return dialogs[0] if dialogs else None


def instance():
    """The ranking of the finder while Studio is shown (None otherwise). Used by the tests."""
    return _finder


def install(shell):
    """Rank the module finder's matches while Studio is shown."""
    global _finder
    uninstall(shell)
    dialog = finderDialog(shell.mainWindow if shell is not None else None)
    if dialog is None:
        logging.warning("Studio: module finder dialog not found, its matches are not ranked")
        return
    _finder = _FinderRanking(dialog)


def uninstall(shell):
    """Disconnect from the module finder and restore its classic filter and title."""
    global _finder
    if _finder is None:
        return
    finder = _finder
    _finder = None
    finder.dispose()

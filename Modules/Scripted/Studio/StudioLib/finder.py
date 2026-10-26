"""Ranking of the module finder and of Search (the command palette) in Slicer Studio.

Search (``StudioLib.palette``, Ctrl+K) ranks every kind of entry with one function of this module,
``searchTier()`` (see "Search tiers" below). The classic module finder (``qSlicerModuleFinderDialog``) is
still reachable (``moduleSelector().showModuleFinder()``, and it is what the search pill opens in builds
without the palette). It matches substrings, lists the matches alphabetically and Enter opens the first
row, so natural partial names opened the wrong module ("vol" opened Add Scalar Volumes, "mark" Landmark
Registration, "trans" Transform Convert (BRAINS)).

In Studio, this plug-in ranks the matches like a person expects (``rankModules()``):

1. exact title or name ("volumes", "segment editor", "segmenteditor");
2. synonym ("fiducial", "point", "landmark" -> Markups; "window level", "contrast" -> Volumes;
   "paint", "brush", "draw", "lesion" -> Segment Editor; see ``SYNONYMS``);
3. title or name prefix ("vol" -> Volumes, Volume Rendering...);
4. word starts: every word of the query starts a word of the title, in order ("seg ed", "stat");
5. prefix of a synonym, for queries of 3 characters or more ("fid" -> Markups, after "Fiducial Registration";
   after the title word starts, so "fil" lists the Filter modules before Data from "files");
6. substring of the title or name;
7. with "Search in full text" checked, the help, acknowledgment and contributors text.

Within a rank, the core modules (``CORE_MODULES``, in that order) come first, then the other modules,
then the command-line (CLI), BRAINS, registration, DICOM plug-in and developer modules; then shorter
titles. The best match is selected, so Enter opens it.

The ranking uses the filter model of the dialog: ``qSlicerModuleFactoryFilterModel.setShowModules()``
shows only the listed modules, in the order of the list. When the filter text is empty the dialog's
own filter is restored. ``uninstall(shell)`` disconnects everything and restores the dialog, so Classic
is unchanged.

Search tiers (SPEC 2.12, USABILITY 4; lower is better), for modules, tools, actions, view commands,
layouts and the other entries of Search alike (``SearchKey``, ``searchTier()``):

0. exact title or alias ("volumes", "segment editor" for Draw);
1. command synonym, exact: synonyms of actions, view commands, tools and workspaces ("fiducial" -> Place point);
2. module synonym, exact (``SYNONYMS``: "fiducial" -> Markups);
3. title or alias prefix ("vol" -> Volumes, "four" -> Four-Up);
4. synonym prefix, for queries of 3 characters or more;
5. word starts in order ("seg ed" -> Draw through its alias Segment Editor);
6. substring;
7. full text (help text), only when nothing above matched (the caller decides; ``TIER_FULL_TEXT``).

The module finder keeps its own module-only order (``rankModules()``, ranks ``RANK_*``). In that scale a
module synonym is ``RANK_MODULE_SYNONYM``: after a command synonym and before a prefix, as in Search.
"""

import logging
import re
import sys
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
RANK_WORD_START = 3
RANK_SYNONYM_PREFIX = 4
RANK_SUBSTRING = 5
RANK_FULL_TEXT = 6
#: A module synonym when modules and commands are ranked together: after a command synonym (``RANK_SYNONYM``)
#: and before a prefix (``RANK_PREFIX``). Search uses the integer tiers below (``TIER_MODULE_SYNONYM``).
RANK_MODULE_SYNONYM = 1.5

#: Tiers of Search (``searchTier()``; lower is better)
TIER_EXACT = 0
TIER_COMMAND_SYNONYM = 1
TIER_MODULE_SYNONYM = 2
TIER_PREFIX = 3
TIER_SYNONYM_PREFIX = 4
TIER_WORD_START = 5
TIER_SUBSTRING = 6
TIER_FULL_TEXT = 7

#: Minimum query length for prefixes of synonyms
SYNONYM_PREFIX_MINIMUM_LENGTH = 3

#: Module groups after the rank: core, other, demoted
_GROUP_CORE = 0
_GROUP_OTHER = 1
_GROUP_DEMOTED = 2

_DEMOTED_CATEGORIES = ("Registration", "Legacy", "Developer Tools", "Testing")
_DEMOTED_TITLE_PATTERN = re.compile(r"(?i)\(BRAINS\)|\bregistration\b|\bplugin\b")

_WORD_PATTERN = re.compile(r"[A-Za-z0-9]+")
_QUERY_SEPARATOR_PATTERN = re.compile(r"[\s_-]+")
_CAMEL_CASE_PATTERN = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")

_FILTER_ROLE_MODULE_NAME = qt.Qt.UserRole
_FILTER_ROLE_FULL_TEXT = qt.Qt.UserRole + 5

_finder = None


def _normalized(text):
    return " ".join(str(text or "").lower().split())


def normalizedQuery(text):
    """Lowercase text with single spaces and without a trailing ellipsis ("Open…" and "open" are the same)."""
    text = _normalized(text)
    for ellipsis in ("…", "..."):
        if text.endswith(ellipsis):
            text = text[: -len(ellipsis)].rstrip()
    return text


def _compact(text):
    """Text without spaces, '-' and '_' ("segment-editor" -> "segmenteditor").

    Other punctuation is kept, so that it still has to match ("c++" does not become "c").
    """
    return _QUERY_SEPARATOR_PATTERN.sub("", text.lower())


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
    # Query words are separated by spaces, '-' or '_' only: other punctuation must match ("c++" is not "c")
    queryWords = [word for word in _QUERY_SEPARATOR_PATTERN.split(query) if word]
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


#
# Search tiers (the command palette)
#


def _moduleSynonymIndex():
    """{module name: synonyms of ``SYNONYMS`` that name it}."""
    index = {}
    for synonym, moduleNames in SYNONYMS.items():
        for moduleName in moduleNames:
            index.setdefault(moduleName, set()).add(synonym)
    return {moduleName: frozenset(synonyms) for moduleName, synonyms in index.items()}


_MODULE_SYNONYMS = _moduleSynonymIndex()


def moduleSynonyms(moduleName):
    """The synonyms of ``SYNONYMS`` that name the module ``moduleName``."""
    return _MODULE_SYNONYMS.get(moduleName, frozenset())


def queryWords(query):
    """Words of a normalized query: separated by spaces, '-' or '_' (other punctuation must match)."""
    return [word for word in _QUERY_SEPARATOR_PATTERN.split(query) if word]


class SearchKey:
    """What ``searchTier()`` needs to know about one entry of Search, lowercased once.

    :param title: the name shown ("Volumes", "Window/level: Auto", "Four-Up").
    :param aliases: other names that match like the title ("Segment Editor" for Draw).
    :param commandSynonyms: words for what a command does ("fiducial" for Place point); tier 1.
    :param moduleName: the module of a module entry: its name matches like the title ("SegmentEditor") and its
        ``SYNONYMS`` are module synonyms (tier 2).
    """

    __slots__ = (
        "commandSynonyms",
        "compactExact",
        "compactNames",
        "compactSubstrings",
        "exact",
        "moduleSynonyms",
        "prefixTexts",
        "substringTexts",
        "wordLists",
    )

    def __init__(self, title, aliases=(), commandSynonyms=(), moduleName=None):
        texts = [normalizedQuery(text) for text in (title, *aliases) if text]
        self.exact = frozenset(texts)
        self.compactExact = frozenset(_compact(text) for text in texts) | ({moduleName.lower()} if moduleName else frozenset())
        self.prefixTexts = tuple(texts)
        self.compactNames = (moduleName.lower(),) if moduleName else ()
        self.wordLists = tuple([word.lower() for word in _WORD_PATTERN.findall(text)] for text in texts)
        if moduleName:
            self.wordLists += ([word.lower() for word in _CAMEL_CASE_PATTERN.findall(moduleName)],)
        self.substringTexts = tuple(texts)
        self.compactSubstrings = self.compactNames
        self.commandSynonyms = frozenset(normalizedQuery(synonym) for synonym in commandSynonyms if synonym)
        self.moduleSynonyms = moduleSynonyms(moduleName) if moduleName else frozenset()


def searchTier(key, query, compactQuery=None, words=None):
    """Tier of an entry (its ``SearchKey``) for a normalized query (``normalizedQuery()``), None if it does not match.

    The full text tier (``TIER_FULL_TEXT``) is not computed here: Search uses it only when no entry matched.
    """
    if not query:
        return None
    if compactQuery is None:
        compactQuery = _compact(query)
    if query in key.exact or (compactQuery and compactQuery in key.compactExact):
        return TIER_EXACT
    if query in key.commandSynonyms:
        return TIER_COMMAND_SYNONYM
    if query in key.moduleSynonyms:
        return TIER_MODULE_SYNONYM
    for text in key.prefixTexts:
        if text.startswith(query):
            return TIER_PREFIX
    if compactQuery:
        for name in key.compactNames:
            if name.startswith(compactQuery):
                return TIER_PREFIX
    if len(query) >= SYNONYM_PREFIX_MINIMUM_LENGTH:
        for synonym in key.commandSynonyms:
            if synonym.startswith(query):
                return TIER_SYNONYM_PREFIX
        for synonym in key.moduleSynonyms:
            if synonym.startswith(query):
                return TIER_SYNONYM_PREFIX
    if words is None:
        words = queryWords(query)
    if words:
        for wordList in key.wordLists:
            if _wordsStartInOrder(words, wordList):
                return TIER_WORD_START
    for text in key.substringTexts:
        if query in text:
            return TIER_SUBSTRING
    if compactQuery:
        for name in key.compactSubstrings:
            if compactQuery in name:
                return TIER_SUBSTRING
    return None


def matchedRanges(text, query):
    """Character ranges ``[(start, end)]`` of ``text`` that match a normalized query, for highlighting.

    The whole query where it is a substring (case insensitive), else the start of each word of the text that a
    query word starts, in order. Empty when the entry matched by a synonym only.
    """
    if not text or not query:
        return []
    lowerText = text.lower()
    position = lowerText.find(query)
    if position >= 0:
        return [(position, position + len(query))]
    ranges = []
    words = queryWords(query)
    searchFrom = 0
    textWords = [(match.start(), match.group(0).lower()) for match in _WORD_PATTERN.finditer(text)]
    for queryWord in words:
        while searchFrom < len(textWords) and not textWords[searchFrom][1].startswith(queryWord):
            searchFrom += 1
        if searchFrom == len(textWords):
            return []
        start = textWords[searchFrom][0]
        ranges.append((start, start + len(queryWord)))
        searchFrom += 1
    return ranges


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


#: Groups of Search, after the tier: workspaces, core modules and commands; other modules; demoted modules
GROUP_CORE = _GROUP_CORE
GROUP_OTHER = _GROUP_OTHER
GROUP_DEMOTED = _GROUP_DEMOTED


def moduleGroup(moduleName, title=None):
    """``GROUP_CORE`` for the core modules, ``GROUP_DEMOTED`` for CLI, BRAINS, registration, DICOM plug-in and
    developer modules, else ``GROUP_OTHER``.
    """
    return _moduleGroup(moduleName, title or moduleName)


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
    """Disconnect from the module finder and restore its classic filter and title; close Search and the shortcut list."""
    global _finder
    # Search (Ctrl+K) and the shortcut list (Ctrl+/) exist only while Studio is shown
    for name in ("StudioLib.palette", "StudioLib.cheatsheet"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "dispose"):
            try:
                module.dispose()
            except Exception:
                logging.error(f"Studio: {name}.dispose() failed:\n{traceback.format_exc()}")
    if _finder is None:
        return
    finder = _finder
    _finder = None
    finder.dispose()

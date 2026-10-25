r"""Search, the command palette of Slicer Studio (``Ctrl+K``; SPEC 2.12, USABILITY 4).

Everything that is not in a workspace is one keystroke away. ``open(shell, category=None)`` shows a
560 x 420 frameless popup near the top of the main window: a 40 px search field and 32 px rows (icon, title
with the matched characters in the accent colour, category, key). Enter runs the current row, the arrows
move, Esc (or ``Ctrl+K`` again) closes, a click runs a row. ``category="modules"`` (``Ctrl+F``) lists only
the modules and the workspaces that replace them.

The index is built on the first open and refreshed incrementally on later opens:

- the workspaces, which replace their modules ("Draw", alias "Segment Editor"; "Library", alias "Studio";
  "Map", alias "Lesion network mapping") and the Classic interface;
- every other module that is not hidden (``slicer.app.moduleManager()``), opened as a transient module;
- the Segment Editor tools by their Studio name ("Brush" for Paint), with their live positional key;
- built-in actions (Open…, Open folder…, Start on MNI template, New lesion, Save lesion as NIfTI…, Close
  scene, Save now (the project, else the Save dialog), Place point, Python console, Error log, Settings…, Keyboard shortcuts, Recent
  messages, theme, density, Hide panels, Show inspector, Reset panel width, Show Data Probe);
- view commands (Show crosshair, Slice intersections, Link views, Reset views, Maximize view,
  Window/level: Auto, Brain, Narrow and Reset, and the CT display presets of the Volumes module, applied in place);
- layouts, one entry per layout id (the picker's and the Layout menu's, Studio Focus, Focus + 3D, Row,
  Compare while a figure is shown);
- recent files (``importer.loadRecentFile``: a file that is open is shown again, never loaded twice),
  sample data, the pages of the settings dialog, "Show toolbar: <name>", the guided tours and the
  'Where did it go?' answers of ``StudioLib.migration`` ("where is add data");
- entries of other work packages: ``registerProvider(fn)`` and module-level ``PALETTE_ACTIONS`` lists of the
  already imported ``StudioLib.libcard_*`` and ``LesionNetworkMappingUI.card_*`` modules.

**Provider contract** (one contract; SPEC 2.10 and 2.12). ``registerProvider(fn)`` / ``unregisterProvider(fn)``.
The palette calls ``fn(shell)`` when it opens and, if that raises ``TypeError``, ``fn()``. ``fn`` returns a list
whose items are either dicts ``{id, title, category, key=None, synonyms=[], callback, enabled=True (a bool or a
callable), priority=0, description='', aliases=[], icon=None}`` or the tuples ``(title, keywords, callback,
enabledFn)`` of the LNM addendum, adapted to a dict with the category "Map" and the id "lnm." + title.
``PALETTE_ACTIONS`` lists use the same item forms. For equal ids the later source wins: built-in entries,
then ``PALETTE_ACTIONS``, then the providers in their registration order (so a provider may replace a
built-in entry by using its id, for example "draw.saveLesion").

**Ranking** is ``finder.searchTier()`` for every entry (exact title or alias, command synonym, module
synonym, prefix, synonym prefix, word starts, substring; the help text only when nothing matched). Within a
tier: the most recently run entries first (``Studio/PaletteMRU``, last 20 ids), then the entry's priority
(higher first), its group (workspaces, core modules, commands and the picker's layouts; other modules and the
other classic layouts; CLI, BRAINS, registration and developer modules; 'Where did it go?' answers) and the
shorter title.

**Empty query**: "Recent tools" (the most recently run entries), "Recent files" and "Favorites", built from the
classic ``Modules/FavoriteModules`` setting, which is only read. A query without a match shows
"No match for “xyz”. Try “save”, “export”, “figure”, “crosshair”."

Settings: ``Studio/PaletteMRU`` only. Nothing here runs in Classic or under ``--testing``: the shell opens the
palette only while Studio is shown.
"""

import logging
import os
import re
import sys
import time
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import finder, layouts, ui, workspaces

#: Most recently run entries (ids), most recent first
MRU_KEY = "Studio/PaletteMRU"
MRU_SIZE = 20
#: The classic favorite modules (an official key: read, never written)
FAVORITES_KEY = "Modules/FavoriteModules"

POPUP_WIDTH = 560
POPUP_HEIGHT = 420
POPUP_TOP = 64
POPUP_MARGIN = 16
FIELD_HEIGHT = 40
ROW_HEIGHT = 32
HEADER_HEIGHT = 24
FOOTER_HEIGHT = 24
ICON_SIZE = 16
#: Rows shown for a query
MAX_RESULTS = 60
#: 'Where did it go?' answers shown for a query
MAX_WHERE_ANSWERS = 3
#: Rows of the empty query sections
MAX_RECENT_TOOLS = 6
MAX_RECENT_FILES = 5

#: ``open(shell, category=CATEGORY_MODULES)``: only the modules (and the workspaces that replace them)
CATEGORY_MODULES = "modules"

#: Kinds of entries
KIND_WORKSPACE = "workspace"
KIND_MODULE = "module"
KIND_TOOL = "tool"
KIND_ACTION = "action"
KIND_VIEW = "view"
KIND_LAYOUT = "layout"
KIND_RECENT = "recent"
KIND_SAMPLE = "sample"
KIND_SETTINGS = "settings"
KIND_TOOLBAR = "toolbar"
KIND_TOUR = "tour"
KIND_WHERE = "where"
KIND_PROVIDER = "provider"

#: Group of the 'Where did it go?' answers: after every other entry of the same tier
GROUP_WHERE = finder.GROUP_DEMOTED + 1
#: Group of the Draw tools under "More tools": after the core modules, commands and shown tools, before the other modules
GROUP_MORE_TOOLS = (finder.GROUP_CORE + finder.GROUP_OTHER) / 2.0

#: Workspace modules: listed as their workspace, never as a module
_WORKSPACE_MODULES = tuple(ws.moduleName for ws in workspaces.WORKSPACES)

#: Studio names of the Segment Editor effects (D27; the effect names are unchanged) and their synonyms
TOOL_NAMES = {
    "Paint": (_("Brush"), ("paint", "brush", "draw lesion", "pen", "paintbrush")),
    "Draw": (_("Outline"), ("outline", "contour", "polygon", "trace outline", "freehand")),
    "Erase": (_("Erase"), ("erase", "eraser", "rubber", "delete voxels")),
    "Smart fill": (_("Smart fill"), ("smart fill", "flood fill", "region grow", "grow region", "fill lesion")),
    "Level tracing": (_("Auto outline"), ("level tracing", "auto outline", "edge", "trace edge")),
    "Fill between slices": (_("Fill gaps"), ("fill gaps", "interpolate", "fill between slices", "interpolation")),
    "Logical operators": (_("Combine"), ("combine", "logical", "union", "intersect", "subtract", "copy segment")),
    "Smoothing": (_("Smooth"), ("smooth", "smoothing", "median", "closing", "opening")),
    "Threshold": (_("Threshold"), ("threshold", "intensity range")),
    "Islands": (_("Islands"), ("islands", "connected components", "remove small", "keep largest")),
    "Scissors": (_("Scissors"), ("scissors", "cut", "crop")),
    "Margin": (_("Margin"), ("margin", "grow", "shrink", "dilate", "erode")),
    "Hollow": (_("Hollow"), ("hollow", "shell", "wall")),
    "Grow from seeds": (_("Grow from seeds"), ("grow from seeds", "seeds", "region growing")),
    "Mask volume": (_("Mask volume"), ("mask volume", "blank out", "mask")),
}

#: Window/level presets of the view commands: (key, title, priority)
WINDOW_LEVEL_PRESETS = (
    ("auto", _("Window/level: Auto"), 3),
    ("brain", _("Window/level: Brain"), 2),
    ("narrow", _("Window/level: Narrow"), 1),
    ("reset", _("Window/level: Reset"), 0),
)
#: Percentiles of the non-zero voxels that the Brain and Narrow presets span
BRAIN_PERCENTILES = (1.0, 99.5)
NARROW_PERCENTILES = (25.0, 95.0)
#: Voxels sampled for the presets (every n-th voxel along each axis of a large image)
_PRESET_SAMPLE_VOXELS = 2_000_000

_WINDOW_LEVEL_SYNONYMS = ("window level", "window/level", "window", "level", "w/l", "wl", "contrast", "brightness")
#: Display presets of the Volumes module offered in Search (``vtkSlicerVolumesLogic`` preset IDs starting with this
#: prefix: CT-Bone, CT-Air, CT-Brain, CT-Abdomen, CT-Lung); PET and DTI also change the colour table and are left out
_VOLUMES_PRESET_PREFIX = "CT_"

_providers = []
_palette = None


def _isAlive(qobject):
    """False if the C++ object was deleted."""
    if qobject is None:
        return False
    try:
        qobject.objectName
    except Exception:
        return False
    return True


def _keyText(key):
    """A key as displayed on this platform ("⌘K" on macOS), or "" for None."""
    return ui.keyText(key) if key else ""


def _settingsList(key):
    """A settings value that is a list of strings (QSettings returns a single string for a one-element list)."""
    value = slicer.util.settingsValue(key, [])
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()] if value else []
    return [str(item) for item in value if item]


def _importOptional(name):
    shellModule = sys.modules.get("StudioLib.shell")
    if shellModule is not None and hasattr(shellModule, "Shell"):
        return shellModule.Shell._importOptional(name)
    try:
        import importlib

        return importlib.import_module(name)
    except Exception:
        return None


#
# Entries
#


class Entry:
    """One row of Search.

    :param entryId: stable identifier (the MRU and providers refer to it).
    :param title: the name shown.
    :param category: what kind of thing it is, shown right of the title ("Module", "Layout", "Map").
    :param callback: called without arguments to run it.
    :param kind: one of the ``KIND_*`` constants.
    :param key: its key ("Ctrl+O"), shown as a key chip, or None.
    :param synonyms: command synonyms (words for what it does); for a module, its synonyms come from ``finder.SYNONYMS``.
    :param aliases: other names that match like the title.
    :param enabled: a bool, or a callable that returns one when the palette opens.
    :param priority: order within a tier (higher first).
    :param description: one line, shown as the tooltip and searched as full text.
    :param group: ``finder.GROUP_*`` or ``GROUP_WHERE``.
    :param iconName: Lucide icon name.
    :param moduleName: the module of a module entry.
    """

    def __init__(self, entryId, title, category, callback, kind=KIND_ACTION, key=None, synonyms=(), aliases=(), enabled=True, priority=0,
                 description="", group=finder.GROUP_CORE, iconName=None, moduleName=None):
        self.id = str(entryId)
        self.title = str(title)
        self.category = str(category or "")
        self.callback = callback
        self.kind = kind
        self.key = key or None
        self.synonyms = tuple(synonyms or ())
        self.aliases = tuple(alias for alias in (aliases or ()) if alias)
        self.enabled = enabled
        try:
            self.priority = float(priority or 0)
        except (TypeError, ValueError):
            self.priority = 0.0
        self.description = str(description or "")
        self.group = group
        self.iconName = iconName
        self.moduleName = moduleName
        self.searchKey = finder.SearchKey(self.title, self.aliases, () if kind == KIND_MODULE else self.synonyms, moduleName)
        self._fullText = None

    def isEnabled(self):
        if callable(self.enabled):
            try:
                return bool(self.enabled())
            except Exception:
                logging.error(f"Studio: Search entry {self.id}: enabled() failed:\n{traceback.format_exc()}")
                return False
        return bool(self.enabled)

    def fullText(self):
        """Lowercase text searched by the full text tier: the description, and the help of a module."""
        if self._fullText is None:
            parts = [self.description, " ".join(self.synonyms)]
            if self.moduleName:
                try:
                    module = slicer.app.moduleManager().module(self.moduleName)
                    for name in ("helpText", "acknowledgementText"):
                        value = getattr(module, name, "")
                        parts.append(value() if callable(value) else value)
                except Exception:
                    pass
            self._fullText = re.sub(r"<[^>]+>", " ", " ".join(str(part) for part in parts if part)).lower()
        return self._fullText

    def __repr__(self):
        return f"Entry({self.id!r}, {self.title!r})"


def _synonymList(value):
    """Synonyms given as a list or as a string of phrases separated by commas."""
    if not value:
        return []
    if isinstance(value, str):
        phrases = [phrase.strip() for phrase in re.split(r"[,;|]", value) if phrase.strip()]
        return phrases
    return [str(item) for item in value if item]


def adaptItem(item, source=""):
    """An ``Entry`` for a provider item (a dict or the addendum's ``(title, keywords, callback, enabledFn)``), None if invalid."""
    if isinstance(item, Entry):
        return item
    if isinstance(item, dict):
        title = item.get("title")
        callback = item.get("callback")
        if not title or not callable(callback):
            logging.warning(f"Studio: Search item without title or callback from {source}: {item!r}")
            return None
        entryId = item.get("id") or f"{source or 'provider'}.{title}"
        return Entry(entryId, title, item.get("category") or _("Action"), callback, kind=KIND_PROVIDER, key=item.get("key"),
                     synonyms=_synonymList(item.get("synonyms")), aliases=_synonymList(item.get("aliases")), enabled=item.get("enabled", True),
                     priority=item.get("priority", 0), description=item.get("description", ""), iconName=item.get("icon") or "play")
    if isinstance(item, (tuple, list)) and len(item) == 4:
        title, keywords, callback, enabledFn = item
        if not title or not callable(callback):
            logging.warning(f"Studio: Search item without title or callback from {source}: {item!r}")
            return None
        return Entry(f"lnm.{title}", title, _("Map"), callback, kind=KIND_PROVIDER, synonyms=_synonymList(keywords),
                     enabled=enabledFn if enabledFn is not None else True, iconName="waypoints")
    logging.warning(f"Studio: unsupported Search item from {source}: {item!r}")
    return None


def registerProvider(fn):
    """Add a provider of Search entries: ``fn(shell)`` (or ``fn()``) returns a list of dicts or tuples (see the module docstring).

    It is called each time Search opens. Registering the same function again has no effect.
    """
    if not callable(fn):
        raise TypeError("a Search provider must be callable")
    if fn not in _providers:
        _providers.append(fn)


def unregisterProvider(fn):
    """Remove a provider added with ``registerProvider()`` (no error if it was not registered)."""
    while fn in _providers:
        _providers.remove(fn)


def providers():
    """The registered providers, in registration order."""
    return list(_providers)


def _callProvider(fn, shell):
    try:
        return fn(shell)
    except TypeError:
        # The addendum's zero-argument form
        return fn()


def providerEntries(shell):
    """Entries of the ``PALETTE_ACTIONS`` lists and of the registered providers, in that order."""
    entries = []
    cardModules = sorted((name, module) for name, module in list(sys.modules.items())
                         if module is not None and (name.startswith("StudioLib.libcard_") or name.startswith("LesionNetworkMappingUI.card_")))
    for moduleName, module in cardModules:
        items = getattr(module, "PALETTE_ACTIONS", None)
        if items is None:
            continue
        try:
            if callable(items):
                items = _callProvider(items, shell)
            for item in items or []:
                entry = adaptItem(item, moduleName)
                if entry is not None:
                    entries.append(entry)
        except Exception:
            logging.error(f"Studio: Search actions of {moduleName} failed:\n{traceback.format_exc()}")
    for fn in list(_providers):
        try:
            items = _callProvider(fn, shell)
        except Exception:
            logging.error(f"Studio: Search provider {getattr(fn, '__qualname__', fn)} failed:\n{traceback.format_exc()}")
            continue
        source = getattr(fn, "__module__", "") or "provider"
        for item in items or []:
            entry = adaptItem(item, source)
            if entry is not None:
                entries.append(entry)
    return entries


#
# MRU and favorites
#


def mru():
    """Ids of the most recently run entries, most recent first (``Studio/PaletteMRU``)."""
    return _settingsList(MRU_KEY)[:MRU_SIZE]


def recordRun(entryId):
    """Put ``entryId`` first in ``Studio/PaletteMRU`` (at most ``MRU_SIZE`` ids)."""
    ids = [entryId] + [other for other in mru() if other != entryId]
    ids = ids[:MRU_SIZE]
    if ids != mru():
        qt.QSettings().setValue(MRU_KEY, ids)


def favoriteModules():
    """Module names of the classic favorite modules (``Modules/FavoriteModules``; read only)."""
    return _settingsList(FAVORITES_KEY)


#
# Commands
#


def _shellOf(shell):
    if shell is not None and shell.isActive():
        return shell
    from . import shell as shellModule

    return shellModule.instance()


def _toast(shell, text, ms=2500):
    shell = _shellOf(shell)
    if shell is not None:
        shell.toast(text, ms)


def _segmentEditor():
    widget = getattr(slicer.modules, "SegmentEditorWidget", None)
    return getattr(widget, "editor", None) if widget is not None else None


def _mainWindowAction(objectName):
    mainWindow = slicer.util.mainWindow()
    return mainWindow.findChild("QAction", objectName) if mainWindow is not None else None


def _saveNow():
    """Save now: the open project (A-09), else the Save dialog of 3D Slicer (File > Save Data)."""
    try:
        from . import projects_ui
    except ImportError:
        _triggerAction("FileSaveSceneAction")
        return
    projects_ui.saveNow()


def _triggerAction(objectName):
    action = _mainWindowAction(objectName)
    if action is not None:
        action.trigger()


def _showDock(objectName):
    """Show a dock widget of the main window (Python console, error log) and give it the focus."""
    mainWindow = slicer.util.mainWindow()
    dock = mainWindow.findChild("QDockWidget", objectName) if mainWindow is not None else None
    if dock is None:
        return
    if dock.isHidden():
        dock.toggleViewAction().trigger()
    dock.raise_()
    if dock.widget() is not None:
        dock.widget().setFocus()


def _crosshairNode():
    node = slicer.mrmlScene.GetNodeByID("vtkMRMLCrosshairNodedefault")
    return node if node is not None else slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")


_lastCrosshairMode = None


def toggleCrosshair(shell=None):
    """Show the crosshair (the last shown mode, else basic) or hide it."""
    global _lastCrosshairMode
    node = _crosshairNode()
    if node is None:
        return
    if node.GetCrosshairMode() == node.NoCrosshair:
        node.SetCrosshairMode(_lastCrosshairMode if _lastCrosshairMode is not None else node.ShowBasic)
        _toast(shell, _("Crosshair shown · Shift+move moves it"))
    else:
        _lastCrosshairMode = node.GetCrosshairMode()
        node.SetCrosshairMode(node.NoCrosshair)
        _toast(shell, _("Crosshair hidden"))


def _intersectingSlicesVisibility():
    logic = slicer.app.applicationLogic()
    mode = getattr(slicer.vtkMRMLApplicationLogic, "IntersectingSlicesVisibility", None)
    if mode is None or not hasattr(logic, "GetIntersectingSlicesEnabled"):
        return None
    return logic, mode


def toggleSliceIntersections(shell=None):
    found = _intersectingSlicesVisibility()
    if found is None:
        return
    logic, mode = found
    visible = not logic.GetIntersectingSlicesEnabled(mode)
    logic.SetIntersectingSlicesEnabled(mode, visible)
    _toast(shell, _("Slice intersections shown") if visible else _("Slice intersections hidden"))


def _figureViewNames():
    space = _importOptional("StudioLib.space")
    try:
        return set(space.figureViewNames()) if space is not None else set()
    except Exception:
        return set()


def _compositeNodes(exceptFigures=True):
    skipped = _figureViewNames() if exceptFigures else set()
    return [node for node in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode") if node.GetLayoutName() not in skipped]


def viewsLinked():
    """True if every slice view (figure views excepted) is linked."""
    nodes = _compositeNodes()
    return bool(nodes) and all(node.GetLinkedControl() for node in nodes)


def toggleLinkViews(shell=None):
    """Link or unlink the slice views. Figure views stay unlinked, so a figure keeps its own slice."""
    linked = not viewsLinked()
    for node in _compositeNodes():
        node.SetLinkedControl(linked)
    _toast(shell, _("Views linked") if linked else _("Views unlinked"))


def resetViews(shell=None):
    """Fit the slice and 3D views; a figure view is fitted to its figure."""
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None:
        return
    layoutManager.resetSliceViews()
    layoutManager.resetThreeDViews()
    for name in _figureViewNames():
        sliceWidget = layoutManager.sliceWidget(name)
        if sliceWidget is not None:
            sliceWidget.sliceLogic().FitSliceToAll()


def _targetVolume(shell=None):
    """The volume that window/level presets change: the scan edited in Draw, else the background of the Red view."""
    editor = _segmentEditor()
    volume = editor.sourceVolumeNode() if editor is not None else None
    if volume is None:
        layoutManager = slicer.app.layoutManager()
        names = list(layoutManager.sliceViewNames()) if layoutManager is not None else []
        for name in (["Red"] if "Red" in names else []) + names:
            logic = layoutManager.sliceWidget(name).sliceLogic()
            candidate = logic.GetBackgroundLayer().GetVolumeNode() if logic is not None else None
            if candidate is not None:
                volume = candidate
                break
    if volume is None or not volume.IsA("vtkMRMLScalarVolumeNode") or volume.GetImageData() is None:
        return None
    return volume


#: Window/level of the volumes before the first preset, by node id (forgotten when the scene is closed: ids are reused)
_savedWindowLevels = {}
_savedWindowLevelsObserver = None


def _forgetWindowLevels(*args):
    _savedWindowLevels.clear()


def _forgetWindowLevelsObserver():
    """Forget the saved window/levels and remove the scene observer (the palette is disposed)."""
    global _savedWindowLevelsObserver
    _savedWindowLevels.clear()
    if _savedWindowLevelsObserver is not None:
        slicer.mrmlScene.RemoveObserver(_savedWindowLevelsObserver)
        _savedWindowLevelsObserver = None


def _rememberWindowLevel(volumeNode, displayNode):
    global _savedWindowLevelsObserver
    if _savedWindowLevelsObserver is None:
        _savedWindowLevelsObserver = slicer.mrmlScene.AddObserver(slicer.vtkMRMLScene.EndCloseEvent, _forgetWindowLevels)
    _savedWindowLevels[volumeNode.GetID()] = (displayNode.GetAutoWindowLevel(), displayNode.GetWindow(), displayNode.GetLevel())


def _sampledNonZero(volumeNode):
    # numpy is imported here, not with the module: the palette module is imported at startup, numpy is not (~0.2 s)
    import numpy as np

    array = slicer.util.arrayFromVolume(volumeNode)
    step = max(1, round((array.size / _PRESET_SAMPLE_VOXELS) ** (1.0 / 3.0)))
    sample = array[::step, ::step, ::step].astype(np.float64, copy=False).ravel()
    sample = sample[np.isfinite(sample)]
    nonZero = sample[sample != 0]
    return nonZero if nonZero.size else sample


def windowLevelPreset(volumeNode, preset):
    """``(window, level)`` of a preset ("brain", "narrow") for a scalar volume, None if it cannot be computed."""
    percentiles = {"brain": BRAIN_PERCENTILES, "narrow": NARROW_PERCENTILES}.get(preset)
    if percentiles is None or volumeNode is None:
        return None
    values = _sampledNonZero(volumeNode)
    if values.size == 0:
        return None
    import numpy as np

    low, high = (float(value) for value in np.percentile(values, percentiles))
    if high <= low:
        return None
    return high - low, (high + low) / 2.0


def applyWindowLevel(preset, shell=None, volumeNode=None):
    """Apply a window/level preset ("auto", "brain", "narrow" or "reset") to the scan of Draw (else the Red view)."""
    volumeNode = volumeNode or _targetVolume(shell)
    displayNode = volumeNode.GetDisplayNode() if volumeNode is not None else None
    if displayNode is None or not hasattr(displayNode, "SetAutoWindowLevel"):
        _toast(shell, _("Open a scan first: window/level applies to the scan in the views"))
        return False
    nodeId = volumeNode.GetID()
    if preset != "reset" and nodeId not in _savedWindowLevels:
        _rememberWindowLevel(volumeNode, displayNode)
    if preset == "auto":
        displayNode.AutoWindowLevelOn()
    elif preset == "reset":
        saved = _savedWindowLevels.pop(nodeId, None)
        if saved is None or saved[0]:
            displayNode.AutoWindowLevelOn()
        else:
            displayNode.AutoWindowLevelOff()
            displayNode.SetWindowLevel(saved[1], saved[2])
    else:
        with slicer.util.WaitCursor():
            windowLevel = windowLevelPreset(volumeNode, preset)
        if windowLevel is None:
            _toast(shell, _("This scan has no intensity range for that preset"))
            return False
        displayNode.AutoWindowLevelOff()
        displayNode.SetWindowLevel(*windowLevel)
    _toast(shell, _("Window/level {window:.0f} / {level:.0f}").format(window=displayNode.GetWindow(), level=displayNode.GetLevel()))
    return True


def volumesDisplayPresets():
    """``[(preset ID, title)]`` of the Volumes display presets offered in Search, for example ("CT_BRAIN", "CT-Brain")."""
    module = getattr(slicer.modules, "volumes", None)
    logic = module.logic() if module is not None else None
    if logic is None or not hasattr(logic, "GetVolumeDisplayPresetIDs"):
        return []
    presets = []
    for presetId in logic.GetVolumeDisplayPresetIDs():
        presetId = str(presetId)
        if presetId.startswith(_VOLUMES_PRESET_PREFIX):
            # The names of VolumeDisplayPresets.json ("CT-Brain"); the name itself is not wrapped in Python
            presets.append((presetId, "CT-" + presetId[len(_VOLUMES_PRESET_PREFIX):].capitalize()))
    return presets


def applyDisplayPreset(presetId, shell=None, volumeNode=None):
    """Apply a display preset of the Volumes module ("CT_BRAIN"...) to the scan of Draw (else the Red view), without
    leaving the workspace. "Window/level: Reset" restores the window/level from before.
    """
    volumeNode = volumeNode or _targetVolume(shell)
    displayNode = volumeNode.GetDisplayNode() if volumeNode is not None else None
    if displayNode is None or not hasattr(displayNode, "SetAutoWindowLevel"):
        _toast(shell, _("Open a scan first: window/level applies to the scan in the views"))
        return False
    if volumeNode.GetID() not in _savedWindowLevels:
        _rememberWindowLevel(volumeNode, displayNode)
    if not slicer.modules.volumes.logic().ApplyVolumeDisplayPreset(displayNode, presetId):
        _toast(shell, _("This preset could not be applied"))
        return False
    _toast(shell, _("Window/level {window:.0f} / {level:.0f}").format(window=displayNode.GetWindow(), level=displayNode.GetLevel()))
    return True


POINTS_LIST_NAME = "Points"


def placePoint(shell=None):
    """Place mode for a point list "Points" (created once, then reused), in the current workspace. Esc leaves it."""
    scene = slicer.mrmlScene
    pointList = None
    for node in slicer.util.getNodesByClass("vtkMRMLMarkupsFiducialNode"):
        if node.GetName() == POINTS_LIST_NAME:
            pointList = node
            break
    if pointList is None:
        pointList = scene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", POINTS_LIST_NAME)
        pointList.CreateDefaultDisplayNodes()
    applicationLogic = slicer.app.applicationLogic()
    selectionNode = applicationLogic.GetSelectionNode()
    if hasattr(selectionNode, "SetReferenceActivePlaceNodeClassName"):
        selectionNode.SetReferenceActivePlaceNodeClassName("vtkMRMLMarkupsFiducialNode")
    else:
        selectionNode.SetActivePlaceNodeClassName("vtkMRMLMarkupsFiducialNode")
    selectionNode.SetActivePlaceNodeID(pointList.GetID())
    interactionNode = applicationLogic.GetInteractionNode()
    # The place mode persistence is the user's choice (Markups toolbar): it is kept
    interactionNode.SetCurrentInteractionMode(interactionNode.Place)
    _toast(shell, _("Click in a view to place a point · Esc cancels"), 3000)
    return pointList


def _openMap(shell):
    ws = workspaces.workspace("map")
    if ws is not None and workspaces.isAvailable(ws):
        shell.switchTo("map")
        return
    shell.toast(_("Map computes the brain network connected to a lesion from a normative connectome. It is not part of this build yet; "
                  "when it is, it appears as the Map tab."), 6000)


def _selectTool(shell, effectName):
    editor = _segmentEditor()
    shell.switchTo("draw")
    editor = editor or _segmentEditor()
    if editor is None:
        return
    if editor.sourceVolumeNode() is None:
        shell.toast(_("Open a scan or start on the MNI template first, then pick the tool"), 3500)
        return
    if effectName not in [name for name, index in _toolNamesAndIndexes(editor) if index is not None]:
        # A tool under "More tools": its tile is shown, so that the active tool is visible in Draw
        widget = getattr(slicer.modules, "SegmentEditorWidget", None)
        if widget is not None and hasattr(widget, "setMoreToolsShown"):
            widget.setMoreToolsShown(True)
    editor.setActiveEffectByName(effectName)


def _openSettings(panel=None):
    slicer.app.openSettingsDialog(panel or "")


def _showToolBar(toolBar, shell):
    if not _isAlive(toolBar):
        return
    if toolBar.isVisible():
        shell.toast(_("{name} is already shown").format(name=toolBar.windowTitle))
        return
    # The toggle action records the toolbar in Studio/AllowedToolbars (the shell watches it)
    toolBar.toggleViewAction().trigger()
    if not toolBar.isVisible():
        toolBar.show()


def _loadSample(source, shell):
    """Load a sample data set: the bundled MNI template starts Draw on it; a single scan is opened like a dropped file."""
    import SampleData

    importer = _importOptional("StudioLib.importer")
    if importer is not None and source.sampleName == getattr(importer, "TEMPLATE_SAMPLE_2MM", None):
        importer.startOnTemplate(shell=shell)
        return
    logic = SampleData.SampleDataLogic()
    fileNames = list(source.fileNames or [])
    singleScan = (importer is not None and len(fileNames) == 1 and not source.customDownloader and all(source.loadFiles or [True])
                  and importer.fileKind(fileNames[0]) == "volume")
    with slicer.util.WaitCursor():
        if not singleScan:
            logic.downloadFromSource(source)
            return
        paths = logic.downloadSourceIntoCache(source)
    if not paths or not paths[0]:
        return
    result = importer.loadFiles([paths[0]], shell)
    nodeNames = list(source.nodeNames or [])
    if result is not None and nodeNames and nodeNames[0] and len(getattr(result, "volumes", [])) == 1:
        result.volumes[0].SetName(nodeNames[0])


def _loadRecent(properties, action, shell):
    importer = _importOptional("StudioLib.importer")
    if importer is not None and hasattr(importer, "loadRecentFile"):
        importer.loadRecentFile(dict(properties), shell=shell)
    elif _isAlive(action):
        action.trigger()


def _setDensity(density, shell):
    from . import appmenu

    if appmenu.currentDensity() == density:
        shell.toast(_("Density is already {density}").format(density=_(density)))
        return
    if appmenu.setDensity(density):
        shell.toast(_("Density: {density}. Takes effect after restart").format(density=_(density)))


def _setTheme(theme):
    from . import appmenu

    appmenu.setTheme(theme)


def _undoRedo(which):
    editor = _segmentEditor()
    if editor is not None:
        getattr(editor, which)()


def _hasLesion():
    draw = _importOptional("StudioLib.draw")
    if draw is None:
        return False
    return draw.lesionNodes()[0] is not None


def _hasScan():
    editor = _segmentEditor()
    return editor is not None and editor.sourceVolumeNode() is not None


#
# The index
#


class Index:
    """The entries of Search. Parts that rarely change (modules, layouts, settings pages, sample data, toolbars) are
    rebuilt only when their source changed; the others (tools, recent files, tours, providers) on each refresh.
    """

    def __init__(self, shell):
        self.shell = shell
        self._parts = {}
        self._signatures = {}
        self.entries = []
        self.byId = {}
        self.buildDurationMs = None

    def _part(self, name, signature, builder):
        if self._signatures.get(name) != signature or name not in self._parts:
            try:
                self._parts[name] = builder()
            except Exception:
                logging.error(f"Studio: Search index part '{name}' failed:\n{traceback.format_exc()}")
                self._parts[name] = []
            self._signatures[name] = signature
        return self._parts[name]

    def refresh(self):
        """Bring the entries up to date. Return the duration in ms."""
        startTime = time.perf_counter()
        shell = self.shell
        mainWindow = shell.mainWindow
        moduleManager = slicer.app.moduleManager()
        moduleNames = tuple(moduleManager.modulesNames())
        layoutMenu = mainWindow.findChild("QMenu", "LayoutMenu")
        settingsDialog = slicer.app.settingsDialog() if hasattr(slicer.app, "settingsDialog") else None
        toolBars = [toolBar for toolBar in mainWindow.findChildren("QToolBar") if toolBar.objectName != "StudioTopBar" and toolBar.parentWidget() == mainWindow]
        ordered = []
        ordered += self._part("commands", 1, self._commandEntries)
        ordered += self._part("workspaces", tuple(workspaces.isAvailable(ws) for ws in workspaces.WORKSPACES), self._workspaceEntries)
        ordered += self._part("modules", len(moduleNames), lambda: self._moduleEntries(moduleNames))
        ordered += self._part("layouts", len(_menuActions(layoutMenu)) if layoutMenu is not None else 0, lambda: self._layoutEntries(layoutMenu))
        ordered += self._compareLayoutEntries()
        ordered += self._part("settings", _settingsSignature(settingsDialog), lambda: self._settingsEntries(settingsDialog))
        ordered += self._part("samples", _sampleSignature(), self._sampleEntries)
        ordered += self._part("toolbars", tuple(toolBar.objectName or toolBar.windowTitle for toolBar in toolBars), lambda: self._toolBarEntries(toolBars))
        editor = _segmentEditor()
        ordered += self._part("tools", _toolSignature(editor), lambda: self._toolEntries(editor))
        ordered += self._recentEntries(mainWindow)
        ordered += self._tourEntries()
        ordered += providerEntries(shell)
        byId = {}
        for entry in ordered:
            # A later source replaces an entry with the same id
            byId.pop(entry.id, None)
            byId[entry.id] = entry
        self.byId = byId
        self.entries = list(byId.values())
        self.buildDurationMs = (time.perf_counter() - startTime) * 1000.0
        return self.buildDurationMs

    def entry(self, entryId):
        return self.byId.get(entryId)

    #
    # Parts
    #

    def _workspaceEntries(self):
        shell = self.shell
        entries = []
        for index, ws in enumerate(workspaces.WORKSPACES):
            key = f"Ctrl+Alt+{index + 1}"
            if ws.key == "library":
                entries.append(Entry("workspace.library", ws.title, _("Workspace"), lambda: shell.switchTo("library"), KIND_WORKSPACE, key,
                                     synonyms=("home", "welcome", "start", "open data"), aliases=("Studio",), priority=2,
                                     description=_("Open scans, lesion masks and sample data; recent files"), iconName=ws.iconName))
            elif ws.key == "draw":
                entries.append(Entry("workspace.draw", ws.title, _("Workspace"), lambda: shell.switchTo("draw"), KIND_WORKSPACE, key,
                                     synonyms=("segmentation", "segment", "lesion", "trace", "tracing", "edit lesion"), aliases=(_("Segment Editor"),),
                                     priority=2, description=_("Segment Editor: brush, outline and erase lesions"), iconName=ws.iconName))
            elif ws.key == "map":
                entries.append(Entry("workspace.map", ws.title, _("Workspace"), lambda: _openMap(shell), KIND_WORKSPACE, key,
                                     synonyms=("network", "networks", "lnm", "connectome", "lesion network", "network mapping", "map lesion"),
                                     aliases=(_("Lesion network mapping"),), priority=2,
                                     description=_("Lesion network mapping: from a traced lesion to its network map"), iconName=ws.iconName))
        entries.append(Entry("workspace.classic", _("Classic interface"), _("Workspace"), shell.classic, KIND_WORKSPACE, "Ctrl+Alt+0",
                             synonyms=("classic", "old interface", "3d slicer interface", "menu bar", "menubar", "slicer interface"),
                             description=_("The 3D Slicer interface, with its menus and toolbars"), iconName="monitor"))
        return entries

    def _moduleEntries(self, moduleNames):
        moduleManager = slicer.app.moduleManager()
        entries = []
        for name in moduleNames:
            if name in _WORKSPACE_MODULES:
                continue
            module = moduleManager.module(name)
            if module is None or bool(module.hidden):
                continue
            title = module.title or name
            categories = [str(category) for category in (module.categories or [])]
            category = categories[0].split(".")[-1] if categories and categories[0] else ""
            entries.append(Entry(f"module.{name}", title, _("Module"), lambda name=name: slicer.util.selectModule(name), KIND_MODULE,
                                 description=category, group=finder.moduleGroup(name, title), iconName="box", moduleName=name))
        return entries

    def _commandEntries(self):
        shell = self.shell
        importer = _importOptional("StudioLib.importer")
        draw = _importOptional("StudioLib.draw")
        action, view = _("Action"), _("View")
        entries = [
            Entry("action.open", _("Open…"), action, shell.openFiles, key="Ctrl+O",
                  synonyms=("open", "add data", "data", "load", "import", "open file", "load data", "figure", "picture", "png"),
                  description=_("Open scans, lesion masks, figures, statistical maps or scenes"), iconName="folder-open"),
            Entry("action.save", _("Save now"), action, _saveNow, key="Ctrl+S",
                  synonyms=("save", "save data", "save scene", "mrb", "mrml", "save all", "save project"),
                  description=_("Save the project now (it is also saved automatically); without a project, the Save dialog of 3D Slicer"),
                  iconName="download"),
            Entry("action.closeScene", _("Close scene"), action, lambda: _triggerAction("FileCloseSceneAction"), key="Ctrl+W",
                  synonyms=("close", "close all", "clear scene", "new scene", "reset scene"), description=_("Close everything that is open"), iconName="x"),
            Entry("action.placePoint", _("Place point"), action, lambda: placePoint(shell),
                  synonyms=("fiducial", "fiducials", "point", "points", "landmark", "landmarks", "markup point", "place fiducial", "add point"),
                  description=_("Click in a view to add a point to the list Points; Esc cancels"), iconName="mouse-pointer-2"),
            Entry("action.pythonConsole", _("Python console"), action, lambda: _showDock("PythonConsoleDockWidget"), key="Ctrl+3",
                  synonyms=("python", "console", "shell", "script", "interactor", "terminal"), iconName="terminal"),
            Entry("action.errorLog", _("Error log"), action, lambda: _showDock("ErrorLogDockWidget"), key="Ctrl+0",
                  synonyms=("errors", "error", "log", "warnings", "messages log"), iconName="circle-alert"),
            Entry("action.settings", _("Settings…"), action, _openSettings, key="Ctrl+2",
                  synonyms=("settings", "preferences", "options", "application settings", "configure"), iconName="settings"),
            Entry("action.shortcuts", _("Keyboard shortcuts"), action, shell.toggleCheatSheet, key="Ctrl+/",
                  synonyms=("shortcuts", "keys", "hotkeys", "cheat sheet", "keyboard", "key bindings"), iconName="keyboard"),
            Entry("action.undo", _("Undo"), action, lambda: _undoRedo("undo"), key="Ctrl+Z", synonyms=("undo", "revert", "take back"),
                  enabled=lambda: _segmentEditor() is not None, description=_("Undo the last lesion edit"), iconName="undo-2"),
            Entry("action.redo", _("Redo"), action, lambda: _undoRedo("redo"), key="Ctrl+Y", synonyms=("redo",),
                  enabled=lambda: _segmentEditor() is not None, description=_("Redo the last undone lesion edit"), iconName="redo-2"),
            Entry("action.recentMessages", _("Recent messages"), action, shell.showRecentMessages,
                  synonyms=("messages", "notifications", "toasts", "history"), iconName="history"),
            Entry("action.hidePanels", _("Hide panels"), view, shell.toggleFocusMode, key="Ctrl+6",
                  synonyms=("focus mode", "full screen", "fullscreen", "hide inspector", "distraction free"), iconName="maximize-2"),
            Entry("action.showInspector", _("Show inspector"), view, shell.toggleInspector, key="Ctrl+\\",
                  synonyms=("inspector", "module panel", "side panel", "panel", "hide inspector"), iconName="layout-panel-left"),
            Entry("action.resetPanelWidth", _("Reset panel width"), view, shell.resetPanelWidth,
                  synonyms=("panel width", "inspector width"), iconName="scaling"),
            Entry("action.dataProbe", _("Show Data Probe"), view, lambda: shell.setDataProbeVisible(True),
                  synonyms=("data probe", "probe", "pixel value", "voxel value"), description=_("The classic Data Probe panel"), iconName="scan-eye"),
        ]
        for theme, iconName, synonyms in (("Dark", "moon", ("dark", "dark mode", "night", "theme")), ("Light", "sun", ("light", "light mode", "day", "theme")),
                                          ("System", "monitor", ("system theme", "auto theme", "theme"))):
            entries.append(Entry(f"appearance.theme.{theme.lower()}", _("{theme} theme").format(theme=_(theme)), _("Appearance"),
                                 lambda theme=theme: _setTheme(theme), synonyms=synonyms + ("appearance", "colors", "colours"), iconName=iconName))
        for density in ("Comfortable", "Compact"):
            entries.append(Entry(f"appearance.density.{density.lower()}", _("Density: {density}").format(density=_(density)), _("Appearance"),
                                 lambda density=density: _setDensity(density, shell), synonyms=("density", density.lower(), "appearance"), iconName="scaling"))
        if importer is not None:
            entries += [
                Entry("action.openFolder", _("Open folder…"), action, lambda: importer.openFolderDialog(shell),
                      synonyms=("folder", "open folder", "directory", "dicom", "load dicom", "add dicom data", "dicom folder"),
                      description=_("A DICOM folder or a folder of NIfTI files"), iconName="folder-open"),
                Entry("action.startOnTemplate", _("Start on MNI template"), action, lambda: importer.startOnTemplate(shell=shell),
                      synonyms=("template", "mni template", "start on template", "new lesion on template", "atlas space"),
                      description=_("The bundled MNI152NLin2009cAsym template in Draw, with an empty lesion"), iconName="brain"),
                Entry("action.newLesion", _("New lesion"), action, lambda: importer.newLesionSegmentation(shell=shell),
                      synonyms=("another lesion", "new segmentation", "add lesion", "next lesion"), enabled=_hasScan,
                      description=_("A new lesion on the scan of Draw; the previous one is hidden"), iconName="brush"),
            ]
        if draw is not None and hasattr(draw, "saveLesionDialog"):
            entries.append(Entry("draw.saveLesion", _("Save lesion as NIfTI…"), action, lambda: draw.saveLesionDialog(shell), key="Ctrl+Shift+S",
                                 synonyms=("export", "nifti", "save lesion", "nii", "nii.gz", "export lesion", "export segmentation", "save mask",
                                           "binary labelmap", "export mask"),
                                 enabled=_hasLesion, description=_("The lesion as a NIfTI mask that official 3D Slicer, FSL and other tools read"),
                                 iconName="download"))
        entries += [
            Entry("view.crosshair", _("Show crosshair"), view, lambda: toggleCrosshair(shell), key="Shift+move",
                  synonyms=("crosshair", "cursor", "cross hair", "crosshairs", "hide crosshair"), description=_("Show where the other views cut"),
                  iconName="crosshair"),
            Entry("view.sliceIntersections", _("Slice intersections"), view, lambda: toggleSliceIntersections(shell),
                  synonyms=("intersections", "slice intersections", "slice lines", "intersecting slices"), iconName="split"),
            Entry("view.linkViews", _("Link views"), view, lambda: toggleLinkViews(shell),
                  synonyms=("link", "linked", "link slices", "sync views", "unlink", "unlink views"), iconName="link"),
            Entry("view.resetViews", _("Reset views"), view, lambda: resetViews(shell), key="r",
                  synonyms=("reset", "fit", "fit to window", "recenter", "center views", "reset zoom"), iconName="scan-eye"),
            Entry("view.maximize", _("Maximize view"), view, lambda: shell.setLayoutFromPicker("single"), key="Alt+2",
                  synonyms=("maximize", "single view", "one view", "enlarge", "zoom view", "big view"), aliases=(_("Single"),), priority=1,
                  description=_("The view under the mouse fills the view area"), iconName="maximize-2"),
        ]
        for preset, title, priority in WINDOW_LEVEL_PRESETS:
            entries.append(Entry(f"view.windowLevel.{preset}", title, view, lambda preset=preset: applyWindowLevel(preset, shell),
                                 synonyms=_WINDOW_LEVEL_SYNONYMS, priority=priority, enabled=lambda: _targetVolume() is not None,
                                 description=_("Window/level of the scan; drag in a view when no tool is active"), iconName="contrast"))
        # The display presets of the Volumes module, applied in place (no detour through the Volumes module); their name
        # in Volumes ("CT-Brain") is an alias, so the name typed by a Slicer user finds them
        for presetId, name in volumesDisplayPresets():
            entries.append(Entry(f"view.displayPreset.{presetId}", _("Window/level: {preset}").format(preset=name), view,
                                 lambda presetId=presetId: applyDisplayPreset(presetId, shell),
                                 synonyms=(name.lower().replace("-", " "), "ct", "preset", "display preset", "volumes preset"),
                                 aliases=(name,), enabled=lambda: _targetVolume() is not None,
                                 description=_("Display preset of the Volumes module, on the scan in the views"), iconName="contrast"))
        return entries

    def _layoutEntries(self, layoutMenu):
        shell = self.shell
        entries = []
        layoutTitle = _("Layout")
        pickerKeys = {layoutId: (title, key) for entryKey, title, key, iconName, layoutId in layouts.PICKER_ENTRIES if layoutId is not None}
        studioLayouts = (
            (layouts.FOCUS_LAYOUT_ID, _("Focus"), "Alt+1", ("Studio Focus",), "layout-panel-left"),
            (layouts.FOCUS_3D_LAYOUT_ID, _("Focus + 3D"), "Alt+5", ("Studio Focus + 3D",), "box"),
            (layouts.ROW_LAYOUT_ID, _("Row"), "Alt+4", ("Studio Row",), "columns-3"),
        )
        for layoutId, title, key, aliases, iconName in studioLayouts:
            entries.append(Entry(f"layout.{layoutId}", title, layoutTitle, lambda layoutId=layoutId: _setLayout(shell, layoutId), KIND_LAYOUT, key,
                                 aliases=aliases, priority=1, iconName=iconName))
        seen = {layoutId for layoutId, *rest in studioLayouts}
        for path, action in _menuActions(layoutMenu):
            text = str(action.text).replace("&", "")
            if len(path) > 0:
                # Compare and Compare Grid: one entry per number of viewers
                submenu = path[-1]
                entryId = f"layout.{_slug(submenu)}.{_slug(text)}"
                title = f"{submenu}: {text}"
                entries.append(Entry(entryId, title, layoutTitle, action.trigger, KIND_LAYOUT, synonyms=(submenu.lower(),), group=finder.GROUP_OTHER,
                                     iconName="layout-grid"))
                continue
            data = action.data()
            try:
                layoutId = int(data)
            except (TypeError, ValueError):
                layoutId = None
            if layoutId is not None and layoutId in seen:
                continue
            if layoutId is not None:
                seen.add(layoutId)
            title, key = pickerKeys.get(layoutId, (None, None))
            synonyms = ("four up", "2x2", "four views") if layoutId == slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView else ()
            # The classic layouts that the picker does not offer ("Tabbed 3D", "Triple 3D") come after the core modules and
            # commands of the same tier: "t" is Transforms, not a rarely used layout
            entries.append(Entry(f"layout.{layoutId}" if layoutId is not None else f"layout.{_slug(text)}", text, layoutTitle, action.trigger,
                                 KIND_LAYOUT, key, synonyms=synonyms, aliases=(title,) if title and title.lower() != text.lower() else (),
                                 priority=1 if key else 0, group=finder.GROUP_CORE if key else finder.GROUP_OTHER, iconName="layout-grid"))
        return entries

    def _compareLayoutEntries(self):
        importer = _importOptional("StudioLib.importer")
        if importer is None or not hasattr(importer, "figureController") or importer.figureController() is None:
            return []
        layoutId = importer.COMPARE_LAYOUT_ID
        return [Entry(f"layout.{layoutId}", _("Compare (figure)"), _("Layout"), lambda: _setLayout(self.shell, layoutId), KIND_LAYOUT,
                      aliases=(getattr(importer, "COMPARE_LAYOUT_TITLE", "Studio Compare"),), synonyms=("figure", "compare figure"),
                      description=_("The template where you draw, next to the figure"), iconName="columns-3")]

    def _settingsEntries(self, dialog):
        entries = []
        for title in _settingsPanels(dialog):
            entries.append(Entry(f"settings.{title}", _("Settings: {page}").format(page=title), _("Settings"), lambda title=title: _openSettings(title),
                                 KIND_SETTINGS, group=finder.GROUP_OTHER, iconName="settings"))
        return entries

    def _sampleEntries(self):
        shell = self.shell
        entries = []
        importer = _importOptional("StudioLib.importer")
        bundled = getattr(importer, "TEMPLATE_SAMPLE_2MM", None) if importer is not None else None
        for category, sources in _sampleSources().items():
            if str(category).lower() in ("testing",):
                continue
            for source in sources:
                name = getattr(source, "sampleName", None)
                if not name:
                    continue
                entries.append(Entry(f"sample.{name}", name, _("Sample data"), lambda source=source: _loadSample(source, shell), KIND_SAMPLE,
                                     priority=1 if name == bundled else 0, description=str(category), group=finder.GROUP_OTHER, iconName="download"))
        return entries

    def _toolBarEntries(self, toolBars):
        shell = self.shell
        entries = []
        for toolBar in toolBars:
            title = str(toolBar.windowTitle or toolBar.objectName)
            if not title:
                continue
            entries.append(Entry(f"toolbar.{toolBar.objectName or title}", _("Show toolbar: {name}").format(name=title), _("Toolbar"),
                                 lambda toolBar=toolBar: _showToolBar(toolBar, shell), KIND_TOOLBAR, synonyms=("toolbar", "toolbars"),
                                 aliases=(f"{title} toolbar",), group=finder.GROUP_OTHER, iconName="sliders-horizontal",
                                 description=_("It stays visible in Studio")))
        return entries

    def _toolEntries(self, editor):
        """Every tool of Draw: the tiles with their positional key, then the tools under "More tools" (no key while
        they are not shown: their key is positional), so that "th" finds Threshold whether or not More tools is open.
        A tool under "More tools" comes after the core modules and commands of the same tier ("t" is Transforms, then
        Threshold) and before the other modules and layouts ("th" is Threshold, not "Three over three").
        """
        if editor is None:
            return []
        shell = self.shell
        entries = []
        for name, index in _toolNamesAndIndexes(editor):
            effect = editor.effectByName(name)
            if effect is None:
                continue
            title, synonyms = TOOL_NAMES.get(name, (None, ()))
            effectTitle = str(getattr(effect, "title", "") or name)
            title = title or effectTitle
            aliases = tuple(alias for alias in {name, effectTitle} if alias.lower() != title.lower())
            key = (str(index % 10) if index <= 10 else f"Shift+{index % 10}") if index is not None else None
            entries.append(Entry(f"tool.{name}", title, _("Draw tool"), lambda name=name: _selectTool(shell, name), KIND_TOOL, key,
                                 synonyms=synonyms, aliases=aliases, group=finder.GROUP_CORE if index is not None else GROUP_MORE_TOOLS,
                                 iconName="brush"))
        return entries

    def _recentEntries(self, mainWindow):
        shell = self.shell
        menu = mainWindow.findChild("QMenu", "RecentlyLoadedMenu")
        entries = []
        if menu is None:
            return entries
        for action in menu.actions():
            properties = action.property("fileParameters")
            if not properties:
                continue
            fileName = str(properties.get("fileName", "") if hasattr(properties, "get") else "")
            if not fileName:
                continue
            title = os.path.basename(fileName)
            entries.append(Entry(f"recent:{fileName}", title, _("Recent file"), lambda properties=properties, action=action: _loadRecent(properties, action, shell),
                                 KIND_RECENT, description=os.path.dirname(fileName), group=finder.GROUP_OTHER, iconName="history"))
        return entries

    def _tourEntries(self):
        tour = sys.modules.get("StudioLib.tour")
        if tour is None or not hasattr(tour, "availableTours"):
            return []
        try:
            tours = tour.availableTours()
        except Exception:
            return []
        if not tours and tour.plugin() is None:
            return []
        entries = []
        for item in tours:
            tourId = item.get("id")
            # After the tools, commands and core modules of the same tier: "t" is Transforms or Threshold before five
            # "Take a tour" rows (a tour is found by "tour", "tutorial" or its name)
            entries.append(Entry(f"tour.{tourId}", _("Take a tour: {title}").format(title=item.get("title", tourId)), _("Tour"),
                                 lambda tourId=tourId: tour.startTour(tourId), KIND_TOUR,
                                 synonyms=("tour", "take a tour", "tutorial", "getting started", "learn", "onboarding", "help"),
                                 description=item.get("description", ""), group=finder.GROUP_OTHER, iconName="signpost"))
        entries.append(Entry("tour.whereDidItGo", _("Where did it go?"), _("Tour"), tour.showMigrationMap, KIND_TOUR,
                             synonyms=("where", "where is", "moved", "coming from slicer", "migration", "where did it go", "missing menu"),
                             description=_("Where the places of 3D Slicer are in Slicer Studio"), iconName="compass"))
        return entries


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def _menuActions(menu, path=()):
    """``[(submenu path, action)]`` of the leaf actions of a menu (not separators)."""
    result = []
    if menu is None:
        return result
    for action in menu.actions():
        if action.isSeparator():
            continue
        submenu = action.menu()
        if submenu is not None:
            result += _menuActions(submenu, path + (str(action.text).replace("&", ""),))
        elif action.text:
            result.append((path, action))
    return result


def _setLayout(shell, layoutId):
    topBar = getattr(shell, "topBar", None) if shell is not None else None
    if topBar is not None and hasattr(topBar, "setLayoutByUser"):
        # A layout chosen in Search is announced in Library like one chosen in the picker
        topBar.setLayoutByUser(layoutId)
        return
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None:
        return
    if layoutId >= layouts.STUDIO_LAYOUT_ID_FIRST:
        node = layouts.layoutNode()
        if node is not None and not node.IsLayoutDescription(layoutId):
            layouts.register(node)
    layoutManager.setLayout(layoutId)


def _settingsPanels(dialog):
    """Titles of the pages of the settings dialog, in tree order."""
    if dialog is None:
        return []
    tree = dialog.findChild("QTreeWidget")
    if tree is None:
        return []
    titles = []

    def visit(item):
        title = str(item.text(0))
        if title:
            titles.append(title)
        for index in range(item.childCount()):
            visit(item.child(index))

    for index in range(tree.topLevelItemCount):
        visit(tree.topLevelItem(index))
    return titles


def _settingsSignature(dialog):
    if dialog is None:
        return 0
    tree = dialog.findChild("QTreeWidget")
    return tree.topLevelItemCount if tree is not None else 0


def _sampleSources():
    """The registered sample data sources by category (registers the built-in ones on first use)."""
    try:
        import SampleData
    except ImportError:
        return {}
    sources = SampleData.SampleDataLogic.sampleDataSourcesByCategory()
    if not sources:
        SampleData.SampleDataLogic()
        sources = SampleData.SampleDataLogic.sampleDataSourcesByCategory()
    return dict(sources or {})


def _sampleSignature():
    sources = getattr(slicer.modules, "sampleDataSources", None) or {}
    return sum(len(items) for items in sources.values()) if isinstance(sources, dict) else 0


def _toolNamesAndIndexes(editor):
    """``[(effect name, positional index or None)]`` of Draw: the shown tools in their order (index 1, 2...), then the
    other available tools (the ones under "More tools"), without an index.
    """
    shown = []
    for index in range(1, 21):
        effect = editor.effectByIndex(index)
        if effect is not None:
            shown.append((str(effect.name), index))
    names = {name for name, index in shown}
    try:
        available = [str(name) for name in editor.availableEffectNames()]
    except Exception:
        available = []
    hidden = [(name, None) for name in available if name and name not in names and name != "None"]
    return shown + hidden


def _toolSignature(editor):
    if editor is None:
        return None
    return tuple(_toolNamesAndIndexes(editor))


#
# Ranking
#


_WHERE_PREFIX = re.compile(r"(?i)^\s*(where\s+(is|are|did|does|do)|where's|where)\b\s*(the\s+)?")


def _mruRanks():
    return {entryId: rank for rank, entryId in enumerate(mru())}


def _sortKey(entry, tier, mruRanks, enabled):
    return (tier, 0 if enabled else 1, mruRanks.get(entry.id, MRU_SIZE + 1), -entry.priority, entry.group, len(entry.title), entry.title.lower())


def _inCategory(entry, category):
    if category == CATEGORY_MODULES:
        return entry.kind in (KIND_MODULE, KIND_WORKSPACE) and entry.id != "workspace.classic"
    return True


def whereAnswers(query, limit=MAX_WHERE_ANSWERS):
    """'Where did it go?' answers for a query: entries that show the row of ``StudioLib.migration`` (Show me)."""
    migration = _importOptional("StudioLib.migration")
    if migration is None or not query:
        return []
    tour = sys.modules.get("StudioLib.tour")
    entries = []
    for row in migration.lookup(query, limit=limit):
        def show(row=row):
            if tour is not None and row.target and tour.showMe(row.id):
                return
            if tour is not None:
                tour.showMigrationMap(row.official)
        entries.append(Entry(f"where.{row.id}", row.official, _("In 3D Slicer"), show, KIND_WHERE, description=row.studioText(),
                             group=GROUP_WHERE, iconName="compass"))
    return entries


def rank(entries, query, category=None):
    """``[(entry, tier)]`` of the entries matching ``query``, best first (see the module docstring)."""
    normalized = finder.normalizedQuery(query)
    if not normalized:
        return []
    whereMatch = _WHERE_PREFIX.match(normalized)
    whereQuery = normalized[whereMatch.end():].strip() if whereMatch else ""
    compactQuery = re.sub(r"[\s_-]+", "", normalized)
    words = finder.queryWords(normalized)
    mruRanks = _mruRanks()
    candidates = [entry for entry in entries if _inCategory(entry, category)]
    matches = []
    for entry in candidates:
        tier = finder.searchTier(entry.searchKey, normalized, compactQuery, words)
        if tier is not None:
            matches.append((entry, tier))
    if category != CATEGORY_MODULES:
        answers = whereAnswers(whereQuery or normalized) if len(whereQuery or normalized) >= 3 else []
        for answer in answers:
            # After the entries that do the thing; first when the query asks "where is"
            matches.append((answer, finder.TIER_EXACT if whereQuery else (finder.searchTier(answer.searchKey, normalized, compactQuery, words) or finder.TIER_SUBSTRING)))
    if not matches and len(normalized) >= 3 and words:
        for entry in candidates:
            text = entry.fullText()
            if text and all(word in text for word in words):
                matches.append((entry, finder.TIER_FULL_TEXT))
    enabled = {}
    for entry, tier in matches:
        enabled[entry.id] = entry.isEnabled()
    matches.sort(key=lambda match: _sortKey(match[0], match[1], mruRanks, enabled[match[0].id]))
    return matches[:MAX_RESULTS]


#
# The popup
#

_ROLE_ROW = qt.Qt.UserRole + 1


class _RowDelegate(qt.QStyledItemDelegate):
    """Paints a row: icon, title with the matched characters highlighted, category and key chip; or a section header."""

    def __init__(self, palette):
        qt.QStyledItemDelegate.__init__(self, palette.list)
        self.palette = palette

    def paint(self, painter, option, index):
        try:
            self._paint(painter, option, index)
        except Exception:
            logging.error(f"Studio: painting a Search row failed:\n{traceback.format_exc()}")

    def _paint(self, painter, option, index):
        palette = self.palette
        row = index.row()
        if row < 0 or row >= len(palette.rows):
            return
        rowKind, payload = palette.rows[row]
        rect = option.rect
        painter.save()
        painter.setRenderHint(qt.QPainter.Antialiasing, True)
        colors = palette.colors
        if rowKind == "header":
            font = qt.QFont(palette.list.font)
            font.setPixelSize(ui.fontPx(11))
            font.setWeight(qt.QFont.DemiBold)
            font.setLetterSpacing(qt.QFont.AbsoluteSpacing, 0.6)
            painter.setFont(font)
            painter.setPen(colors["text3"])
            painter.drawText(qt.QRect(rect.x() + 12, rect.y(), rect.width() - 24, rect.height() - 4),
                             int(qt.Qt.AlignLeft | qt.Qt.AlignBottom), str(payload).upper())
            painter.restore()
            return
        entry = payload
        enabled = palette.enabledState.get(entry.id, True)
        if row == palette.list.currentRow:
            painter.fillRect(rect, colors["selected"])
        elif row == palette.hoverRow:
            painter.fillRect(rect, colors["hover"])
        x = rect.x() + 12
        iconName = entry.iconName or "circle-dashed"
        try:
            pixmap = palette.iconPixmap(iconName, enabled)
            painter.drawPixmap(x, rect.y() + (rect.height() - ICON_SIZE) // 2, pixmap)
        except Exception:
            pass
        x += ICON_SIZE + 12
        right = rect.right() - 12
        # Key chip
        smallFont = qt.QFont(palette.list.font)
        smallFont.setPixelSize(ui.fontPx(11))
        painter.setFont(smallFont)
        smallMetrics = painter.fontMetrics()
        if entry.key:
            keyText = _keyText(entry.key)
            chipWidth = _advance(smallMetrics, keyText) + 12
            chipRect = qt.QRect(right - chipWidth, rect.y() + (rect.height() - 20) // 2, chipWidth, 20)
            painter.setPen(colors["border"])
            painter.setBrush(qt.QBrush(qt.Qt.NoBrush))
            painter.drawRoundedRect(qt.QRectF(chipRect).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
            painter.setFont(smallFont)
            painter.setPen(colors["text2"])
            painter.drawText(chipRect, int(qt.Qt.AlignCenter), keyText)
            right = chipRect.left() - 8
        # Category
        categoryFont = qt.QFont(palette.list.font)
        categoryFont.setPixelSize(ui.fontPx(12))
        painter.setFont(categoryFont)
        categoryMetrics = painter.fontMetrics()
        if entry.category:
            # +2: the advance is rounded down, and elidedText() elides a text exactly as wide as the width
            categoryWidth = min(_advance(categoryMetrics, entry.category) + 2, 140)
            categoryRect = qt.QRect(right - categoryWidth, rect.y(), categoryWidth, rect.height())
            painter.setFont(categoryFont)
            painter.setPen(colors["text3"])
            painter.drawText(categoryRect, int(qt.Qt.AlignRight | qt.Qt.AlignVCenter),
                             categoryMetrics.elidedText(entry.category, qt.Qt.ElideRight, categoryWidth))
            right = categoryRect.left() - 12
        # Title, with the matched characters in the accent colour
        titleFont = qt.QFont(palette.list.font)
        titleFont.setPixelSize(ui.fontPx(13))
        boldFont = qt.QFont(titleFont)
        boldFont.setWeight(qt.QFont.DemiBold)
        painter.setFont(boldFont)
        boldMetrics = painter.fontMetrics()
        painter.setFont(titleFont)
        metrics = painter.fontMetrics()
        available = max(0, right - x)
        textColor = colors["text"] if enabled else colors["disabled"]
        baseline = rect.y() + (rect.height() + metrics.ascent() - metrics.descent()) // 2
        ranges = finder.matchedRanges(entry.title, palette.normalizedQuery) if enabled else []
        segments = _segments(entry.title, ranges)
        width = sum(_advance(boldMetrics if highlighted else metrics, text) for text, highlighted in segments)
        if width > available:
            painter.setFont(titleFont)
            painter.setPen(textColor)
            painter.drawText(x, baseline, metrics.elidedText(entry.title, qt.Qt.ElideRight, available))
        else:
            for text, highlighted in segments:
                painter.setFont(boldFont if highlighted else titleFont)
                painter.setPen(colors["accent"] if highlighted else textColor)
                painter.drawText(x, baseline, text)
                x += _advance(boldMetrics if highlighted else metrics, text)
        painter.restore()


def _advance(metrics, text):
    advance = getattr(metrics, "horizontalAdvance", None)
    return advance(text) if advance is not None else metrics.width(text)


def _segments(text, ranges):
    """``[(text, highlighted)]`` pieces of ``text`` for highlighted character ranges."""
    if not ranges:
        return [(text, False)]
    segments = []
    position = 0
    for start, end in sorted(ranges):
        if start > position:
            segments.append((text[position:start], False))
        segments.append((text[max(start, position):end], True))
        position = max(position, end)
    if position < len(text):
        segments.append((text[position:], False))
    return [segment for segment in segments if segment[0]]


def _hasControl(event):
    """True if Ctrl (⌘ on macOS) is held during a key event."""
    try:
        # Explicit integers: a QFlags value may not compare equal to an enum value (Qt 6)
        return bool(int(event.modifiers()) & int(qt.Qt.ControlModifier))
    except (TypeError, ValueError):
        return bool(event.modifiers() & qt.Qt.ControlModifier)


class _KeyFilter(qt.QObject):
    """Keys of the search field: arrows move, Enter runs, Esc and Ctrl+K close."""

    def __init__(self, palette):
        qt.QObject.__init__(self)
        self.palette = palette

    def eventFilter(self, obj, event):
        try:
            if event.type() == qt.QEvent.KeyPress:
                return self.palette._onKey(event)
            if event.type() == qt.QEvent.Hide and obj == self.palette.widget:
                self.palette._onHidden()
        except Exception:
            logging.error(f"Studio: Search key handling failed:\n{traceback.format_exc()}")
        return False


class Palette:
    """The Search popup (one per main window, reused)."""

    def __init__(self, shell):
        self.shell = shell
        self.mainWindow = shell.mainWindow
        self.index = Index(shell)
        self.category = None
        self.rows = []
        self.enabledState = {}
        self.hoverRow = -1
        self.normalizedQuery = ""
        self.lastOpenMs = None
        self.firstOpenMs = None
        self.lastSearchMs = None
        self._iconCache = {}
        self._paletteKey = None
        self.colors = {}

        widget = qt.QFrame(self.mainWindow, qt.Qt.Popup | qt.Qt.FramelessWindowHint)
        widget.setObjectName("StudioPalette")
        widget.setAccessibleName(_("Search"))
        widget.setWindowTitle(_("Search"))
        layout = qt.QVBoxLayout(widget)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self.widget = widget

        field = qt.QLineEdit()
        field.setObjectName("StudioPaletteSearch")
        field.setFixedHeight(FIELD_HEIGHT)
        field.setClearButtonEnabled(True)
        field.setAccessibleName(_("Search"))
        layout.addWidget(field)
        self.field = field

        listWidget = qt.QListWidget()
        listWidget.setObjectName("StudioPaletteList")
        listWidget.setFrameShape(qt.QFrame.NoFrame)
        listWidget.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        listWidget.setFocusPolicy(qt.Qt.NoFocus)
        listWidget.setMouseTracking(True)
        listWidget.setUniformItemSizes(False)
        listWidget.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        layout.addWidget(listWidget, 1)
        self.list = listWidget
        self.delegate = _RowDelegate(self)
        listWidget.setItemDelegate(self.delegate)

        emptyLabel = qt.QLabel()
        emptyLabel.setObjectName("StudioPaletteEmpty")
        emptyLabel.setWordWrap(True)
        emptyLabel.setAlignment(qt.Qt.AlignCenter)
        emptyLabel.setContentsMargins(24, 24, 24, 24)
        emptyLabel.hide()
        layout.addWidget(emptyLabel, 1)
        self.emptyLabel = emptyLabel

        footer = qt.QLabel(_("↑ ↓ move · Enter run · Esc close · Ctrl+/ all keys"))
        footer.setObjectName("StudioPaletteFooter")
        footer.setFixedHeight(FOOTER_HEIGHT)
        footer.setContentsMargins(12, 0, 12, 0)
        layout.addWidget(footer)
        self.footer = footer

        self._keyFilter = _KeyFilter(self)
        field.installEventFilter(self._keyFilter)
        widget.installEventFilter(self._keyFilter)
        field.connect("textChanged(QString)", self._onTextChanged)
        listWidget.connect("itemClicked(QListWidgetItem*)", self._onItemClicked)
        listWidget.connect("entered(QModelIndex)", self._onEntered)
        listWidget.connect("currentRowChanged(int)", lambda row: listWidget.viewport().update())

    #
    # Look
    #

    def _applyStyle(self):
        key = slicer.app.palette().cacheKey()
        if key == self._paletteKey:
            return
        self._paletteKey = key
        self._iconCache = {}
        accentSubtle = ui.token("accent-subtle")
        self.colors = {
            "text": ui.token("text"), "text2": ui.token("text2"), "text3": ui.token("text3"), "disabled": ui.token("text-disabled"),
            "accent": ui.token("accent-fg"), "border": ui.token("border"), "selected": accentSubtle, "hover": ui.token("control-hover"),
        }
        card = ui.token("card").name()
        border = ui.token("border").name()
        hairline = ui.token("hairline").name()
        self.widget.setStyleSheet(
            f"QFrame#StudioPalette {{ background-color: {card}; border: 1px solid {border}; }}"
            f"QLineEdit#StudioPaletteSearch {{ background-color: {card}; color: {ui.token('text').name()}; border: none; "
            f"border-bottom: 1px solid {hairline}; padding: 0px 12px; font-size: {ui.fontPx(15)}px; }}"
            f"QListWidget#StudioPaletteList {{ background-color: {card}; border: none; outline: none; }}"
            f"QLabel#StudioPaletteEmpty {{ color: {ui.token('text2').name()}; font-size: {ui.fontPx(13)}px; }}"
            f"QLabel#StudioPaletteFooter {{ color: {ui.token('text3').name()}; border-top: 1px solid {hairline}; font-size: {ui.fontPx(11)}px; }}")

    def iconPixmap(self, name, enabled=True):
        key = (name, enabled)
        pixmap = self._iconCache.get(key)
        if pixmap is None:
            try:
                pixmap = ui.pixmap(name, ICON_SIZE, "text2" if enabled else "text-disabled")
            except Exception:
                pixmap = ui.pixmap("circle-dashed", ICON_SIZE, "text2")
            self._iconCache[key] = pixmap
        return pixmap

    #
    # Open and close
    #

    def isOpen(self):
        return _isAlive(self.widget) and self.widget.isVisible()

    def open(self, category=None):
        startTime = time.perf_counter()
        self.category = category
        self._applyStyle()
        self.index.refresh()
        self.field.setPlaceholderText(_("Search modules") if category == CATEGORY_MODULES else _("Search modules, tools, actions, layouts, files…"))
        self.field.blockSignals(True)
        self.field.clear()
        self.field.blockSignals(False)
        self._place()
        self._update("")
        self.widget.show()
        self.widget.raise_()
        self.widget.activateWindow()
        self.field.setFocus(qt.Qt.PopupFocusReason)
        self.lastOpenMs = (time.perf_counter() - startTime) * 1000.0
        if self.firstOpenMs is None:
            self.firstOpenMs = self.lastOpenMs
        return True

    def _place(self):
        mainWindow = self.mainWindow
        width = min(POPUP_WIDTH, max(320, mainWindow.width - 2 * POPUP_MARGIN))
        height = min(POPUP_HEIGHT, max(200, mainWindow.height - POPUP_TOP - POPUP_MARGIN))
        topLeft = mainWindow.mapToGlobal(qt.QPoint((mainWindow.width - width) // 2, POPUP_TOP))
        self.widget.setGeometry(topLeft.x(), topLeft.y(), width, height)

    def close(self):
        if _isAlive(self.widget) and self.widget.isVisible():
            self.widget.hide()

    def _onHidden(self):
        self.hoverRow = -1

    #
    # Results
    #

    def setQuery(self, text):
        """Type ``text`` in the field (tests)."""
        self.field.setText(text)

    def _onTextChanged(self, text):
        self._update(text)

    def _update(self, text):
        startTime = time.perf_counter()
        self.normalizedQuery = finder.normalizedQuery(text)
        rows = []
        if not self.normalizedQuery:
            rows = self._emptyQueryRows()
        else:
            matches = rank(self.index.entries, text, self.category)
            if matches and matches[0][1] == finder.TIER_FULL_TEXT:
                # Nothing is named like the query: say so, and list where the help mentions it
                rows = [("header", _("Mentioned in the help"))]
            rows += [("entry", entry) for entry, tier in matches]
        self._setRows(rows)
        named = any(kind == "entry" for kind, payload in rows) and rows[0][0] == "entry" if self.normalizedQuery else True
        noMatch = bool(self.normalizedQuery) and not named
        if noMatch:
            self.emptyLabel.setText(_("No match for “{query}”. Try “save”, “export”, “figure”, “crosshair”.").format(query=text.strip()))
        self.emptyLabel.setVisible(noMatch)
        self.list.setVisible(any(kind == "entry" for kind, payload in rows))
        self.lastSearchMs = (time.perf_counter() - startTime) * 1000.0

    def isNoMatchShown(self):
        """True if the "No match for …" line is shown (nothing is named like the query)."""
        return _isAlive(self.emptyLabel) and not self.emptyLabel.isHidden()

    def _emptyQueryRows(self):
        entries = self.index.byId
        rows = []
        modulesOnly = self.category == CATEGORY_MODULES
        recentTools = []
        for entryId in mru():
            entry = entries.get(entryId)
            if entry is None or entry.kind == KIND_RECENT or not _inCategory(entry, self.category):
                continue
            recentTools.append(entry)
            if len(recentTools) >= MAX_RECENT_TOOLS:
                break
        if recentTools:
            rows.append(("header", _("Recent tools") if not modulesOnly else _("Recent modules")))
            rows += [("entry", entry) for entry in recentTools]
        if not modulesOnly:
            recentFiles = [entry for entry in entries.values() if entry.kind == KIND_RECENT][:MAX_RECENT_FILES]
            if recentFiles:
                rows.append(("header", _("Recent files")))
                rows += [("entry", entry) for entry in recentFiles]
        favorites = []
        shown = {payload.id for kind, payload in rows if kind == "entry"}
        for moduleName in favoriteModules():
            ws = workspaces.workspaceForModule(moduleName)
            entry = entries.get(f"workspace.{ws.key}") if ws is not None else entries.get(f"module.{moduleName}")
            if entry is not None and entry.id not in shown:
                favorites.append(entry)
                shown.add(entry.id)
        if favorites:
            rows.append(("header", _("Favorites")))
            rows += [("entry", entry) for entry in favorites]
        return rows

    def _setRows(self, rows):
        listWidget = self.list
        listWidget.setUpdatesEnabled(False)
        try:
            listWidget.blockSignals(True)
            listWidget.clear()
            self.rows = rows
            self.enabledState = {}
            self.hoverRow = -1
            firstEntry = -1
            for row, (kind, payload) in enumerate(rows):
                item = qt.QListWidgetItem(str(payload) if kind == "header" else payload.title)
                if kind == "header":
                    item.setFlags(qt.Qt.NoItemFlags)
                    item.setSizeHint(qt.QSize(0, HEADER_HEIGHT))
                else:
                    enabled = payload.isEnabled()
                    self.enabledState[payload.id] = enabled
                    item.setSizeHint(qt.QSize(0, ROW_HEIGHT))
                    toolTip = ui.tooltip(payload.title, payload.key, payload.description or None)
                    item.setToolTip(toolTip)
                    if firstEntry < 0:
                        firstEntry = row
                item.setData(_ROLE_ROW, row)
                listWidget.addItem(item)
            listWidget.blockSignals(False)
            if firstEntry >= 0:
                listWidget.setCurrentRow(firstEntry)
                listWidget.scrollToItem(listWidget.item(firstEntry))
        finally:
            listWidget.blockSignals(False)
            listWidget.setUpdatesEnabled(True)

    def resultEntries(self):
        """The entries listed now, in order (tests)."""
        return [payload for kind, payload in self.rows if kind == "entry"]

    def currentEntry(self):
        row = self.list.currentRow
        if 0 <= row < len(self.rows) and self.rows[row][0] == "entry":
            return self.rows[row][1]
        return None

    def _moveCurrent(self, step):
        rows = self.rows
        row = self.list.currentRow
        count = len(rows)
        if count == 0:
            return
        candidate = row
        for _attempt in range(count):
            candidate += step
            if candidate < 0 or candidate >= count:
                return
            if rows[candidate][0] == "entry":
                self.list.setCurrentRow(candidate)
                self.list.scrollToItem(self.list.item(candidate))
                return

    #
    # Input
    #

    def _onKey(self, event):
        key = event.key()
        control = _hasControl(event)
        if key == qt.Qt.Key_Escape:
            self.close()
            return True
        if control and key == qt.Qt.Key_K:
            self.close()
            return True
        if key in (qt.Qt.Key_Down, qt.Qt.Key_Up):
            self._moveCurrent(1 if key == qt.Qt.Key_Down else -1)
            return True
        if key in (qt.Qt.Key_PageDown, qt.Qt.Key_PageUp):
            for _step in range(8):
                self._moveCurrent(1 if key == qt.Qt.Key_PageDown else -1)
            return True
        if key in (qt.Qt.Key_Return, qt.Qt.Key_Enter):
            self.activateCurrent()
            return True
        if key in (qt.Qt.Key_Tab, qt.Qt.Key_Backtab):
            self._moveCurrent(-1 if key == qt.Qt.Key_Backtab else 1)
            return True
        return False

    def _onItemClicked(self, item):
        row = self.list.row(item)
        if 0 <= row < len(self.rows) and self.rows[row][0] == "entry":
            self.list.setCurrentRow(row)
            self.activateCurrent()

    def _onEntered(self, index):
        self.hoverRow = index.row()
        self.list.viewport().update()

    def activateCurrent(self):
        """Run the current row (Enter)."""
        entry = self.currentEntry()
        if entry is None:
            return False
        return self.run(entry)

    def run(self, entry, deferred=True):
        """Close the popup and run ``entry`` (on the next event loop iteration, after the popup has closed)."""
        if not entry.isEnabled():
            self.close()
            self.shell.toast(_("{title} is not available now").format(title=entry.title))
            return False
        if entry.kind != KIND_WHERE:
            recordRun(entry.id)
        self.close()
        if deferred:
            qt.QTimer.singleShot(0, lambda entry=entry: _invoke(entry, self.shell))
        else:
            _invoke(entry, self.shell)
        return True

    def dispose(self):
        self.close()
        if _isAlive(self.widget):
            self.field.removeEventFilter(self._keyFilter)
            self.widget.removeEventFilter(self._keyFilter)
            self.widget.deleteLater()
        self.widget = None


def _invoke(entry, shell):
    try:
        entry.callback()
    except Exception:
        logging.error(f"Studio: Search entry {entry.id} failed:\n{traceback.format_exc()}")
        if shell is not None and shell.isActive():
            shell.toast(_("{title} failed: see the error log").format(title=entry.title), 4000)


#
# API
#


def instance():
    """The palette of the live shell (None before the first ``open()`` or in Classic)."""
    if _palette is None or not _isAlive(_palette.widget) or not _palette.shell.isActive():
        return None
    return _palette


def open(shell, category=None):
    """Show Search (``Ctrl+K``); ``category="modules"`` lists only the modules (``Ctrl+F``). A second call closes it.

    Return True if it is shown.
    """
    global _palette
    if shell is None or not shell.isActive():
        return False
    if _palette is not None and (_palette.shell is not shell or not _isAlive(_palette.widget) or _palette.mainWindow is not shell.mainWindow):
        _palette.dispose()
        _palette = None
    if _palette is None:
        _palette = Palette(shell)
    if _palette.isOpen() and _palette.category == category:
        _palette.close()
        return False
    return _palette.open(category)


def close():
    """Close Search if it is shown."""
    if _palette is not None:
        _palette.close()


def search(shell, query, category=None):
    """The entries that Search lists for ``query``, best first, without showing it (tests, other plug-ins)."""
    global _palette
    if shell is None or not shell.isActive():
        return []
    if _palette is None or _palette.shell is not shell or not _isAlive(_palette.widget):
        if _palette is not None:
            _palette.dispose()
        _palette = Palette(shell)
    _palette.index.refresh()
    return [entry for entry, tier in rank(_palette.index.entries, query, category)]


def runEntry(shell, entryId, deferred=False):
    """Run the entry ``entryId`` as if it was chosen in Search (recorded in ``Studio/PaletteMRU``). Return True if it ran."""
    global _palette
    if shell is None or not shell.isActive():
        return False
    if _palette is None or _palette.shell is not shell or not _isAlive(_palette.widget):
        _palette = Palette(shell)
    _palette.index.refresh()
    entry = _palette.index.entry(entryId)
    return _palette.run(entry, deferred=deferred) if entry is not None else False


def dispose():
    """Remove the popup (the shell goes Classic or quits)."""
    global _palette
    _forgetWindowLevelsObserver()
    if _palette is not None:
        _palette.dispose()
        _palette = None

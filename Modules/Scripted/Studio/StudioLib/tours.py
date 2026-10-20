r"""Content of the guided tours of Slicer Studio (T-01): "Slicer Studio in 2 minutes", "Coming from 3D Slicer" and
one tour per workspace.

The tours are data, played by ``StudioLib.tour``. A step points at one control (``target``: an objectName, a path
"Parent/Child" of objectNames, a tuple of alternatives, or a function ``target(shell)`` that returns a widget), says
what it is (``title``, ``body``), how the same thing is done in official 3D Slicer (``rows``: rows of
``StudioLib.migration``, the single source of the comparisons, and/or ``inSlicer``) and which keys do it (``keys``).
A step whose control is not built or not visible is skipped, so the tours grow as features land and never point at
nothing. A step may ask for a workspace (``workspace``) or the inspector (``inspector``); the player restores the
user's workspace, layout and inspector afterwards and never changes the scene.

Keep the texts plain and short, written for someone who knows official 3D Slicer well. When a work package adds,
moves or renames a control, update the step that shows it and the 'Where did it go?' rows (D-T1), and run
StudioTourTest. Raise ``CONTENT_VERSION`` when a tour changes enough to be offered again.
"""

import slicer
from slicer.i18n import tr as _

from . import migration

#: Version of the tour content, stored with the completed tours (``Studio/Tour/Completed``)
CONTENT_VERSION = 1


class Step:
    """One step of a tour.

    :param stepId: identifier, unique in its tour.
    :param target: the control: objectName, "Parent/Child", a tuple of alternatives, or ``function(shell) -> widget``.
    :param title: one short line.
    :param body: one to three short sentences, or ``function(shell) -> str``.
    :param rows: ids of ``migration.ROWS``: the "In 3D Slicer" line lists their official places.
    :param inSlicer: how it is done in official 3D Slicer, instead of the rows (may contain ``{key:...}``).
    :param keys: ``[(keys, text)]``: key chips and what they do, for example ``(("Ctrl+O",), "open files")``.
    :param workspace: the workspace to show first ("library", "draw", "map"), or None.
    :param inspector: True to show the inspector first.
    :param placement: preferred side of the card: "auto", "right", "below", "left" or "above".
    :param tryIt: an action to try inside the spotlight ("click Draw"); clicks reach the control, and the tour moves
        on when ``done(shell)`` returns True. Next stays available.
    :param done: ``function(shell) -> bool`` for ``tryIt``.
    :param when: ``function(shell) -> bool``: the step is shown only when it returns True (feature detection).
    """

    def __init__(self, stepId, target, title, body, rows=(), inSlicer=None, keys=(), workspace=None, inspector=False,
                 placement="auto", tryIt=None, done=None, when=None):
        self.id = stepId
        self.target = target
        self.title = title
        self.body = body
        self.rows = tuple(rows)
        self.inSlicer = inSlicer
        self.keys = tuple(keys)
        self.workspace = workspace
        self.inspector = inspector
        self.placement = placement
        self.tryIt = tryIt
        self.done = done
        self.when = when

    def bodyText(self, shell):
        return self.body(shell) if callable(self.body) else self.body

    def inSlicerText(self, native=True):
        """The "In 3D Slicer" line: ``inSlicer``, else the official places of ``rows``."""
        if self.inSlicer:
            return migration.formatKeys(self.inSlicer, native)
        places = []
        for rowId in self.rows:
            row = migration.row(rowId)
            if row is not None:
                places.append(row.officialText(native))
        return " · ".join(places)

    def targets(self):
        """The target alternatives (objectName paths or functions), in order."""
        return self.target if isinstance(self.target, (tuple, list)) else (self.target,)

    def __repr__(self):
        return f"Step({self.id!r})"


class Tour:
    """A guided tour: an ordered list of steps.

    :param tourId: stable identifier ("studio", "from-slicer", "library", "draw", "map").
    :param workspace: the workspace that the tour is about ("Tour this workspace"), or None.
    :param available: ``function(shell) -> bool``: the tour is offered only when it returns True.
    :param finishText: the label of the last Next button (default "Done").
    """

    def __init__(self, tourId, title, description, steps, workspace=None, available=None, finishText=None):
        self.id = tourId
        self.title = title
        self.description = description
        self.steps = list(steps)
        self.workspace = workspace
        self.available = available
        self.finishText = finishText

    def isAvailable(self, shell):
        if self.workspace is not None and not _workspaceAvailable(self.workspace):
            return False
        return self.available is None or bool(self.available(shell))

    def __repr__(self):
        return f"Tour({self.id!r})"


#
# Feature detection
#


def _workspaceAvailable(key):
    from . import workspaces

    ws = workspaces.workspace(key)
    return ws is not None and workspaces.isAvailable(ws)


def _mapAvailable(shell=None):
    return _workspaceAvailable("map")


def _leftDragWindowLevel(shell=None):
    """True if left-drag adjusts window/level in the views (Studio only, ``Studio/LeftDragWindowLevel``)."""
    windowLevelWidget = getattr(slicer, "vtkMRMLWindowLevelWidget", None)
    if windowLevelWidget is None or not hasattr(windowLevelWidget, "SetLeftDragWindowLevel"):
        return False
    from . import shell as shellModule

    return bool(shellModule.Shell.navigationBehaviours().get("leftDragWindowLevel"))


def _hasSourceVolume():
    widget = getattr(slicer.modules, "SegmentEditorWidget", None)
    editor = getattr(widget, "editor", None) if widget is not None else None
    return editor is not None and editor.sourceVolumeNode() is not None


#
# Texts that depend on the build or on the data
#


def _workspacesBody(shell):
    text = _("Library opens your data, Draw traces lesions and Map runs lesion network mapping. Every other module is still there, one search away.")
    if not _mapAvailable():
        text = _("Library opens your data and Draw traces lesions. Map, for lesion network mapping, works when it is part of this build. "
                 "Every other module is still there, one search away.")
    return text


def _drawToolsBody(shell):
    if _hasSourceVolume():
        return _("The same effects as in 3D Slicer, next to your lesion. Paint is ready: drag in a view to draw.")
    return _("The same effects as in 3D Slicer, next to your lesion. Once a scan is open (Library, or Start on MNI template), "
             "Paint is ready and you draw straight away.")


def _viewsBody(shell):
    text = _("Scroll changes the slice, Ctrl + wheel zooms and Shift + move jumps the crosshair, as in 3D Slicer.")
    if _leftDragWindowLevel():
        text += " " + _("New: drag with the left button to adjust window/level when no tool is active.")
    return text


def _escapeBody(shell):
    return _("Esc closes a popover, leaves Place or window/level mode, restores a maximized view, shows hidden panels, "
             "deselects the Draw tool and returns from a module opened with Search: one step per press.")


#
# The tours
#

_STUDIO_STEPS = (
    Step("top-bar", "StudioTopBar", _("One bar instead of menus and toolbars"),
         _("Slicer Studio is 3D Slicer with a calmer face: the same application, modules and files. This bar is how you get around."),
         inSlicer=_("The menu bar (File, Edit, View, Help) and seven toolbars: Load/Save, Module Selection, Favorite Modules, Layout Selection, "
                    "Mouse Interaction, Crosshair Selection and Extensions."),
         placement="below"),
    Step("library", ("StudioDropZone", "StudioLibraryOpenButton"), _("Library: open your data"),
         _("Drop scans, lesion masks, scenes or a DICOM folder anywhere on the window, or press Ctrl+O. "
           "They open with the same readers as in 3D Slicer."),
         rows=("add-data", "add-dicom"), keys=((("Ctrl+O",), _("open files")),), workspace="library"),
    Step("template", "StudioLibraryStartOnTemplateButton", _("Start on the MNI template"),
         _("Opens the bundled MNI152NLin2009cAsym template in Draw, ready for a lesion. It works offline."),
         inSlicer=_("No single step: File > Add Data to load a template, then Add segment in the Segment Editor module."),
         workspace="library"),
    Step("workspaces", "StudioWorkspaceSwitcher", _("Three workspaces"), _workspacesBody,
         rows=("modules", "favorite-modules"), keys=((("Ctrl+Alt+1", "Ctrl+Alt+2", "Ctrl+Alt+3"), _("Library, Draw, Map")),),
         placement="below", tryIt=_("Click Draw to go on."), done=lambda shell: shell.currentWorkspace() == "draw"),
    Step("draw-tools", "EffectsGroupBox", _("Draw is the Segment Editor"), _drawToolsBody,
         rows=("segment-editor",), keys=((("1", "0"), _("pick a tool")), (("Esc",), _("deselect")), (("Space",), _("pause"))),
         workspace="draw", inspector=True, placement="left"),
    Step("save-lesion", "StudioSaveLesionButton", _("Save lesion"),
         _("Writes the lesion as a NIfTI mask (.nii.gz) that official 3D Slicer, FSL and other tools read. Ctrl+S still saves the whole scene, as in 3D Slicer."),
         rows=("export-segmentation",), keys=((("Ctrl+Shift+S",), _("save lesion")), (("Ctrl+Enter",), _("the blue button"))),
         workspace="draw", inspector=True, placement="left"),
    Step("inspector", "StudioPanelHeader", _("The inspector"),
         _("The current workspace or module, on the right. The ? button shows help for the current tool and the ⋯ button has more, "
           "such as the Data Probe."),
         rows=("module-panel",), keys=((("Ctrl+\\", "Ctrl+5"), _("show or hide")),), workspace="draw", inspector=True, placement="left"),
    Step("layout", "StudioLayoutButton", _("Layouts"),
         _("Focus shows one large view and two small ones. Single, Four-up and Row are one key away, and More layouts… lists every 3D Slicer layout."),
         rows=("layout",), keys=((("Alt+1", "Alt+4"), _("Focus, Single, Four-up, Row")),), placement="below"),
    Step("search", "StudioSearchPill", _("Search"),
         _("Find any module by name, also the ones that no workspace shows. A module opened from Search has a ← back button, and Esc returns too."),
         rows=("modules", "module-finder"), keys=((("Ctrl+K",), _("search")),), placement="below"),
    Step("app-menu", "StudioAppMenuButton", _("Everything else: the ≡ menu"),
         _("File, Edit, View and Help are all here, with Settings, the Python console and the error log, and Help > Take a tour to see this again."),
         inSlicer=_("The menu bar: File, Edit, View and Help; Edit > Application Settings ({key:Ctrl+2}); View > Python Console ({key:Ctrl+3})."),
         keys=((("F10",), _("open the menu")),), placement="below"),
    Step("autosave", "StudioSaveStatus", _("Autosave"),
         _("Studio autosaves your work to a private folder and offers to recover it after a crash. Autosave is not Save: your files change only when you save."),
         inSlicer=_("No autosave: you save with File > Save Data ({key:Ctrl+S})."), placement="below"),
    Step("classic", "StudioLibraryClassicLink", _("Classic interface, one switch away"),
         _("Everything you know from 3D Slicer is one switch away: menus, toolbars and the module panel, exactly as in 3D Slicer. "
           "Your scene stays as it is when you switch."),
         inSlicer=_("This is the 3D Slicer interface itself, unchanged."), keys=((("Ctrl+Alt+0",), _("switch and back")),), workspace="library"),
)

_FROM_SLICER_STEPS = (
    Step("same-files", ("StudioDropZone", "StudioLibraryOpenButton"), _("Your files open in both"),
         _("Slicer Studio is built from the 3D Slicer source. Scenes (.mrml, .mrb) and data saved here open in official 3D Slicer, "
           "and files from 3D Slicer open here."),
         rows=("add-data", "save-data"), workspace="library"),
    Step("modules", "StudioSearchPill", _("Every module is still here"),
         _("Library, Draw and Map cover the lesion workflow. The other modules, and those of your extensions, are one search away."),
         rows=("modules", "module-finder"), keys=((("Ctrl+K",), _("search")),), placement="below"),
    Step("draw", "StudioDrawWorkspaceButton", _("Draw is the Segment Editor"),
         _("The same effects and keys, with your lesion in the inspector. Save lesion writes a NIfTI mask; Ctrl+S saves the scene as before."),
         rows=("segment-editor", "export-segmentation"), keys=((("Ctrl+Alt+2",), _("Draw")),), placement="below"),
    Step("views", "qMRMLSliceWidgetRed/SliceView", _("The views work as you know them"), _viewsBody,
         rows=("mouse-mode", "view-controllers"), workspace="draw"),
    Step("probe-toolbars", "StudioPanelMoreButton", _("Data Probe and toolbars"),
         _("Show the Data Probe from ⋯. A toolbar that you turn on in ≡ > View > Toolbars stays visible in Studio."),
         rows=("data-probe", "toolbars"), workspace="draw", inspector=True, placement="left"),
    Step("menus-python", "StudioAppMenuButton", _("Menus, Python and extensions"),
         _("File, Edit, View and Help are in ≡. The Python console, slicer.util, scripted modules and extensions work unchanged. "
           "Settings are shared with your 3D Slicer installations."),
         rows=("python-console", "extensions", "settings"), keys=((("F10",), _("open the menu")),), placement="below"),
    Step("escape", "StudioWorkspaceSwitcher", _("Esc is your back key"), _escapeBody,
         inSlicer=_("The Previous arrow of the Module Selection toolbar ({key:Ctrl+Left}) returns to the last module; Esc deselects the Segment Editor effect."),
         keys=((("Esc",), _("one step back")),), placement="below"),
    Step("where", "StudioAppMenuButton", _("Where did it go?"),
         _("≡ > Help > Where did it go? lists the places of 3D Slicer and where they are now, with Show me. "
           "The Classic interface is always one switch away."),
         inSlicer=_("No such list: everything is in the menus, the toolbars and the Modules drop-down."),
         keys=((("Ctrl+Alt+0",), _("Classic interface")),), placement="below"),
)

_LIBRARY_STEPS = (
    Step("drop", "StudioDropZone", _("Drop anything here"),
         _("Scans, lesion masks, scenes (.mrml, .mrb) and DICOM folders: drop them anywhere on the window. A case-report figure "
           "(PNG, JPEG, TIFF) is shown next to the template, a statistical map as an overlay."),
         rows=("add-data",), workspace="library"),
    Step("open", "StudioLibraryOpenButton", _("Open and Open folder"),
         _("Open… picks files; Open folder… reads a DICOM folder or a folder of NIfTI files."),
         rows=("add-data", "add-dicom"), keys=((("Ctrl+O",), _("open files")),), workspace="library"),
    Step("template", "StudioLibraryStartOnTemplateButton", _("Start on the MNI template"),
         _("Opens the bundled 2 mm MNI152NLin2009cAsym template in Draw with an empty lesion. It works offline."),
         inSlicer=_("No single step: File > Add Data to load a template, then Add segment in the Segment Editor module."), workspace="library"),
    Step("recent", "StudioRecentList", _("Recent"), _("The files you opened last. A file that is already open is not opened twice."),
         rows=("recent",), workspace="library"),
    Step("sample", "StudioLibrarySampleDataLink", _("Sample data"), _("The 3D Slicer sample data sets, downloaded when you choose one. A scan opens in Draw."),
         rows=("sample-data",), workspace="library"),
    Step("classic", "StudioLibraryClassicLink", _("Classic interface"),
         _("The 3D Slicer interface, exactly as you know it. Your scene stays as it is when you switch."),
         inSlicer=_("This is the 3D Slicer interface itself, unchanged."), keys=((("Ctrl+Alt+0",), _("switch and back")),), workspace="library"),
)

_DRAW_STEPS = (
    Step("tools", "EffectsGroupBox", _("The tools"), _drawToolsBody, rows=("segment-editor-tools",),
         keys=((("1", "0"), _("pick a tool")), (("Esc",), _("deselect")), (("Space",), _("pause"))), workspace="draw", inspector=True, placement="left"),
    Step("paint", "EffectsGroupBox/Paint", _("Paint and the brush size"),
         _("Paint is armed when you arrive with a scan. Shift + wheel or the + and - keys change the brush size, Ctrl + wheel zooms."),
         inSlicer=_("Segment Editor > Paint: Shift + mouse wheel or +/- keys adjust the brush size."), workspace="draw", inspector=True, placement="left"),
    Step("smart-fill", "EffectsGroupBox/Smart fill", _("Smart fill"),
         _("Click inside a lesion: Smart fill grows the region with an automatic intensity tolerance and shows a preview before you apply it."),
         inSlicer=_("No such effect; the closest are Grow from seeds and Level tracing."), workspace="draw", inspector=True, placement="left"),
    Step("segments", "qMRMLSegmentEditorWidget/SegmentsTableView", _("Your lesion"),
         _("The segments of the lesion segmentation, as in 3D Slicer. Each segment is saved as one label of the mask."),
         inSlicer=_("The segments table of the Segment Editor module."), workspace="draw", inspector=True, placement="left"),
    Step("undo", "StudioUndoButton", _("Undo and Redo"), _("Undo and redo your lesion edits, also from the keyboard."),
         rows=("segment-editor-undo",), keys=((("Ctrl+Z",), _("undo")), (("Ctrl+Y",), _("redo"))), workspace="draw", placement="below"),
    Step("save", "StudioSaveLesionButton", _("Save lesion"),
         _("Writes the lesion as a NIfTI mask (.nii.gz) that official 3D Slicer, FSL and other tools read. Ctrl+S still saves the whole scene."),
         rows=("export-segmentation",), keys=((("Ctrl+Shift+S",), _("save lesion")),), workspace="draw", inspector=True, placement="left"),
)

_MAP_STEPS = (
    Step("map", "StudioMapWorkspaceButton", _("Map"), _("Lesion network mapping: from traced lesions to network maps, with quality checks."),
         inSlicer=_("Not part of official 3D Slicer."), placement="below"),
    Step("panel", "PanelDockWidget", _("The Map inspector"), _("Choose the lesions and the connectome, then Map network. Results open in the views."),
         inSlicer=_("Not part of official 3D Slicer."), workspace="map", inspector=True, placement="left"),
)

TOURS = (
    Tour("studio", _("Slicer Studio in 2 minutes"), _("The top bar, the three workspaces, Search, layouts and the Classic interface."), _STUDIO_STEPS),
    Tour("from-slicer", _("Coming from 3D Slicer"), _("What stays the same, what moved, and where to find it now."), _FROM_SLICER_STEPS,
         finishText=_("Open the map")),
    Tour("library", _("Library tour"), _("Opening data, the MNI template, Recent and sample data."), _LIBRARY_STEPS, workspace="library"),
    Tour("draw", _("Draw tour"), _("The tools, the brush, your lesion, Undo and Save lesion."), _DRAW_STEPS, workspace="draw"),
    Tour("map", _("Map tour"), _("Lesion network mapping."), _MAP_STEPS, workspace="map", available=_mapAvailable),
)


def tour(tourId):
    """The tour ``tourId``, or None."""
    for candidate in TOURS:
        if candidate.id == tourId:
            return candidate
    return None


def workspaceTour(workspaceKey):
    """The tour of a workspace ("library", "draw", "map"), or None."""
    for candidate in TOURS:
        if candidate.workspace == workspaceKey:
            return candidate
    return None

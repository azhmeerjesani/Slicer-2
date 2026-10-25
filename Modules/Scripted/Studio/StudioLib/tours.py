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


def _cohortSeedAvailable(shell=None):
    """True when Map offers the Cohort seed (its Cohort card plug-in is part of this build)."""
    import importlib.util

    try:
        return _mapAvailable() and importlib.util.find_spec("LesionNetworkMappingUI.card_cohort") is not None
    except (ImportError, ValueError):
        return False


def _leftDragWindowLevel(shell=None):
    """True if left-drag adjusts window/level in the views (Studio only, ``Studio/LeftDragWindowLevel``)."""
    windowLevelWidget = getattr(slicer, "vtkMRMLWindowLevelWidget", None)
    if windowLevelWidget is None or not hasattr(windowLevelWidget, "SetLeftDragWindowLevel"):
        return False
    from . import shell as shellModule

    return bool(shellModule.Shell.navigationBehaviours().get("leftDragWindowLevel"))


def _flatViewBars(shell=None):
    """True if the view controller bars use the flat presentation (``qMRMLViewControllerBar.flatStyle``, W2-03)."""
    layoutManager = slicer.app.layoutManager()
    widget = layoutManager.sliceWidget("Red") if layoutManager is not None else None
    controller = widget.sliceController() if widget is not None else None
    return bool(controller is not None and getattr(controller, "flatStyle", False))


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


#: The tools of Draw, or its empty state while no scan is open (the tools appear with the scan)
_DRAW_TOOLS_TARGET = ("EffectsGroupBox", "StudioDrawEmptyState")


def _drawToolsBody(shell):
    text = _("The Segment Editor effects as labelled tools: Brush, Outline, Smart fill, Erase and Fill gaps; More tools shows the others. ")
    if _hasSourceVolume():
        return text + _("Pick Brush (P) and drag in a view to draw. On the MNI template or a new lesion it is ready.")
    return text + _("Once a scan is open (Library, or Start on MNI template), pick Brush (P) and drag in a view to draw.")


def _viewsBody(shell):
    text = _("Scroll changes the slice, Ctrl + wheel zooms and Shift + move jumps the crosshair, as in 3D Slicer.")
    if _leftDragWindowLevel():
        text += " " + _("New: drag with the left button to adjust window/level when no tool is active.")
    text += " " + _("Letters at the edges of each view name the patient's directions and \"Radiological · L on right\" (bottom right) says where the patient's left is; "
                    "a pill at the top of the views names a mode such as Place, with a ✕ to leave it.")
    if _flatViewBars(shell):
        text += " " + _("The bar above each view starts with the view color, then the orientation (Axial ▾, with Rotate to volume plane, "
                        "Reformat… and Reset), the slider, the readout (the position \"S 17.5 mm\" and the slice number \"49 / 97\"), link, fit, "
                        "the sliders button for layers, opacity and interpolation (on click), and maximize.")
    else:
        text += " " + _("The bar above each view is the one of 3D Slicer: hover the pin for layers, opacity and interpolation.")
    return text


def _escapeBody(shell):
    return _("Esc closes a popover, leaves Place or window/level mode (a pill at the top of the views names it), restores a maximized view, "
             "shows hidden panels, deselects the Draw tool and returns from a module opened with Search: one step per press.")


#
# The tours
#

_STUDIO_STEPS = (
    Step("top-bar", "StudioTopBar", _("One bar instead of menus and toolbars"),
         _("Slicer Studio is 3D Slicer with a calmer face: the same application, modules and files. This bar is how you get around. "
           "On the left, the name of the open project: a click shows it in Library, and its ▾ renames it, shows it in your file manager, "
           "starts a new project or opens a recent one."),
         inSlicer=_("The menu bar (File, Edit, View, Help) and seven toolbars: Load/Save, Module Selection, Favorite Modules, Layout Selection, "
                    "Mouse Interaction, Crosshair Selection and Extensions."),
         placement="below"),
    Step("library", ("StudioDropZone", "StudioLibraryOpenButton"), _("Library: open your data"),
         _("Drop scans, lesion masks, scenes or a DICOM folder anywhere on the window, or press Ctrl+O. "
           "They open with the same readers as in 3D Slicer."),
         rows=("add-data", "add-dicom"), keys=((("Ctrl+O",), _("open files")),), workspace="library"),
    Step("template", "StudioLibraryStartOnTemplateButton", _("Start on the MNI template"),
         _("Opens your default MNI template in Draw, ready for a lesion: the bundled MNI152NLin2009cAsym works offline. The ▾ next to it "
           "lists your templates and adds your own MNI brain."),
         inSlicer=_("No single step: File > Add Data to load a template, then Add segment in the Segment Editor module."), rows=("own-template",),
         workspace="library"),
    Step("workspaces", "StudioWorkspaceSwitcher", _("Three workspaces"), _workspacesBody,
         rows=("modules", "favorite-modules"), keys=((("Ctrl+Alt+1", "Ctrl+Alt+2", "Ctrl+Alt+3"), _("Library, Draw, Map")),),
         placement="below", tryIt=_("Click Draw to go on."), done=lambda shell: shell.currentWorkspace() == "draw"),
    Step("draw-tools", _DRAW_TOOLS_TARGET, _("Draw is the Segment Editor"), _drawToolsBody,
         rows=("segment-editor",), keys=((("P", "D", "E"), _("Brush, Outline, Erase")), (("Esc",), _("deselect")), (("Space",), _("pause"))),
         workspace="draw", inspector=True, placement="left"),
    Step("save-lesion", "StudioSaveLesionButton", _("Save lesion"),
         _("Writes the lesion as a NIfTI mask (.nii.gz) on its own grid, which official 3D Slicer, FSL and other tools read; in a "
           "project the dialog proposes its Lesions folder. After the first save it saves again to the same file. The whole project is saved "
           "automatically; Ctrl+S saves it at once."),
         rows=("export-segmentation",), keys=((("Ctrl+Shift+S",), _("save lesion")), (("Ctrl+Enter",), _("the blue button"))),
         workspace="draw", inspector=True, placement="left"),
    Step("inspector", "StudioPanelHeader", _("The inspector"),
         _("The current workspace or module, on the right. The ? button shows help for the current tool and the ⋯ button has more, "
           "such as the Data Probe."),
         rows=("module-panel",), keys=((("Ctrl+\\", "Ctrl+5"), _("show or hide")),), workspace="draw", inspector=True, placement="left"),
    Step("layout", "StudioLayoutButton", _("Layouts"),
         _("Focus shows one large view and two small ones. Single enlarges the view you worked in (press it again or Esc to restore), "
           "Four-up, Row and Focus + 3D are one key away, the View section has the crosshair, slice intersections and linked views, "
           "and More layouts… lists every 3D Slicer layout."),
         rows=("layout", "crosshair"), keys=((("Alt+1", "Alt+4"), _("Focus, Single, Four-up, Row")), (("Alt+5",), _("Focus + 3D"))), placement="below"),
    Step("search", "StudioSearchPill", _("Search"),
         _("Find any module, Draw tool, layout, menu command, setting or sample by name, also the ones that no workspace shows. "
           "A module opened from Search has a ← back button, and Esc returns too. Ctrl+/ lists every key."),
         rows=("modules", "module-finder", "shortcuts-reference"),
         keys=((("Ctrl+K",), _("search")), (("Ctrl+F",), _("modules only")), (("Ctrl+/",), _("all keys"))), placement="below"),
    Step("app-menu", "StudioAppMenuButton", _("Everything else: the ≡ menu"),
         _("File, Edit, View and Help are all here, with Settings, the Python console and the error log, and Help > Take a tour to see this again."),
         inSlicer=_("The menu bar: File, Edit, View and Help; Edit > Application Settings ({key:Ctrl+2}); View > Python Console ({key:Ctrl+3})."),
         keys=((("F10",), _("open the menu")),), placement="below"),
    # The status appears with the first save: until then (or with saving off) the step shows the ≡ menu, where the File items are
    Step("autosave", ("StudioSaveStatus", "StudioAppMenuButton"), _("Saved automatically"),
         _("Your work is saved as you go, in the Slicer Studio folder you chose: one folder per project, under Projects, that "
           "official 3D Slicer opens too. The top bar shows \"Saved\"; Ctrl+S saves at once, Ctrl+N starts a new project, and "
           "File > Save a copy as… writes a .mrb file anywhere. Without a project (Classic, a study) Studio keeps a recovery copy instead."),
         rows=("save-data", "save-as", "where-files"),
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
         _("Library, Draw and Map cover the lesion workflow. The other modules, and those of your extensions, are one search away; "
           "Search also finds menu commands and layouts, and answers \"where is …\". Ctrl+/ lists every key."),
         rows=("modules", "module-finder", "shortcuts-reference"), keys=((("Ctrl+K",), _("search")), (("Ctrl+/",), _("all keys"))),
         placement="below"),
    Step("draw", "StudioDrawWorkspaceButton", _("Draw is the Segment Editor"),
         _("The same effects and keys, with your lesion in the inspector. Save lesion writes a NIfTI mask; Ctrl+S saves the scene as before."),
         rows=("segment-editor", "export-segmentation"), keys=((("Ctrl+Alt+2",), _("Draw")),), placement="below"),
    Step("views", "qMRMLSliceWidgetRed/SliceView", _("The views work as you know them"), _viewsBody,
         rows=("mouse-mode", "view-controllers"), workspace="draw"),
    Step("probe-toolbars", "StudioPanelMoreButton", _("Data Probe and toolbars"),
         _("The view under the mouse shows the position, the value and the lesion at its bottom right; the classic Data Probe is in ⋯. "
           "A toolbar that you turn on in ≡ > View > Toolbars stays visible in Studio."),
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
    Step("drop", ("StudioDropZone", "StudioLibraryDropHint"), _("Drop anything here"),
         _("Scans, lesion masks, scenes (.mrml, .mrb) and DICOM folders: drop them anywhere on the window. A case-report figure "
           "(PNG, JPEG, TIFF) is shown next to the template, a statistical map as an overlay. Once something is open, this "
           "zone makes room for your projects: drop more files anywhere on the window, as the line under Projects says."),
         rows=("add-data",), workspace="library"),
    Step("open", "StudioLibraryOpenButton", _("Open and Open folder"),
         _("Open… picks files; Open folder… reads a DICOM folder or a folder of NIfTI files."),
         rows=("add-data", "add-dicom"), keys=((("Ctrl+O",), _("open files")),), workspace="library"),
    Step("template", "StudioLibraryStartOnTemplateButton", _("Start on the MNI template"),
         _("Opens your default template in Draw with an empty lesion (the bundled 2 mm MNI152NLin2009cAsym works offline)."),
         inSlicer=_("No single step: File > Add Data to load a template, then Add segment in the Segment Editor module."), rows=("own-template",),
         workspace="library"),
    Step("own-template", "StudioLibraryTemplateMenuButton", _("Your own MNI templates"),
         _("The ▾ lists every template, adds your own MNI brain (FSL MNI152, ICBM 2009b, a 0.5 mm or study template, with an optional "
           "brain mask and 3D surfaces) and opens Settings › Templates, where you choose the default. A file that is not in MNI space "  # noqa: RUF001
           "is refused."),
         rows=("own-template",), workspace="library"),
    Step("current-work", "StudioCurrentWorkCard", _("Current work"),
         _("When something is open, this card says what: each scan with its lesions, their side (only in MNI space), volume and "
           "whether they are saved. Continue in Draw → goes on where you were; Close closes the scene and asks first if a lesion "
           "is not saved. The inspector lists the same under Open now."),
         rows=("data-module",), workspace="library"),
    Step("loaded-set", "StudioLoadedSetCard", _("Loaded set"),
         _("Lesion masks loaded together (up to 60) are reviewed one at a time: Review in Draw shows the first, Alt+← and Alt+→ "
           "step through them. Close set removes exactly these masks from the scene; your files are never deleted."),
         rows=("data-module",), workspace="library"),
    Step("projects", "studioProjectsSection", _("Projects"),
         _("Everything you open or draw is saved automatically as a project in the Slicer Studio folder (Projects/<name>), with "
           "Images, Segmentations, Lesions, Maps and Exports folders inside. Each card shows the lesion on its scan. Click a project to "
           "continue it; right-click to rename, duplicate, show it in your file manager or move it to the Trash. Last edited ▾ sorts them "
           "by name or creation date, and the two buttons next to it switch between cards and a list."),
         rows=("save-data", "where-files"), keys=((("Ctrl+S",), _("save now")), (("Ctrl+N",), _("new project"))), workspace="library"),
    Step("studies", "studioStudiesSection", _("Studies"),
         _("Your lesion network mapping studies, with how many subjects are traced and how many need review. Click one to open it; "
           "New study… and Open study… are below."),
         inSlicer=_("No studies: 3D Slicer opens one scene at a time; a cohort is a folder you go through file by file."), workspace="library"),
    Step("recent", "StudioRecentList", _("Opened from elsewhere"),
         _("The folders and files you opened last from outside the Slicer Studio folder, newest first, with when you opened them. "
           "A file that is already open is not opened twice. The template and the sample data that Studio keeps in its cache are not listed."),
         rows=("recent",), workspace="library"),
    Step("sample", "StudioLibrarySampleDataLink", _("Sample data"), _("The 3D Slicer sample data sets, downloaded when you choose one. A scan opens in Draw."),
         rows=("sample-data",), workspace="library"),
    Step("classic", "StudioLibraryClassicLink", _("Classic interface"),
         _("The 3D Slicer interface, exactly as you know it. Your scene stays as it is when you switch."),
         inSlicer=_("This is the 3D Slicer interface itself, unchanged."), keys=((("Ctrl+Alt+0",), _("switch and back")),), workspace="library"),
)

_DRAW_STEPS = (
    Step("lesion", "StudioLesionCard", _("Your lesion"),
         _("Its name, volume, parts and gaps, and whether it is saved; amber when something needs a look. The buttons center the views on it "
           "and let the other views follow your strokes; ⋯ has New lesion, Rename and Save lesion as…."),
         inSlicer=_("The segments table of the Segment Editor module, and Segment Statistics for the volume."),
         keys=((("C",), _("center on the lesion")), (("N", "Shift+N"), _("next / previous gap slice"))), workspace="draw", inspector=True,
         placement="left"),
    Step("tools", _DRAW_TOOLS_TARGET, _("The tools"), _drawToolsBody, rows=("segment-editor-tools", "segment-editor-masking"),
         keys=((("P", "D", "M", "E"), _("Brush, Outline, Smart fill, Erase")), (("Esc",), _("deselect")), (("Space",), _("pause"))),
         workspace="draw", inspector=True, placement="left"),
    Step("paint", "EffectsGroupBox/Paint", _("Brush and its size"),
         _("Brush is the Paint effect. It is ready when you start on the MNI template or a new lesion; on a scan you open, press P first, "
           "so that a drag adjusts the contrast until then. The size chips (1, 3, 6, 12 mm) or Shift + wheel change its size."),
         inSlicer=_("Segment Editor > Paint: Shift + mouse wheel or +/- keys adjust the brush size."), keys=((("P",), _("Brush")),),
         workspace="draw", inspector=True, placement="left"),
    Step("smart-fill", "EffectsGroupBox/Smart fill", _("Smart fill"),
         _("Click inside a lesion: Smart fill grows the region with an automatic intensity tolerance and shows a preview before you apply it."),
         inSlicer=_("No such effect; the closest are Grow from seeds and Level tracing."), keys=((("M",), _("Smart fill")),),
         workspace="draw", inspector=True, placement="left"),
    Step("display", "StudioDisplayCard", _("Display"),
         _("The Overlay slider sets the opacity of the lesion fill and the Outline switch draws its outline. ⋯ in the inspector header has "
           "Show lesion in 3D, Voxel view (crisp voxels, on by default on the MNI template) and the Window/level presets."),
         inSlicer=_("The Segmentations module (display) and the Volumes module (window/level, interpolation)."), keys=((("O",), _("fill or outline")),),
         workspace="draw", inspector=True, placement="left"),
    Step("undo", "StudioUndoButton", _("Undo and Redo"), _("Undo and redo your lesion edits, also from the keyboard."),
         rows=("segment-editor-undo",), keys=((("Ctrl+Z",), _("undo")), (("Ctrl+Y",), _("redo"))), workspace="draw", placement="below"),
    Step("save", "StudioSaveLesionButton", _("Save lesion"),
         _("Writes the lesion as a NIfTI mask (.nii.gz) on its own grid, which official 3D Slicer, FSL and other tools read, with a small .json "
           "sidecar (scan, space, notes). After the first save it saves again to the same file. Ctrl+S still saves the whole scene."),
         rows=("export-segmentation",), keys=((("Ctrl+Enter", "Ctrl+Shift+S"), _("save lesion")),), workspace="draw", inspector=True, placement="left"),
)

_MAP_STEPS = (
    Step("map", "StudioMapWorkspaceButton", _("Map"),
         _("Lesion network mapping: the brain network connected to a lesion, computed from a normative connectome, with quality checks."),
         rows=("lesion-network-mapping",), keys=((("Ctrl+Alt+3",), _("Map")),), placement="below"),
    Step("seed", "LNMSeedCard", _("Seed: the lesion"),
         _("The segmentation and the segment that hold the lesion (by default the segment named Lesion). The lesion is used on its own grid."),
         inSlicer=_("Not part of official 3D Slicer. The lesion is a segment, as in the Segment Editor module."),
         workspace="map", inspector=True, placement="left"),
    Step("seed-mode", "LNMSeedModeSegmented", _("Lesion or cohort"),
         _("Lesion maps one lesion. Cohort maps every lesion of a study that may be analysed (included, reviewed, in MNI space), "
           "in batched passes with a cache, for the Cohort card: overlap, sensitivity and the group analyses."),
         inSlicer=_("Not part of official 3D Slicer."), workspace="map", inspector=True, placement="left",
         when=lambda shell: _cohortSeedAvailable()),
    Step("connectome", "LNMConnectomeCard", _("Connectome"),
         _("A connectome is a folder on this computer (a .lnmc bundle): Get a connectome… lists the ones you can obtain from their providers "
           "(it downloads only after you read and accept the provider's terms); ⋯ next to it has Add connectome… for one you have, Use the "
           "demo (a small synthetic connectome, not real data) and Convert… for your own data. Nothing is downloaded unless you ask. "
           "Once chosen, the step folds to one line; click it to change the connectome."),
         rows=("connectome",), workspace="map", inspector=True, placement="left"),
    Step("compute", "LNMComputeButton", _("Map network"),
         _("Computes the network map of the lesion with the chosen connectome, in the background (with Seed = Cohort: Run everything). "
           "The button stays at the bottom of the inspector, above the research-use line, wherever the panel is scrolled. Specificity QA "
           "(in the Compute card) maps relocated copies of the lesion in the same pass, to compare with what any lesion of this shape gives."),
         rows=("lesion-network-mapping",), keys=((("Ctrl+Enter",), _("the blue button")),), workspace="map", inspector=True, placement="left"),
    Step("result", "LNMResultCard", _("Result: statistic, rule and threshold"),
         _("The map opens in the views. Its threshold is a rule (FWE, the |T| ≥ 7 convention, an Fz effect size, a custom T or the top x%), "
           "worked out again from the number of subjects, so a T threshold is never silently reused on another N. "
           "The statistic, the sign (±, +, −) and the opacity change only what is shown. Notes about the map are one line with Details, "  # noqa: RUF001
           "the peaks name their region from an atlas you have downloaded, and ⋯ in the inspector header has the recipes and the "
           "reporting checks. A map opened from Library is shown here too; Alt+← / Alt+→ show the maps next to it, one at a time."),
         inSlicer=_("Not part of official 3D Slicer. The map is an ordinary volume: the Volumes and Data modules work on it as usual."),
         workspace="map", inspector=True, placement="left"),
    Step("disclaimer", "LNMDisclaimer", _("Research use only"),
         _("Network maps are for research, not for clinical decisions, and the specificity of lesion network mapping is debated. "
           "This line is always shown while Map is open, and the exported files carry it too."),
         inSlicer=_("Not part of official 3D Slicer."), workspace="map", inspector=True, placement="left"),
)

TOURS = (
    Tour("studio", _("Slicer Studio in 2 minutes"), _("The top bar, the three workspaces, Search, layouts and the Classic interface."), _STUDIO_STEPS),
    Tour("from-slicer", _("Coming from 3D Slicer"), _("What stays the same, what moved, and where to find it now."), _FROM_SLICER_STEPS,
         finishText=_("Open the map")),
    Tour("library", _("Library tour"), _("Opening data, the MNI template, Recent and sample data."), _LIBRARY_STEPS, workspace="library"),
    Tour("draw", _("Draw tour"), _("Your lesion, the tools, the brush, Display, Undo and Save lesion."), _DRAW_STEPS, workspace="draw"),
    Tour("map", _("Map tour"), _("Lesion network mapping: the lesion, the connectome, Map network, the result and its threshold."), _MAP_STEPS,
         workspace="map", available=_mapAvailable),
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

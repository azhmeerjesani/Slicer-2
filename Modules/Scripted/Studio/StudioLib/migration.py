r"""'Where did it go?': the map from official 3D Slicer to Slicer Studio (T-01).

One table, ``ROWS``, is the single source of the comparisons: the "In 3D Slicer" row of the guided tours
(``StudioLib.tours``), the 'Where did it go?' panel (``StudioLib.tour``), the "Coming from 3D Slicer" page of the
user guide (``Docs/user_guide/studio_coming_from_slicer.md``, its table is ``markdownTable()``) and, later, the
answers of Search to "where is X" (``lookup()``).

Each row names a place of official 3D Slicer 5.13 exactly as its menus, toolbars and modules spell it (checked
against the official binary), where the same thing is in Studio, the keys of both, and the Studio control that
"Show me" points at (``target``: an objectName, or a path of objectNames "Parent/Child", shown in ``workspace``).
A row whose control is not built yet names the work package that adds it (``planned``); a row whose feature is
only in the Classic interface has ``classic`` set (the panel offers "Open Classic").

Keys are written portably ("Ctrl+O") and shown as the platform displays them (``keyText()``: "⌘O" on macOS).
In texts, ``{key:Ctrl+O}`` is a key. This module only reads the application; it never changes it.
"""

import re

import qt
import slicer
from slicer.i18n import tr as _

#: A key inside a text: "{key:Ctrl+O}"
_KEY_PATTERN = re.compile(r"\{key:([^}]+)\}")
#: Words of a search query
_WORD_PATTERN = re.compile(r"[\w+/#.-]+", re.UNICODE)


class Row:
    """One place of official 3D Slicer and where it is in Slicer Studio.

    :param rowId: stable identifier (the tours refer to rows by id).
    :param official: the place in official 3D Slicer, spelled as there ("File > Add Data", "Segment Editor module").
    :param studio: where the same thing is in Studio, in plain words (may contain ``{key:...}``).
    :param officialKey: the key in official 3D Slicer, or None.
    :param key: the key in Studio, or None.
    :param target: the Studio control that "Show me" points at: an objectName or "Parent/Child", a tuple of them (the
        first one shown), or None.
    :param workspace: the workspace that shows ``target`` ("library", "draw", "map"), None for the top bar.
    :param note: one more sentence, or None.
    :param same: True if nothing changed (the place is the same in both).
    :param planned: the work package that adds ``target`` when it is not built yet, else None.
    :param classic: True if Studio deliberately hides it: "Open Classic" shows it.
    :param keywords: more words that find the row.
    :param category: "menu", "module", "toolbar", "panel" or "editor".
    """

    def __init__(self, rowId, official, studio, officialKey=None, key=None, target=None, workspace=None, note=None,
                 same=False, planned=None, classic=False, keywords="", category="menu"):
        self.id = rowId
        self.official = official
        self.studio = studio
        self.officialKey = officialKey
        self.key = key
        self.target = target
        self.workspace = workspace
        self.note = note
        self.same = same
        self.planned = planned
        self.classic = classic
        self.keywords = keywords
        self.category = category

    def officialText(self, native=True):
        """The official place with its key: "File > Add Data (Ctrl+O)"."""
        text = self.official
        if self.officialKey:
            text += f" ({keyText(self.officialKey) if native else self.officialKey})"
        return text

    def studioText(self, native=True):
        """Where it is in Studio, with the keys as displayed on this platform (``native``) or portable."""
        return formatKeys(self.studio, native=native)

    def asDict(self, native=True):
        """The row as a dictionary (for the command palette and the tests)."""
        return {
            "id": self.id, "official": self.official, "officialKey": self.officialKey, "officialText": self.officialText(native),
            "studio": self.studioText(native), "key": self.key, "target": self.target, "workspace": self.workspace,
            "note": formatKeys(self.note, native) if self.note else None, "same": self.same, "planned": self.planned,
            "classic": self.classic, "category": self.category,
        }

    def searchText(self):
        return " ".join(str(part) for part in (self.official, self.officialKey or "", formatKeys(self.studio, native=False), self.key or "",
                                               self.note or "", self.keywords) if part).lower()

    def __repr__(self):
        return f"Row({self.id!r})"


def keyText(key, native=True):
    """``key`` ("Ctrl+O") as this platform displays it ("⌘O" on macOS), or portable with ``native=False``.

    Several keys may be given with " / " between them.
    """
    if not native or not key:
        return key or ""
    parts = []
    for part in str(key).split(" / "):
        sequence = qt.QKeySequence(part)
        text = sequence.toString(qt.QKeySequence.NativeText) if sequence.count() == 1 else ""
        parts.append(text or part)
    return " / ".join(parts)


def formatKeys(text, native=True):
    """Replace the ``{key:...}`` markers of ``text`` by the keys as displayed (``native``) or portable."""
    if not text:
        return text or ""
    return _KEY_PATTERN.sub(lambda match: keyText(match.group(1), native), text)


def keysIn(text):
    """The keys marked in ``text`` (portable)."""
    return _KEY_PATTERN.findall(text or "")


#: The map. Official names are those of 3D Slicer 5.13 (menus File, Edit, View, Help; toolbars Load/Save, Module
#: Selection, Favorite Modules, Layout Selection, Mouse Interaction, Crosshair Selection, Extensions, Markups).
ROWS = (
    # Menus and toolbars
    Row("modules", _("Modules drop-down (Module Selection toolbar)"),
        _("The Library, Draw and Map tabs for the lesion workflow; every other module is one search away: Search ({key:Ctrl+K}), "
          "which also finds Draw tools, layouts, menu commands, settings pages and sample data by name"),
        key="Ctrl+K", target="StudioSearchPill", keywords="module selector modules list all tools find module palette command palette",
        note=_("A module opened from Search shows a ← back button that returns to your workspace (or press Esc)."), category="toolbar"),
    Row("module-finder", _("View > Module Finder"), _("Search in the top bar ({key:Ctrl+K}); {key:Ctrl+F} opens Search with the modules only"),
        officialKey="Ctrl+F", key="Ctrl+K", target="StudioSearchPill", keywords="find module search palette command"),
    Row("favorite-modules", _("Favorite Modules toolbar"), _("The workspace tabs Library, Draw and Map ({key:Ctrl+Alt+1}, {key:Ctrl+Alt+2}, {key:Ctrl+Alt+3})"),
        key="Ctrl+Alt+1", target="StudioWorkspaceSwitcher", keywords="favorites toolbar data volumes models transforms",
        note=_("Turn the toolbar on in ≡ > View > Toolbars > Favorite Modules if you miss it."), category="toolbar"),
    Row("home", _("View > Home"), _("The Library tab ({key:Ctrl+Alt+1}; {key:Ctrl+H} on Windows and Linux)"), officialKey="Ctrl+H", key="Ctrl+Alt+1",
        target="StudioLibraryWorkspaceButton", keywords="welcome home module start"),
    Row("add-data", _("File > Add Data"), _("Library > Open… ({key:Ctrl+O}), or drop files anywhere on the window"), officialKey="Ctrl+O",
        key="Ctrl+O", target="StudioLibraryOpenButton", workspace="library", keywords="load open import nifti nrrd file volume scan",
        note=_("The same readers as 3D Slicer: NIfTI, NRRD, .seg.nrrd, .mrml and .mrb open the same way in both.")),
    Row("add-dicom", _("File > Add DICOM Data"), _("Library > Open folder… (a DICOM folder); the DICOM module is one search away"),
        target="StudioLibraryOpenFolderButton", workspace="library", keywords="dicom import series database folder load dicom"),
    Row("sample-data", _("File > Download Sample Data"), _("Library > Load sample data, or Search ({key:Ctrl+K}) for a sample by name"),
        target="StudioLibrarySampleDataLink", workspace="library",
        keywords="sample data download example mrhead"),
    Row("own-template", _("Where do I add my own MNI template?"), _("Library > Start on template ▾ > Add your own template…, or Search ({key:Ctrl+K}) "
                                                                     "for Add MNI template; Settings › Templates lists them and sets the default"),  # noqa: RUF001
        target="StudioLibraryTemplateMenuButton", workspace="library",
        keywords="template templates own mni brain model add template icbm fsl mni152 2009b colin custom atlas space default template",
        note=_("Official 3D Slicer has no template list: you load a template with File > Add Data. In Studio a template you add is "
               "copied into the Slicer Studio folder (Templates) and used by Start on template, Map and Normalize to MNI.")),
    Row("recent", _("File > Recent"), _("Library > Recent"), target="StudioRecentList", workspace="library", keywords="recent files history reopen"),
    Row("save-data", _("File > Save Data"), _("Saved automatically to Projects/<name> in the Slicer Studio folder ({key:Ctrl+S} saves now)"),
        officialKey="Ctrl+S", key="Ctrl+S", target=("StudioSaveStatus", "StudioAppMenuButton"), keywords="save scene mrb mrml save data write save project autosave",
        note=_("Each project is a 3D Slicer scene (.mrml) with its data in folders. To save only the lesion as a NIfTI mask, use Save "
               "lesion in Draw ({key:Ctrl+Shift+S}); without a project (a study, Classic), {key:Ctrl+S} opens the Save dialog of 3D Slicer.")),
    Row("save-as", _("File > Save Data (to another folder or a .mrb)"), _("≡ > File > Save a copy as…: the whole scene as one .mrb file, anywhere"),
        target="StudioAppMenuButton", keywords="save as save copy export mrb bundle share send",
        note=_("The project itself stays in the Slicer Studio folder and keeps saving there.")),
    Row("where-files", _("Where are my files?"), _("Show project folder (the save status menu in the top bar, ≡ > File, or Search): Finder, "
                                                  "Explorer or Files opens on Projects/<name>"),
        target=("StudioSaveStatus", "StudioAppMenuButton"), keywords="where are my files folder finder explorer location documents slicer studio folder reveal",
        note=_("Settings › Saving shows and changes the Slicer Studio folder.")),  # noqa: RUF001
    Row("close-scene", _("File > Close Scene"), _("≡ > File > Close Scene ({key:Ctrl+W})"), officialKey="Ctrl+W", key="Ctrl+W",
        target="StudioAppMenuButton", same=True, keywords="close scene clear new"),
    Row("settings", _("Edit > Application Settings"), _("≡ > Settings… ({key:Ctrl+2})"), officialKey="Ctrl+2", key="Ctrl+2",
        target="StudioAppMenuButton", keywords="settings preferences options application settings",
        note=_("Settings are shared with your other 3D Slicer installations; Studio keeps its own choices under Studio/ and Appearance/.")),
    Row("layout", _("View > Layout"), _("The layout button in the top bar: Focus, Single (enlarges one view; Esc restores), Four-up, Row ({key:Alt+1} to "
                                         "{key:Alt+4}) and Focus + 3D ({key:Alt+5}); More layouts… lists every 3D Slicer layout, and Search finds any layout by name"),
        key="Alt+1", target="StudioLayoutButton", keywords="layout conventional four-up four up one up red slice only views arrangement",
        note=_("The Layout Selection toolbar button of 3D Slicer offers the same layouts.")),
    Row("python-console", _("View > Python Console"), _("≡ > Python console ({key:Ctrl+3})"), officialKey="Ctrl+3", key="Ctrl+3",
        target="StudioAppMenuButton", keywords="python console shell script slicer.util interactor"),
    Row("extensions", _("View > Extensions Manager"), _("≡ > View > Extensions Manager ({key:Ctrl+4})"), officialKey="Ctrl+4", key="Ctrl+4",
        target="StudioAppMenuButton", same=True, keywords="extensions manager install extension catalog"),
    Row("error-log", _("View > Error Log"), _("≡ > Error log ({key:Ctrl+0}); a red badge appears in the top bar when errors are logged"),
        officialKey="Ctrl+0", key="Ctrl+0", target="StudioAppMenuButton", keywords="error log warnings messages problems"),
    Row("module-panel", _("View > Appearance > Show Module Panel"), _("The inspector on the right: {key:Ctrl+\\} or {key:Ctrl+5} shows or hides it, the \u2039 handle brings it back"),
        officialKey="Ctrl+5", key="Ctrl+\\", target="StudioPanelHeader", workspace="draw", keywords="module panel inspector side panel dock hide show",
        category="panel"),
    Row("status-bar", _("View > Appearance > Show Status Bar"), _("Hidden in Studio; messages show briefly at the bottom of the views and ≡ > View > Recent messages… lists them"),
        target="StudioAppMenuButton", classic=True, keywords="status bar messages"),
    Row("toolbars", _("View > Toolbars"), _("≡ > View > Toolbars: a toolbar you turn on stays visible in Studio"), target="StudioAppMenuButton",
        keywords="toolbars toolbar show hide", category="toolbar"),
    Row("shortcuts-reference", _("Help > Keyboard Shortcuts Reference"),
        _("≡ > Keyboard shortcuts ({key:Ctrl+/}): the keys of the workspace, the view gestures and the Esc steps, searchable; "
          "the 3D Slicer reference stays in ≡ > Help"),
        key="Ctrl+/", target="StudioAppMenuButton", keywords="keyboard shortcuts keys hotkeys cheat sheet gestures mouse"),
    Row("about", _("Help > About 3D Slicer"), _("≡ > Help > About 3D Slicer"), target="StudioAppMenuButton", same=True,
        keywords="about version license"),
    Row("documentation", _("Help > Documentation"), _("≡ > Help > Documentation ({key:Ctrl+1}); the ? button of the inspector shows help for the current tool"),
        officialKey="Ctrl+1", key="Ctrl+1", target="StudioAppMenuButton", same=True, keywords="help documentation manual"),
    Row("classic", _("The 3D Slicer interface itself"), _("≡ > Classic interface ({key:Ctrl+Alt+0}) switches to it and back; your scene stays as it is"),
        key="Ctrl+Alt+0", target="StudioAppMenuButton", keywords="classic interface old interface original menus menubar switch back"),
    # Modules
    Row("data-module", _("Data module"), _("Search > Data ({key:Ctrl+K}) opens it in the inspector; Library shows what you open"),
        key="Ctrl+K", target="StudioSearchPill", keywords="data module subject hierarchy nodes tree rename delete", category="module"),
    Row("volumes-module", _("Volumes module"), _("Search > Volumes ({key:Ctrl+K}); window/level: drag with the left mouse button in a view when no tool is active"),
        key="Ctrl+K", target="StudioSearchPill", keywords="volumes window level contrast brightness display lookup table preset presets ct brain ct-brain ct-bone ct-lung",
        category="module"),
    Row("segment-editor", _("Segment Editor module"), _("Draw ({key:Ctrl+Alt+2}): the Segment Editor in the inspector, with the same effects"),
        key="Ctrl+Alt+2", target="StudioDrawWorkspaceButton", keywords="segment editor paint draw brush erase threshold lesion segmentation edit",
        category="module"),
    Row("segment-editor-tools", _("Segment Editor effects (Paint, Draw, Erase, Threshold...)"),
        _("Draw: the same effects as labelled tools (Paint is Brush, Draw is Outline, Fill between slices is Fill gaps; More tools shows the "
          "others); {key:P}, {key:D}, {key:M}, {key:E}, {key:L} or {key:1} to {key:0} pick one, Esc deselects it and Space pauses it"),
        target=("EffectsGroupBox", "StudioDrawEmptyState"), workspace="draw",
        keywords="effects tools paint brush draw outline erase threshold islands smoothing scissors keys more tools",
        category="editor"),
    Row("segment-editor-masking", _("Segment Editor > Masking (Editable area, Editable intensity range, Modify other segments)"),
        _("Draw > More tools ▾: the overwrite options appear under the tool options; they also stay shown while they limit where you paint"),
        target=("MaskingGroupBox", "SegmentEditorMoreToolsButton", "EffectsGroupBox", "StudioDrawEmptyState"), workspace="draw",
        keywords="masking mask editable area intensity range overwrite modify other segments paint everywhere overlap",
        category="editor"),
    Row("segment-editor-display", _("Segmentations > Display (opacity, outline, Show 3D) and Volumes > window/level"),
        _("Draw > Overlay: the slider sets the fill opacity and the Outline switch the outline; ⋯ in the inspector header > Show "
          "lesion in 3D, Voxel view and Window/level presets"),
        key="O", target=("StudioDisplayCard", "StudioPanelMoreButton"), workspace="draw",
        keywords="display opacity fill outline show 3d surface voxel view interpolation window level preset brain narrow auto",
        category="editor"),
    Row("segment-editor-controls", _("Segment Editor: Segmentation and Source volume selectors, segments table, Add / Remove"),
        _("Draw: the Lesion card names the lesion (⋯ > New lesion), the source chip ('on T1 ▾') picks the source volume; "
          "⋯ of the panel > Show all controls shows the classic controls"),
        target=("StudioLesionCard", "StudioDrawEmptyState"), workspace="draw",
        keywords="segmentation selector source volume master volume segments table add segment remove segment show all controls",
        category="editor"),
    Row("export-segmentation", _("Segment Editor > Segmentations > Export to files..."),
        _("Draw > Save lesion ({key:Ctrl+Shift+S}): a NIfTI mask (.nii.gz) that official 3D Slicer, FSL and other tools read"),
        key="Ctrl+Shift+S", target="StudioSaveLesionButton", workspace="draw",
        keywords="export segmentation labelmap nifti mask save lesion binary labelmap export to file export as nifti nii nii.gz fsl spm "
                 "afni nibabel export scan export volume export map one mask per segment label map dicom to nifti convert dicom",
        note=_("Export as NIfTI… (the ⋯ of a row under Open now in Library, the project menu, Draw's lesion ⋯ or Search) writes scans, label "
               "maps, maps and segmentations (one label map, or one mask per segment) as NIfTI with sidecars; Search > Convert DICOM to "
               "NIfTI converts the loaded DICOM series. In 3D Slicer: File > Save Data with the .nii.gz format, or right-click the "
               "segmentation in the Data module > Export visible segments to binary labelmap."),
        category="editor"),
    Row("segment-editor-undo", _("Segment Editor Undo / Redo"), _("Undo and Redo in the top bar while you draw ({key:Ctrl+Z}, {key:Ctrl+Y})"),
        officialKey="Ctrl+Z / Ctrl+Shift+Z", key="Ctrl+Z", target="StudioUndoButton", workspace="draw", keywords="undo redo history", category="editor",
        note=_("Ctrl+Shift+Z, and Z and Y without Ctrl, still work as in 3D Slicer.")),
    Row("segmentations-module", _("Segmentations module"),
        _("Search > Segmentations ({key:Ctrl+K}); in Draw, ⋯ of the panel > Show all controls also shows its button"),
        key="Ctrl+K", target="StudioSearchPill", keywords="segmentations module representations closed surface import export",
        category="module"),
    Row("markups", _("Markups toolbar / Place mode"),
        _("Search > Place point puts a point in a view; ≡ > View > Toolbars > Markups (it stays visible in Studio), or Search > Markups; "
          "while you place, a pill at the top of the views says so, and its ✕ or Esc leaves Place mode"),
        target="StudioAppMenuButton", keywords="markups fiducial point landmark ruler line place mode pill placing", category="toolbar"),
    Row("mouse-mode", _("Mouse Interaction toolbar (Window/level)"),
        _("Drag with the left mouse button in a view to adjust window/level when no tool is active, or Search > Window/level: Auto; "
          "the toolbar is in ≡ > View > Toolbars > Mouse Interaction. Adjust window/level from a view's menu shows a pill at the top of the views, "
          "whose ✕ or Esc leaves it"),
        target="qMRMLSliceWidgetRed/SliceView", workspace="draw", keywords="mouse mode window level adjust contrast interaction toolbar pill",
        category="toolbar"),
    Row("crosshair", _("Crosshair Selection toolbar"),
        _("Shift + move the mouse in a view jumps the crosshair, as in 3D Slicer; the layout button's View section shows or hides it (also "
          "slice intersections and linked views), and the toolbar is in ≡ > View > Toolbars"),
        target="StudioLayoutButton", workspace="draw", keywords="crosshair cursor jump slices intersections link views reset", category="toolbar"),
    Row("view-controllers", _("Slice view controller bars"),
        _("Above each view. In the flat bars the sliders button opens the layers, opacity and interpolation on click, the orientation "
          "button (Axial ▾) has Reformat… and Reset, and the position shows the slice number; the classic bars keep the pin"),
        target="qMRMLSliceWidgetRed/SliceController", workspace="draw",
        note=_("The color swatch names the view (Red, Yellow, Green): it is not a direction. Right-click the link button for hot linking."),
        keywords="slice controller bar pin link offset slider foreground background opacity view controllers options orientation "
                 "reformat readout slice number index swatch flat maximize fit",
        category="panel"),
    Row("data-probe", _("Data Probe (bottom of the module panel)"),
        _("The probe at the bottom right of the view under the mouse: position (MNI or scanner), value and lesion; "
          "⋯ in the inspector header > Show Data Probe shows the classic panel"),
        target="StudioPanelMoreButton", workspace="draw", keywords="data probe pixel value coordinates ras intensity mni probe", category="panel"),
    # Not in official 3D Slicer
    Row("lesion-network-mapping", _("Lesion network mapping (not part of official 3D Slicer)"),
        _("Map ({key:Ctrl+Alt+3}): choose the lesion and a connectome, then Map network ({key:Ctrl+Enter}); the map opens in the views with "
          "its threshold rule, quality checks and export"),
        key="Ctrl+Alt+3", target="StudioMapWorkspaceButton", keywords="lnm lesion network mapping map network connectivity seed specificity",
        note=_("Official 3D Slicer has no lesion network mapping; the maps that Map writes are standard NIfTI files that official 3D Slicer opens."),
        category="module"),
    Row("connectome", _("Connectome (not part of official 3D Slicer)"),
        _("Map > Connectome: Get a connectome… downloads one from its provider after you accept its terms, Add connectome… registers a "
          "folder on this computer (a .lnmc bundle), or try the synthetic demo; nothing is downloaded unless you ask"),
        target="LNMConnectomeCard", workspace="map", keywords="connectome gsp1000 normative functional connectivity bundle lnmc demo terms get download catalog",
        note=_("⋯ next to Get a connectome… has Add connectome…, Use the demo and Convert…."), category="panel"),
    Row("lnm-recipe", _("Map recipes and reporting checks (not part of official 3D Slicer)"),
        _("Map: ⋯ in the inspector header > Recipe applies a saved set of settings, and Reporting checks… lists what a paper about the "
          "map should report; in a study a compact row under the result shows both"),
        target="StudioPanelMoreButton", workspace="map", keywords="recipe preset settings reporting checklist battery dots report checks lnm",
        category="panel"),
)


def rows():
    """All rows of the map, in display order."""
    return list(ROWS)


def row(rowId):
    """The row ``rowId``, or None."""
    for candidate in ROWS:
        if candidate.id == rowId:
            return candidate
    return None


def lookup(text, limit=None):
    """Rows matching ``text`` ("add data", "python", "window level"), best first. An empty text returns every row.

    Every word must be found (as a word start) in the row; a row whose official place contains the whole text
    comes first, then rows whose official place starts with a word of the query, in table order.
    """
    query = (text or "").strip().lower()
    if not query:
        result = list(ROWS)
        return result[:limit] if limit else result
    words = _WORD_PATTERN.findall(query)
    scored = []
    for index, candidate in enumerate(ROWS):
        haystack = candidate.searchText()
        haystackWords = _WORD_PATTERN.findall(haystack)
        if not all(any(word.startswith(queryWord) for word in haystackWords) or queryWord in haystack for queryWord in words):
            continue
        official = candidate.official.lower()
        score = 0
        if query in official:
            score += 100
        elif query in haystack:
            score += 50
        officialWords = _WORD_PATTERN.findall(official)
        score += sum(10 for queryWord in words if any(word.startswith(queryWord) for word in officialWords))
        scored.append((-score, index, candidate))
    scored.sort(key=lambda item: (item[0], item[1]))
    result = [candidate for score, index, candidate in scored]
    return result[:limit] if limit else result


def markdownTable():
    """The map as a Markdown table, with portable keys (the docs page includes it verbatim)."""
    lines = ["| In 3D Slicer | In Slicer Studio |", "| --- | --- |"]
    for candidate in ROWS:
        studio = candidate.studioText(native=False)
        if candidate.same:
            studio += " " + _("(same as 3D Slicer)")
        lines.append(f"| {_escapeCell(candidate.officialText(native=False))} | {_escapeCell(studio)} |")
    return "\n".join(lines) + "\n"


def _escapeCell(text):
    return text.replace("\\", "\\\\").replace("|", "\\|")


def resolveTarget(path, root=None):
    """The visible widget named by ``path`` (an objectName, or "Parent/Child" objectNames) under ``root``, or None.

    ``root`` defaults to the main window. Hidden widgets with the same name are skipped, and a widget that is on screen
    is preferred to one scrolled out of view or covered (the Map inspector line and the Report card both have an
    ``LNMDisclaimer``).
    """
    if not path:
        return None
    root = root if root is not None else slicer.util.mainWindow()
    if root is None:
        return None
    candidates = [root]
    for name in path.split("/"):
        found = []
        for parent in candidates:
            for widget in parent.findChildren("QWidget", name):
                if widget.isVisible() and widget not in found:
                    found.append(widget)
        if not found:
            return None
        candidates = found
    unobscured = [widget for widget in candidates if not widget.visibleRegion().isEmpty()]
    return (unobscured or candidates)[0]

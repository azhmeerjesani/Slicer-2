# Slicer Studio

Slicer Studio is an edition of [3D Slicer](https://www.slicer.org) for one kind of work: drawing brain
lesions slice by slice and computing lesion network maps (LNM). It opens on a simple Library page, puts
the Segment Editor next to a large axial view in a Draw workspace, and computes a lesion's network from a
normative connectome in a Map workspace (you provide the connectome; a small synthetic demo connectome
is there to try it). Your work is saved for you as you go, as projects in one Slicer Studio folder that you choose once (see
[Your work is saved in a Slicer Studio folder](#your-work-is-saved-in-a-slicer-studio-folder)), and a private
recovery copy covers scenes that are not projects. A study (a folder of subjects) lets you trace lesion after lesion with *Done → next*, shows the
subjects as a list or as a grid of lesion thumbnails, and a lesion traced on a patient's own scan can be
normalized to MNI space in one click. *Run everything* runs the whole lesion network analysis of a study, from
the lesion masks to a report page, in the application or from the command line. Research tools around it (validation, robustness and null checks,
analysis recipes, a review workflow with blind second tracing, literature sources, a share-safe export and a
command line twin) are described under [Research tools of wave 5](#research-tools-of-wave-5). The layout borrows from photo
editors such as Lightroom: a library to choose what to work on, a focused editing space and a quiet dark
interface. Underneath it is 3D Slicer 5.13, so its files open in official 3D Slicer and the other way round.
Its packaging scripts build a macOS disk image, a Linux package and a version that runs in a web browser on
your own computer; no package has been published yet, so the in-app update check (see
[In-app updates](#in-app-updates)) has nothing to find.

> [!NOTE]
> **Early development, no release yet.** Waves 1 to 5 and the packaging work are implemented: the
> Studio interface (Library, Draw and Map), a command palette and a shortcut list, guided tours, the
> streamlined Draw panel with Smart fill (assisted tracing), autosave and crash recovery, flat view bars, thin
> sliders and faster slice navigation, usability work (an `Esc` that backs out one state at a time, visible mouse
> modes, a Library that shows the current work, safer file import, accessibility and a first-run flow), the Map
> workspace, the lesion network mapping library with its command lines, file compatibility checks and the
> packaging scripts. Wave 3 added the study loop (a subject list in the Library and *Done → next*), one-click
> normalization of native-space scans to MNI space, the cohort statistics screens of Map and a connectome
> manager. Wave 4 added the *Run everything* cohort pipeline with an HTML report ([Automate a cohort
> analysis](#automate-a-cohort-analysis)), a Library grid with lesion thumbnails, faster segment editing
> (cheaper undo, faster Draw and Scissors), quality checks while you trace (lesion checks, tracing from a
> published figure), Library cards for columns and for cohort diagnostics, Map cards for QA, preflight, clusters
> and networks, connectome tools (a catalog, a streamed archive converter, Lead-DBS import and verification),
> research numerics with a command line (figures, nulls, validation, robustness, a fast parcel mode), in-app
> updates, web-build support for lesion network mapping and the user guide pages for Studio and lesion network
> mapping ([Studio](Docs/user_guide/modules/studio.md),
> [Lesion Network Mapping](Docs/user_guide/modules/lesionnetworkmapping.md)). Wave 5 added the Slicer Studio
> folder (projects that save as you go), your own MNI templates, a redesign of Library, Draw, Map and the slice
> views, and the research tools of lesion network mapping ([Research tools of wave 5](#research-tools-of-wave-5)):
> *Get a connectome…*, the Validate, Robust, Nulls, Circuits, Compare, Calibrate and Report cards, analysis recipes
> with a plan lock, coordinate, atrophy and stimulation seeds, voxelwise lesion-symptom mapping, a review workflow
> with blind second tracing, a case strip and trace queues, literature sources with PRISMA counts, control-lesion
> sets, a share-safe export, tutorial cohorts and the `slicer-lnm` command. Everything was written and checked on
> Linux with Qt 5.15; nothing of waves 2 to 5 has been run on macOS (Qt 6) or Windows, and no real connectome has
> been used. Packages are built on your own computer (see [Install](#install)); no disk image, Linux package or
> web image has been published yet.

> [!WARNING]
> **Research use only.** Slicer Studio, like 3D Slicer, is not a medical device. Lesion network maps must
> not be used for clinical decisions; the specificity of the method is debated (van den Heuvel et al.,
> 2026; Siddiqi et al., 2026; Zalesky and Cash, 2026; see [References](#references)).

Contents: [A fork of 3D Slicer](#a-fork-of-3d-slicer) ·
[Highlights](#highlights) · [Install](#install) · [Quick start](#quick-start) ·
[Lesion network mapping](#lesion-network-mapping) · [Status and roadmap](#status-and-roadmap) ·
[Development](#development) · [License and third-party notices](#license-and-third-party-notices) ·
[Based on 3D Slicer](#based-on-3d-slicer)

## A fork of 3D Slicer

This repository is a fork of [3D Slicer](https://www.slicer.org)
([Slicer/Slicer on GitHub](https://github.com/Slicer/Slicer)). It branched from upstream `main` at
commit `bccfb40` (2026-10-16, version 5.13). Slicer Studio is not an official 3D Slicer release, is not
on download.slicer.org and is not supported by the 3D Slicer developers: please report problems with
Slicer Studio to the maintainer of this fork (GitHub Issues are not enabled yet), not on the Slicer forum.

What stays the same:

- **All of 3D Slicer.** Every module, the Python console and API, and extensions. Nothing is removed or
  renamed; modules that the Studio interface does not show are one search away (`Ctrl+K`).
- **The Classic interface.** `≡ > Classic interface` or `Ctrl+Alt+0` restores the standard Slicer window
  exactly: menu bar, toolbars, module panel and status bar. `View > Studio interface` or `Ctrl+Alt+0` switches
  back. The choice is remembered. The appearance settings (theme, font, flat view bars) belong to the
  application, so they apply to both interfaces.
- **Files.** Scenes and data are written in the same formats as official Slicer. Slicer Studio adds no
  node types and no file formats (see [File compatibility](#file-compatibility-with-official-3d-slicer)).
- **Testing.** With `--testing` or `--no-main-window`, the application starts in the Classic interface
  with the light palette and Slicer's compact metrics, so tests and scripts see the familiar application.
- **License.** The [3D Slicer license](License.txt).

## Highlights

### Studio interface: Library, Draw and Map

On a normal launch Slicer Studio shows its own interface, Studio, instead of the classic window:

- **One top bar** replaces the menu bar, the toolbars and the status bar. It holds the `≡` menu
  (workspaces, the File, Edit, View and Help menus, Appearance, Settings, Python console, Error log and
  Classic interface), a breadcrumb that names the current work (the scan and the lesion, or the loaded
  set), the **Library | Draw | Map** switcher in the centre, a status chip, Undo and Redo (in Draw), a
  layout picker, **Search**, the save status of the project ("Saved", "Saving…", "Not saved · Retry", see
  [Your work is saved in a Slicer Studio folder](#your-work-is-saved-in-a-slicer-studio-folder)) or, for a scene that is
  not a project, the autosave status ("Autosaved · 2 min ago", see
  [Autosave and crash recovery](#autosave-and-crash-recovery)), and an error badge. Every control of the bar
  takes the keyboard focus and has a tooltip of the form "Name · key · description". In a window narrower
  than 1100 px, Search and the switcher shrink to icons.
- **One inspector.** The module panel is a panel on the right, 340 px wide by default (300–480 px). A
  workspace is a module shown in it. The workspaces share one width: if a tool of the Segment Editor
  needs more than that (up to 600 px), the inspector takes that width once, remembers it, and keeps it
  when you choose another tool, so the views do not move under the cursor. Wider contents scroll
  sideways. Any other module, opened from Search or by a script, appears in the inspector too, with a
  labelled back button (*← Draw*) that returns to the workspace.
- **Search is a command palette.** `Ctrl+K` (or the search box) finds the workspaces, every module, the
  Draw tools, actions (open, save, close the scene, place a point, settings pages, autosave), layouts,
  view commands, recent files, sample data, the guided tours and the answers of *Where did it go?*. A
  query ranks an exact title first, then synonyms, prefixes and word starts; the entries you ran last
  come first (`Studio/PaletteMRU`). `Ctrl+F` searches the modules only. `Ctrl+/` shows a searchable list
  of the keys of the workspace, the view gestures and the `Esc` steps.
- **Dark by default.** A graphite dark theme, a light theme and System, an accent color and a
  Comfortable or Compact density, in *Application Settings > Appearance* (theme and density also in
  `≡ > Appearance`; a density change takes effect after a restart). Text uses the
  [Inter](https://rsms.me/inter/) font that comes with Slicer Studio unless you chose another font
  there. Check boxes and radio buttons are redrawn for contrast in the dark theme, a tool or mouse mode
  that is on looks on, and collapsible sections are flat. The default 3D view background follows the
  theme. Icons are [Lucide](https://lucide.dev) SVGs.
- **Modes are visible.** While a mouse mode is active (placing points, adjusting window/level), a pill at
  the top of the views says so and has an exit; `Esc` also leaves it.
- **Export as NIfTI.** Any scan, label map, segmentation (one label map, or one mask per segment), map (with
  a mask of the voxels that pass its threshold), every loaded DICOM series or the whole project is written as
  NIfTI (`.nii.gz` or `.nii`) with a `.json` sidecar: *Export as NIfTI…* in the `⋯` of a Library row (several
  at once), Draw's lesion `⋯`, Map's `⋯`, the project menu and Search. The affine is 3D Slicer's IJK-to-RAS
  matrix, so left stays left in FSL, SPM, AFNI and nibabel; sform/qform codes say MNI (4), aligned (2) or
  scanner (1). See [Export as NIfTI](Docs/user_guide/studio_nifti_export.md).
- **Guided tours.** `≡ > Help > Take a tour…` is a two-minute tour that points at the controls on screen
  and says how the same thing is done in 3D Slicer; *Where did it go?* is a searchable list for people who
  come from 3D Slicer (see [Coming from 3D Slicer](Docs/user_guide/studio_coming_from_slicer.md)). The
  tour is offered once, in a small card that never blocks anything.
- **Keyboard and low-vision access.** A slice view that gets the keyboard focus with `Tab`, `Shift+Tab` or
  `F6` shows a 2 px accent frame. Studio's labels and buttons follow a change of the application font size
  at once and return to their sizes when it is restored. Below 1100 px the top bar is compact and the
  inspector may be as narrow as 300 px; Draw fits a 1080p screen at a scale factor of 2
  (`QT_SCALE_FACTOR=2`), which a script checks (nothing is cut off at the right, the inspector's controls
  fit). On Windows, where AltGr arrives as `Ctrl+Alt`, a text field keeps the characters that AltGr types
  instead of running a workspace key (written for Windows, not tried there).
- **First run and closing.** The research-use notice is a line at the bottom of the Library ("Research use
  only, not for clinical use · Don't show again") instead of a dialog; *Don't show again* writes the official
  key. Classic keeps the official dialog. Closing the application after you saved the lesion asks one
  question (*Quit*, *Save session…*, *Cancel*); closing with unsaved edits of the lesion offers *Save
  lesion…* first, and the autosave line is added when autosave runs.

| Workspace | What you do there | State |
|---|---|---|
| **Library** | Your projects as cards with thumbnails (the lesion outlined), a *Continue* card for the open work, and the files you opened from elsewhere. A drop zone, *Open…*, *Open folder…* and *Start on template ▾* (the built-in MNI brain or one you added) open scans, lesion masks, scenes, figures or DICOM folders. Once data is loaded, a *Current work* card lists the scans and lesions that are open, with *Continue in Draw →*. *New study…* and *Open study…* make or open a study: a subject list with QC, filters and batch actions (see [Studies and the study loop](#studies-and-the-study-loop)), shown as a list or as a grid of lesion thumbnails. Cards of the inspector give the columns of the table, diagnostics of the lesion set, the state of the last analysis, review, second tracing, control sets, literature sources and the share export. After a crash, a card offers to open the project again. | Available (W1-08, U-02, W3-07, A-01b, W4-02, L-31, A-04c, A-09, A-10, V-02, L-42, L-43, L-44, L-46, L-51) |
| **Draw** | Trace the lesion. The views use the *Studio Focus* layout: a large axial view, with the coronal and sagittal views stacked on its right. The inspector shows a lesion card, labelled tool tiles (Brush, Outline, Smart fill, Erase, Fill gaps), an *Overlay* slider with an *Outline* switch and a *Save lesion* button at the bottom (*Done → next* in a study). A figure row and, on a native-space scan, a *Normalize to MNI* row open their cards. In a study a case strip shows the queue, the case and its paper, and in review mode the footer has *Approve*, *Request changes* and *Reject*. Chips on the lesion card check the tracing. See [Drawing](#drawing). | Available (W2-05, A-02, A-03b, L-28, L-29, L-43, L-45, V-03) |
| **Map** | Compute and read the lesion's network map from a connectome that you provide (or the synthetic demo), or get one with *Get a connectome…*. A row of seed modes (Lesion, Cohort, Coords, Stim) sits above numbered steps that collapse to one line when they are done. A cohort has overlap, contrast, symptom and VLSM tabs; a connectome manager adds, converts and removes connectomes. A *Run everything* card runs a whole cohort analysis, and cards for preflight checks, QA, validation, robustness, nulls, clusters, networks, circuits, comparison, calibration and reporting read the result. A legend and layer chips follow the Red view while Map is shown. See [Lesion network mapping library](#lesion-network-mapping-library) and [Research tools of wave 5](#research-tools-of-wave-5). | Available (W2-06, W3-08, L-20, A-03b, A-04c, L-30, L-32, L-36, L-49, L-50, V-03) |

Keyboard (on macOS, `Ctrl` is `Cmd` and `Alt` is `Option`; the full list is in `Ctrl+/`):

| Keys | Action |
|---|---|
| `Ctrl+Alt+1` / `2` / `3` | Library / Draw / Map |
| `Ctrl+Alt+0` | Switch between Studio and Classic |
| `Ctrl+O` | Open files, loaded directly (Classic keeps *Add Data*) |
| `Ctrl+K` / `Ctrl+F` | Search (everything) / Search (modules only) |
| `Ctrl+/` | Keyboard shortcuts |
| `Alt+1` / `2` / `3` / `4` / `5` | Focus / Single / Four-up / Row / Focus + 3D layout |
| `Ctrl+\` or `Ctrl+5` | Show or hide the inspector (`Tab` keeps its normal job of moving the keyboard focus) |
| `F11` or `Ctrl+6` | Hide panels (focus mode): hide the inspector and the top bar. In Classic, *View > Maximize view* does the same for the module panel, the toolbars and the dock widgets, and showing the module panel leaves it |
| `F6` / `Shift+F6` | Move the keyboard focus between the top bar, the inspector and the views |
| `F10` | Open the `≡` menu (on Windows and Linux also `Alt+F`, `Alt+E` and `Alt+H` for its File, Edit and Help menus) |
| `Esc` | Backs out of one state per press: closes a popover, Search or the shortcut list; leaves Place or window/level mode; restores a maximized or single view; shows hidden panels; deselects the Draw tool; returns from a module opened by Search to the workspace |
| `Ctrl+Z` / `Ctrl+Y` | Undo / redo the lesion (in Draw; `Ctrl+Shift+Z` also redoes) |
| `Ctrl+Enter` | The primary button of the inspector: *Save lesion* in Draw (*Done → next* in a study), *Map network* in Map (*Run everything* when the seed is a cohort) |
| `Ctrl+E` | Map: export |
| `Ctrl+Shift+S` | Save lesion… (in Draw) |
| `Ctrl+V` | Draw, on the template: paste a figure from the clipboard to trace from it (only while the clipboard holds an image; not in Classic) |
| `P` / `D` / `M` / `E` / `L` / `Shift+F` / `O` | Draw: Brush / Outline / Smart fill / Erase / Auto outline / Fill gaps / fill or outline only. `1` to `0` pick the tools in order. |
| `N` / `Shift+N` / `C` | Draw: next / previous slice with a gap in the lesion / center the views on the lesion |
| `Alt+Left` / `Alt+Right` | Draw: previous / next lesion of a loaded set, or subject of a study (written like *Done → next*, without marking the subject traced). Map: previous / next map of a browsed set |
| `[` / `]` | Draw, Brush: smaller / larger brush in Studio's Draw panel. In Classic they keep the official meaning, as they do under `--testing` (setting `Studio/BracketKeysResizeBrush`, see [Settings added by Slicer Studio](#settings-added-by-slicer-studio)) |
| `P` / `X` / `U` | Library subject list: QC pass / fail / not reviewed; `Delete` removes the subjects from the study (the files are kept) and `/` searches the list |
| `PageUp` / `PageDown` / `Home` / `End` | In a slice view: several slices at once / first and last slice (see [Slicing and navigation](#slicing-and-navigation)) |

Slicer's own shortcuts, such as `Ctrl+S` (save), `Ctrl+2` (settings) or `Ctrl+3` (Python console), keep
working with the menu bar hidden.

Navigation details:

- **Hidden panels** show a translucent *Show panels* pill at the top right of the window and a toast.
  `Esc` or the pill shows them again.
- **Clicking the current tab** (or pressing its `Ctrl+Alt+1` / `2` / `3` again) shows an inspector you
  hid and the hidden panels. A hidden inspector leaves a small handle at the right edge.
- **Draw remembers its tool.** Going from Draw to the Library or to another module and back keeps the
  selected segment and the armed tool. Pressing `Esc` to deselect the tool is respected. Place and
  window/level modes put the tool aside and bring it back when they end.
- **Files that are already open** are not loaded a second time from *Recent*, *Open…* or a drop in
  Studio: the existing scan or lesion becomes current and a message says so. Classic still loads the
  file again, as official Slicer does.
- **Back to Studio from Classic** returns to the workspace of the module you were in (else the last
  workspace, else the Library).
- **View > Toolbars** is honoured: a toolbar you switch on stays on and is remembered under
  `Studio/AllowedToolbars`.
- **First start** with no stored window geometry opens a window of 90% of the screen, centered.

The Classic interface is also used, whatever the last choice, with `--testing`, with
`--no-main-window`, when the environment variable `SLICER_CLASSIC_UI=1` is set, or when the setting
`Studio/Enabled` is `false`.

### Your work is saved in a Slicer Studio folder

Studio saves your work as you go; there is no Save button to remember. The first start asks one question,
*Where should Slicer Studio keep your work?*, and proposes a folder called *Slicer Studio* in your Documents
folder: *Continue* accepts it, *Choose folder…* picks another one, and closing the window accepts the proposal.
*Settings > Saving* changes the folder later (and offers to move your projects). A folder that a cloud service
syncs is flagged, because scans can contain patient data. Everything is ordinary files, sorted so that a person
who never used 3D Slicer can find them in Finder, Explorer or a file manager:

```text
Slicer Studio/
├── Projects/<name>/   one folder per project: <name>.mrml (the scene), Images/, Segmentations/, Lesions/, Maps/,
│                      Markups/, Models/, Tables/, Transforms/, Other/, Exports/ and a hidden .studio/ folder
├── Studies/           lesion network mapping studies
├── Templates/         your own MNI templates
└── LNM/               normalization outputs, control sets and recipes
```

- **Projects.** A project is created when you open a scan, a DICOM series or a figure, or start on the template. It is
  named after the scan ("Untitled project" otherwise), and a message says "Saving to Projects/<name>" with a *Rename*
  button. The files you open are copied into the project and your originals are never changed. A Slicer scene opened
  from somewhere else becomes a new project that starts as a copy.
- **When it is saved.** After a pause (10 seconds by default) and at most every few minutes while you keep working,
  with only the changed data written again; at once on `Ctrl+S`, when you go to the Library, switch projects, start a
  new project, close the scene or quit. Quitting asks nothing when everything is saved. A file that another program
  changed while the project is open is never overwritten: Slicer Studio writes its own version beside it and says so.
  After a crash the next start offers *Open project*.
- **Library.** The Library lists the projects as cards (or as a list) with thumbnails of an axial, a coronal and a
  sagittal slice with the lesion outlined, rendered in the background, sorted by *Last edited*, *Name* or *Created*
  (`Studio/Library/ProjectSort`, `Studio/Library/ProjectView`). A right-click offers *Rename*, *Duplicate*, *Show in
  Finder/Files* and *Move to Trash*; the project name in the top bar has a menu with the same kind of actions.
- **Official 3D Slicer.** A project is a standard scene with paths relative to its folder: choose *File > Add Data* in
  3D Slicer 5.12 or later and pick the `.mrml` file. Changes saved there are kept when Slicer Studio opens the project
  again, and *File > Save a copy as…* writes a single `.mrb`. The compatibility check tests this with `--projects`.
- **Where it does not apply.** Classic behaves like official 3D Slicer (no project; *File > Save Data*), a study saves
  its own files in its folder, and scenes that are not projects go to the private recovery folder described under
  [Autosave and crash recovery](#autosave-and-crash-recovery). The web version saves in `Slicer Studio` of the data
  folder it was given, without asking.
- **Settings.** `Studio/Projects/Root` holds the folder; the environment variable `SLICER_STUDIO_PROJECTS_ROOT` overrides it
  and skips the question, and `SLICER_STUDIO_PROJECTS=0` turns projects off. Scans and lesion masks can hold patient data:
  keep the folder private and share projects only as your data protection rules allow.

The guide is [Where Slicer Studio saves your work](Docs/user_guide/studio_projects.md). This was written and checked on
Linux with Qt 5.15; a cloud-synced folder, macOS, Windows and the web version in Docker have not been tried.

### Your own MNI templates

Studio comes with one MNI brain, the 2 mm MNI152NLin2009cAsym template, which works offline. You can add another one
once (FSL's MNI152 at 1 mm, an ICBM 2009a or 2009b template, a 0.5 mm or 7T template, a head with the skull on, or an
average of your own study in MNI space) and use it like the built-in brain: start drawing on it, read sides and
coordinates, map networks from lesions drawn on it, normalize scans to it and share studies that use it.

- **Add.** *Library > Start on template ▾ > Add your own template…*, *Settings > Templates > + Add your own…* or Search
  (*Add MNI template*). The check reads the file header only: a known template grid is accepted, otherwise the field of
  view must look like MNI space, and anything else is refused ("This image is not in MNI space. Open it as a scan
  instead, or normalize it to MNI first."). You can add a brain mask (needed to normalize scans to it), a whole-head
  image and 3D brain surfaces.
- **Use.** The files are copied into the Templates folder of the Slicer Studio folder and never changed. A template can
  be made the default (`Studio/Templates/Default`) and can start a lesion study. Draw, Map, Normalize and studies use
  the chosen template and record it in their sidecars; a study asks for its template when it is missing, and a batch
  normalization uses the study's own template.
- **Never downloaded or bundled.** You bring the files, and the licence of a template you add is yours to check,
  for example before you share a study that includes it.

The guide is [Your own MNI templates](Docs/user_guide/studio_templates.md). Files written with a user template still
open in official 3D Slicer (the compatibility check of 2026-10-25 passed for a project that holds one); the tests use
small synthetic template files, and no real 0.5 mm or 7T template was tried.

### The look of Studio

Wave 5 redesigned the Studio screens in the dark and the light theme (the Classic interface keeps its look). The screens
were reviewed against the design mockups by eye (the mockups are not in the repository) and captured with
[`StudioScreens.py`](Utilities/Scripts/StudioScreens.py) at 1920x1080 and 1366x768.

![The Library with project cards, the Continue card and the project inspector](Docs/images/studio/library-projects.png)

- **Tokens and controls.** One set of colours, radii and type sizes is shared by the C++ style and the Studio code, with
  sentence-case headers and a switch control. The Library is a full-width page; the top bar shows the project name,
  the save status and a Search field; Search groups its results under headers; dialogs and the shortcut list share one
  card look.
- **Draw** is compact: a figure row and a normalize row that become cards only while they have something to say, a
  notes row, an *Overlay* slider with an *Outline* switch (*Show lesion in 3D*, the voxel view and the window/level
  presets are in the panel's `⋯` menu), the masking options under *More tools ▾* (shown by themselves when masking limits editing), and one
  elided hint line.

![Draw with the lesion card, tool tiles and the orientation cue](Docs/images/studio/draw.png)

- **Map** shows its steps as numbered cards that collapse to a 44 px row when they are done, a four-item row of seed
  modes, a one-line connectome control ("Get a connectome…" with a `⋯` menu), one warning line with *Details* instead
  of paragraphs, and a peaks table with the columns *Peak region*, *x, y, z* and the statistic. The region names come
  from atlases that are already cached (nothing is downloaded; "Outside atlas" when no atlas labels the point, "—"
  when no atlas is cached) and are looked up in a background thread. A legend card and layer chips follow the Red
  view while Map is shown and are removed when you leave it. A map opened from a file shows Compute as "Opened from a
  file", not as done.

![Map with the seed modes, the step cards and the connectome row](Docs/images/studio/map.png)

- **Slice views.** The bars are slimmer and the corner text is shorter: the name of the volume without prefixes or
  underscores at the lower left ("MNI152NLin2009cAsym · 2 mm") and, at the lower right, the orientation cue
  "Radiological · L on right" or "Neurological · L on left" (a short "L on right" on a narrow view). Orientation
  letters are 11 px bold.

`StudioBench.py` found no metric more than 15% above its value before the redesign in the work package's runs (the
median `segment_editor_first_open_ms` was 299.5 ms against 351.9 ms; five runs each). It also showed that the cost of
a mouse hover over a slice view depends on the painting state of Slicer core and is as high with the whole Studio
interface switched off (see [Known limitations](#status-and-roadmap)).

### Drawing

**The Draw panel** (Studio interface). The Segment Editor is shown as a lesion card, labelled tool tiles
and the options of the active tool:

- The **lesion card** names the lesion (a menu switches between lesions or makes a new one), has an eye,
  a *center on lesion* button and *follow strokes* (the other views move to each stroke:
  `Studio/FollowStrokes`), and reads out the lesion as you draw, for example "Left · 2.27 mL · 1 part ·
  0 gaps". When there are several parts, gap slices or a part outside the brain, a link acts on them
  (*Keep largest*, *Fill*, *Clip*). It also shows whether the lesion is saved, and the source volume you
  are tracing on ("on T1 ▾"). *Fill gaps and specks* closes gaps up to 2 mm and removes islands under
  10 mm³.
- **Brush size** chips (1, 3, 6 and 12 mm) set the brush; the diameter is remembered
  (`Studio/BrushDiameterMm`). A small label next to the brush ("Ø 3.0 mm") shows the diameter for about a
  second after the size changes and while `Shift` is held; `[` and `]` make the brush smaller and larger.
- **Tool tiles:** Brush, Outline, Smart fill, Erase and Fill gaps, with *More tools ▾* for the others (the
  Segment Editor effects keep their names in the Segment Editor itself). Tools have letter keys (see the
  keyboard table above); `Esc` deselects the tool and `Space` pauses it, so that a left-drag adjusts
  window/level.
- An **Overlay** slider (with its percentage) and an **Outline** switch replace the old Display section; *Show lesion
  in 3D*, the voxel view and the window/level presets (Auto, Brain, Narrow) are in the panel's `⋯` menu. The masking
  options (editable area, overwrite mode, intensity range) are under *More tools ▾* and show by themselves when masking
  limits editing. At the bottom is the primary button: *Save lesion* (`Ctrl+Enter`), or *Done → next* when a study is
  open (see [Studies and the study loop](#studies-and-the-study-loop)), with *Map this lesion's network →* under it.
  *⋯ > Show all controls* of the panel brings back the classic Segment Editor controls.
- A **Normalize to MNI** row, shown when the source scan is in native space (it becomes a card while it works or has a
  result): one click registers the scan
  to the MNI template with the lesion masked out, in the background, and makes the lesion in MNI space the
  seed of Map (*Use for Map →*). *Normalize scan only* is for tracing directly on the warped scan, and
  *Update MNI lesion* re-warps a lesion that you edited afterwards, in a few seconds, without registering
  again. See [Normalize native-space lesions](#normalize-native-space-lesions).
- **Lesion checks** are chips on the lesion card: the traced side against the side a report names (only in
  MNI space; in scan space they say the side is unknown), where the lesion lies against an atlas, the share
  of cerebrospinal fluid, the cleanliness of the mask, a possible duplicate of another lesion of the study, and
  whether the tracing is 2D or 3D. A chip is a prompt to look, not a verdict. One-click fixes (mirror left to
  right, binarize, clip to the brain, mark as duplicate) keep the original file, can be undone and are recorded
  in the study's audit file. A tracing on a single slice asks, before *Done → next*, how to turn it into a
  volume (keep it single-slice, extrude it to a thickness with fractional edges, or extrude it symmetrically as a
  binary mask); the choice is stored in the study. The Library has a *Flags* column and the filters *2D* and
  *Has flags*.
- **Case strip and trace queues.** While a study subject is open, a strip at the top of the inspector shows the trace
  queue ("Untraced · 3/12"), the case id and the case's paper (DOI, figure, modality, side, symptom and the reviewer's
  comment). A click edits a field in place; the value is checked against the study's columns and saved with the
  study. *Done → next* and `Alt+Left` / `Alt+Right` follow the chosen queue (untraced, needs review, changes requested,
  flagged, one paper, second tracing), which is restored when you reopen the study. In a literature study the lesion
  card's `⋯` menu has *New case from this paper* and *New case from clipboard figure*, the study card shows the
  throughput and the time left, and the next cases are read in the background so that they open faster.
- **Review mode.** A reviewer opens the lesions that wait for a decision one by one in Draw (the mask as an outline on
  the scan or figure it was traced on, with no network map and, if *Blind tracing* is on, no group or score). The footer
  reads *Approve → next*, *Request changes…* or *Reject…*; every decision is one line of the study's audit file. See
  [Research tools of wave 5](#research-tools-of-wave-5).
- **Trace from a figure** (the *Figure* row and card, on the MNI template): drop a published figure, paste it with
  `Ctrl+V`, or use *Open figure…*, *Trace from figure…* in the `⋯` menu of the lesion card, or Search. Crop the
  figure, choose its plane, say which side of the figure is the patient's right, place pairs of landmarks and fit the
  figure to the template (a similarity fit; an affine fit from four pairs). The views then show the figure over
  the cut plane of the template, and you paint with Brush, Outline or Smart fill. The fit's residual is shown as
  landmark consistency, not as the accuracy of the lesion. In a study the crop and its fit are saved under
  `<study>/sources/` and the subject's row records the figure, the panel, the convention and the fit error;
  *Done → next* waits until the left-right convention of the figure is chosen. On a scan in scan space a figure
  stays a reference beside the template.

**Smart fill** (assisted tracing, tool tile *Smart fill*, key `M`). Click inside a lesion and the lesion
is grown from that point; the intensity tolerance is chosen from the contrast at its boundary. The result
is a preview until you apply it with `Enter`, as one undo step; you review it, it is not approved for you.

- Click: new preview. `Shift`+click adds a region; `Ctrl`+click (`Cmd` on macOS) removes one. Dragging
  left or right, `Shift`+wheel, or `+` and `-` change the tolerance of the last region. `Enter` applies,
  `Backspace` discards.
- *This slice* fills in the clicked slice only; *3D* fills the lesion in 3D. A leak guard cuts leaks
  through bridges thinner than about twice its size (1.5 mm by default, or off), a maximum radius limits
  the region, and
  on a segmentation that uses an MNI152 template grid the region stays inside the template's brain mask
  (an option). *Apply on click* applies each click at once.
- It computes in small steps on the main thread, so the application stays responsive. It also works in the
  Classic Segment Editor, where it is the last effect, after *Mask volume*.
- **Limits.** It needs visible contrast between the lesion and its surroundings. On the bare MNI template
  there is nothing to find: use the patient's scan (native, or registered to the template) as the source
  volume. Lesions on T1 images with grey-matter-like intensity are ambiguous: use *This slice*, a lower
  tolerance or `Ctrl`+click, and click well inside the lesion.

Other changes are in the Segment Editor itself, so they also apply in the Classic interface:

- **Draw effect (outline).** New *Add* / *Subtract* option, and *Apply on release (lasso)*: drag around
  the lesion and release to fill it. The outline is drawn in the segment's color. An outline started on
  the slice at offset 0 (for example the MNI z = 0 plane) is now tied to its slice like any other.
- **Level tracing.** New *Add* / *Subtract* option.
- **Islands.** *Keep largest island* keeps the largest island whatever its size; before, a lesion smaller
  than the minimum size (1,000 voxels by default) was deleted. When *Remove small islands* empties a
  segment, a message says so (Undo restores it). The minimum size is also shown in mm³.
- **Margin.** Shrinking an empty segment no longer fills the image and erases the other segments.
- **Margin, Hollow, Islands and whole-segment Smoothing** process only the region around the segment
  instead of the whole image: about 3 to 4 times faster on a small lesion, with voxel-identical results.
  Their option panels no longer compute the segment's voxels just to read the voxel size.
- **Undo and Draw (wave 4).** Undo snapshots copy only the labelmap and share the closed surfaces, undo and redo skip
  segments that did not change, and the Draw (outline) and Scissors effects combine their shape into the segment only
  inside the region they touch. The voxels are the same as before (a check against the earlier code) and the
  Studio undo depth is 30 steps (20 in Classic). Measured numbers are in [Performance](#performance).
- **Painting with masking** (editable area or intensity range) processes only the region of the stroke.
  After a stroke, only segments whose voxels changed get their 3D surface rebuilt. This also fixes 3D
  surfaces that official Slicer 5.12 and 5.13 leave out of date after erasing in segments that share a
  layer.
- **Paint effect: hover and strokes.** Moving the brush over a view renders only the view under the mouse (the
  other slice views and the 3D view are rendered only when the brush outline reaches them), and the
  brush-size label and the `[` `]` keys described above are part of the effect. The five test strokes of the
  work package's check paint the same voxels as the official 5.13 nightly (0 voxels differ). Measured numbers
  are in [Performance](#performance).
- **Threshold and Fill between slices.** Threshold no longer costs anything until it is used, and no
  longer recomputes a histogram on every mouse move; a click no longer resets the range silently, and the
  option *Paint within this range* is explained. *Fill between slices* can act on the selected segment
  only and fills with one click.
- **Segment Editor widget.** Tooltips name the keys, the masking section shows a one-line summary, the
  intensity range starts from the window/level, an empty segment table says what to do, and the brush
  size limits follow the volume's geometry.
- In the compatibility test, the segmentation files (`.seg.nrrd`) that Slicer Studio saves are
  byte-identical to those of official 3D Slicer 5.12.4 and 5.13.

### Studies and the study loop

A **study** is a plain folder that other tools can read: `study.json`, a BIDS-style `participants.tsv`, a
`lesions` folder, a `derivatives` folder and a hidden `.studio` folder (the journal of the lesion being
traced, an inbox for background jobs, a heartbeat while the study is open, caches). In the Library, *New
study…* makes one from a folder, and *Create study* does the same for a folder or a set of lesion files that
you opened; *Open study…* and the recent studies reopen one. Creating a study reads file headers only: no
image is loaded. A folder that already has a `participants.tsv` is adopted in place: its columns and their
order are kept, a backup is written once to `.studio/participants.orig.tsv`, and the values of a group column
("patient", "HC", "1") are proposed as case, control or exclude.

- **Subject list.** Columns ID, Group, Lesion mL, Space, Status, QC and Norm (the normalization state, see
  [Normalize native-space lesions](#normalize-native-space-lesions)); sortable; filter chips (All, Cases,
  Controls, Untraced, QC fail, and *Needs normalization* and *Normalization to review*). `Enter` opens the
  selected subject in Draw (else the next untraced one); `P`, `X` and `U` set QC; *New case on template* adds a
  case without a scan, to draw on the study template. The inspector has the *Study*, *Subject* and *Batch* cards
  (group, scores and other columns; *Remove from study* never deletes files).
- **List or grid.** *List* and *Grid* buttons above the subjects switch the view (`Studio/Library/StudyView`,
  the list is the default). The grid shows one card per subject: an axial thumbnail through the lesion over the
  template or the scan, with R and L at its edges (the patient's right is on the left of the card, as in the
  views), the group, a QC mark and a status dot; a slider sets the size of the cards (`Studio/Library/GridThumbSize`).
  Thumbnails are computed by a background thread, never on the main thread, and cached in the study's `.studio` folder,
  so reopening a study draws them at once; a changed lesion or scan is redrawn. The list and the grid share the
  selection and the keys (`P`, `X`, `U`, `Enter`), keep their scroll position when the table refreshes, and the
  filter chips wrap on a narrow window so no button is hidden. See [Performance](#performance) for measured numbers.
- **Columns.** The *Columns* card of the study inspector shows how many columns have no confirmed role and which
  numeric columns are only offered as unconfirmed scores. *Columns…* opens a table of every column with its type,
  role (score, covariate, group and others), units, levels and description; *Confirm* writes the roles to
  `participants.json` and saves them with the study. A column that you have not confirmed as a score is not used as
  one by the Symptom tab or by *Run everything* (the run says so). Moving a bookkeeping or metadata column to *score*
  asks first, with a sentence on why such a score would mislead. The *Case* section of the Subject card edits the
  fields of one subject in place (source, imaging, patient and lesion, workflow), checks each value against the
  column's type, keeps a value it cannot read with a warning, and writes only through the study session. A DOI is
  stored without its prefix and *Open* shows it in your browser. Notes and a source reference that were typed in
  Draw (before the columns existed) are offered for moving into the `notes`, `doi`, `figure` and `citation` columns.
- **Cohort diagnostics.** The *Cohort diagnostics* card, *Check the lesion set*, runs in a background thread (a study
  of more than 500 lesions runs the `lesionset` command line as a job) and reports the lesions per group, which
  lesions could not be used and why, the volume (median, IQR, skew, with a suggestion to use log volume), a histogram
  of the pairwise Dice (amber from 0.6, the level at which two lesions may be the same one), the maximum overlap, the
  hemisphere split, the 2D/3D tally, the source types, tissue percentages, the standardized mean difference of
  each column between the groups (amber above 0.25), possible duplicates, and flags (more than 50% white matter, less
  than one grey-matter voxel). The tissue priors are three TemplateFlow 2 mm files, downloaded only when you click
  *Get tissue priors…*. *Create volume-matched control subset* stores a named subset in `study.json` and never edits
  the include flags. The result is saved in `lesionset.json` in `derivatives/lnm` and shown again when the study is
  reopened. *Lesion overview* writes the exact overlap count and percent maps and loads them as volumes with discrete
  colour tables (the count map becomes the foreground), and exports a numbered 300 dpi gallery, the overlap figure
  and `supplementary_lesions.tsv` under `derivatives/lesions`, with the same numbering in all three. These are
  descriptive checks of the lesion set, not tests.
- **Analysis.** A small *Analysis* card says in one line where the cohort analysis of the study stands ("Never run",
  "Running · Network maps 34%", "up to date · finished 2 h ago", "3 new or changed lesions", "Stopped" or "Failed")
  and has *Open in Map* and *Open report*. When every included subject is traced, a toast points to it once per
  session. It never starts a run by itself (see [Automate a cohort analysis](#automate-a-cohort-analysis)).
- **One subject at a time** is in the scene. Opening a subject replaces the previous subject's scan and lesion
  (the shared study template stays) and puts the lesion in Draw; the undo history goes with the removed lesion.
- **Done → next** (`Ctrl+Enter`) writes the lesion as a NIfTI mask on the grid it was traced on, to
  `lesions/<sid>_space-<space>_desc-lesion_mask.nii.gz` with its sidecar, updates `participants.tsv` (status,
  volume, parts, gaps, centroid, tracing time) and opens the next untraced subject. A pending Smart fill preview
  is applied first. `Alt+Left` and `Alt+Right` move to the previous and next subject and write the lesion in
  the same way, without marking the subject traced. A lesion file that is not in the `lesions` folder (in
  `derivatives`, or in the files of an adopted folder) is never overwritten: the row is repointed to the new
  file. An erased lesion writes nothing (its file is kept), and edits that cancel out leave the file untouched.
- **Journal.** Two seconds after the last change of the lesion, its voxels are written to
  `<study>/.studio/journal/<sid>.nii.gz` by a background thread. If Slicer stopped before the lesion was
  written, opening the subject offers "Recover unsaved tracing of sub-012 (2 min ago)" (*Recover* is one undo
  step; *Discard* removes the journal). Writing the lesion removes the journal. The journal covers the lesion
  inside the study; the scene autosave (see [Autosave and crash recovery](#autosave-and-crash-recovery)) covers
  the scene.
- **One writer.** `participants.tsv` is written only by the Studio session, on the main thread and under a
  lock. Background jobs (normalization, maps) post their values to `.studio/inbox`, which the session merges
  when a study is opened, when the Library is shown and on request. Nothing is written outside the study folder.
- **Classic.** The study session also works in the Classic interface: the Studio module panel lists the
  subjects there.

### Slicing and navigation

- **Studio layouts.** *Studio Focus* (large axial view, coronal over sagittal on the right), *Studio
  Focus + 3D* and *Studio Row* (three equal views), and *Studio Compare* (the template and a case-report
  figure side by side, used when you open a figure). They are registered in every mode, so a scene saved
  with one of them reopens with it in Studio and Classic, and official Slicer can open it. The layout
  picker (`Alt+1` to `Alt+5`) offers Focus, a single view,
  Four-up, Row and Focus + 3D; *More layouts…* opens Slicer's layout menu, and Search finds every layout
  by name. Each workspace remembers its layout, and switching workspaces changes the layout only when it
  differs.
- **Flat view bars.** Above each slice view, a flat bar replaces the colored gradient bar of 3D Slicer:
  a stripe and a swatch in the view's color, the orientation button (*Axial ▾*, with *Reformat…* and
  *Reset*), the slice position (for example `97/193`), the slice slider, the link button, a button that
  opens the layers, opacity and interpolation, and the maximize button. The bars follow the theme and use
  crisp icons. *Application Settings > Views > Flat view bars* (`Appearance/FlatViewBars`, restart
  needed) turns them on or off; they are on except under `--testing` and `--no-main-window`, and they
  apply in Classic too.
- **Slice navigation in Studio.** `PageUp` and `PageDown` move 10 slices at once (`Studio/PageStepSlices`),
  `Home` and `End` go to the first and last slice, stepping stops at the first and last slice, `Ctrl`+wheel
  zoom in and zoom out cancel exactly, the keys of a view work on the view under the mouse without a
  click (`Studio/FocusFollowsMouse`), and a left-drag adjusts window/level when no tool or mode is active
  (`Studio/LeftDragWindowLevel`). Markups and Segment Editor tools still win over the left-drag. All of
  it is off in Classic and with `--testing`, which keep Slicer's behavior. *Application Settings > Views >
  Navigation in Studio* has a switch or a value for each of the three keys, and the 3D views follow the mouse
  too (a 3D view takes the keyboard focus when the mouse enters it; text fields, menus and dialogs keep the
  focus). The user guide lists them in
  [Navigation in Studio](Docs/user_guide/user_interface.md#navigation-in-studio).
- **In-view probe.** The slice view under the mouse shows one line at its bottom-right corner: the
  position, the value and the lesion, for example "Left 11.2 · P 17.8 · S 9.5 (MNI) · 412 · Lesion". MNI
  or scanner space is stated; hemisphere words are shown only where they are true (MNI), and a figure shows
  pixel coordinates, not patient directions. It updates at most 30 times per second and not while a mouse
  button is held. `⋯` in the inspector header, *Show Data Probe*, brings back the classic Data Probe panel.
- **Fewer updates per slice step.** A scroll step causes one slice-logic update instead of three. Other
  views are re-rendered only when they show something that moved (slice intersections, slabs, handles).
  A slice node change that changes nothing no longer reslices the volume. The slice controller bar
  updates only what changed. The rendered views are pixel-identical to before (416 captures compared).
- **Data Probe and slice annotation updates** are coalesced and skipped while hidden;
  `slicer.i18n.tr()` caches the source file of its caller.
- **Orientation letters.** Every slice view shows a letter at each edge (for example R and L, A and P),
  computed from the view's own orientation, so they stay right when you rotate or flip a view. Within
  25 degrees of an anatomical axis one letter is shown; otherwise the letters of the axes involved are
  combined (for example `RA`). The default display is radiological. `Studio/OrientationLetters`
  (default true) turns them off. A figure is labelled as a figure and gets no patient letters.
- **Thin sliders and scroll bars.** Sliders have a 4 px groove with an accent-colored fill and a round
  handle (also the range sliders and the slice offset slider), scroll bars are thin, combo and spin boxes are
  flat with working arrows, and progress bars (a 4 px bar with the text beside it), tabs, splitters and header
  sections follow the theme. The thin scroll bars (no step buttons) and the 14 px slider handles are those of
  the Comfortable density; the Compact density and `--testing` keep the historical metrics. This is in the
  application style, so it applies in Classic too.
- **Focus mode and shortcuts without a menu bar.** Menu-only shortcuts keep working with the menu bar hidden.
  *View > Maximize view* (`Ctrl+6` or `F11`) is a focus mode of the main window that hides the module panel,
  the toolbars and the dock widgets and shows exactly what it hid when you leave it. The module panel keeps
  its width when you select another module (`Studio/ClassicPanelWidth`, Classic), and the main window state is
  restored once, when the window is first shown.
- **Loading lesion masks.** A 3D NIfTI file is never grouped with its numbered neighbors into an image
  series, so a folder of numbered lesion masks (`sub-001_lesion.nii.gz` … `sub-100_lesion.nii.gz`) gives one
  row per file in *Add Data* instead of one volume. A file whose name has *lesion*, *mask* or *roi* as a whole
  word (any case) is offered as a labelmap, except probability maps (`lesion_prob`). The *Add Data* dialog
  builds the options of each file only when *Show Options* is on, so large lists open faster (see
  [Performance](#performance)). These are fixes of the data loading, so they apply in Classic too.

### Lesion network mapping library

- `LesionNetworkMappingLib`: a numpy/scipy library and command lines that map one or many lesions
  against a normative functional connectome in one streaming pass, with N-aware threshold rules,
  specificity QA (connectome degree similarity, relocated-lesion null, similarity to the connectome's
  reference maps), a plain-file study model, a lesion-aware normalization of native scans to MNI space, a cohort
  pipeline with an HTML report and an export package with provenance. Its unit tests use synthetic data and need no
  network access (see [Tests](#tests); the normalization tests run BRAINSFit on a synthetic subject made from the
  bundled template).
- **Map workspace** (the Slicer module around the library; `Ctrl+Alt+3`). A column of cards in the
  inspector:
  - *Seed*: *Lesion* (a segment to map, with the grid it is resampled onto) or *Cohort* (the subjects of a
    study, see *Cohort* below).
  - *Normalize to MNI*, right under Seed when the seed lesion is in native space: see
    [Normalize native-space lesions](#normalize-native-space-lesions).
  - *Connectome*: a connectome bundle (`.lnmc` folder, see below) that you add from this computer with
    *Add connectome…*, or *Use the demo*, a small synthetic connectome that is labelled "SYNTHETIC — not
    real data" everywhere and exists to try the workspace. The connectome's data use terms are shown and
    you accept them before a map is computed.
  - *Compute*: *Map network* (`Ctrl+Enter`) for all subjects, or a *Preview* with N random subjects. The
    computation runs in a separate process with progress and a time estimate; the map appears with the
    first partial result, and a run can be stopped (a stopped run is a partial run and is exported as
    one). Optionally, 50 relocated lesions are mapped in the same pass for the specificity check. Before
    it starts, the cards check what they can (for example that the lesion is in the connectome's space) and
    show a message with a fix button when something is wrong.
  - *Preflight*: before a run, what will be analysed: what blocks it, what only warns and what is for
    information (missing, empty or non-3D lesions, mixed template families, lesions that are not reviewed or not
    normalized, duplicates, too few subjects per group, disk space), each with a fix where there is one.
  - *Run everything* (Seed = *Cohort*): the whole cohort analysis as one job, see
    [Automate a cohort analysis](#automate-a-cohort-analysis).
  - *Result*: the maps (mean r, mean Fisher z and T) with a diverging color table, and the threshold rule
    with its resolved value. Thresholds are rules, re-resolved for the run's N and number of voxels.
  - *QA*: the similarity of the map with the connectome's degree map, the relocated-lesion null (always
    stated, also when it was not run), the similarity with the reference maps, and the coverage of the
    lesion on the connectome. Each is a chip calibrated against the relocated-lesion null (below its 5th percentile,
    between the 5th and 95th percentile in amber, above the 95th in red, with the resolution of the null stated), and
    a *debiased* view shows the map with the mean connectivity profile and the first principal component regressed
    out (descriptive, not an inferential correction). The card opens itself when a check is amber or red.
  - *Export*: the maps, one JSON sidecar per lesion, `provenance.json`, `methods.md` and `reproduce.sh`
    (`Ctrl+E`). The sidecars, `provenance.json` and `methods.md` carry the research-use disclaimer (see
    [Disclaimer](#disclaimer)).
  - *Cohort* (Seed = *Cohort*), described below.
  - *Clusters*: the suprathreshold clusters of the map (two-sided, 26-connected) with peak, extent in mm³ (at least
    100 mm³ unless you choose another minimum) and atlas labels from the atlases in the cache (nothing is
    downloaded unless you ask), in a sortable table with a jump to the peak and an export as TSV or HTML. It is
    descriptive: there is no cluster-extent inference.
  - *Networks*: for each Yeo-7 or Yeo-17 network (Schaefer cortex labels, Buckner cerebellum, Tian subcortex) the
    share of the suprathreshold voxels in it, the share of the network that is suprathreshold, the mean and median
    value and the spatial r. A significance is given only against the relocated-lesion maps (one lesion) or the null
    cohorts (a group map) when they exist, and otherwise every row says "not tested". Your own regions of interest
    can be added; for a cohort they give a chart of the percentage of lesions whose map is connected to each region.
    The exports hold no local path and include the volumetric files that the Network Correspondence Toolbox reads
    (on the FSL MNI 2 mm grid). Both cards work for one lesion and for a cohort.

  A network map that you open from a file (a `_funcmap` file, for example) is shown as a thresholded
  overlay with a color legend, and Draw never shows a network map. The lesion must be in the connectome's
  MNI space: a lesion traced on a native scan is normalized first (see
  [Normalize native-space lesions](#normalize-native-space-lesions)). In Studio, entering Map shows the
  template of the connectome's space behind the result (80% opacity, the lesion as an outline) and leaving
  it puts back the layers you had; the status chip reads "No connectome", "Ready", "Done · N=1000" or
  "Mapping 34% · 2 min left", `Ctrl+E` exports, and `Alt+Left` and `Alt+Right` browse the per-lesion maps of
  a cohort one at a time (one volume is reused, so the scene does not fill up).
- **Connectomes.** *Manage…* of the Connectome card opens the connectome manager. It lists the bundles
  registered on this computer (`Studio/LNM/Connectomes`: the local paths stay in the settings, never in a
  scene) with subjects, time points, voxels, size, space, terms and reference maps, and has *Add bundle…*,
  *Convert…* (a folder of preprocessed 4D NIfTI time series, or a Lead-DBS connectome folder, as a job that
  you can stop and resume, after an estimate of the size on disk), *Build reference maps…* and *Build
  hubness map* (never automatic, with a time and disk estimate first), *Remove from the list* (the files are
  kept) and *Create synthetic demo*. MATLAB v7.3 run files need the `h5py` package, which is installed only
  after you agree. *Import Lead-DBS maps…* copies maps that Lead-DBS computed into the open study, so that the
  Cohort card can use them, and *Verify against Lead-DBS…* maps 24 reference lesions with a connectome and reports
  how closely the maps agree with the published Lead-DBS maps (it downloads the references only after you have
  accepted the data use terms shown first). Connectomes are never bundled or downloaded by themselves: you add a local folder,
  use the demo, or ask for *Get a connectome…* and accept the provider's terms first (see
  [Research tools of wave 5](#research-tools-of-wave-5) and [Data and licensing](#data-and-licensing)).
- **Cohort** (Seed = *Cohort*): the subjects of the study that is open in the Library, or a study folder
  chosen with *Open study…* in Map (kept in `Studio/LNM/CohortStudy`, so scenes hold no local path; this also
  works in Classic). *Map all N lesions* maps every included lesion in one batched pass with a per-lesion
  cache, then the tabs of the Cohort card work on those maps. Lesions in native space (the card says how many
  were left out and points to the normalization), lesions that the study's review has not approved, and
  normalized lesions whose review is not *Pass* are left out of the analyses.
  - *Overlap*: each lesion's map is thresholded with the Result card's rule (re-resolved for N), and the card
    shows the overlap count and percent, and a sensitivity map (voxels in at least k % of the lesions, 90% by
    default; over the cases when the study has case and control groups). It is the classic sensitivity map,
    labelled as such. Optional legacy two-group maps (Welch t of the mean Fz maps, Liebermeister p of the
    binarized maps, "legacy two-group, no covariates", descriptive) and their conjunction with the
    sensitivity map.
  - *Contrast*: a group contrast of the per-lesion maps by a voxelwise GLM with Freedman-Lane permutation
    inference (max-statistic FWE as the primary result, FDR secondary, optional TFCE with its estimated time).
    Every decision of the design is shown before it runs: the groups and the contrast, the covariates (lesion
    volume is on, and turning it off needs a reason that is recorded), how the permutations are done and why
    (for example "group is nested in dataset"), and a design preview with the design matrix, rank, variance
    inflation, the number of unique permutations and the smallest attainable p. A blocking check disables
    Run. No parametric p is shown. *Export for PALM…* and *Import PALM results…* hand the design and maps to
    PALM or FSL randomise and bring the results back.
  - *Symptom*: where the maps covary with a score (sLNM 2.0): the partial r with lesion volume (and other
    covariates), Pearson or Spearman, Freedman-Lane permutation of the score (within datasets when the
    design says so), FWE p and FDR q maps, optional leave-one-dataset-out and control-score comparisons
    ("r = 0.56 → partial(PC1, μC) r = 0.30, p_perm = 0.11"), and *Use as circuit*. A column whose role you
    have not confirmed is marked "(role not confirmed)".

  The results go under `<study>/derivatives/lnm/`; each job records itself in the run registry.
- Also in the library, with a command line or a Python API (a screen of its own, where there is one, is named in
  the item):
  - a study schema with optional columns, roles, `participants.json`, lesion sidecars and listwise
    exclusion; a shared job protocol (JSON event lines, a cancel file, resume, a run registry with
    provenance); connectome provenance (DOI, version, global signal regression, population, terms), named
    subject subsets and a guard against pooling maps of different connectomes; a registry of coordinate
    spaces and template-family transforms; reference maps per connectome; and QA numerics that compare a
    map with null maps (the Map workspace and the Cohort card use these through jobs);
  - *Intake checks* (`lesionops`): 2D, multi-2D and 3D lesions, an exact-thickness extrusion with stated
    semantics, an approximate world mirror, mask hygiene, the traced side against the reported side, the
    share of the lesion in CSF, and duplicate candidates (the chips of the lesion card in Draw); *lesion-set
    diagnostics* (`lesionset`): exact integer overlap maps, pairwise Dice, volumes, tissue fractions of the same
    template family and case and control balance (the cohort diagnostics card of the Library);
  - *Preflight* (`preflight`): before a run, block, warn or inform about what will be analysed (missing,
    empty or non-3D lesions, mixed template families, unreviewed or unnormalized lesions, duplicates,
    too few subjects per group, disk space), each with a fix, in 88 to 91 ms for 300 lesions (three runs),
    and assertions on the outputs after a run (the Preflight card of Map);
  - *Reference files* (`fetch`, `atlases`): a verified, resumable and offline-tolerant fetch of atlases,
    templates and tissue priors with a licence badge for each, and space-aware multi-atlas labels that refuse
    to mix template families. Nothing is bundled and nothing is downloaded unless a command asks for one entry;
  - *Cluster and peak tables* (`clusters`): two-sided, 26-connected clusters with sub-peaks, extents in mm³ and
    atlas labels, descriptive only (there is no cluster-extent inference; the Clusters card of Map);
  - *Reporting v2* (`methods`, `checklist`, `export`): a sectioned methods text generated from what was
    run (with "not recorded" and "not run" where that is true), a 15-item reporting checklist with a
    six-dot battery, a supplementary case table, a publication manifest and NeuroVault metadata (nothing is
    uploaded);
  - *Figure fit* (`figurefit`): the landmark fits, cut planes and fit files of the Figure card in Draw; fits never
    contain a reflection, so the left-right convention of a figure is always an explicit choice;
  - *Figures* (`figures`): a renderer of publication figures that needs no Slicer, VTK or Qt: the presets gallery,
    overlap, montage, pair, triptych, replication, glass and ortho, with hard, transparent and unthresholded display
    modes, the neurological convention by default with L and R stamps on every panel, and one shared symmetric
    colour range. It writes PNG, TIFF, SVG and PDF with a `figure.json`, a caption and a source-data CSV, without
    local paths. The report of *Run everything* and the lesion overview use it;
  - *Null models* (`nulls`): independent and cohort-shift relocation, strength-matched, overlap-matched
    (experimental) and empirical-control-pool nulls, exact (pooled relocation maps, resumable) or fast, with
    excess, p, q, FWE and bias maps, the false-positive rate of the classic rule, and `--estimate` to see the time
    first. They say how much of a cohort's overlap chance placement would give; they do not make an overlap map
    specific;
  - *Fast parcel mode* (`fastmap`): a parcel index (the brain mask tiled with atlas parcels and supervoxels, built
    once from a connectome bundle as a resumable job) gives approximate maps quickly. They are labelled
    approximate, and a parity gate compares them with exact maps and allows the fast mode only for the strata and
    sizes that pass;
  - *Validation* (`validate`): a circuit (a GLM, sLNM or top-k map, rebuilt inside every fold) is tested on held-out
    subjects with leave-one-dataset-out folds, or stratified repeated k-fold when a dataset holds one class,
    against baselines (lesion volume, mean connectivity, PC1, lesion location) with bootstrap intervals and
    permutation p values; per-lesion scores (damage, similarity, ROI connectivity); a frozen circuit can be applied to
    a new cohort;
  - *Robustness* (`robustness`): a threshold sweep, replication on a second connectome (with a warning when the
    maps correlate below 0.7), lesion split-half, leave-one-lesion-out influence, alternative lesion seeds
    (binarized, probabilistic, extruded, spheres, flipped; the traced masks are not touched) and a consolidation
    of all lesions onto one hemisphere that is labelled as such. A connectome split-half is reported as sampling
    stability only;
  - *Connectomes* (`catalog`, `connectome`, `leaddbs`): a catalog of connectomes with licence badges, subset choices
    and a disk plan (it lists, and downloads nothing); a Python converter (`from_tar_stream`) that streams a tar archive (the GSP or the Lead-DBS
    layout) into a bundle without writing the archive to disk, with gzip and NIfTI checks, detection of a truncated
    archive and resumable checkpoints across several archives; Lead-DBS names for our maps; import of Lead-DBS maps
    into a study and a check against 24 pinned Lead-DBS reference lesions (the actions of the connectome manager);
  - *Cohort pipeline and report* (`pipeline`, `report`, `reportfigs`): see
    [Automate a cohort analysis](#automate-a-cohort-analysis).

  See [Lesion network mapping](#lesion-network-mapping).

### Research tools of wave 5

The lesion network mapping tools of this wave are described in the user guide pages
[Lesion Network Mapping](Docs/user_guide/modules/lesionnetworkmapping.md),
[statistics](Docs/user_guide/modules/lnm_statistics.md), [data](Docs/user_guide/modules/lnm_data.md) and
[intake](Docs/user_guide/modules/lnm_intake.md). They are research tools: nothing issues a "specific" verdict, a "best
threshold" or a "best connectome", and every result keeps the research-use statement. They were checked with synthetic
data, small fixtures and local test servers (see [Known limitations](#status-and-roadmap)).

**Getting data**

- **Get a connectome…** (Connectome card, `⋯` menu or *Manage…*, also before any connectome is registered). A catalog
  lists each connectome with its licence badge, size, number of subjects and space. Nothing downloads until you have
  clicked *Load terms*, read the provider's live data use terms and ticked *I accept* (the text and its checksum are
  kept with the bundle, and a changed text asks again). The download runs as a low-priority job: each archive is
  streamed into the converter, so no archive is stored unless you ask; checksums are verified as the data is read;
  *Pause* and *Resume* continue from where it stopped, and nothing resumes by itself. Entries whose terms are not
  stated are links only. `Studio/LNM/EnableConnectomeFetch` set to `false` switches the download off and leaves links.
- **Tutorial cohorts** (*Library > Import > Tutorial cohorts…*). Four example data sets, each with licence, size,
  citation and a learning path. Three come from CC0 sources and are downloaded only when you click; the fourth (LESYMAP)
  is a link. Each becomes a study with lesion masks, volumes, a merged table and column roles; native-space cohorts are
  flagged to normalize first, a failure or *Stop* leaves no partial study, and figures made from them carry the
  watermark "TUTORIAL DATA".
- **Control-lesion sets.** Register a folder of control lesions once, with its template space, and link it to studies
  from the *Control sets* card or *Import > Add control set…*; no lesion file is copied. Each set is mapped once per
  connectome into its own cache, so a second study reuses the result. A mismatch report (volume, 2D share, hemisphere,
  template family) compares the study's cases with the controls, and a matched subsample can be drawn.
- **Reference maps and the Compare card.** Compare maps of this study, of other connectomes and of a reference library
  with Pearson r, Spearman rho and partial r (heat map, pairs table, top 10, exports). Reference maps (NeuroVault, a
  URL, a file, the van den Heuvel degree map after you accept its terms) are added only when you ask. A permutation
  p-value is given only for pairs of in-study maps; any other pair says why it has none.
- **Literature sources and PRISMA** (*Sources* card of the study). Import records from nbib, RIS, BibTeX, CSV or pasted
  DOIs and PMIDs (exact matches merge; near duplicates are offered), screen them with `P`, `X` (with a reason) and `U`,
  fetch metadata only when you click, add case rows linked to a record, and export PRISMA counts and a flow diagram.

**Tracing and review**

- **Review workflow.** A lesion is *pending* after *Done*, then *approved*, *changes requested* or *rejected*, each
  decision with its comment in the study's audit file. *Require review before analyses* lets only approved lesions into
  analyses, and an automatic segmentation always needs a human approval (an override is recorded and named in the
  methods text). *Blind tracing* hides group, scores and symptoms from tracers and reviewers, and network maps are never
  shown while tracing or reviewing.
- **Second tracing and agreement.** Assign a share of the lesions to a second rater (or to the same rater again) who
  never sees the first mask; the agreement is measured on the masks, never on the maps: Dice, centroid distance,
  volume ratio and HD95, against the distance between different subjects' lesions. A disagreement is adjudicated by
  keeping A, B, the union, the intersection or an edit, and every decision can be undone.

**Seeds and plans**

- **Coordinate, atrophy and stimulation seeds.** The seed modes Coords and Stim map spheres from CSV, Sleuth or NiMARE
  foci (Talairach is converted to MNI; radius or diameter is explicit), atrophy maps and stimulation sites (points,
  E-field or VTA files) with the same engine as lesions. An optional random-coordinate control compares the observed
  overlap with grey-matter null sets of the same size and radii.
- **Recipes, plan lock and ROI.** Eight built-in analysis recipes with status tags and citations, lab recipes that record
  their differences from a base recipe, and a *Lock plan* that writes the plan with a checksum; later changes to the
  rule or the *Run everything* options need a recorded reason and go into a deviations log. A recipe is never marked as
  recommended. A-priori region of interest analysis gives ROI means, a GLM with maximum-statistic correction and
  small-volume masks.

**Statistics and checks**

- **VLSM** (a tab of the Cohort card). Voxel-based lesion-symptom mapping on the cohort's binary masks with a
  minimum-overlap mask and lesion volume as a default covariate, FWE and FDR correction, and *Seed from VLSM cluster*.
  It needs a study and no connectome.
- **Validate card.** Out-of-sample validation of a circuit against baselines (lesion volume, location, reference maps and
  others) with a baselines table, an incremental row, neutral verdicts and, for a frozen circuit, replication on another
  study.
- **Robust card.** Seven sections: a threshold sweep with the point at which the conclusion flips, replication on another
  connectome, lesion split-half, leave-one-lesion-out influence, seed variants, hemisphere consolidation and connectome
  split-half. The primary analysis stays fixed and a warning shows when it has changed since a result.
- **Nulls card.** Pick a null kind (independent, cohort shift, strength, overlap, control set), exact or fast mode, and
  see the smallest attainable p and q before running. Fast mode stays off until *Build fast index…* (in *Manage…*)
  has passed a parity check on your own lesions.
- **Circuits card.** Re-seed a map, a cluster or a peak sphere as a circuit with its parent map and source lesions,
  score each lesion's damage, and score user-given targets against random grey-matter spheres of the same size.
  Association with a circuit made from the same lesions is refused (double dipping).
- **Calibrate card.** *Calibrate this analysis* measures the false-positive rate of the configured analysis on null
  datasets (with exact confidence intervals) and plants a known network to report power and recovery.
- **Report card and battery strip.** Six status dots jump to the matching cards; the card shows the 15-item reporting
  checklist with evidence links and a methods text to copy; *Export publication bundle* writes the methods, the
  checklist, a reproduce script, `images.json` and a hashed manifest. Omissions are shown and never block.

**Sharing and automation**

- **Share card** (Library). A share-safe export as BIDS derivatives (folder or zip) with pseudonymised ids, only the
  columns you choose, opt-in lesion masks and group maps and a checklist that blocks unsafe exports, or a
  NeuroVault-ready folder. File headers are scrubbed; no local path or study internals are included; the pseudonym key
  stays in the study folder. The licence is your choice, and the dialog notes that maps derived from GSP data have terms.
- **Headless twin.** The `slicer-lnm` command (BIDS App style, `participant` and `group` levels) runs the same pipeline
  on a BIDS dataset or a Slicer Studio study and writes the same files, within numerical tolerance. It is a Python wheel
  (`pyproject.toml` in the module folder), with a Dockerfile and an Apptainer recipe in
  [Utilities/SlicerStudioLNM](Utilities/SlicerStudioLNM) that you build on your own computer; the macOS disk image
  gets a wrapper. `reproduce.sh` uses `slicer-lnm` when it is installed.

### Autosave and crash recovery

In Studio, a project is saved as you go into the Slicer Studio folder (see
[Your work is saved in a Slicer Studio folder](#your-work-is-saved-in-a-slicer-studio-folder)); the autosave described here
is the private recovery copy for everything that is not a project: the Classic interface, studies, and Studio when
`SLICER_STUDIO_PROJECTS=0`. The top bar status and the recovery card below belong to such scenes.

While you work, Slicer Studio autosaves the scene, in the Studio and the Classic interface, so that
unsaved work can be recovered after a crash or after closing without saving. **Autosave is not
Save:** an autosave never changes the scene's file, the scene still counts as modified, and `Ctrl+S` (File >
Save) and the exit prompt keep their meaning. Use *File > Save* to keep your work.

- **What is autosaved.** The whole scene, incrementally: only data that changed since the previous
  autosave is written again, and files you opened and did not modify are referenced, not copied (files
  from a temporary or cache folder are copied). Each autosave is a standard Slicer scene (`Scene.mrml`
  with its data files), with a thumbnail and a short summary of the scene for the recovery list.
- **Where.** A private folder in your user account's application data folder, never a folder of yours:
  `~/.local/share/slicer.org/Slicer/StudioAutosave` on Linux,
  `~/Library/Application Support/slicer.org/Slicer/StudioAutosave` on macOS and
  `%LOCALAPPDATA%/slicer.org/Slicer/StudioAutosave` on Windows (the Windows path has not been tried).
  `Studio/Autosave/Directory`, or the environment variable `SLICER_STUDIO_AUTOSAVE_DIR`, chooses another
  folder.
- **How often.** After 10 seconds without input once the scene has changed, and at most every 2 minutes
  during continuous work. Very large modified data (a node that takes more than 100 ms to write) is
  autosaved at most every 10 minutes and only after 30 seconds of idle time, and data above 2 GB is not
  autosaved (the recovery list says so). Writing happens in small steps, and by a background thread
  for large volumes, so that the interface stays responsive; autosaving pauses after a disk error or when
  less than 1 GB is free on the disk.
- **How long.** The last 5 sessions, 2 GB in total and 14 days; the settings below change this.
- **Privacy.** An autosave may contain copies of patient data. The folder is private to your account
  (mode `0700` on Linux and macOS), has a `README.txt` that says what it is, and is not synchronized by
  Slicer Studio; choose a private folder if you change it. *Application Settings > Autosave > Delete all
  autosaves…* removes them all.
- **Recovery.** If Slicer closed unexpectedly, or you closed it without saving, the next start lists what can
  be recovered, with a thumbnail and a summary: in the Classic interface as a window, in Studio as a card
  (below). The same list is *File > Recover Autosaved Work…*. A recovered scene is put back on the original
  files of your data, never on the autosave folder, and still counts as unsaved.
- **Status in Studio.** After the first autosave of a scene, the top bar shows a quiet status: "Autosaving…",
  "Autosaved · 2 min ago" (refreshed every 30 seconds) or "Autosave paused" (after a disk error or when the
  disk is nearly full). Nothing is shown while nothing was autosaved. Its tooltip says that autosave is not
  Save ("This is not saving: use File › Save (Ctrl+S) to save your files") and gives the time and size of the
  last autosave. Clicking it opens a menu: the last autosave, *Autosave now*, *Recover autosaved work…*,
  *Open autosave folder* and *Autosave settings…*. In a window narrower than 1500 px (1700 px while the
  status chip is shown), or when the top bar is compact, it is an icon and the text moves to the tooltip.
  When a study is open, the tooltip also shows when the study's journal was last saved.
- **Recovery card in Studio.** After a crash, a card at the top of the Library lists the sessions that can be
  recovered (the card never covers a view): *Recover*, *Discard*, *All autosaves…* (the full list of Classic's
  window) and ✕ (later; the sessions stay in *File > Recover Autosaved Work…*). In another workspace, a
  one-line notice at the top of the inspector ("Unsaved work from a previous session can be recovered ·
  Show") points to it. *Recover* puts the scene back and returns to the workspace (Draw or Map) that was
  current at the autosave. Every autosave records the workspace and the open study and subject for this. If
  the autosave was made with a study open, *Recover* reads *Reopen study at sub-012*: it reopens the study at
  that subject (where the journal offers the lesion, below) instead of loading the scene. Switching to
  Classic removes the status and the card, and Classic's window and status-bar messages take over.
- **Study journal and scene autosave.** The two work side by side. The scene autosave copies the whole
  scene to the private folder above. The journal of an open study (see
  [Studies and the study loop](#studies-and-the-study-loop)) writes only the lesion being traced, to
  `<study>/.studio/journal/<sid>.nii.gz`, two seconds after the last change, so it is inside the folder of
  the study (and holds the patient's lesion). Autosave pauses while a subject is switched.
- **Official Slicer.** The `Scene.mrml` of an autosave opens in official 3D Slicer 5.12 and 5.13
  (*File > Load Scene*), which the compatibility check tests with `--autosave`.
- **Off.** `SLICER_STUDIO_AUTOSAVE=0` turns autosave off for a run. The *Autosave the scene* option of
  the Autosave panel (`Studio/Autosave/Enabled`) stops autosaving but keeps recovery. Autosave never runs
  with `--testing`, `--no-main-window` or `--exit-after-startup`.
- **Web version.** The autosave folder is in the `slicer-studio-web-home` volume, so it survives
  stopping and starting the container. Stopping the container ends Slicer with a signal and not through the
  normal quit, which Slicer Studio treats as a crash, so the next start should offer recovery. This has not
  been tried with Docker yet.

### In-app updates

An installed Slicer Studio can look for a newer version. Updates are the public releases of this repository's
GitHub page; the maintainer creates and uploads each release by hand, with the files that
[`make_release.py`](Utilities/Scripts/SlicerStudioRelease/make_release.py) prepares (it never contacts GitHub; the
steps are in [its README](Utilities/Scripts/SlicerStudioRelease/README.md)). **No release has been published yet, so
there is nothing to find today.**

- **The check.** One anonymous HTTPS request, at start and then at most once a day, for `studio-update.json` of the
  latest release (GitHub's public download address: no account, no token, no API). It runs in a background thread.
  Nothing is checked under `--testing`, without a main window, in a build tree or a source checkout (the development
  channel; *Check now* only shows what the latest release is), or in the web version (its launcher has an `update`
  command). `SLICER_STUDIO_UPDATE_FEED=off` turns every check off.
- **Optional update.** A quiet notice ("Update 1.2.0 available · Update now · Skip") in Studio, and *Settings >
  Updates* in Studio and Classic, which also has the release notes, *Check now* and the switch *Check for updates
  automatically when Slicer Studio starts* (`Studio/Updates/AutoCheck`, on by default). Also in `≡ > Check for
  updates…` and in Search.
- **Required update.** When the maintainer marks a release as required, or your version is older than its minimum
  supported version, Studio shows a blocking dialog at start (*Update now* or *Quit*), before the recovery offer. If
  the check cannot reach GitHub, a required update found earlier is only a warning.
- **Installing.** The file for your platform is downloaded to a private folder with progress and *Cancel*, its size and
  SHA-256 are checked against the manifest before anything is replaced, the application is swapped after it exits
  (the previous version is put back if that fails), and unsaved work is autosaved and offered for recovery by the new
  version. This has been tested with a local test feed and fixtures, not with a real release, and not on macOS.

### Settings added by Slicer Studio

Slicer Studio keeps its own settings under `Studio/*` and `Appearance/*` in the shared settings file
(`Slicer.ini`). Official 3D Slicer ignores them. Studio is not applied under `--testing`, which keeps the
classic behavior, so most of these keys have no effect there. The ones you may want to change:

| Key | Default | Meaning |
|---|---|---|
| `Studio/Enabled` | `true` | `false` starts the Classic interface |
| `Studio/StartWorkspace` | `Library` | Workspace shown at start (`Last`: the one you left) |
| `Studio/Autosave/Enabled` | `true` | Autosave on or off (Application Settings > Autosave) |
| `Studio/Autosave/IntervalMinutes` | `2` | Longest time between autosaves during continuous work |
| `Studio/Autosave/IdleSeconds` | `10` | Autosave after this many seconds without input |
| `Studio/Autosave/Directory` | (private folder) | Folder of new autosaves |
| `Studio/Autosave/KeepSessions` | `5` | Sessions kept |
| `Studio/Autosave/MaxDiskMB` | `2048` | Largest size of all autosaves |
| `Studio/Autosave/MaxAgeDays` | `14` | Days after which autosaves are deleted |
| `Studio/Autosave/MaxNodeMB` | `2048` | Data larger than this is not autosaved |
| `Studio/SmartFill/Mode` | `3D` | `3D` or `2D` (this slice) |
| `Studio/SmartFill/AutoTolerance` | `true` | Choose the tolerance from the boundary contrast |
| `Studio/SmartFill/TolerancePercent` | `10` | Tolerance when it is not automatic (1 to 60) |
| `Studio/SmartFill/MaxRadiusMm` | `100` | Largest distance from the click |
| `Studio/SmartFill/LeakGuardMm` | `1.5` | Leak guard size (0 is off) |
| `Studio/SmartFill/FillHoles` | `true` | Fill holes and smooth edges |
| `Studio/SmartFill/StayInsideBrain` | `true` | Stay inside the brain mask of an MNI template grid |
| `Studio/SmartFill/ApplyOnClick` | `false` | Apply each click at once |
| `Studio/BrushDiameterMm` | `3` | Brush diameter, remembered between sessions |
| `Studio/BracketKeysResizeBrush` | `true` in Studio's Draw panel, `false` in Classic, never under `--testing` | `[` and `]` resize the Paint brush; `false` keeps the official meaning (they cycle the volumes of the slice view). Written as `true`, they resize the brush in Classic too. Not in a settings page: edit `Slicer.ini` |
| `Studio/FollowStrokes` | `true` | The other views move to each stroke |
| `Studio/PageStepSlices` | `10` | Slices moved by `PageUp` and `PageDown` (1 to 100; same settings page) |
| `Studio/FocusFollowsMouse` | `true` | The keys of a slice or 3D view act on the view under the mouse (same settings page) |
| `Studio/ClassicPanelWidth` | (what you dragged) | Width of the module panel of the Classic window, kept between modules |
| `Studio/RecentStudies` | (list) | The recently opened studies (a JSON list of local paths) |
| `Studio/LeftDragWindowLevel` | `true` | Left-drag adjusts window/level when no tool is active (Application Settings > Views > Navigation in Studio) |
| `Studio/OrientationLetters` | `true` | Orientation letters on the slice views |
| `Appearance/FlatViewBars` | `true` (not under `--testing`) | Flat view bars (Application Settings > Views, restart needed) |
| `Appearance/Default3DView/*` | follows the theme | 3D view background and box colors |
| `Studio/Library/StudyView` | `list` | `list` or `grid`: how the Library shows the subjects of a study (the List and Grid buttons) |
| `Studio/Projects/Root` | `Documents/Slicer Studio` (in the web version `/data/Slicer Studio`) | The Slicer Studio folder (Settings > Saving); `Studio/Projects/RootChosen` records that you chose it. `SLICER_STUDIO_PROJECTS_ROOT` overrides both and skips the question; `SLICER_STUDIO_PROJECTS=0` turns projects off |
| `Studio/Library/ProjectSort` | `modified` | How the Library sorts the projects: `modified`, `name` or `created` |
| `Studio/Library/ProjectView` | `grid` | `grid` or `list`: how the Library shows the projects |
| `Studio/Templates/Default` | (the built-in template) | The MNI template that *Start on template* uses; `Studio/Templates/LastDirectory` is the folder of the last template you added |
| `Studio/LNM/EnableConnectomeFetch` | (on) | `false` switches *Get a connectome…* off and leaves links. Only read: set it yourself. Also kept: `Studio/LNM/FetchDestination` (the last folder you chose), `Studio/LNM/ActiveFetch` (an unfinished download) and the accepted terms texts |
| `Studio/Library/GridThumbSize` | `160` | Side of the thumbnails of the grid in pixels (120 to 220; the card-size slider) |
| `Studio/LazyPythonWrappers` | `true` | Import the Python wrappers of the modules when they are first used (and the rest while the application is idle), which shortens the start; `false` imports them all at start. Read at start |
| `Studio/Updates/AutoCheck` | `true` | Look for a newer version at start (Settings > Updates). Also kept: `Studio/Updates/LastCheck`, `Studio/Updates/LastManifest` and `Studio/Updates/SkippedVersion` |
| `Studio/Figure/LastFolder` | (none) | The folder of the last figure opened with *Open figure…* |
| `Studio/LNM/MaxJobThreads` | (CPUs minus one) | Most threads of a Map job |
| `Studio/LNM/MemoryBudgetGB` | (a quarter of the memory, at most 4 GB) | Memory budget of one pass of *Run everything*. Only read: set it yourself |
| `Studio/LNM/Pipeline/Options` | (the defaults) | The options of *Run everything* (a JSON text), written when you start a run from the options dialog |
| `Studio/LNM/Clusters/MinExtentMm3` | `100` | Smallest cluster that the Clusters card lists, in mm³ |
| `Studio/LNM/Clusters/Atlases` | (the cached atlases) | The atlases that label the clusters (a JSON list of catalog ids) |
| `Studio/LNM/Networks/Scheme` | `yeo7` | `yeo7` or `yeo17`, the networks of the Networks card |
| `Studio/LNM/Networks/RoiMinPercent` | `50` | Share of a region of interest that must be suprathreshold for a lesion to count as connected |
| `Studio/LNM/Normalize/LastOptions` | (none) | Normalization options (template, quality, modality, margin), written when you tick *Remember these options* |
| `Studio/LNM/DataDirectory` | `Documents/SlicerStudio` (`/data/SlicerStudio` in the web version) | Where normalization results go when no study is open. Only read: set it yourself |
| `Studio/LNM/CohortStudy` | (none) | The study folder of the Cohort seed that you chose with *Open study…* in Map |
| `Studio/RaterName` | the operating-system user | The name recorded with a normalization review. Only read: set it yourself |

Smart fill keys are written when you change the options of the tool. The Autosave, Appearance and
navigation settings are in *Application Settings*. Map also keeps `Studio/LNM/Connectomes` (the
connectomes you added) and the data use terms you accepted, under `Studio/LNM/*`. A study keeps its own
choices in its folder, not in the settings: `study.json` has an additive `normalization` entry
(`auto_after_done`, `template`, `quality`, `dilate_mm`) for the automatic normalization of the Library.

### Performance

Measured with [`Utilities/Scripts/StudioBench.py`](Utilities/Scripts/StudioBench.py) and the work
package verification scripts, on a Linux (Ubuntu 24.04) Release build: a shared 4-core virtual machine,
Xvfb with software OpenGL (Mesa llvmpipe), 1920x1080 window. Absolute times vary up to 4 times with the
load of the machine, so only compare the two values in the same row.

| Operation | Before | After | Comparison basis |
|---|---|---|---|
| Paint stroke release, 256³ volume, 7 segments, 3D surfaces shown | 773–853 ms | 217 ms | official 5.13 nightly vs Slicer Studio, same session (official: 3 runs; Studio: median of 3) |
| 3D surfaces rebuilt per stroke, same scene | 7 | 1 | same |
| Paint stroke with intensity masking, MNI 1 mm grid | 68–90 ms | 20 ms | same |
| Median smoothing (3 mm) of a small lesion (sphere of radius 10 voxels, about 4,100 voxels, plus a one-voxel island) in a 256³ volume, 1 mm voxels | 452 ms | 125 ms | same build tree before and after the change, median of 5 runs; identical voxels |
| Islands, keep largest, same lesion | 468 ms | 123 ms | same |
| Margin, grow 2 mm, same lesion | 364 ms | 120 ms | same |
| Refresh of the Margin options, 384x384x256 volume | 26.7 ms | 0.2 ms | same build before and after, median |
| One slice scroll step through the event loop | 8.0–10.5 ms | 5.5–5.6 ms | same script with the libraries before and after the change, 2 runs each, heavily loaded machine |
| `sliceNode.Modified()` that changes nothing | 18.5 ms | 3.5 ms | 3 interleaved before/after pairs, heavily loaded machine |
| Slice-logic updates per scroll step / renders of the other views (slice intersections off) | 3 / 2 | 1 / 0 | same |
| Warm module switch | 102 ms (Classic) | 21 ms (Studio) | same build, Classic vs Studio interface, interleaved runs |
| 300 crosshair moves over a slice view, events processed after each, Data Probe open (loop wall time) | 1361 ms | 21 ms | original vs new Data Probe code, same process, interleaved |
| Same, paced at 125 Hz (main-thread CPU) | 1185 ms | 529 ms | same |
| One scroll step in the Studio interface (`scroll_step_ms` of StudioBench) | 59.5 ms | 45.7 ms | wave 2 fix (W2-I2): the wave 2 tree before and after the fix, same build tree and day, median of 3 runs, loaded machine. At the end of wave 1 the same metric was 32.5 ms on a quieter machine, and the unchanged official 5.13 binary measured 60.6 ms that day |
| Switching workspaces (`workspace_switch_ms`) | 168 ms | 72 ms | same (49 ms at the end of wave 1, on the quieter machine) |
| Brush hover over a slice view, time per mouse move (three volumes and a lesion segment) | 64.7 ms | 21.1 ms | wave 3 (W3-05): the same script on the tree before and after, one run each; a second run, in review, gave 26.8 ms |
| Views rendered by 30 hover moves, same scene | 30 of 30 in each slice view and in the 3D view | 29 of 30 in the view under the mouse, 0 in the others | same |
| Paint stroke, time per mouse move, first 20 moves | 68.5 ms | 40.1 ms | same (33.3 ms in the review run) |
| *Add Data* with 400 files named `lesion_aa.nii.gz` … (time until the table is built) | 4,377 ms | 638 ms | wave 3 (W3-04): same script before and after, median of 3 runs |
| Same, 100 files | 928 ms | 153 ms | same |
| Warm module switch in Classic, with the thin sliders and scroll bars of the style (`module_switch_warm_ms`) | 56.2 ms | 56.5 ms | wave 3 (W3-01): StudioBench before and after, median of 3 runs |
| First open of the Segment Editor (`segment_editor_first_open_ms`) | 351.9 ms | 299.5 ms | wave 5 (V-03): StudioBench, median of 5 runs each, before and after the Draw and Map polish; a hidden top-level window of each list row had cost two 20 ms layout passes, which was fixed |
| Undo of a stroke with 3D surfaces shown, 256³ volume, 7 segments (`undo_surfaces_ms`, time until the 3D view shows it) | 236.1 ms | 18.7 ms | wave 4 (W4-01): StudioBench before and after the change, same build tree, median of 3 runs |
| Same scene, undo and redo (median of 30 strokes) | 219.6 ms and 226.2 ms | 18.2 ms and 17.3 ms | same, memory script of the work package |
| Memory per undo state, same scene | 2.27 MB | 0.92 MB | same (the 30 states of Studio's undo depth hold 28 MB more than before the strokes instead of 68 MB) |
| Draw (outline) apply on the MNI 1 mm grid (`draw_apply_ms`) | 71.6 ms | 12.8 ms | same |
| Paint stroke release, 256³ volume, 3D surfaces shown (`stroke_release_256_3d_ms`) | 208.1 ms | 195.4 ms | same: not changed beyond the noise (the stroke path was made faster in wave 1) |
| Start of the application, lazy Python wrappers (`startup_total_ms`, wall time of the process in brackets) | 3,471 ms (4,222 ms) with `Studio/LazyPythonWrappers` false | 2,990 ms (3,856 ms) with it true | wave 4 (W4-03): StudioBench `--startup`, 3 runs each, medians; the machine was loaded |

The scroll and workspace-switch rows are not comparable with the earlier rows of the table: they are other
metrics, measured on a more loaded machine, with the flat view bars and the wave 2 Studio panels in place.
The wave 3 rows are other metrics again. A folder of 400 numbered lesion masks (`sub-001_lesion.nii.gz` …) used
to give one row in *Add Data* (25 ms, all files grouped as one image series); it gives 400 rows in 610 ms now.

Not faster yet:

- **Startup.** Studio 3.1 s, Classic 3.0 s in wave 3 (same build, median of 5 alternating runs): the Studio
  interface adds about 0.1 s. The lazy wrappers of wave 4 save about 0.5 s of the start (table above); the 71
  wrapper sets that are not needed at once are imported in the background after the start (about 1.8 s in total,
  while the application is idle), which has not been timed against a user's first actions. Studio and Classic were
  not compared again after wave 4.
- **Brush hover with large 3D surfaces.** The hover renders only the view under the mouse (table above), and the
  plan's goal of 10 ms was not reached: 52.0 ms before and 46.8 ms after wave 4 in the StudioBench scene with
  large surfaces (`paint_hover_surfaces_ms`), and 21 to 27 ms in the smaller scene of the table.
- **The Library grid.** In a synthetic study of 300 subjects (226 of them traced) the
  226 thumbnails were computed in the background in 23 s, none of the 468 paint events while scrolling or while the
  thumbnails filled took more than 13 ms, and reopening the study read all 226 from the cache in 0.64 s, computing none.
  This is a count of events on one machine, not a comparison with another tool.

A build made on Ubuntu 24.04 (before the Studio changes) started in 2.5–3.3 s, against 6.1–6.4 s for
the official Linux nightly package on the same idle machine. This comes from Ubuntu's linker default
(`--as-needed`, which drops unused library dependencies) and from building without the Qt Designer
plugins (`Slicer_BUILD_QT_DESIGNER_PLUGINS=OFF`, about 0.6 s less first-load work), not from Slicer
Studio code. Measured again with the Studio changes (fresh settings, machine under some load), the
build started in 3.3–3.9 s and the official 5.13 nightly in 8.0–8.8 s. Wave 4 added the CMake option
`Slicer_USE_LINKER_AS_NEEDED` (on by default) so that toolchains that do not link this way by default do too.
On this build it changed nothing: GCC on Ubuntu already did it (the libraries record the same 2,858 dependencies
before and after) and the start did not get faster (3.7 s before and 3.9 s after, within the noise of a loaded machine).

### File compatibility with official 3D Slicer

- **Both directions.** Scenes and data saved by Slicer Studio open in official 3D Slicer 5.12 and 5.13,
  and files saved by those versions open in Slicer Studio. The compatibility check (below) passes for
  all 9 ordered writer/reader pairs of Slicer Studio, official 5.12.4 and the official 5.13 nightly of
  2026-10-16 (and a Slicer Studio writer in a path with spaces and non-ASCII characters), for scene
  folders and `.mrb` bundles, in normal and `--testing` mode, last run completely on 2026-10-22 (with `--autosave`
  and in `--testing` mode, both passed) with the tree of the end of wave 3. The LNM outputs and the scene autosave (see
  [Autosave and crash recovery](#autosave-and-crash-recovery)) written by Slicer Studio load in all three.
- **Wave 3.** The files of wave 3 add no format. Normalization results are standard NIfTI files (the lesion
  and the scan in template space, with the exact TemplateFlow header), `.h5` transforms, JSON and PNG; the
  study files (`study.json`, `participants.tsv`, the journal, the lesion sidecars) are plain files that official
  Slicer does not read. A scene saved after a normalization (the template-space volume and lesion and the
  transform) loads in official 5.12.4 and the 5.13 nightly with the same voxels, and the compatibility check
  passed in the same way on 2026-10-21 (A-03b, 4 writers and 3 checkers).
- **Wave 4.** The files of wave 4 add no format either. The cohort pipeline writes the same NIfTI maps as before
  (with the exact header of the connectome, and the group maps with JSON sidecars), TSV and JSON tables, an HTML
  report and shell scripts; the lesion overview of the Library loads its maps as standard volumes with
  standard discrete colour tables; the Figure card uses standard nodes tagged by attributes; the cheaper undo changes
  only how Slicer Studio keeps its own history. Official 5.12.4 and the 5.13 nightly loaded, with the same voxels, the
  pipeline's per-lesion and group maps (A-04), a scene with the traced figure (L-29, PASS), the overlap scene of the
  Library (L-31, PASS) and a segmentation written after strokes and undo, as a file and in a `.mrb` (W4-01).
  These are checks of single work packages, not a run of the complete check.
- **Wave 5.** The files of wave 5 add no format either. A project is a standard scene folder with relative paths; the
  compatibility check with `--projects` saved one as a fork writer and official 5.12.4 and the 5.13 nightly loaded it
  (2026-10-24, PASS), and a project that holds a user template loaded in both and in the fork again (2026-10-25,
  PASS). The new study files (review audit file, second tracings, control sets, sources records, share exports) are plain
  JSON, TSV and NIfTI files that official Slicer does not read; the share export holds no absolute local path, and
  neither do the manifest, `images.json` and checklist of the publication bundle. Its `reproduce.sh` names local paths
  by design, so check it before you share the bundle. These are checks of single work packages, not a run of the complete check.
- **No new formats.** No new node classes, storage node classes or file formats, and no changes to MRML
  defaults. Existing writers produce the same output, except that a layout node with a Studio layout
  also writes a `studioViewArrangement` XML attribute (see *Portable layouts*). Module state is kept in
  standard scripted module parameter nodes. Extra information is only stored as node attributes or XML
  attributes that official Slicer ignores.
- **Portable layouts.** The Studio layouts (ids 7001–7003 and 7005; the range 7001–7099 is reserved) are
  registered in C++ in every mode. A scene saved with one of them stores `currentViewArrangement="2"` (the Conventional
  layout, which official Slicer knows) plus `studioViewArrangement="7001"`. Official Slicer ignores the
  extra attribute and shows the Conventional layout; Slicer Studio restores its own layout.
- **Shared settings.** All Slicer installations share the user settings file
  (`~/.config/slicer.org/Slicer.ini`), so recent files and other preferences carry over. Slicer Studio
  keeps its own preferences under `Studio/*` and `Appearance/*`, which official Slicer does not read (the
  list of the ones you can change is in [Settings added by Slicer Studio](#settings-added-by-slicer-studio)),
  and does not change keys that official Slicer reads (`Styles/Style`, `font`, `Modules/*`, `Python/*`,
  `Developer/*` and others) through defaults or migrations; only your own changes in the settings do.
  The fork's default settings file is installed with the application; at startup it copies into your
  settings only the keys that are missing (`Studio/Enabled`, `Studio/StartWorkspace`, `Studio/PanelWidth`
  and the default theme and font under `Appearance/*`).
  The one exception is the window state: when Slicer Studio closes, it writes the Classic window state to
  `MainWindow/windowState` and replaces a Studio layout in `MainWindow/layout` with the Conventional
  layout, so that official Slicer opens a normal window.
- **Side by side.** On macOS the application is "Slicer Studio.app" with its own bundle identifier
  (`org.slicer.slicerstudio`), so it does not replace official 3D Slicer. Each installation keeps its
  own extensions and Python packages.
- **Check it yourself** with
  [`Utilities/Scripts/SlicerFileCompatibilityCheck.py`](Utilities/Scripts/SlicerFileCompatibilityCheck.py)
  (see [Compatibility check](#compatibility-check)).

## Install

Slicer Studio is not on download.slicer.org, and no package has been published yet: you build it yourself,
on your own computer. The full guide is [Install Slicer Studio](Docs/user_guide/install_studio.md).

| Form | Runs on | How to get it |
|---|---|---|
| macOS disk image | macOS 14 (Sonoma) or newer, Apple Silicon or Intel | A build script on your Mac |
| Web browser on localhost | Any computer with Docker (macOS, Windows, Linux); on Linux also without Docker | Python launcher plus a Docker image |
| Linux package | Ubuntu 24.04, x86-64 | `make package` |
| Build from source | Linux (tested on Ubuntu 24.04); macOS through the disk image script | Slicer build instructions |

There is no Windows package; on Windows, use the web version.

### macOS disk image

**Build it on your Mac**, from the top of the source tree (macOS 14 or newer, Xcode Command Line Tools,
Homebrew, about 40 GB of free disk space, several hours):

```console
# Homebrew Qt: runs on this Mac and newer macOS
Utilities/Scripts/SlicerStudioMacOS/build_dmg.sh
# Official Qt: runs on macOS 14 or newer, on Macs with the same processor (Apple Silicon or Intel)
Utilities/Scripts/SlicerStudioMacOS/build_dmg.sh --qt=official
```

The disk image is written to `~/SlicerStudio/dist/`. If the build stops, run the same command again to
continue; `--dry-run` prints the plan. On Intel Macs, and on Apple Silicon Macs with macOS 14, use
`--qt=official` (Homebrew would compile Qt for hours). Options and troubleshooting:
[Utilities/Scripts/SlicerStudioMacOS/README.md](Utilities/Scripts/SlicerStudioMacOS/README.md).

**Install:** open the disk image and drag **Slicer Studio** to **Applications**. The application is
signed ad hoc but not notarized, so macOS blocks it the first time: click **Open Anyway** in
*System Settings > Privacy & Security*, or run

```console
xattr -dr com.apple.quarantine "/Applications/Slicer Studio.app"
```

On Apple Silicon, also install Rosetta 2 once, for the bundled `PythonSlicer` launcher:

```console
softwareupdate --install-rosetta --agree-to-license
```

> [!NOTE]
> The macOS packages are the first Qt 6 builds of this fork. A disk image built on 2026-10-18 passed the
> start-up test on macOS 15 with Apple Silicon (Qt 6.11.2) and the bundle self-containment check; other
> macOS versions and Intel Macs have not been tested. Slicer Studio has so far been run on Linux with
> Qt 5.15.

### In a web browser (localhost)

The web version is the real desktop application: the Linux build runs on a virtual display in a Docker
container, and [noVNC](https://novnc.com) shows it in your browser at <http://localhost:8080>. Nothing
leaves your computer. You need Docker Desktop (or Docker Engine) and Python 3.8 or newer; the launcher
uses only the Python standard library. On Apple Silicon, turn on *Use Rosetta for x86_64/amd64
emulation* in Docker Desktop, because the image is built for amd64.

```console
cd Utilities/SlicerStudioWeb
python3 slicer_studio_web.py build --package ~/Downloads/Slicer-<version>-linux-amd64.tar.gz  # once
python3 slicer_studio_web.py        # start Slicer Studio and open the browser
python3 slicer_studio_web.py stop
```

The package is the Linux package of this fork (see [Linux package](#linux-package)).

On Linux, the launcher can also run a local Slicer installation or build tree without Docker:

```console
sudo apt install tigervnc-standalone-server tigervnc-tools websockify novnc openbox
python3 slicer_studio_web.py local --slicer ~/Slicer-SuperBuild-Release/Slicer-build/Slicer
```

Good to know: with Docker, `~/SlicerStudioData` is shared with Slicer as its `Documents` folder
(`run --data` to choose another one); the server listens on `127.0.0.1` only (`--ask-password` adds a
password); shortcuts use `Ctrl`, also on a Mac; rendering uses the CPU, so large volume renderings are
slow. The web version has been tested with Docker on Linux, not yet with Docker Desktop on macOS or
Windows. Details and troubleshooting:
[Utilities/SlicerStudioWeb/README.md](Utilities/SlicerStudioWeb/README.md).

Lesion network mapping works in the web version with these limits (set up in wave 4; the package smoke check
[`SlicerStudioSmokeCheck.py`](Utilities/Scripts/SlicerStudioSmokeCheck.py) runs a lesion network mapping command line
through `PythonSlicer`; not tried with Docker Desktop): file dialogs start in `/data`,
so keep what you want to keep under `/data` (the rest of the container is lost when it is removed); a figure is opened
with *Open figure…* because the browser version has no image clipboard and no drag and drop from your computer; and the
heavy jobs (normalization, maps, permutation tests, *Run everything*) are 3 to 5 times slower than natively on an Apple
Silicon Mac, where the image runs under emulation. For a whole cohort, the macOS disk image is the better choice.

### Linux package

The Linux package, `Slicer-<version>-linux-amd64.tar.gz`, is built on Ubuntu 24.04 with the system Qt 5.15
and the system OpenSSL (see [Build from source](#build-from-source); `make package` in the `Slicer-build`
folder writes it). It runs on Ubuntu 24.04.

1. Install the runtime libraries (Qt 5 and a few others) listed in
   [the install guide](Docs/user_guide/install_studio.md#linux).
2. Extract the package anywhere: `tar -xzf Slicer-<version>-linux-amd64.tar.gz`
3. Run `Slicer` in the extracted folder. To uninstall, remove the folder.

### Build from source

Follow the [3D Slicer build instructions](Docs/developer_guide/build_instructions/index.md)
([online](https://slicer.readthedocs.io/en/latest/developer_guide/build_instructions/index.html)). On
Ubuntu 24.04 with the system Qt 5, which is how this fork is developed and how the Linux package is built:

```console
sudo apt update && sudo apt install git git-lfs build-essential cmake libssl-dev libxt-dev \
  libgl-dev libglu1-mesa-dev libqt5x11extras5-dev qtmultimedia5-dev libqt5svg5-dev qtwebengine5-dev \
  libqt5xmlpatterns5-dev qttools5-dev qtbase5-private-dev
# Slicer Studio is not on main yet
git clone --branch claude/hopeful-hawking-kibs73 https://github.com/azhmeerjesani/Slicer-2.git Slicer
cmake -S Slicer -B Slicer-SuperBuild-Release \
  -DCMAKE_BUILD_TYPE:STRING=Release \
  -DSlicer_USE_SYSTEM_OpenSSL:BOOL=ON \
  -DBUILD_TESTING:BOOL=OFF \
  -DSlicer_BUILD_DOCUMENTATION:BOOL=OFF \
  -DSlicer_USE_VTK_DEBUG_LEAKS:BOOL=OFF \
  -DSlicer_BUILD_QT_DESIGNER_PLUGINS:BOOL=OFF
cmake --build Slicer-SuperBuild-Release --parallel "$(nproc)"
Slicer-SuperBuild-Release/Slicer-build/Slicer
```

- **Use the system OpenSSL on Ubuntu 24.04** (`Slicer_USE_SYSTEM_OpenSSL=ON`). Ubuntu's Qt 5 is built
  against OpenSSL 3. With the OpenSSL 1.1 that the superbuild bundles otherwise, Qt has no HTTPS
  ("TLS initialization failed"), and for example the Extensions Manager cannot download its catalog.
- `BUILD_TESTING=OFF` shortens the build. The test commands in [Development](#development) do not need
  ctest.
- To make the Linux package: `cmake --build Slicer-SuperBuild-Release/Slicer-build --target package`.
- The first build takes hours. Python-only changes need no compilation: run
  `make CopySlicerPythonScriptFiles CopySlicerPythonResourceFiles` in `Slicer-build`.

## Quick start

1. **Start Slicer Studio.** It opens on the Library.
2. **Open a scan, or start on the template.**
   - Drop a NIfTI, NRRD or MetaImage file onto the window, or click *Open…* (`Ctrl+O`). A file whose
     name contains *lesion*, *mask*, *seg*, *label* or *roi* as a separate word (between `_`, `-` or `.`,
     as in `sub-01_lesion.nii.gz`) loads as the lesion, a segment named "Lesion". Other images load as
     the scan, and a segmentation "Lesions (*scan name*)" with an empty "Lesion" segment is created on
     the scan's grid.
     Scenes (`.mrb`, `.mrml`) open as scenes. `Shift`+drop opens the classic *Add Data* dialog instead.
   - *Open folder…*: a DICOM folder goes to the DICOM module; a folder with several NIfTI files offers
     *Load all* (disabled above 20 files: open one subject's files with *Open…*); any other folder opens
     *Add Data*.
   - *Start on MNI template* loads the MNI152NLin2009cAsym 2 mm brain that comes with Slicer Studio
     (no network needed) and creates a "Lesions" segmentation on its grid. After your first stroke, the
     inspector offers the 1 mm template: a 13.7 MB download from TemplateFlow with progress, Cancel and a
     checksum check. It never covers the views.
   - Other files: a case-report figure (PNG, JPEG, TIFF) opens large beside the template as a reference
     that cannot be painted into; a statistical map opens as a thresholded overlay; several lesion files
     opened together form a set that you review one lesion at a time (`Alt+Left` and `Alt+Right` in
     Draw). A lesion file without a scan is shown on the template only when it is on an MNI grid.
   - *Recent* lists the most recent files; `Enter` reopens the first one.
3. **Trace the lesion in Draw.** After a scan, a lesion or the template is loaded, Draw opens by itself
   with the "Lesion" segment selected. Use the tool tiles: Brush (`P`), Outline (`D`, with the lasso),
   Smart fill (`M`: click inside the lesion, `Enter` to apply it, then check the result), Erase (`E`) and
   Fill gaps (`Shift+F`); *More tools* has the others (Auto outline, Threshold, Smooth, Islands, Margin
   and more). `Ctrl+Z` and `Ctrl+Y` undo and redo. The lesion card shows the lesion's volume, parts and
   gaps as you draw. The scene is autosaved while you work (see
   [Autosave and crash recovery](#autosave-and-crash-recovery)). For many subjects, make a study first
   (*New study…* in the Library): the primary button is then *Done → next*, which writes the lesion, updates
   `participants.tsv` and opens the next subject (see [Studies and the study loop](#studies-and-the-study-loop)).
4. **Save.** *Save lesion* (bottom of the inspector in Draw, `Ctrl+Shift+S` or `Ctrl+Enter`) writes the
   lesion as a NIfTI mask (`.nii.gz`, 0/1 values, `uint8`) on the lesion's own grid, with a small `.json`
   sidecar next to it: a lesion mask that you opened on the FSL MNI152 2 mm grid is saved on that grid, not
   on the grid of the template you drew over. It suggests `sub-XX_lesion.nii.gz` when the scan name has a
   BIDS subject, and remembers the folder. After the first save, the button and `Ctrl+Shift+S` save again
   to the same file; *Save lesion as…* always asks. `Ctrl+S` saves the whole scene as a folder or a `.mrb`
   file, which also opens in official 3D Slicer. An autosave is not a save: only these commands write your
   files. For other export options, open the Segmentations module (`Ctrl+K`, type "Segmentations") and use
   *Export to files*.
5. **Map the network (normalize native scans first).** Open Map (`Ctrl+Alt+3`). Choose the lesion and a
   connectome (*Add connectome…* for a `.lnmc` bundle on your computer, or *Use the demo* to try Map with
   synthetic data), accept its data use terms, and click *Map network* (`Ctrl+Enter`). Read the maps, the
   threshold rule and the QA, and *Export…* them. See
   [Lesion network mapping library](#lesion-network-mapping-library) and, for the command line,
   [Lesion network mapping](#lesion-network-mapping). The lesion must be in the connectome's MNI space.
   If you traced it on the patient's own scan, click *Normalize to MNI* first (the card in Draw, or the card
   under Seed in Map, or *Normalize N native subjects* in a study): it runs in the background, and you
   check the alignment in the QC mosaic before you use the result (see
   [Normalize native-space lesions](#normalize-native-space-lesions)). Note that the template that comes
   with Slicer Studio (MNI152NLin2009cAsym) and the FSL MNI152 template used by GSP1000
   (MNI152NLin6Asym) differ by a few millimeters. For a study, set the seed to *Cohort* and click *Run everything*
   to run the whole cohort analysis and get a report (see
   [Automate a cohort analysis](#automate-a-cohort-analysis)).

## Lesion network mapping

`LesionNetworkMappingLib`
([Modules/Scripted/LesionNetworkMapping](Modules/Scripted/LesionNetworkMapping/LesionNetworkMappingLib))
uses only numpy and scipy (no Slicer, VTK or Qt imports) and runs in Slicer's own Python, `PythonSlicer`.
For every subject of a normative connectome, it correlates (Pearson r) the lesion's mean time course
with every voxel, averages over runs and applies the Fisher z transform; across subjects it computes the
mean r, the mean Fisher z and the one-sample T statistic of Fisher z, as Lead-DBS does. All lesions of
a run share one pass over the connectome: one matrix product per chunk of voxels serves all of them.

The library is part of the built application (it is installed with the Map module, and `PythonSlicer`
of a build tree finds it). You can also run it from the source tree, with `PYTHONPATH` pointing to
`Modules/Scripted/LesionNetworkMapping` and the `PythonSlicer` of any Slicer Studio or 3D Slicer 5.12+
installation:

| Installation | PythonSlicer |
|---|---|
| Build tree | `Slicer-SuperBuild-Release/python-install/bin/PythonSlicer`, or `Slicer-build/Slicer --launch PythonSlicer` |
| Linux package | `bin/PythonSlicer` in the extracted folder |
| macOS | `/Applications/Slicer Studio.app/Contents/bin/PythonSlicer` |

In the commands below, `PythonSlicer` stands for the path in the table above, with `PYTHONPATH` set:

```console
export PYTHONPATH=/path/to/Slicer/Modules/Scripted/LesionNetworkMapping
```

To check the setup:

```console example
PythonSlicer -m LesionNetworkMappingLib.engine --help
```

### Connectome bundles (`.lnmc`)

A connectome is converted once into a bundle folder that the engine reads quickly:

```text
MyConnectome.lnmc/
  connectome.json          name, citation, data use terms, grid (shape, affine), subjects and runs
  mask.nii.gz              brain mask on the connectome grid
  subjects/<id>.f16.npy    time series, voxel-major float16, each run demeaned and unit-norm
  subjects/<id>.norms.npy  norms of the runs (float32)
  derived/degree.npy       optional connectome degree map, for QA
  derived/degree.json      how the degree map was computed (written with it by qa degree)
```

Converters (Python API; tested on synthetic data):

```python
from LesionNetworkMappingLib import connectome

# Preprocessed 4D NIfTI time series on one mask grid, one file per run (the pattern must
# match only the time series); the regular expression groups the runs of each subject
connectome.from_nifti_timeseries("/data/rest/sub-*_bold.nii.gz", "/data/mask.nii.gz",
                                 "/data/MyConnectome.lnmc",
                                 runs_grouping=r"(?P<subject>sub-[A-Za-z0-9]+)",
                                 citation="...", data_use_terms="...")

# Lead-DBS fMRI connectome folder (dataset_volsurf.mat, dataset_info.json, vol/*.mat; MATLAB v5/v7 files)
connectome.from_leaddbs("/data/leaddbs/MyConnectome", "/data/MyConnectome.lnmc")

# Small synthetic connectome with planted networks, to try the tools (not real data)
connectome.make_synthetic("/tmp/Synthetic.lnmc")
```

Both converters can continue an interrupted conversion (`resume=True`). For the degree-similarity QA,
compute the degree map once (by default 1,000 random seed voxels over the first 100 subjects):

```console
PythonSlicer -m LesionNetworkMappingLib.qa degree --bundle /data/MyConnectome.lnmc
```

### Command line

```console example
PythonSlicer -m LesionNetworkMappingLib.engine \
  --bundle /data/MyConnectome.lnmc \
  --lesion sub-01_lesion.nii.gz --lesion sub-02_lesion.nii.gz \
  --out results --snapshot-every 50 --null-relocations 50 --run-json results/run.json
```

| Option | Meaning |
|---|---|
| `--bundle`, `--lesion` (repeatable), `--out` | Connectome bundle, lesion NIfTI files on any grid, output folder. Lesion images are binarized (unless `--probabilistic`) and resampled onto the connectome grid through their affines, as fractional voxel occupancy. |
| `--subjects N` [`--random` `--seed S`] | Use only N subjects, for a preview. |
| `--subject-list SPEC` | Use a named subset of the bundle, a text file of subject ids or a comma-separated list of ids. |
| `--snapshot-every K` | Write partial maps every K subjects. |
| `--null-relocations K` | Specificity QA: also map K rigid relocations of each lesion inside the brain mask, in the same pass (`--null-seed`, `--brain-mask`, `--null-voxels`, `--write-null`). |
| `--threshold-rule JSON` | Recorded with the run; maps are always written unthresholded. |
| `--parcels LABELS` | A parcel run instead of lesions: map every label of a label image as a seed (`parcel_fz.f16.npy`, `parcel_C_fz.npy`, `parcel_labels.json`). |
| `--accumulate welford\|mean` | `welford` (default for lesions) gives the AvgR, AvgRFz and T maps; `mean` (always used by `--parcels`) keeps float32 sums, with less memory and time, and gives AvgR and AvgRFz only. |
| `--split-half` | Also write the mean Fz maps of the odd and even subjects and report their spatial r (stability of the connectome sampling). |
| `--run-json FILE` | Run record: arguments, subject list hash, seeds, versions, times. |
| `--threads K` | BLAS threads (default 2). |

The engine prints one JSON object per line (`start`, `progress`, `snapshot`, `warning`, `done`, `error`)
and writes, for each lesion, `<name>_conn-<connectome>_desc-{AvgR,AvgRFz,T}_funcmap.nii.gz` (Lead-DBS
names; float32 on the connectome's grid, same shape and affine; the T map carries its degrees of freedom
in the NIfTI intent), and one `qa.json` for the run. `<name>` is the lesion file name without extension
or, for BIDS-style names, its entities without the suffix and without `space`, `res`, `den`, `label`
and `desc` (`sub-01_lesion.nii.gz` gives `sub-01`). `--help` lists every option.

To export a run with one JSON sidecar per lesion (N, degrees of freedom, threshold rule and its resolved
value, lesion checksum, QA, disclaimer), `provenance.json`, a generated `methods.md` and a
`reproduce.sh`:

```python
from LesionNetworkMappingLib import export
export.export_run("results", "results-export")  # partial=True exports the last snapshot of a stopped run
```

The other command lines of the library write the same job protocol (one JSON object per line) and
take `--threads`; most of them also take `--cancel-file`, and `cohortmaps`, `glm`, `slnm`, `cohort`, `nulls` and
`normalize` also take `--resume`, `--run-root` and `--parent-run` (`--help` lists them):

| Module (`PythonSlicer -m ...`) | What it does |
|---|---|
| `LesionNetworkMappingLib.cohortmaps` | Maps of a whole cohort in batches under a memory budget (`--budget-gb`), with a per-lesion cache (`--cache`), a `maps.tsv`, and `--dry-run` to see the plan |
| `LesionNetworkMappingLib.glm` | Voxelwise GLM with Freedman-Lane permutation inference: FWE and FDR p values, optional TFCE (`--maps`, `--participants`, `--design`, `--mask`, `--perm`, `--tfce`) |
| `LesionNetworkMappingLib.reference` | `build`, `estimate` and `compare` the reference maps of a connectome |
| `LesionNetworkMappingLib.transforms` | `warp`, `points` and `check` for template-family transform files |
| `LesionNetworkMappingLib.qa` | `degree`: the connectome degree map |
| `LesionNetworkMappingLib.normalize` | `run`, `batch`, `warp` and `check`: lesion-aware normalization to MNI (see [Normalize native-space lesions](#normalize-native-space-lesions)) |
| `LesionNetworkMappingLib.cohort` | Group maps of a cohort from `maps.tsv` or a study: overlap, sensitivity, legacy two-group maps, conjunction |
| `LesionNetworkMappingLib.slnm` | Symptom mapping (sLNM 2.0): partial r, Spearman, score permutation, leave-one-dataset-out, control scores |
| `LesionNetworkMappingLib.preflight` | Checks before a run (exit code 5 when something blocks), `--save` writes the report under `derivatives/qc` |
| `LesionNetworkMappingLib.lesionset` | Lesion-set diagnostics: overlap maps, pairwise Dice, volumes, tissue fractions, case and control balance |
| `LesionNetworkMappingLib.fetch` | `get`, `list` and `adopt` atlases, templates and priors of the catalogs (one entry at a time, checksum verified) |
| `LesionNetworkMappingLib.manager` | `convert` and `estimate` for the connectome manager |
| `LesionNetworkMappingLib.pipeline` | `init`, `plan`, `run`, `status` and `report`: the cohort pipeline (see [Automate a cohort analysis](#automate-a-cohort-analysis)) |
| `LesionNetworkMappingLib.nulls` | Cohort null models, exact or fast; `--estimate` reports the time without running |
| `LesionNetworkMappingLib.fastmap` | `build`, `parity` and `apply` the parcel index of the fast mode |
| `LesionNetworkMappingLib.validate` | Cross-validated validation of a circuit against baselines |
| `LesionNetworkMappingLib.robustness` | `sweep`, `replicate`, `splithalf`, `influence`, `seeds` and `consolidate` |
| `LesionNetworkMappingLib.figures` | Publication figures of maps and lesions (`--preset`) |
| `LesionNetworkMappingLib.leaddbs` | `import` Lead-DBS maps, `parity` with the reference lesions, `list` and `terms` |

Speed, measured on synthetic data with one BLAS thread: 0.08 s per subject for 1 lesion, 0.11 s for 10
lesions and 0.21 s for 50 lesions (230,000 voxels, 240 time points, in memory); 0.13 s per subject for
10 lesions read from a float16 bundle on disk (228,093 voxels, 2 runs of 120 time points). A full pass
over a large connectome reads the whole bundle, so disk speed matters: GSP1000 is about 126 GB as a
float16 bundle (computed from 1,000 subjects, about 216 time points and 292,019 voxels).

### Thresholds and QA

Thresholds are stored as rules and resolved from the number of subjects N and of voxels V each time
they are used, so a value that depends on N is never silently reused for another N. A connectome
preprocessed without global signal regression gets a note on the symmetric T rules:

| Rule | Resolves to |
|---|---|
| `fwe_bonferroni` (default, alpha 0.05) | Bonferroni-corrected T: \|T\| ≥ 5.27 at N = 1000, V = 292,019 |
| `t_convention` | T = 7, defined for N ≈ 1000; not applied below N = 900 unless overridden, with a warning when N differs by more than 10% |
| `fz_abs` | An effect size on the mean Fisher z |
| `custom_t` | A T value, with an optional reference N and then the same warning |
| `top_pct` | The top x% of the absolute values of the map (default 10%, on the mean Fisher z): an alternative that does not depend on N |

QA, following the critique by van den Heuvel et al. (2026):

- **Degree similarity:** Pearson and Spearman correlation between the T map and the connectome degree
  map. A high value means the map may reflect connectome hubs rather than lesion-specific connectivity.
- **Relocated-lesion null:** maps of the same lesion shape moved to random places in the brain, computed
  in the same pass; reports how similar the lesion's map is to them.
- **Reference similarity:** the similarity of the map with what every map of a connectome shares: its mean
  connectivity profile, its leading spatial modes and a hubness map, computed once per connectome
  (`LesionNetworkMappingLib.reference`) and shown as chips, calibrated against the relocated-lesion null
  when that was run.

Cohort statistics (overlap, sensitivity, Welch t, Liebermeister test, conjunction, the permutation GLM with
max-statistic FWE, and symptom mapping) are in the Cohort card of Map and also run from the command line
(`cohort`, `glm`, `slnm`). Cluster and peak tables of thresholded maps are in the Clusters card of Map
(`LesionNetworkMappingLib.clusters`). The study model is a plain-file one
(`study.json` plus a BIDS-style `participants.tsv`), which the Library's studies use.

### Normalize native-space lesions

A lesion traced on a patient's own (native) scan has to be in the connectome's MNI space before it can be
mapped. `LesionNetworkMappingLib.normalize` registers the scan to an MNI template and carries the lesion
across, in one command or one click. It runs BRAINSFit, BRAINSResample and BRAINSROIAuto, which ship with
every Slicer, as child processes of `PythonSlicer`; there is nothing to install.

**Method.**

- **Cost-function masking.** The lesion, dilated by 3 mm (`--dilate-mm`), is left out of the similarity metric
  (Brett et al., 2001; Andersen et al., 2010), so that the lesion does not pull the registration. An affine
  stage is followed by a BSpline stage (a grid of 8×8×8 at *Standard* quality, 14×10×12 at *Fine*; *Affine*
  stops after the first stage); the affine is kept when the BSpline lowers the match inside the brain by more
  than 0.02.
- **Tilt recovery.** When the first alignment is poor (NCC inside the template brain, minus the lesion, below
  0.40), it retries by itself: a head-to-head rigid registration against the whole-head template, then a
  seven-start rigid search. The 1.8 MB whole-head template is not bundled (see the limits below).
- **Two steps for other contrasts.** For FLAIR, T2w, DWI or CT, give the same subject's T1w (`--anat`): the
  scan is registered to the T1w first and the T1w to the template, and the final transform is one flattened
  composite.
- **The lesion in MNI space.** It is warped by linear interpolation onto the 1 mm grid of the template family,
  once as a fraction (`desc-lesionProb`, float32) and once binarized at 0.5 (`desc-lesion`, uint8), with the
  exact TemplateFlow header, so that it lies on the same grid as other files of that space. The scan is
  warped as well, for review (`desc-preproc`).
- **Reuse and safety.** A run whose inputs, template files, method and tool versions have the same
  fingerprint as a complete earlier result is reported as reused and not computed again (`--no-reuse` forces
  a rerun). The final files replace the earlier ones only after QC, so a failed or stopped run keeps the
  previous result, and a lock keeps two jobs out of the same folder (exit code 75). BRAINSFit samples at
  random and uses several threads, so a rerun with `--no-reuse` can differ slightly.

**Where results go.** With a study: `<study>/derivatives/normalization/sub-<id>/`. Without one:
`Documents/SlicerStudio/normalization/<name>-<hash>`, or the folder in `Studio/LNM/DataDirectory`
(`/data/SlicerStudio` in the web version). The folder holds the transforms (`.h5`), the lesion and the scan in
template space, the cost-function mask, `normalization.json` (inputs, fingerprint, tool versions),
logs, and the QC files `sub-<id>_desc-normqc.png` (a 3×3 mosaic: axial, coronal and sagittal through the lesion,
patient's left on the image left) and `sub-<id>_desc-normqc.json`.

**QC, and a person looks.** The metrics are the match inside the brain minus the lesion (NCC), the Dice of the
warped and template brain masks, the lesion volume change after dividing by the global brain-size ratio (so
that a small head is not flagged only for being small), the log-Jacobian in the lesion relative to the brain,
folding, and the share of the warped lesion outside the brain. From them the run gives a suggestion,
*looks-ok* or *review* with the reasons, which is never a verdict: image similarity alone missed a collapse of a
small lesion in the measurements behind it (NCC 0.774, as good as a good run), so the volume and Jacobian
checks are needed. A person then judges the alignment from the mosaic, or in the views (the template behind,
the warped scan in front at 50%, the lesion outline, `T` to flicker): *Looks right* or *Doesn't look right*
is stored as `norm_qc` (`pass` or `fail`) with the name in `Studio/RaterName` (default: the operating-system
user). **A normalized lesion whose `norm_qc` is not `pass` is left out of cohort analyses** (the cohort maps and
the preflight checks exclude it and say so).

**Measured accuracy and time** (MRHead, the 1 mm Slicer sample head, with synthetic chronic-stroke lesions of
45 mL and 3 mL; one run of each case on a shared 4-core virtual machine; synthetic lesions on one brain, not
patient data):

| Case | Result |
|---|---|
| Lesion masked (default), Standard quality | Dice with the same lesion carried by the registration of the lesion-free head: 0.94 (45 mL), 0.91 (3 mL); 41.5–45.4 s per subject |
| Lesion not masked (`--dilate-mm -1`, diagnostics only) | Dice 0.34 and 0.79; the 45 mL lesion shrank to 12.3 mL in MNI space against 61.7 mL when masked (normalized volume ratio 0.23 against 1.17), and the run is marked *review* |
| Head tilted by 20° and by 35° | The first alignment failed (NCC 0.13 and 0.05); the head-to-head rigid step (20°, second attempt) and the seven-start search (35°, third attempt) recovered it, Dice 0.996 and 0.995 with the untilted run; 72 s and 100 s; both marked *review* (the 35° one also for folding of 1.8%) |
| FLAIR with the same subject's T1w | *looks-ok*; 69 s |
| FLAIR alone | Affine only, NCC 0.07, marked *review* |
| Quality *Affine* / *Standard* / *Fine* | 22 s / 41.5–45.4 s / 83 s |

**Limits.**

- This build has no SyN (the ANTs registration is not built): BRAINSFit's affine and BSpline only.
- Without the same subject's T1w, a FLAIR, T2w, DWI or CT is registered to the T1 template by an affine only
  and is always marked *review*: the program's own message gives a measured error of 5–9 mm for this case.
- MNI152NLin2009cAsym (the bundled template) and MNI152NLin6Asym (the FSL grid of GSP1000) differ by a few
  millimeters. The template is chosen with the study's space or the connectome's family, and can be set in
  *Options*.
- A scan or a lesion whose header has left and right swapped cannot be detected by the normalization (the
  registration succeeds and the lesion lands on the wrong side). `lesionops.side_check` compares the traced
  hemisphere with a recorded `reported_side`, but nothing runs it for you yet; the preflight only reports the
  lesions that were mirrored with the Library's tools.
- **Templates are downloaded only when you click.** The 2 mm MNI152NLin2009cAsym brain and its mask come with
  the application. The whole-head template for tilted heads (1.8 MB) and the MNI152NLin6Asym 2 mm template
  (1.4 MB, under the FSL license terms, non-commercial) are fetched from TemplateFlow into the application
  cache after an explicit click on *Download…* in the options or in the message that offers it, checked by
  SHA-256, and never under `--testing`.
- The numbers above come from one image on one machine. No measurement on patient scans, or against a manual
  normalization, has been made.

**Command line.** The first command below normalizes a scan (the paths contain spaces); the second normalizes two
subjects of a study (without `--subjects`, every native subject with a scan and a lesion). One JSON object per
line is printed (on a terminal, text; `--events json` forces JSON). The exit code is 0 when done, 3 when
stopped, 4 when there is nothing to do (an empty lesion, a scan that is already in template space, no eligible
row) and 75 when the folder is locked.

```console example
PythonSlicer -m LesionNetworkMappingLib.normalize run --scan "My Study/sub-01_T1w.nii.gz" --lesion "My Study/sub-01_lesion.nii.gz" --out "My Study/derivatives/normalization/sub-01"
PythonSlicer -m LesionNetworkMappingLib.normalize batch --study "My Study" --subjects sub-02,sub-03
```

`warp` carries another native image across with the transforms of a finished run, and `check` tells whether
BRAINSFit and the templates are found. `batch` skips a row whose lesion was edited in template space unless
`--force` is given. It posts each subject's values to the study's inbox and never writes `participants.tsv` or
the `lesions` folder itself.

**In the application.**

- **Draw** (Studio): the *Normalize to MNI* card, with *Use for Map →*, *Normalize scan only* and *Update MNI
  lesion* (see [Drawing](#drawing)).
- **Map**, in the card under Seed (Studio and Classic): *Scan*, *Normalize to MNI* and *Options…* (template,
  quality, modality, the anatomical T1w, the lesion margin, the two *Download…* buttons, *Help it: rough
  manual alignment…* which starts the registration from a position you set with the Transforms module, and
  *Remember these options*, which stores them in `Studio/LNM/Normalize/LastOptions`). The scan and the
  lesion are exported in the background, the job runs with progress and *Stop*, and the result is loaded as
  the volume "*scan* (MNI152NLin2009cAsym)" and the segmentation "Lesion (MNI152NLin2009cAsym)" (made the
  seed), with the QC mosaic, the suggestion and the metrics, *Review in views*, *Load transform* and *Redo*.
  Editing the native lesion afterwards offers *Update MNI lesion* (a re-warp with the same transform) or, if
  the edit leaves the masked margin, *Re-register*.
- **Library** (Studio, and the Studio module panel in Classic): the Normalization card of a study. *Normalize N
  native subjects* runs `batch` as one queued background job, one subject at a time, with progress, an estimate,
  *Stop* and *Resume*, while you keep tracing. The list has a **Norm** column (*Native*, *Queued*, *Running
  40%*, *Review*, *Unreviewed*, *Pass*, *Fail*, *Stale*, *Needs scan*), the filters *Needs normalization* and
  *Normalization to review*, and *Review N →*, a queue in which `P` and `X` set `norm_qc`. The Subject card
  has the QC panel (*Looks right*, *Doesn't look right*) and a menu with *Review in Draw*, *Redo*, *Re-warp the
  edited lesion* and *Revert to native…*. *Normalize native lesions automatically after Done → next* is off
  by default and is stored in `study.json` (`normalization`: `auto_after_done`, `template`, `quality`,
  `dilate_mm`). *Stale* means that the native lesion changed after the normalization.

### Automate a cohort analysis

*Run everything* maps every included lesion of a study against a connectome that you have, runs the cohort analyses
with the defaults below, and writes the maps, the tables, a methods text, a provenance record and one report page. It
only orchestrates: each stage is the module that the cards of Map and the commands above already use, and the
pipeline computes no statistics of its own. It is a research tool; read [Disclaimer](#disclaimer).

**In the application.** Open the study in the Library, then *Map ▸ Seed: Cohort ▸ Run everything*. While the seed is a
cohort, the blue main button of the inspector reads "Run everything · N lesions" and `Ctrl+Enter` runs it; the Compute
card's button becomes *Maps only*. A run needs the connectome's data use terms to be accepted in the Connectome card
(the run never accepts them for you), and it refuses to start while another card of Map blocks Compute. The *Run
everything* card lists the stages with a symbol each (waiting, running, done, reused, skipped, failed), a progress bar
and the estimated time, which is shown before you start. The `⋯` button opens the options (extra threshold rules, the
number of permutations, TFCE, which score columns to use, the nulls, normalization, the atlas for cluster labels, the
memory budget and threads, *Keep the computer awake*); they are kept in `Studio/LNM/Pipeline/Options`. The run is a
separate process: *Stop* (a button, never `Esc`) ends it cleanly and keeps what is finished, and closing the
application stops it in the same way, so the next run resumes. A run that was started elsewhere (the command line,
another Slicer) is found and followed by the card. When it is done, the card has *Open report*, *Show folder*, *Load
maps* and *Warnings*, and the Library's *Analysis* card shows the state. Search finds "Run cohort analysis (lesion
network mapping)…", "Open last cohort report" and "Show cohort analysis folder".

**On the command line.** The first command makes a study from a folder of lesion masks and a table (the file names
carry the subject, as in `sub-01_lesion.nii.gz`; the table has `participant_id`, a `group` column with `case` or
`control` (or use `--group-column` and `--group-map`), a `space` column such as `MNI152NLin2009cAsym` unless the file
names say it (`space-MNI152NLin2009cAsym`), and your scores; `--score-columns` confirms the columns that are scores).
`init` only reads the files in place and refuses a study that is open in Slicer. The second command runs everything
with your connectome bundle (here a bundle called `GSP1000.lnmc` that you converted yourself, see
[Connectome bundles](#connectome-bundles-lnmc)). Paths with spaces need quotes.

```console example fixture
PythonSlicer -m LesionNetworkMappingLib.pipeline init --from lesions/ --participants scores.csv --study "My Study" --score-columns aphasia_score
PythonSlicer -m LesionNetworkMappingLib.pipeline run --study "My Study" --bundle GSP1000.lnmc --accept-terms
```

`--accept-terms` states that you have accepted the data use terms of the connectome; without it the command stops
with exit code 4 and writes nothing. The other commands:

```console example
PythonSlicer -m LesionNetworkMappingLib.pipeline plan --study "My Study" --bundle GSP1000.lnmc
PythonSlicer -m LesionNetworkMappingLib.pipeline status --out "My Study/derivatives/lnm/conn-GSP1000" --json
PythonSlicer -m LesionNetworkMappingLib.pipeline report --out "My Study/derivatives/lnm/conn-GSP1000"
```

`plan` shows what would be computed and the estimated time without running anything, `status` reads the progress of
a run, and `report` writes `report.html` again from the folder, with no computation. `--help` of each command lists
every option (`--preview N` for a preview on N connectome subjects, `--native skip`, `--nulls off`, `--strict`,
`--events text`, `--out`, `--recompute`). Exit codes: 0 done, 1 error, 3 stopped, 4 the terms were not accepted, 5
blocked by the preflight (see `plan`), 6 no lesion can be analysed, 75 another run holds the folder.

**What it computes, with the defaults as built.** The stages run in this order and a stage that does not apply is
skipped and says why:

1. *Prepare* (the lock, the terms, the plan, a copy of the inputs) and *Preflight* (the checks of Map's Preflight card).
2. *Normalize* native-space lesions to MNI space (`--native normalize`). Lesions whose normalization review is not
   *Pass* are left out, as everywhere (`--include-unreviewed` records an exception). The normalization took 41 to 45 s
   per lesion in its measurement (see [Normalize native-space lesions](#normalize-native-space-lesions)).
3. *Reference maps*, only with `--build-references` (about 15 to 25 minutes on a GSP1000-size connectome).
4. *Network maps* of all lesions in batches, with a per-lesion cache. Lesions that are excluded, empty, outside the
   connectome's mask or not in MNI space are listed, and the run goes on without them.
5. *Null models*: the relocation null of each lesion (50, 10 or none, by the size of the cohort), and the cohort null
   models. The exact pooled null runs only when its estimate is below 30 minutes (`--nulls-budget-min`); otherwise it
   is skipped, the run says so with the estimate, and the report shows "not run".
6. *Overlap and sensitivity* (90% of the lesions, over the cases when there are groups), *lesion-set diagnostics*,
   *specificity* (the general linear model with Freedman-Lane permutation, 5000 permutations, FWE as the primary
   result and FDR as the secondary one, lesion volume as a covariate; run when both groups have at least 3 subjects),
   *symptom maps* (sLNM) for each column that you confirmed as a score and that at least 10 subjects have, and the
   *conjunction*.
7. *QA and clusters* (the QA of the group maps; cluster and peak tables with labels from the atlases in the cache:
   nothing is downloaded), *export* (maps with sidecars, the methods text, the reporting checklist) and the
   *report*.
8. *Finish*: the study's inbox gets the new status ("mapped"); the session of the application merges it. The
   pipeline never writes `participants.tsv` itself.

The primary rule is FWE (Bonferroni, alpha 0.05) on the mean Fisher z by default. A threshold that does not suit the
connectome (for example the |T| ≥ 7 convention with a connectome of fewer than 900 subjects) is refused unless you ask
for it (`--allow-rule-anyway`), and the exception is recorded.

**Resume, Stop and the lock.** A stage whose inputs, settings and code are unchanged is reused without running; the
per-lesion maps are cached; and a child job continues from its own progress. After a *Stop*, a closed application or a
crash, run the same command again. In the checks of the work package, a run on a synthetic cohort that was stopped
during the maps recomputed 9 lesions and reused 3, a second identical run reused everything (1.3 s), and changing one
lesion recomputed that lesion only. One STOP file in the output folder cancels the whole run (the children included);
the application writes it when it quits. A lock in the output folder, with a heartbeat, keeps two runs apart (exit
code 75), and `init` and the application do not write a study that the other one has open.

**The output folder.** `<study>/derivatives/lnm/conn-<label>/` (with `_n-<N>` for a preview):

| Item | Contents |
|---|---|
| `report.html` | The report: one page that opens offline, with no scripts and no local paths, and 16 sections (header, summary, preflight, lesions, network maps, null models, specificity, conjunction, symptom maps, connectome-confound QA, clusters and peaks, lesion gallery, methods, reporting checklist, how to reproduce, files), with the research-use text at the top and at the bottom |
| `pipeline.json` | The plan, the settings, the state of every stage, the warnings (paths relative to the folder) |
| `reproduce_pipeline.sh` | The command that repeats the run. It stops at the terms unless you set `ACCEPT_TERMS=1`. It, `provenance.json` and `reproduce.sh` hold local paths on purpose |
| Per-lesion maps | `<sub>_conn-<label>_desc-{AvgR,AvgRFz,T}_funcmap.nii.gz` and `<sub>_conn-<label>_lnm.json`. They are **copies** of the cache, not links, so that editing one cannot change the cache or another run |
| Group maps | `group_conn-<label>_desc-<name>_map.nii.gz` with a JSON sidecar each: lesion count and percent, overlap, sensitivity, the null maps, the T, Hedges g, p (uncorrected, FWE, FDR) maps of the contrast, the sLNM maps, the conjunction |
| Tables and text | `peaks.tsv`, `clusters_<name>.tsv` and `.html`, `qa.json`, `lesionset.json`, `checklist.md`, `methods.md`, `.html` and `.txt`, `provenance.json` |
| `inputs` | Byte copies of the lesion masks that were analysed (in `inputs/lesions`) |
| `stages`, `.pipeline` | The folders of the stages, the lock, the progress, the plan and the log |

The files that are meant to be shared hold paths relative to the folder. Every sidecar, the report and the scripts
carry the research-use text of [Disclaimer](#disclaimer). The classic overlap map is labelled in its sidecar as
"Classic overlap map (hub-inflated); see the null models for a null-referenced version", and the report shows the
false-positive rate of the classic rule next to it when the null models ran, and "not run" when they did not.
Overlap and sensitivity maps by themselves do not show that a network belongs to a symptom.

**How long it takes.** Measured here only on synthetic data, never on a real connectome. A complete run on the
synthetic fixture of the checker (12 lesions, a 20-subject synthetic connectome of 3,752 voxels, nulls and report
included) took 7.5 s; the report of 40 synthetic lesions takes 2.2 s to render (1.2 MB) and that of 300 lesions is
4.8 MB. The permutation test takes 10.6 ms per permutation at 292,019 voxels (3 threads), so 5000 permutations take
about 53 s for the contrast and for each score. The engine takes about 0.13 s plus 4.2 ms per lesion per connectome
subject at that size (2 threads); the estimate that `plan` and the card show uses these numbers and agreed with the
measured time within 0.8 to 1.0 on the synthetic runs it was checked against. For a connectome of GSP1000's size
(1,000 subjects, about 126 GB) it gives about 5 minutes for a pass of 40 lesions and about 9 minutes for a pass of 100
(the engine's time at 2 threads; reading the 126 GB at an assumed 400 MB/s alone takes about 5 minutes, so the disk sets
the lower limit). For 40 lesions, the exact pooled null would take about 53 minutes by its own
estimate, which is why `--nulls auto` skips it above 30 minutes. The normalization is estimated at 31 s per lesion but
measured at 41 to 45 s, so that part of an estimate is low. Treat every estimate as an order of magnitude.

**Limits.** No run on GSP1000 or any real connectome has been made, and no result of this pipeline has been compared
with a published analysis. *Keep the analysis up to date* is not part of this build (a changed lesion is found by the
Library's Analysis card, which says "3 new or changed lesions", and you run again). The message that a skipped exact
null "can be run from Map ▸ Nulls" points to the Nulls card (see [Research tools of wave 5](#research-tools-of-wave-5)). The pipeline has not been run on macOS or Windows.

### Data and licensing

- **No connectome is bundled with Slicer Studio, and none is downloaded automatically.** You can obtain the data
  yourself and convert it once (the synthetic demo of Map is generated on request and is not real data), or use
  *Get a connectome…*: it downloads a connectome only when you ask and have accepted the provider's terms as they are
  shown live, and sources that state no terms stay links. The catalog (`catalog.py`) lists each connectome with its
  licence badge, subset choices and a disk plan, and `connectome.from_tar_stream` converts an archive of the GSP or the
  Lead-DBS layout without writing it to disk again (tried on synthetic archives and a local test server only).
- **GSP1000** (Brain Genomics Superstruct Project, 1,000 subjects) is a widely used normative
  connectome for LNM. It is distributed on Harvard Dataverse as the *GSP1000 Preprocessed Connectome*
  ([doi:10.7910/DVN/ILXIKS](https://doi.org/10.7910/DVN/ILXIKS), about 236 GB). Access requires a
  Dataverse account and accepting the GSP Open Access Data Use Terms. Use and share it, including any
  converted bundle, only under those terms, and keep its citation and terms in the bundle
  (`citation=`, `data_use_terms=`).
- Templates and atlases: see [License and third-party notices](#license-and-third-party-notices).

### Disclaimer

Every sidecar, `provenance.json` and `methods.md` carries this text:

> Research use only — not for clinical decisions. Lesion network mapping specificity is debated
> (van den Heuvel et al., 2026, Nat Neurosci, doi:10.1038/s41593-025-02196-7;
> Siddiqi et al., 2026, bioRxiv, doi:10.64898/2026.02.24.707529;
> Zalesky & Cash, 2026, Nat Neurosci, doi:10.1038/s41593-026-02259-3).

### References

- Boes AD, Prasad S, Liu H, et al. Network localization of neurological symptoms from focal brain
  lesions. *Brain*. 2015;138(10):3061-3075. [doi:10.1093/brain/awv228](https://doi.org/10.1093/brain/awv228)
- Fox MD. Mapping symptoms to brain networks with the human connectome. *N Engl J Med*.
  2018;379(23):2237-2245. [doi:10.1056/NEJMra1706158](https://doi.org/10.1056/NEJMra1706158)
- van den Heuvel et al. *Nat Neurosci*. 2026.
  [doi:10.1038/s41593-025-02196-7](https://doi.org/10.1038/s41593-025-02196-7). A critique: it reports
  that many published lesion network maps carry a trace of connectome degree and that randomly placed
  lesions give similar maps.
- Siddiqi SH, Horn A, Schaper FLWVJ, et al. The methodological foundations of lesion network mapping
  remain sound. *bioRxiv*. 2026.
  [doi:10.64898/2026.02.24.707529](https://doi.org/10.64898/2026.02.24.707529). A response to the
  critique above.
- Zalesky A, Cash RFH. Null models for lesion network mapping. *Nat Neurosci*. 2026;29:1025-1028.
  [doi:10.1038/s41593-026-02259-3](https://doi.org/10.1038/s41593-026-02259-3).

## Status and roadmap

The work is organized in five waves of work packages (identifiers such as W1-03 or A-01), plus
packaging. "Done" means written and verified with this repository's tests, benchmarks, screenshots and
compatibility check; nothing is released yet.

### Done

| What | Work packages | Where it is checked |
|---|---|---|
| Benchmark, screenshot and test tools; fork defaults; bundled MNI templates | W1-01 | `StudioBench.py`, `StudioScreens.py`, `StudioTestRunner.py` |
| Dark theme, accent color, density and the Appearance settings | W1-02 | `StudioTest` |
| Studio interface: top bar, workspaces, Studio layouts, Classic switch, inspector header | W1-03 | `StudioTest` |
| Draw and Level tracing *Add* / *Subtract*, lasso, Islands keep-largest, Margin and Smoothing on the region around the segment | W1-04 | Segment Editor tests, `StudioBench.py` |
| Faster masked painting and per-stroke 3D surface rebuild | W1-05 | `StudioBench.py` |
| Faster slice updates | W1-06 | `StudioBench.py`, pixel comparison of rendered views |
| `LesionNetworkMappingLib`: thresholds, QA, study model, lesion writer, export with provenance | W1-07 | LNM unit tests |
| Library page, direct loading, Recent, bundled MNI template | W1-08 | `StudioTest` |
| Portable layouts and the cross-version file round trip | W1-09 | `SlicerFileCompatibilityCheck.py`, `StudioLayoutPortabilityTest` |
| Web browser launcher with a Docker image | P-01 | built and run locally |
| macOS disk image script and install guide | P-02 | built locally; start-up test on macOS 15 |
| Draw keeps its tool and segment through Library and module round trips; Paint armed on entry; `Ctrl+Y`; *Save lesion…* (`Ctrl+Shift+S`, `Ctrl+Enter`) on the lesion's own grid; files that are already open are not loaded twice | Navigation quick wins | `StudioTest` |
| `Esc` steps back one state at a time; focus mode exit button; tab re-click shows the inspector; `Ctrl+\` for the inspector; Classic back to Studio returns to the workspace; View > Toolbars honoured; first-run window size; stable inspector width with a remembered width per workspace | Navigation quick wins | `StudioNavigationTest` |
| Orientation letters on slice views; ranked module search with synonyms | Navigation quick wins | `StudioOrientationTest` |
| Qt 6 fixes: Studio start-up, flag and widget handling in Studio and the Segment Editor, Library panel detection, macOS bundle checks | Qt 6 fixes | start-up tests of the packages |
| Segment names, colors and terminology kept when a `.seg.nrrd` is opened; a scene or segmentation opened from the Library is shown in Draw | Wave 1 integration fixes | `StudioTest` |
| Autosave and crash recovery: incremental scene autosave to a private folder, recovery list, Autosave settings, retention | A-01 | `StudioAutosaveTest`, `StudioAutosaveCrashCheck.py`, `SlicerFileCompatibilityCheck.py --autosave` |
| Smart fill, the click-to-segment tool (assisted tracing) | A-02 | `SmartFillAlgorithmTest`, `SegmentEditorSmartFillTest` |
| Flat controls, Inter font bundled and used for the interface and the views, theme-aware 3D view defaults | W2-01, W2-02 | `StudioTest`, screenshot review (`StudioScreens.py`) |
| Flat view bars with the slice index | W2-03 | `StudioTest`, `StudioTopBarTest`, screenshot review |
| Segment Editor widget UX (key tooltips, masking summary, compact rows, brush limits); Threshold and Fill between slices fixes | W2-04, W2-07 | Segment Editor tests, `StudioBench.py` |
| Streamlined Draw panel: lesion card, labelled tool tiles, brush sizes, letter keys, lesion navigation keys, Display card | W2-05 | `StudioTest`, `StudioNavigationTest`, usability gate |
| Map workspace (Map v1): seed, connectome, compute, result with threshold rules, QA, export | W2-06 | `LesionNetworkMappingTest` (module self-test) |
| Slice navigation core: `PageUp`/`PageDown`, `Home`/`End`, clamped stepping, symmetric zoom, focus follows the mouse, left-drag window/level | W3-02a | `StudioNavigationTest` |
| Command palette (`Ctrl+K`), shortcut list (`Ctrl+/`), in-view probe | W3-06 | `StudioPaletteTest` |
| Guided tours and the *Where did it go?* list | T-01 | `StudioTourTest` |
| Usability work: `Esc` ladder, back button, mode pill, Library that shows the current work, top bar tooltips and focus, import routes that never hijack the views | U-01, U-02, U-04, U-06, U-07 | `StudioNavigationTest`, `StudioModesTest`, `StudioLibraryTest`, `StudioImportTest`, `StudioTopBarTest` |
| Usability regression gate: the walkthrough tasks re-run headless, with action counts | U-05 | `StudioUsabilityGateTest`, `StudioUsabilityGate.py` |
| LNM foundations: study schema v2, job protocol, connectome provenance, threshold rules v2, space registry, design builder, GLM permutation engine, cohort maps, reference maps, QA v2, Map plug-in framework | L-01 to L-11 | LNM unit tests |
| Wave 2 integration fixes: the lesion is never overwritten when you start on the template again, exhaustive permutation orientations in the design builder, flat view bars on by default, faster scrolling and workspace switching, Inter embedded once, mode pill and toast placement, a compact top bar, the Map demo built off the main thread | W2-I1, W2-I2, W2-I3 | `Studio*Test` suites, `StudioBench.py`, usability gate |
| Studio autosave status and recovery card (top bar, Library card, inspector notice, *Reopen study at*) | A-01b | `StudioAutosaveTest`, `StudioAutosaveCrashCheck.py --studio` |
| Library study loop: study folder, subject list, *Done → next*, `Alt+Left` and `Alt+Right`, crash-safe journal, QC, batch actions | W3-07 | `LesionNetworkMappingStudyTest`, `StudioNormalizeTest`, a headless walkthrough that is not in the repository |
| Lesion-aware normalization to MNI: command line (`run`, `batch`, `warp`, `check`), Draw and Map cards, Library batch with the `norm_qc` review, QC numerics | A-03, A-03b, A-03c, L-21 | `LesionNetworkMappingNormalizeTest`, `LesionNetworkMappingNormQcTest`, `LesionNetworkMappingNormalizeUITest`, `StudioNormalizeTest`; official 5.12.4 and 5.13 load the results |
| Map: cohort mode with the Overlap, Contrast and Symptom tabs, connectome manager, Studio layers, status chip and map browsing | W3-08, L-20 | `selftest LesionNetworkMapping`, `StudioTourTest`, `StudioImportTest` |
| LNM numerics: symptom mapping (sLNM 2.0), intake checks, lesion-set diagnostics, fetch and atlas labels, engine v2 (parcels, split-half), preflight, reporting v2, cluster tables, figure-fit numerics | L-12 to L-19, L-22 | LNM unit tests (`LesionNetworkMappingSlnmTest`, `LesionNetworkMappingLesionOpsTest`, `LesionNetworkMappingLesionSetTest`, `LesionNetworkMappingFetchTest`, `LesionNetworkMappingEngineV2Test`, `LesionNetworkMappingPreflightTest`, `LesionNetworkMappingReportingTest`, `LesionNetworkMappingClustersTest`, `LesionNetworkMappingFigureFitTest`) |
| Thin sliders and scroll bars, flat combo and spin boxes, progress bars, tabs and splitters in the application style | W3-01 | `StudioTest`, `StudioTopBarTest`, `StudioModesTest`, `StudioBench.py` |
| Main window hooks: shortcuts without a menu bar, focus mode, stable panel width, one deferred state restore; hover focus in 3D views and the *Navigation in Studio* settings | W3-03, W3-02b | `StudioNavigationTest`, `StudioTest`, `StudioBench.py` |
| Data loading: no series grouping for 3D NIfTI, lesion, mask and roi names as labelmaps, lazy *Add Data* options | W3-04 | the timing script of the work package (not in the repository), `StudioImportTest` |
| Brush-size display, `[` `]` keys, hover that renders one view, faster strokes | W3-05 | the work package's script (not in the repository), Segment Editor self-test |
| Accessibility and first run: focus frames, AltGr guard, live text size, narrow windows, the research-use line, the close prompt | U-03 | `StudioAccessibilityTest` |
| Faster Draw and Scissors apply, cheaper undo snapshots with shared surfaces, undo and redo that skip unchanged segments | W4-01 | `StudioBench.py` (`draw_apply_ms`, `undo_surfaces_ms`), the work package's history and memory scripts (not in the repository); official 5.12.4 and 5.13 load the segmentations |
| Library grid with background lesion thumbnails | W4-02 | the work package's grid script (not in the repository); `StudioLibraryTest` covers the Library page but not the study list or grid |
| Lazy Python wrappers, `Studio/LazyPythonWrappers` | W4-03 | `StudioBench.py --startup`, the work package's sweeps of every module and of the self-tests (not in the repository) |
| Linker option `Slicer_USE_LINKER_AS_NEEDED` (no measurable effect on this toolchain) | W4-04 | the work package's comparison of the needed libraries (not in the repository) |
| User guide pages for Studio and lesion network mapping | W4-05 | the work package's check of links, keys and object names (not in the repository) |
| Web build: file dialogs in `/data`, lesion network mapping limits, smoke check | P-03 | `SlicerStudioSmokeCheck.py`; not run with Docker Desktop |
| *Run everything* cohort pipeline and command line (`init`, `plan`, `run`, `status`, `report`) | A-04 | `LesionNetworkMappingPipelineTest`; the checker's `--run-examples` on a synthetic fixture; official 5.12.4 and 5.13 load the maps |
| HTML report of a pipeline run | A-04b | `LesionNetworkMappingReportTest` |
| *Run everything* card in Map and *Analysis* card in the Library | A-04c | `LesionNetworkMappingPipelineUITest`, the usability gate (see [Tests](#tests)) |
| In-app updates and `make_release.py` | A-08 | `StudioUpdatesTest`; a local test feed, never a real release |
| LNM research workbench, Library and Draw: lesion checks and 2D extrusion (L-28), tracing from a figure (L-29), Columns card and cohort diagnostics with lesion overview (L-31) | L-28, L-29, L-31 | `StudioTest` (figure), the work packages' scripts (not in the repository); official 5.12.4 and 5.13 load the figure scene and the overlap scene |
| LNM research workbench, Map: QA card v2 and Preflight card (L-30), Clusters and Networks cards (L-32) | L-30, L-32 | `selftest LesionNetworkMapping` and the work packages' scripts (not in the repository) |
| LNM research workbench, numerics: publication figures (L-23), fast parcel index (L-24), cohort nulls (L-25), cross-validated validation (L-26), robustness (L-27), connectome catalog and tar-stream converter (L-33), Lead-DBS import and verification (L-34) | L-23 to L-27, L-33, L-34 | `LesionNetworkMappingFiguresTest`, `LesionNetworkMappingFastmapTest`, `LesionNetworkMappingNullsTest`, `LesionNetworkMappingValidateTest`, `LesionNetworkMappingRobustnessTest`, `LesionNetworkMappingTarStreamTest`, `LesionNetworkMappingLeadDbsTest` |
| Privacy and settings checkers; scrubbed normalization logs; the G3 re-check fixes | W4-I0, G3b | `StudioPrivacyCheck.py`, `StudioSettingsCheck/run_settings.sh` |
| Slicer Studio folder: first-start question, projects saved as you go in a Projects and Studies layout, project cards with thumbnails in the Library, projects that open in official 3D Slicer | A-09 | `StudioProjectsTest`, `SlicerFileCompatibilityCheck.py --projects` (2026-10-24, PASS), the privacy check |
| Your own MNI templates: add, check, set as default, start on, use in Draw, Map, Normalize and studies | A-10 | `StudioTemplatesTest`, `LesionNetworkMappingTemplatesTest`, the compatibility check with a template project (2026-10-25, PASS) |
| Visual redesign: tokens, Library, top bar, settings, Search, dialogs, Draw and Map panels, orientation cue, Map legend and layer chips, region-named peaks | V-01, V-02, V-03 | `StudioVisualTest`, `StudioOrientationTest`, `LesionNetworkMappingPeakRegionsTest`, screenshots (`StudioScreens.py`), `StudioBench.py` |
| *Get a connectome…*: catalog dialog, live terms acceptance, streamed and resumable download | L-35 | `LesionNetworkMappingFetchConnectomeTest` (a local test server), the work package's scripts (not in the repository); no GSP1000 archive was downloaded |
| Map Validate, Robust and Nulls cards; fast index builder with parity gate | L-36, L-37, L-38 | the work packages' scripts (not in the repository) and the library tests of earlier work packages |
| Analysis recipes, plan lock, deviations log and a-priori ROI analysis | L-39 | `LesionNetworkMappingRecipesTest` |
| Report card, battery strip and publication bundle | L-40 | the work package's scripts (not in the repository) |
| Circuits card: re-seeding, damage scores, target scoring | L-41 | `LesionNetworkMappingCircuitsTest` |
| Control-lesion sets | L-42 | `LesionNetworkMappingControlSetsTest` |
| Review workflow with approval gate and blind tracing | L-43 | `LesionNetworkMappingReviewTest` |
| Blind second tracing, mask-level agreement and adjudication | L-44 | `LesionNetworkMappingAgreementTest` |
| Draw case strip, trace queues, case series and prefetch | L-45 | the work package's scripts (not in the repository) |
| Share-safe BIDS-derivatives and NeuroVault export | L-46 | `LesionNetworkMappingShareTest` |
| `slicer-lnm` headless twin: wheel, BIDS App entry point, container recipes, disk image wrapper | L-47 | `LesionNetworkMappingAppTest`; the wheel was built and run in a clean environment; the container images were not built and the disk image wrapper was only dry-run |
| Pipeline calibration card | L-48 | `LesionNetworkMappingCalibrateTest` |
| Coordinate, atrophy and stimulation seeds | L-49 | `LesionNetworkMappingCoordsTest` |
| VLSM tab and *Seed from VLSM cluster* | L-50 | `LesionNetworkMappingVlsmTest` |
| Literature sources and PRISMA counts | L-51 | `LesionNetworkMappingSourcesTest` |
| Reference-map library and Compare card | L-52 | `LesionNetworkMappingRefmapsTest` (a local test server) |
| Tutorial cohorts | L-53 | `LesionNetworkMappingTutorialsTest` (a local test server) |
| User guide pages for statistics, data and intake; wider lazy library exports; `StudioBench.py` lesion network mapping scenarios | L-54 | `StudioBench.py` (the lesion network mapping scenarios); the pages themselves have no test |

### Roadmap

The waves are listed with the identifiers of their work packages. Titles are shortened. All five waves are done; the
open work is the list of known limitations below.

| Wave | Contents | State |
|---|---|---|
| Wave 1: foundation | W1-01 to W1-09 (see *Done*) | Done |
| Packaging | P-01, P-02, P-03 (see *Done*) | Done |
| Wave 2: streamlined editor, navigation, autosave, Smart fill, Map v1, LNM foundations | A-01, A-02, L-01 to L-11, U-01 to U-07, W2-01 to W2-07, W3-02a, W3-06 and the integration fixes W2-I1 to W2-I4 (see *Done*) | Done |
| Wave 3: study loop, normalization, cohort statistics | W3-01 thin sliders and scroll bars; W3-03 main-window hooks and shell speed; W3-04 NIfTI loading fixes; W3-05 brush size display and paint speed; W3-07 Library study loop (subject list, switching); W3-08 cohort statistics UI, connectome manager, Map integration; W3-02b navigation settings UI; U-03 accessibility and first-run/close flow; A-01b autosave status and recovery card in Studio; A-03, A-03b, A-03c one-click normalization to MNI for a lesion and for a study; L-12 to L-22 research numerics and the cohort Contrast and Symptom tabs (see *Done*) | Done |
| Wave 4: cohort pipeline, report, performance, user guide | A-04, A-04b, A-04c *Run everything* cohort pipeline, HTML cohort report and its cards; A-08 in-app updates; W4-01 faster Draw and Scissors apply and cheaper undo; W4-02 Library grid with thumbnails; W4-03 lazy Python wrappers; W4-04 linker option `--as-needed` for packaged builds; W4-05 user guide pages; P-03 web build; L-23 to L-34 and W4-I0 the LNM research workbench (figures, fast parcel index, nulls, validation, robustness, lesion checks and 2D extrusion, tracing from a figure, QA, preflight, clusters, networks, Library columns and cohort diagnostics, connectome catalog and tar-stream converter, Lead-DBS import and verification) | Done |
| Wave 5: Slicer Studio folder, templates, visual redesign, LNM research features | A-09, A-10, V-01 to V-03 and L-35 to L-54 (see *Done*) | Done |

The automation work, by feature:

| Feature | Work packages | State |
|---|---|---|
| Autosave and crash recovery | A-01 | Implemented |
| Autosave status and recovery card in Studio | A-01b | Implemented |
| Smart fill, assisted tracing | A-02 | Implemented |
| One-click normalization of native-space scans to MNI space: command line; card in Draw and Map; Library batch with a review | A-03, A-03b, A-03c | Implemented |
| *Run everything*: the cohort pipeline from lesions to an HTML report, in the application and on the command line | A-04, A-04b, A-04c | Implemented |
| In-app updates from the public releases (none published yet) | A-08 | Implemented |
| Slicer Studio folder and projects saved as you go | A-09 | Implemented |
| Your own MNI templates | A-10 | Implemented |
| Visual redesign of the Studio screens, the slice view cues and the Map overlay | V-01, V-02, V-03 | Implemented |
| LNM research workbench of wave 5: *Get a connectome…*, Validate, Robust, Nulls, Circuits, Compare, Calibrate and Report cards, recipes and plan lock, review and blind second tracing, case strip, control sets, coordinate seeds, VLSM, literature sources, share-safe export, tutorial cohorts, the `slicer-lnm` command | L-35 to L-54 | Implemented |
| LNM research workbench of wave 4: lesion checks, tracing from a figure, QA, preflight, clusters and networks cards, Library columns and cohort diagnostics, nulls, validation, robustness, figures, connectome tools | L-23 to L-34 | Implemented |

Connectomes are never bundled. A connectome is downloaded only when you ask for it (*Get a connectome…*) and
have accepted the provider's terms. Templates are downloaded only when you ask.

Known limitations today:

- **No real connectome has been used.** Every test, measurement and example of the lesion network mapping code ran
  on synthetic data (the synthetic connectome and GSP-like synthetic output folders): nothing, including the time of
  *Run everything* on a full-size connectome, has been measured on GSP1000 or compared with a published analysis. The
  run times of a full-size connectome in this README are estimates from the per-subject timings. Map needs a
  connectome that you provide (or the synthetic demo, which is not real data) and a lesion in the connectome's MNI
  space (or normalized, see below).
- **Wave 5 research tools** were checked with synthetic data, small fixtures and local test servers. No GSP1000 archive was
  downloaded or converted, so the time, the disk plan and the resume of *Get a connectome…* on 236 GB have not been
  seen; the tutorial cohorts, the reference-map library and the Sources metadata lookups were run against local test
  servers, not the real sites. The Validate, Robust, Nulls and Report cards have no automated test of their own in the
  repository (the work packages used scripts that are not in it). The Calibrate card measures the configured analysis
  on your lesions: it does not make an analysis correct. No statistic of a card was compared with a published analysis.
  The Library filter on 1000 subjects is slightly over its time budget in `StudioBench.py`.
- **The headless twin** (`slicer-lnm`): the wheel was built and run in a clean environment, but the Docker and Apptainer
  recipes were not built, and the disk image wrapper was checked in a dry run, not on a Mac. A run in the application and
  a `slicer-lnm` run give the same files within numerical tolerance, not byte for byte.
- **Projects and templates.** The Slicer Studio folder, the project menu and the thumbnails were run on Linux with Qt 5.15
  only: not on macOS, on Windows, in a cloud-synced folder or in the web version under Docker. A user template is
  checked from its header only (a wrong header is not detected), and the tests use small synthetic templates.
- **Look.** The redesign was checked by screenshots (1920x1080 and 1366x768, both themes) and tests on Linux, not on
  macOS (Qt 6); the Map legend and chips are Qt widgets laid over the Red view, so they follow its position but are
  not part of a screenshot taken by Slicer's own view capture. Hovering over a slice view costs more in some painting
  states of Slicer core (a Green view re-render was seen on every move), also with the Studio interface switched off;
  this was not fixed.
- ***Run everything*** does not keep an analysis up to date by itself (the Library's *Analysis* card says when lesions
  changed, and you run it again). Closing the application stops a run, which then resumes. It has not been run on
  macOS or Windows, and the estimates of the normalization stage are low (31 s assumed, 41 to 45 s measured).
- **Normalization** uses BRAINSFit (no SyN in this build). It was measured on one head with synthetic lesions,
  on one machine (41.5 to 45.4 s per subject at *Standard* quality), not on patient scans. A FLAIR, T2w, DWI or
  CT without the same subject's T1w is unreliable (5 to 9 mm of error was measured in the work package) and a
  left-right swap in a header cannot be detected. A person must review each result (`norm_qc`) before it
  enters a cohort. The 1.8 MB whole-head template (tilted heads) and MNI152NLin6Asym are downloaded only when
  you click. Not yet tried on macOS or in the web version.
- **Tracing from a figure** works on the MNI template only (on a scan in scan space a figure stays a reference), and
  the fit's residual measures the consistency of your landmarks, not how well the traced lesion matches the lesion of
  the paper: no measurement of that accuracy has been made. The web version has no image clipboard and no drop.
- **Lesion checks and diagnostics** are prompts. The side check needs a reported side, the duplicate pairs of the
  lesion-set check are not kept when a study is reopened, and the tissue priors need one download that
  you start. Draw still writes the `Notes` and `SourceReference` of the lesion sidecar, which the *Case* section can
  move into the columns of the study.
- **Studies** have a subject list and a grid. The grid was measured on one synthetic study of 300 subjects. The study
  session assumes one Slicer per study folder (a heartbeat file marks an open study, `pipeline init` refuses a study
  that is open, and `participants.tsv` is written under a lock); two Slicers editing the same study have not been
  tested.
- **Smart fill** needs visible contrast between the lesion and its surroundings. There is nothing to find
  on the bare MNI template: use the patient's scan as the source volume. Lesions with grey-matter-like
  intensity on T1 images are ambiguous, and a click near the edge of a lesion can give a different
  region than a click well inside it. It is assisted tracing: review every result.
- **Autosave** is not Save. Modified data above 2 GB is not autosaved, very large modified data is
  autosaved at most every 10 minutes, and an autosave may contain copies of patient data (so may the journal
  of a study, which is inside the study folder). The Windows folder and the web version (stopping the
  container) have not been tried.
- **In-app updates** have no release to find, were tested with a local test feed and fixtures only, and have not
  been tried on macOS (the installer swaps the application bundle).
- The `[` and `]` keys resize the Paint brush in Studio's Draw panel, which differs from official Slicer, where they
  cycle the volumes of the slice view. In Classic they keep the official meaning, and under `--testing` as well;
  `Studio/BracketKeysResizeBrush` written as `true` or `false` overrides this. Its settings key has no settings page,
  and the shortcut list (`Ctrl+/`) shows the `[ ]` row unless the key is `false`.
- **Tests.** No automated test in the repository covers the Library grid, the Columns and cohort diagnostics cards,
  the lesion checks, the QA, Preflight, Clusters and Networks cards or the faster undo: the work packages checked
  them with scripts that are not in the repository, besides the existing suites.
- Waves 2 to 5 were run on Linux with Qt 5.15 only. The AltGr guard of text fields is written for Windows and has not
  been tried there, and nothing since the first disk image has been run on macOS (Qt 6). The complete compatibility
  check was last run on 2026-10-22 (see [File compatibility](#file-compatibility-with-official-3d-slicer)).
- The Draw inspector width is chosen from what the tools need (up to 600 px, remembered), so it can be
  wider than the 340 px default after you have used the tools.
- The *System* theme follows the light or dark setting of the operating system on Windows and with Qt 6.5
  or newer, and the desktop palette with Qt 5 on Linux (light when that is unknown). It has not been
  tried on macOS.
- Slicer Studio has been run on Linux with Qt 5.15. The macOS packages (Qt 6) passed the start-up test
  on macOS 15 with Apple Silicon only, on a disk image built before wave 2: the features of waves 2 to 5
  have not been run on macOS.

## Development

### Where things are

| Path | Contents |
|---|---|
| [Modules/Scripted/Studio](Modules/Scripted/Studio) | Studio interface (`StudioLib/`): shell, top bar, workspaces, layouts, Library page, importer, downloads, Draw (`draw.py`, `lesioncard.py`), Search (`palette.py`, `finder.py`), shortcut list (`cheatsheet.py`), in-view probe (`probe.py`), mode pill (`modes.py`), orientation letters, guided tours (`tour.py`, `tours.py`, `migration.py`); the `Studio*Test` suites in `Testing/Python` |
| [Modules/Scripted/Studio/StudioLib](Modules/Scripted/Studio/StudioLib) | Autosave and recovery: `autosave.py` (the service, menu actions), `autosave_io.py` (writing of autosave scenes), `recovery.py` (scan, recover, discard, recovery window), `autosave_settings.py` (the settings panel), `autosave_ui.py` (the top-bar status, the Library recovery card and the inspector notice) |
| [Modules/Scripted/Studio/StudioLib](Modules/Scripted/Studio/StudioLib) | Studies: `study_ui.py` (the session: open, *Done → next*, inbox, `shell.studySession`), `journal.py` (the crash-safe journal), `library.py` (subject list, inspector cards, and the `libcard_*.py` extension point); `libcard_normalize.py` (the Library batch normalization, the Norm column and the review); `drawcard_normalize.py` (the Draw card; `drawcard_*.py` is the extension point of Draw); `mappage.py` (Map layers, status chip, map browsing); `a11y.py` and `firstrun.py` (accessibility, the research-use line, the close prompt) |
| [Modules/Loadable/Segmentations/EditorEffects/Python/SegmentEditorEffects](Modules/Loadable/Segmentations/EditorEffects/Python/SegmentEditorEffects) | Smart fill: `SegmentEditorSmartFillEffect.py` (the effect) and `SmartFillAlgorithm.py` (the computation, numpy and VTK only) |
| [Modules/Scripted/LesionNetworkMapping](Modules/Scripted/LesionNetworkMapping) | `LesionNetworkMappingLib` and its unit tests, the Map module (`LesionNetworkMapping.py`) and its card plug-ins (`LesionNetworkMappingUI/`) |
| [Modules/Scripted/LesionNetworkMapping/LesionNetworkMappingLib](Modules/Scripted/LesionNetworkMapping/LesionNetworkMappingLib) | Wave 3 numerics: `normalize.py` (lesion-aware normalization, command line) and `normqc.py` (its QC), `slnm.py` (symptom mapping), `cohort.py` (group maps), `manager.py` (connectome conversion jobs), `preflight.py`, `lesionops.py`, `lesionset.py`, `fetch.py` and `atlases.py` (with the catalogs in `catalog/`), `clusters.py`, `methods.py` and `checklist.py` (reporting), `figurefit.py` |
| [Modules/Scripted/LesionNetworkMapping/LesionNetworkMappingUI](Modules/Scripted/LesionNetworkMapping/LesionNetworkMappingUI) | Map cards: `normalize_controller.py` (the controller that Map and Draw share) and `card_normalize.py`, `card_cohort.py` with the `cohort_*.py` tabs (`cohort_contrast.py`, `cohort_symptom.py`), `jobrunner.py` (one heavy job at a time); wave 4: `card_pipeline.py` (*Run everything*), `card_preflight.py`, `card_qa.py` (QA v2), `card_clusters.py`, `card_networks.py`, `manager_parity.py` (Lead-DBS import and verification) |
| [Modules/Scripted/LesionNetworkMapping/LesionNetworkMappingLib](Modules/Scripted/LesionNetworkMapping/LesionNetworkMappingLib) | Wave 4 numerics: `pipeline.py` (the cohort pipeline and its command line), `report.py` and `reportfigs.py` (the HTML report and its figures), `figures.py` (publication figures), `fastmap.py`, `nulls.py`, `validate.py`, `robustness.py`, `catalog.py` (with `catalog/connectomes.json`), `leaddbs.py`; the tar-stream converter is in `connectome.py` |
| [Modules/Scripted/Studio/StudioLib](Modules/Scripted/Studio/StudioLib) | Wave 4 in the Studio module: `libcard_analysis.py` (the *Analysis* card), `libcard_columns.py` (the *Columns* card and the *Case* section), `libcard_cohort.py` (cohort diagnostics and the lesion overview), `libcard_flags.py` (the *Flags* column and filters), `lesionqc.py` (lesion checks), `figure.py` (tracing from a figure), `thumbnails.py` and the grid of `library.py`, and the in-app updates (`updates.py`, `updates_install.py`, `updates_settings.py`, `version.py`) |
| [Libs/vtkSegmentationCore/vtkSegmentationHistory.cxx](Libs/vtkSegmentationCore/vtkSegmentationHistory.cxx), [Modules/Loadable/Segmentations/Logic/vtkSlicerSegmentEditorLogic.cxx](Modules/Loadable/Segmentations/Logic/vtkSlicerSegmentEditorLogic.cxx), [Base/QTCore/qSlicerScriptedUtils.cxx](Base/QTCore/qSlicerScriptedUtils.cxx), [Utilities/Scripts/GenerateSlicerWrapperIndex.py](Utilities/Scripts/GenerateSlicerWrapperIndex.py) | Wave 4 in C++ and the build: cheaper undo snapshots, the Draw and Scissors apply, lazy Python wrappers (with `slicer.util`), the linker option |
| [Base/QTApp/qSlicerMainWindow.cxx](Base/QTApp/qSlicerMainWindow.cxx), [Base/QTGUI/qSlicerSettingsViewsPanel.cxx](Base/QTGUI/qSlicerSettingsViewsPanel.cxx), [Base/QTGUI/qSlicerDataDialog.cxx](Base/QTGUI/qSlicerDataDialog.cxx), [Modules/Loadable/Volumes/Logic/vtkSlicerVolumesReader.cxx](Modules/Loadable/Volumes/Logic/vtkSlicerVolumesReader.cxx), [Modules/Loadable/Segmentations/EditorEffects/qSlicerSegmentEditorPaintEffect.cxx](Modules/Loadable/Segmentations/EditorEffects/qSlicerSegmentEditorPaintEffect.cxx) | Wave 3 in C++: focus mode, panel width and the disclaimer and close hooks of the main window; *Navigation in Studio* settings; *Add Data* and volume loading; the brush-size display and hover of the Paint effect |
| [Modules/Scripted/SampleData](Modules/Scripted/SampleData) | "MNI152 (neuro)" category: bundled 2 mm template, TemplateFlow downloads |
| [Base/QTGUI/qSlicerStyle.cxx](Base/QTGUI/qSlicerStyle.cxx) | Palettes, color tokens, density, accent color |
| [Libs/MRML/Core/vtkMRMLLayoutNode.cxx](Libs/MRML/Core/vtkMRMLLayoutNode.cxx), [Libs/MRML/Logic/vtkMRMLLayoutLogic.cxx](Libs/MRML/Logic/vtkMRMLLayoutLogic.cxx) | Studio layouts and their portable saving |
| [Utilities/SlicerStudioWeb](Utilities/SlicerStudioWeb) | Web browser launcher and Docker image |
| [Utilities/SlicerStudioLNM](Utilities/SlicerStudioLNM) | The `slicer-lnm` container recipes (Dockerfile, Apptainer) and their README |
| [Docs/user_guide/studio_projects.md](Docs/user_guide/studio_projects.md), [Docs/user_guide/studio_templates.md](Docs/user_guide/studio_templates.md) | The guides to the Slicer Studio folder and to your own MNI templates |
| [Utilities/Scripts/SlicerStudioMacOS](Utilities/Scripts/SlicerStudioMacOS) | macOS disk image build script |
| [Utilities/Scripts/SlicerStudioRelease](Utilities/Scripts/SlicerStudioRelease) | `make_release.py`: prepares the files of a release and the update manifest (it never contacts GitHub) |
| [Utilities/Scripts](Utilities/Scripts) | `StudioTestRunner.py`, `StudioBench.py`, `StudioScreens.py`, `SlicerFileCompatibilityCheck.py`, `StudioAutosaveCrashCheck.py`, `StudioUsabilityGate.py`, `StudioReadmeCheck.py`, `StudioPrivacyCheck.py` (scans shared files for local paths), `SlicerStudioSmokeCheck.py` |
| [Docs/user_guide/install_studio.md](Docs/user_guide/install_studio.md) | Install guide |
| [Docs/user_guide/modules/studio.md](Docs/user_guide/modules/studio.md), [Docs/user_guide/modules/lesionnetworkmapping.md](Docs/user_guide/modules/lesionnetworkmapping.md) | The user guide pages of Studio and of lesion network mapping |

### Tests

[`StudioTestRunner.py`](Utilities/Scripts/StudioTestRunner.py) runs tests headless under `xvfb-run`
(`--no-xvfb` to turn it off) and returns the test's exit code:

```console example
SLICER=~/Slicer-SuperBuild-Release/Slicer-build/Slicer
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest Studio StudioTest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest Studio StudioAutosaveTest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest Studio StudioNormalizeTest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest Studio StudioAccessibilityTest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest Studio StudioUpdatesTest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest LesionNetworkMapping LesionNetworkMappingNormalizeUITest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest LesionNetworkMapping LesionNetworkMappingPipelineUITest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest Segmentations SegmentEditorSmartFillTest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" pyunit Segmentations Testing/Python/SmartFillAlgorithmTest.py
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest Applications/SlicerApp/Testing/Python StudioLayoutPortabilityTest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" --generic-dir "$(mktemp -d)" generic Studio
```

`unittest DIR NAME` accepts a module name as `DIR`; `selftest MODULE` runs a module's self-test (for example
`selftest LesionNetworkMapping`); `pyunit DIR FILE` runs a pure Python test with `PythonSlicer`. Upstream
tests run the same way, for example `unittest Segmentations SegmentEditorLogicTest`. The Studio suites are
`StudioTest`, `StudioNavigationTest`, `StudioOrientationTest`, `StudioTopBarTest`, `StudioPaletteTest`,
`StudioLibraryTest`, `StudioImportTest`, `StudioModesTest`, `StudioTourTest`, `StudioAutosaveTest`,
`StudioAccessibilityTest`, `StudioNormalizeTest`, `StudioUpdatesTest`, `StudioProjectsTest`, `StudioTemplatesTest`,
`StudioRegressionTest`, `StudioVisualTest` and `StudioUsabilityGateTest`; a new `Studio*Test.py` file in
`Testing/Python` is registered without editing the CMake file.

The LNM unit tests need only `PythonSlicer`, use synthetic data and no network, and take several minutes on a 4-core
machine (the count changes with every work package, so none is given here). The normalization tests run BRAINSFit on a
synthetic subject. The test that needs the whole-head template is skipped unless that template is in a folder named by
`LNM_TEST_TEMPLATE_DIR`, and the files that need the Slicer application (`LesionNetworkMappingNormalizeUITest`,
`LesionNetworkMappingPipelineUITest` and `StudioNormalizeTest`) skip themselves under a plain `PythonSlicer`, so run the
files with `discover`:

```console
cd Modules/Scripted/LesionNetworkMapping
PythonSlicer -m unittest discover -s Testing/Python -p 'LesionNetworkMapping*Test.py'
```

The README checker (below) also runs the cohort pipeline on a small synthetic fixture. To scan the files of a study or
of a run for absolute local paths before you share them, run the privacy check:

```console
python3 Utilities/Scripts/StudioPrivacyCheck.py --strict "My Study"
```

The usability gate re-runs the walkthrough tasks headless and counts the actions from the input events that
it sends. Run it in `Slicer-build`, with a throwaway settings folder:

```console
HOME=$(mktemp -d) xvfb-run -a -s "-screen 0 1920x1080x24" ./Slicer --no-splash \
  --python-script /path/to/Slicer/Utilities/Scripts/StudioUsabilityGate.py -- --task all --wave 2 --out gate
```

The autosave crash check kills a Slicer Studio process in the middle of a session, recovers the scene in the
next start and loads the autosave in official installations:

```console
python3 Utilities/Scripts/StudioAutosaveCrashCheck.py --slicer "$SLICER" --out crash-report \
  --checker official-5.13=/opt/Slicer-5.13.0-linux-amd64/Slicer
```

### Benchmarks

[`StudioBench.py`](Utilities/Scripts/StudioBench.py) is the before/after gate for performance work. It
measures startup, Studio, module and layout switching, slicing, painting, segment operations, effect
panels and LNM, and checks them against budgets:

```console
cd ~/Slicer-SuperBuild-Release/Slicer-build
xvfb-run -a -s "-screen 0 1920x1080x24" ./Slicer --no-splash \
  --python-script /path/to/Slicer/Utilities/Scripts/StudioBench.py -- --out after.json --check --compare before.json
python3 /path/to/Slicer/Utilities/Scripts/StudioBench.py --startup --slicer ./Slicer --runs 3 --out startup.json
python3 /path/to/Slicer/Utilities/Scripts/StudioBench.py --merge run1.json run2.json run3.json --out merged.json
```

`--only` selects metrics or groups (`--list` shows them). A metric that fails to measure counts as a
failure. Timings vary with machine load: compare medians of three or more runs taken under the same
conditions (`--merge`).

### Screenshots

[`StudioScreens.py`](Utilities/Scripts/StudioScreens.py) captures the start screen, the Segment Editor,
the Studio workspaces and a widget gallery, in several window sizes and both themes. In `Slicer-build`:

```console
HOME=$(mktemp -d) xvfb-run -a -s "-screen 0 1920x1080x24" ./Slicer --no-splash \
  --python-script /path/to/Slicer/Utilities/Scripts/StudioScreens.py -- --out shots --sizes 1920x1080,1366x768
```

### Compatibility check

[`SlicerFileCompatibilityCheck.py`](Utilities/Scripts/SlicerFileCompatibilityCheck.py) builds a test
scene in each Slicer (volume, segmentation with a closed surface, point list, table, color table with
alpha, T map, parameter node, 3D view settings, Studio layout), saves it as a folder and as a `.mrb`,
loads every writer's files in every other Slicer and compares node counts, voxels, geometry, segments,
points, tables, colors, views and new log messages. Each Slicer runs with its own temporary settings,
so your settings are not touched.

```console
python3 Utilities/Scripts/SlicerFileCompatibilityCheck.py \
  --slicer fork=$HOME/Slicer-SuperBuild-Release/Slicer-build/Slicer \
  --slicer official-5.12.4=/opt/Slicer-5.12.4-linux-amd64/Slicer \
  --slicer official-5.13=/opt/Slicer-5.13.0-linux-amd64/Slicer \
  --out compat-report --pairs all --autosave
```

With `--autosave`, a Slicer Studio writer also writes the scene with the scene autosave (before the regular
saves), and every checker loads that `Scene.mrml` as a third scene. Official installations run under the
`C.UTF-8` locale, because official 5.12.4 fails on non-ASCII paths under the POSIX locale.

The report is written to `report.txt` and `report.json`, with screenshots; the exit code is 1 if any
check fails. Run it after any change to what Slicer saves or loads.

### README check

[`StudioReadmeCheck.py`](Utilities/Scripts/StudioReadmeCheck.py) rejects what is no longer true in this
README: broken links and anchors, settings keys, environment variables, file paths, command line flags,
modules and shortcuts that are not in the source, unbalanced code fences, banned wording, a disclaimer that
differs from the one the library writes, and a claim that a feature is implemented or available when its work
package did not pass. It also rejects statements that go stale on their own (a number of tests, a test named as
failing, a compatibility check reported as not run, what the bracket keys do in Classic). It reads a state file
that maps work package identifiers to `passed`, `failed`, `deferred` or `pending`; with `--wave 5` it also requires that
every work package of wave 5 passed, and it rejects wording that offers a build service or a container registry as a way to
install Slicer Studio. With `--run-examples` it runs the
commands of the `console example` blocks with `--help`, and the blocks marked `console example fixture` as written, on a
synthetic study and connectome that it makes in a temporary folder (about ten seconds; `--pythonslicer` names the launcher when
`PythonSlicer` is not on the path). Run it after editing the README:

```console example
python3 Utilities/Scripts/StudioReadmeCheck.py --self-test
python3 Utilities/Scripts/StudioReadmeCheck.py --readme README.md --source . --state wave_state.json --run-examples
python3 Utilities/Scripts/StudioReadmeCheck.py --wave 5 --state wave_state.json
```

### Contributing

Follow upstream's [CONTRIBUTING.md](CONTRIBUTING.md), for example its commit message prefixes (`ENH:`,
`BUG:`, `PERF:` and others). In addition, for this fork:

- Do not change what Slicer writes to files, and add no node classes or file formats. Run the
  compatibility check.
- Keep `--testing` and `--no-main-window` behaving as upstream. Studio code must work when
  `StudioLib.shell.instance()` returns `None` (Classic, testing, no main window).
- Do not rename or remove modules, settings keys, widget object names or APIs; add instead.
- Store fork settings under `Studio/*` or `Appearance/*` only (the Map workspace uses `Studio/LNM/*`);
  never rewrite keys that official Slicer reads.
- Run the [README check](#readme-check) after editing this file.
- Back performance claims with StudioBench before/after numbers, medians of three or more runs.
- Never add connectomes or FSL-licensed templates to the repository.

Before committing:

```console
python3 -m pre_commit run --files <changed files>
codespell --ignore-words .codespellignore <changed files>
```

### Keeping up with upstream

```console
git remote add upstream https://github.com/Slicer/Slicer.git
git fetch upstream
git merge upstream/main
```

Most fork code is in new files (the Studio module, the LNM library, the scripts). Edits to upstream files
add behavior without renaming or removing anything, to keep merges manageable. After a merge, rebuild
and run the tests, StudioBench and the compatibility check. Fixes that help every Slicer user, such as
commit `7fa9dd9` (merge the default settings after the command-line arguments are parsed), can be
proposed upstream.

## License and third-party notices

Slicer Studio uses the 3D Slicer license: see [License.txt](License.txt) (a BSD-style license) and
[COPYRIGHT.txt](COPYRIGHT.txt). The libraries that 3D Slicer builds and bundles (VTK, ITK, CTK, Qt,
Python and others) keep their own licenses.

Added by this fork:

| Component | Where | License | Notes |
|---|---|---|---|
| [Lucide](https://lucide.dev) icons 0.468.0 | [Modules/Scripted/Studio/Resources/Icons/Lucide](Modules/Scripted/Studio/Resources/Icons/Lucide) | ISC; portions MIT (Feather) | 62 icons from lucide-static 0.468.0, stroke width changed from 2 to 1.75; the module icon `Resources/Icons/Studio.svg` is Lucide's *library* icon, recolored. [LICENSE](Modules/Scripted/Studio/Resources/Icons/Lucide/LICENSE) next to the icons |
| Lucide icons 0.468.0, second copy | [Libs/MRML/Widgets/Resources/Icons/Lucide](Libs/MRML/Widgets/Resources/Icons/Lucide) | ISC; portions MIT (Feather) | 13 icons from lucide-static 0.468.0, stroke width 1.75, for the flat view controller bars (tinted to the theme by `qMRMLSymbolicIconEngine`). [LICENSE](Libs/MRML/Widgets/Resources/Icons/Lucide/LICENSE) next to the icons. The Smart fill icon (`Resources/Icons/SmartFill.png` of the Segment Editor effects) and the Map module icon are rendered or recolored from Lucide icons |
| [Inter](https://rsms.me/inter/) font 4.001 | [Base/QTGUI/Resources/Fonts](Base/QTGUI/Resources/Fonts) | SIL Open Font License 1.1 | `Inter-Regular.ttf`, `Inter-Medium.ttf` and `Inter-SemiBold.ttf`, embedded in the application and used as its interface font; copyright The Inter Project Authors. [OFL.txt](Base/QTGUI/Resources/Fonts/OFL.txt) next to the fonts |
| MNI152NLin2009cAsym 2 mm brain T1w and brain mask, from [TemplateFlow](https://www.templateflow.org), unmodified | [Modules/Scripted/SampleData/Resources/Data](Modules/Scripted/SampleData/Resources/Data) | MNI / McGill permissive license | [LICENSE-MNI152NLin2009cAsym.txt](Modules/Scripted/SampleData/Resources/Data/LICENSE-MNI152NLin2009cAsym.txt); cite Fonov et al., NeuroImage 2011 and TemplateFlow (Ciric et al., Nature Methods 2022). The two thumbnails are rendered from this template. |
| TemplateFlow downloads on request: MNI152NLin2009cAsym 1 mm T1w and mask; MNI152NLin6Asym 1 mm and 2 mm T1w, 2 mm mask; Schaefer2018 400 parcels; Harvard-Oxford th25 | Downloaded to the Slicer cache when you ask, SHA-256 checked | Their own terms; MNI152NLin6Asym and Harvard-Oxford: FSL license (non-commercial) | Never bundled |
| noVNC, websockify, TigerVNC, Openbox | Only in the web Docker image ([Dockerfile](Utilities/SlicerStudioWeb/Dockerfile)), installed from Ubuntu 24.04 packages | noVNC: MPL-2.0 (some bundled files under other permissive licenses); websockify: LGPL-3.0; TigerVNC: GPL-2.0; Openbox: GPL-2.0 | The image uses Ubuntu's noVNC 1.3.0. Local mode uses the system packages, or downloads noVNC 1.6.0 from GitHub (checksum verified). The image also contains Ubuntu 24.04 runtime packages (Qt 5: LGPL-3.0; Mesa and tini: MIT; others); their licenses are under `/usr/share/doc/*/copyright` in the image and their sources are the Ubuntu 24.04 source packages. |
| GSP1000 connectome | Not included | GSP Open Access Data Use Terms | Obtain it from Harvard Dataverse |

## Based on 3D Slicer

[![DOI: 10.1016/j.mri.2012.05.001](https://img.shields.io/badge/DOI-10.1016/j.mri.2012.05.001-blue.svg)](https://pubmed.ncbi.nlm.nih.gov/22770690/)
[![Citations on Semantic Scholar](https://img.shields.io/badge/dynamic/json?label=Citations&query=%24.citationCount&url=https%3A%2F%2Fapi.semanticscholar.org%2Fgraph%2Fv1%2Fpaper%2FDOI%3A10.1016%2Fj.mri.2012.05.001%3Ffields%3DcitationCount)](https://www.sciencedirect.com/science/article/abs/pii/S0730725X12001816?via%3Dihub#preview-section-cited-by)
[![CI (Build)](https://github.com/Slicer/Slicer/actions/workflows/ci.yml/badge.svg)](https://github.com/Slicer/Slicer/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/Slicer/Slicer/badge)](https://securityscorecards.dev/viewer/?uri=github.com/Slicer/Slicer)

The badges above are those of upstream 3D Slicer. 3D Slicer is a free, open source software package for
visualization and image analysis, available for Windows, Linux and macOS. Slicer Studio is built on it,
and the platform is the work of the 3D Slicer community.

- Documentation, tutorials and downloads: <https://www.slicer.org> and <https://slicer.readthedocs.io>
- Community announcements and support for 3D Slicer: <https://discourse.slicer.org>

If you use Slicer Studio in your research, please cite 3D Slicer (see [CITATION.cff](CITATION.cff)):

> Fedorov A, Beichel R, Kalpathy-Cramer J, Finet J, Fillion-Robin J-C, Pujol S, Bauer C, Jennings D,
> Fennessy F, Sonka M, Buatti J, Aylward S, Miller JV, Pieper S, Kikinis R. 3D Slicer as an Image
> Computing Platform for the Quantitative Imaging Network. *Magnetic Resonance Imaging*.
> 2012;30(9):1323-1341. doi:10.1016/j.mri.2012.05.001

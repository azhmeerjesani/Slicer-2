# Slicer Studio

Slicer Studio is an edition of [3D Slicer](https://www.slicer.org) for one kind of work: drawing brain
lesions slice by slice and computing lesion network maps (LNM). It opens on a simple Library page, puts
the Segment Editor next to a large axial view in a Draw workspace, and computes a lesion's network from a
normative connectome in a Map workspace (you provide the connectome; a small synthetic demo connectome
is there to try it). While you work, the scene is autosaved, so that unsaved work can be recovered after a crash.
The layout borrows from photo editors such as Lightroom: a library to choose what to work on, a focused
editing space and a quiet dark interface. Underneath it is 3D Slicer 5.13, so its files open in official
3D Slicer and the other way round. Its packaging scripts build a macOS disk image, a Linux package and a
version that runs in a web browser on your own computer; no package has been published yet.

> [!NOTE]
> **Early development, no release yet.** Waves 1 and 2 of 5 and the packaging work are implemented: the
> Studio interface (Library, Draw and Map), a command palette and a shortcut list, guided tours, the
> streamlined Draw panel with Smart fill (assisted tracing), autosave and crash recovery, flat view bars
> and faster slice navigation, a first round of usability work (an `Esc` that backs out one state at a
> time, visible mouse modes, a Library that shows the current work, safer file import), the Map
> workspace, the lesion network mapping library with its command lines, file compatibility checks and
> the packaging scripts. The Qt 6 start-up problems found so far are fixed. Planned next, wave 3: the
> study loop (a subject list and *Done → next*), the autosave status and recovery card in Studio,
> one-click normalization of native-space scans to MNI space (command line, Draw and Map card, Library
> batch) and the cohort statistics screens. Wave 4: the *Run everything* cohort pipeline with an HTML
> report, then performance work and the user guide. Packages are built on your own computer (see
> [Install](#install)); no disk image, Linux package or web image has been published yet. See
> [Status and roadmap](#status-and-roadmap).

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
  layout picker, **Search** and an error badge. Every control of the bar takes the keyboard focus and has
  a tooltip of the form "Name · key · description". In a window narrower than 1100 px, Search and the
  switcher shrink to icons.
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
- **Guided tours.** `≡ > Help > Take a tour…` is a two-minute tour that points at the controls on screen
  and says how the same thing is done in 3D Slicer; *Where did it go?* is a searchable list for people who
  come from 3D Slicer (see [Coming from 3D Slicer](Docs/user_guide/studio_coming_from_slicer.md)). The
  tour is offered once, in a small card that never blocks anything.

| Workspace | What you do there | State |
|---|---|---|
| **Library** | Open scans, lesion masks, scenes, figures or DICOM folders, or start on the MNI template. A drop zone, *Open…*, *Open folder…*, *Start on MNI template* and the most recent files are shown over the views. Once data is loaded, a *Current work* card lists the scans and lesions that are open, with *Continue in Draw →*. | Available (W1-08, U-02) |
| **Draw** | Trace the lesion. The views use the *Studio Focus* layout: a large axial view, with the coronal and sagittal views stacked on its right. The inspector shows a lesion card, labelled tool tiles (Brush, Outline, Smart fill, Erase, Fill gaps) and a *Save lesion* button at the bottom. See [Drawing](#drawing). | Available (W2-05, A-02) |
| **Map** | Compute and read the lesion's network map from a connectome that you provide (or the synthetic demo). See [Lesion network mapping library](#lesion-network-mapping-library). | Available (W2-06) |

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
| `F11` or `Ctrl+6` | Hide panels: hide the inspector and the top bar (formerly focus mode) |
| `F6` / `Shift+F6` | Move the keyboard focus between the top bar, the inspector and the views |
| `F10` | Open the `≡` menu (on Windows and Linux also `Alt+F`, `Alt+E` and `Alt+H` for its File, Edit and Help menus) |
| `Esc` | Backs out of one state per press: closes a popover, Search or the shortcut list; leaves Place or window/level mode; restores a maximized or single view; shows hidden panels; deselects the Draw tool; returns from a module opened by Search to the workspace |
| `Ctrl+Z` / `Ctrl+Y` | Undo / redo the lesion (in Draw; `Ctrl+Shift+Z` also redoes) |
| `Ctrl+Enter` | The primary button of the inspector: *Save lesion* in Draw, *Map network* in Map |
| `Ctrl+Shift+S` | Save lesion… (in Draw) |
| `P` / `D` / `M` / `E` / `L` / `Shift+F` / `O` | Draw: Brush / Outline / Smart fill / Erase / Auto outline / Fill gaps / fill or outline only. `1` to `0` pick the tools in order. |
| `N` / `Shift+N` / `C` | Draw: next / previous slice with a gap in the lesion / center the views on the lesion |
| `Alt+Left` / `Alt+Right` | Draw: previous / next lesion of a loaded set |
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
  (`Studio/BrushDiameterMm`).
- **Tool tiles:** Brush, Outline, Smart fill, Erase and Fill gaps, with *More tools ▾* for the others (the
  Segment Editor effects keep their names in the Segment Editor itself). Tools have letter keys (see the
  keyboard table above); `Esc` deselects the tool and `Space` pauses it, so that a left-drag adjusts
  window/level.
- A **Display** card (collapsed) and, at the bottom, the primary button: *Save lesion* (`Ctrl+Enter`).
  *⋯ > Show all controls* of the panel brings back the classic Segment Editor controls.

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
- **Painting with masking** (editable area or intensity range) processes only the region of the stroke.
  After a stroke, only segments whose voxels changed get their 3D surface rebuilt. This also fixes 3D
  surfaces that official Slicer 5.12 and 5.13 leave out of date after erasing in segments that share a
  layer.
- **Threshold and Fill between slices.** Threshold no longer costs anything until it is used, and no
  longer recomputes a histogram on every mouse move; a click no longer resets the range silently, and the
  option *Paint within this range* is explained. *Fill between slices* can act on the selected segment
  only and fills with one click.
- **Segment Editor widget.** Tooltips name the keys, the masking section shows a one-line summary, the
  intensity range starts from the window/level, an empty segment table says what to do, and the brush
  size limits follow the volume's geometry.
- In the compatibility test, the segmentation files (`.seg.nrrd`) that Slicer Studio saves are
  byte-identical to those of official 3D Slicer 5.12.4 and 5.13.

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
  it is off in Classic and with `--testing`, which keep Slicer's behavior.
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
- **Planned (wave 3):** thin sliders and scroll bars (W3-01) and a settings page for the navigation
  behaviors above (W3-02b).

### Lesion network mapping library

- `LesionNetworkMappingLib`: a numpy/scipy library and command lines that map one or many lesions
  against a normative functional connectome in one streaming pass, with N-aware threshold rules,
  specificity QA (connectome degree similarity, relocated-lesion null, similarity to the connectome's
  reference maps), a plain-file study model and an export package with provenance. It has 271 unit
  tests on synthetic data, without network access.
- **Map workspace** (the Slicer module around the library; `Ctrl+Alt+3`). A column of cards in the
  inspector:
  - *Seed*: the lesion (a segment) to map, with the grid it is resampled onto.
  - *Connectome*: a connectome bundle (`.lnmc` folder, see below) that you add from this computer with
    *Add connectome…*, or *Use the demo*, a small synthetic connectome that is labelled "SYNTHETIC — not
    real data" everywhere and exists to try the workspace. The connectome's data use terms are shown and
    you accept them before a map is computed.
  - *Compute*: *Map network* (`Ctrl+Enter`) for all subjects, or a *Preview* with N random subjects. The
    computation runs in a separate process with progress and a time estimate; the map appears with the
    first partial result, and a run can be stopped (a stopped run is a partial run and is exported as
    one). Optionally, 50 relocated lesions are mapped in the same pass for the specificity check.
  - *Result*: the maps (mean r, mean Fisher z and T) with a diverging color table, and the threshold rule
    with its resolved value. Thresholds are rules, re-resolved for the run's N and number of voxels.
  - *QA*: the similarity of the map with the connectome's degree map, the relocated-lesion null (always
    stated, also when it was not run), the similarity with the reference maps, and the coverage of the
    lesion on the connectome. The card opens itself when a check is amber or red.
  - *Export*: the maps, one JSON sidecar per lesion, `provenance.json`, `methods.md` and `reproduce.sh`.
    The sidecars, `provenance.json` and `methods.md` carry the research-use disclaimer (see
    [Disclaimer](#disclaimer)).

  A network map that you open from a file (a `_funcmap` file, for example) is shown as a thresholded
  overlay with a color legend, and Draw never shows a network map. The lesion must be in the connectome's
  MNI space; normalizing native-space scans is planned (wave 3). Connectomes: today you add a local folder
  or use the demo. Planned (wave 5): a *Get a connectome…* action that downloads a connectome only when you
  ask and have accepted the provider's terms (see [Data and licensing](#data-and-licensing)).
- Also in the library, with command lines and no user interface yet (planned, wave 3 and later): a study
  schema with optional columns, roles, `participants.json`, lesion sidecars and listwise exclusion; a
  shared job protocol (JSON event lines, a cancel file, resume, a run registry with provenance); connectome
  provenance (DOI, version, global signal regression, population, terms), named subject subsets and a
  guard against pooling maps of different connectomes; a registry of coordinate spaces and
  template-family transforms; a design builder (design types, covariates, nesting and permutation
  schemes, PALM export); a voxelwise GLM with Freedman-Lane permutation inference (FWE, FDR, optional
  TFCE); cohort map orchestration with a per-lesion cache; reference maps per connectome; and QA numerics
  that compare a map with null maps. See [Lesion network mapping](#lesion-network-mapping).

### Autosave and crash recovery

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
- **Recovery.** If Slicer closed unexpectedly, or you closed it without saving, a window at the next
  start lists what can be recovered, with a thumbnail and a summary; the same list is *File > Recover
  Autosaved Work…*. A recovered scene is put back on the original files of your data, never on the
  autosave folder, and still counts as unsaved. Planned (wave 3, A-01b): a status line and a recovery
  card in Studio.
- **Official Slicer.** The `Scene.mrml` of an autosave opens in official 3D Slicer 5.12 and 5.13
  (*File > Load Scene*), which the compatibility check tests with `--autosave`.
- **Off.** `SLICER_STUDIO_AUTOSAVE=0` turns autosave off for a run. The *Autosave the scene* option of
  the Autosave panel (`Studio/Autosave/Enabled`) stops autosaving but keeps recovery. Autosave never runs
  with `--testing`, `--no-main-window` or `--exit-after-startup`.
- **Web version.** The autosave folder is in the `slicer-studio-web-home` volume, so it survives
  stopping and starting the container. Stopping the container ends Slicer with a signal and not through the
  normal quit, which Slicer Studio treats as a crash, so the next start should offer recovery. This has not
  been tried with Docker yet.

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
| `Studio/FollowStrokes` | `true` | The other views move to each stroke |
| `Studio/PageStepSlices` | `10` | Slices moved by `PageUp` and `PageDown` |
| `Studio/FocusFollowsMouse` | `true` | The keys of a view act on the view under the mouse |
| `Studio/LeftDragWindowLevel` | `true` | Left-drag adjusts window/level when no tool is active |
| `Studio/OrientationLetters` | `true` | Orientation letters on the slice views |
| `Appearance/FlatViewBars` | `true` (not under `--testing`) | Flat view bars (Application Settings > Views, restart needed) |
| `Appearance/Default3DView/*` | follows the theme | 3D view background and box colors |
| `Studio/LNM/MaxJobThreads` | (CPUs minus one) | Most threads of a Map job |

Smart fill keys are written when you change the options of the tool. The Autosave and Appearance
settings are in *Application Settings*. Map also keeps `Studio/LNM/Connectomes` (the connectomes you
added) and the data use terms you accepted, under `Studio/LNM/*`.

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

The scroll and workspace-switch rows are not comparable with the earlier rows of the table: they are other
metrics, measured on a more loaded machine, with the flat view bars and the wave 2 Studio panels in place.

Not faster yet:

- **Startup.** Studio 3.1 s, Classic 3.0 s (same build, median of 5 alternating runs): the Studio
  interface adds about 0.1 s. Startup work is planned for wave 4.
- **Undo with 3D surfaces.** Not faster than official 5.13 (Studio 174–292 ms, official 191–249 ms
  across sessions). Planned for wave 4.
- **Brush hover with 3D surfaces.** Unchanged. Planned for wave 3.

A build made on Ubuntu 24.04 (before the Studio changes) started in 2.5–3.3 s, against 6.1–6.4 s for
the official Linux nightly package on the same idle machine. This comes from Ubuntu's linker default
(`--as-needed`, which drops unused library dependencies) and from building without the Qt Designer
plugins (`Slicer_BUILD_QT_DESIGNER_PLUGINS=OFF`, about 0.6 s less first-load work), not from Slicer
Studio code. Measured again with the Studio changes (fresh settings, machine under some load), the
build started in 3.3–3.9 s and the official 5.13 nightly in 8.0–8.8 s. An option to link packaged
builds this way is planned (wave 4).

### File compatibility with official 3D Slicer

- **Both directions.** Scenes and data saved by Slicer Studio open in official 3D Slicer 5.12 and 5.13,
  and files saved by those versions open in Slicer Studio. The compatibility check (below) passes for
  all 9 ordered writer/reader pairs of Slicer Studio, official 5.12.4 and the official 5.13 nightly of
  2026-10-16 (and a Slicer Studio writer in a path with spaces and non-ASCII characters), for scene
  folders and `.mrb` bundles, in normal and `--testing` mode, last run on 2026-10-20 with the tree of
  the end of wave 2. The LNM outputs and the scene autosave (see
  [Autosave and crash recovery](#autosave-and-crash-recovery)) written by Slicer Studio load in all three.
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
   [Autosave and crash recovery](#autosave-and-crash-recovery)). *Planned (wave 3):* the study loop
   (*Done → next*).
4. **Save.** *Save lesion* (bottom of the inspector in Draw, `Ctrl+Shift+S` or `Ctrl+Enter`) writes the
   lesion as a NIfTI mask (`.nii.gz`, 0/1 values, `uint8`) on the lesion's own grid, with a small `.json`
   sidecar next to it: a lesion mask that you opened on the FSL MNI152 2 mm grid is saved on that grid, not
   on the grid of the template you drew over. It suggests `sub-XX_lesion.nii.gz` when the scan name has a
   BIDS subject, and remembers the folder. After the first save, the button and `Ctrl+Shift+S` save again
   to the same file; *Save lesion as…* always asks. `Ctrl+S` saves the whole scene as a folder or a `.mrb`
   file, which also opens in official 3D Slicer. An autosave is not a save: only these commands write your
   files. For other export options, open the Segmentations module (`Ctrl+K`, type "Segmentations") and use
   *Export to files*.
5. **Map the network.** Open Map (`Ctrl+Alt+3`). Choose the lesion and a connectome (*Add connectome…* for
   a `.lnmc` bundle on your computer, or *Use the demo* to try Map with synthetic data), accept its data
   use terms, and click *Map network* (`Ctrl+Enter`). Read the maps, the threshold rule and the QA, and
   *Export…* them. See [Lesion network mapping library](#lesion-network-mapping-library) and, for the
   command line, [Lesion network mapping](#lesion-network-mapping). The lesion must be in the
   connectome's MNI space. Note that the template that comes with Slicer Studio (MNI152NLin2009cAsym)
   and the FSL MNI152 template used by GSP1000 (MNI152NLin6Asym) differ by a few millimeters. Normalizing
   native-space scans to MNI is planned (wave 3).

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
take `--threads`; `cohortmaps` and `glm` also take `--cancel-file`, `--resume`, `--run-root` and
`--parent-run`:

| Module (`PythonSlicer -m ...`) | What it does |
|---|---|
| `LesionNetworkMappingLib.cohortmaps` | Maps of a whole cohort in batches under a memory budget (`--budget-gb`), with a per-lesion cache (`--cache`), a `maps.tsv`, and `--dry-run` to see the plan |
| `LesionNetworkMappingLib.glm` | Voxelwise GLM with Freedman-Lane permutation inference: FWE and FDR p values, optional TFCE (`--maps`, `--participants`, `--design`, `--mask`, `--perm`, `--tfce`) |
| `LesionNetworkMappingLib.reference` | `build`, `estimate` and `compare` the reference maps of a connectome |
| `LesionNetworkMappingLib.transforms` | `warp`, `points` and `check` for template-family transform files |
| `LesionNetworkMappingLib.qa` | `degree`: the connectome degree map |

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

Also in the library, with a user interface planned: cohort statistics (overlap, sensitivity, Welch t,
Liebermeister test, max-statistic permutation FWE, conjunction, symptom correlation, peak table) and a
plain-file study model (`study.json` plus a BIDS-style `participants.tsv`).

### Data and licensing

- **No connectome is bundled with Slicer Studio, and none is downloaded automatically.** Today you obtain
  the data yourself and convert it once (the synthetic demo of Map is generated on request and is not real
  data). Planned (wave 5, L-35): a *Get a connectome…* action that downloads a connectome only when you
  ask and have accepted the provider's terms as they are shown live; sources that state no terms stay
  links.
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
compatibility check; nothing is released yet. Everything under *Planned* is not available in the
application today.

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
| LNM foundations: study schema v2, job protocol, connectome provenance, threshold rules v2, space registry, design builder, GLM permutation engine, cohort maps, reference maps, QA v2, Map plug-in framework | L-01 to L-11 | LNM unit tests (271) |
| Wave 2 integration fixes: the lesion is never overwritten when you start on the template again, exhaustive permutation orientations in the design builder, flat view bars on by default, faster scrolling and workspace switching, Inter embedded once, mode pill and toast placement, a compact top bar, the Map demo built off the main thread | W2-I1, W2-I2, W2-I3 | `Studio*Test` suites, `StudioBench.py`, usability gate |

### Roadmap

Planned items are listed by wave, with the identifiers of the work packages. Titles are shortened and the
plan may change.

| Wave | Planned contents | State |
|---|---|---|
| Wave 1: foundation | W1-01 to W1-09 (see *Done*) | Done |
| Packaging | P-01, P-02 (see *Done*); P-03: web container environment and LNM smoke checks for packages | P-01, P-02 done; P-03 planned (wave 4) |
| Wave 2: streamlined editor, navigation, autosave, Smart fill, Map v1, LNM foundations | A-01, A-02, L-01 to L-11, U-01 to U-07, W2-01 to W2-07, W3-02a, W3-06 and the integration fixes W2-I1 to W2-I4 (see *Done*) | Done |
| Wave 3: study loop, normalization, cohort statistics | W3-01 thin sliders and scroll bars; W3-03 main-window hooks and shell speed; W3-04 NIfTI loading fixes; W3-05 brush size display and paint speed; W3-07 Library study loop (subject list, switching); W3-08 cohort statistics UI, connectome manager, Map integration; W3-02b navigation settings UI; U-03 accessibility and first-run/close flow; A-01b autosave status and recovery card in Studio; A-03, A-03b, A-03c one-click normalization to MNI for a lesion and for a study; L-12 to L-22 research numerics and the cohort Contrast and Symptom tabs; A-05b README update | Planned |
| Wave 4: cohort pipeline, report, performance, user guide | A-04, A-04b, A-04c *Run everything* cohort pipeline and the HTML cohort report; W4-01 faster Draw and Scissors apply and cheaper undo; W4-02 Library grid with thumbnails; W4-03 lazy Python wrappers; W4-04 linker option `--as-needed` for packaged builds; W4-05 user guide; L-23 to L-34 figures, nulls, validation, robustness, Draw intake and Map QA surfaces, connectome converters, Lead-DBS interoperability; P-03; A-06 README update | Planned |
| Wave 5: LNM research features | L-35 to L-54 connectome fetcher, Validate, Robust and Nulls cards, analysis recipes, control-lesion sets, review workflow, blind double tracing, case series, share-safe study export, headless twin, pipeline calibration, coordinate seeds, VLSM, literature sources, reference maps, tutorial cohorts, LNM documentation; A-07 README update | Planned |

The automation work, by feature:

| Feature | Work packages | State |
|---|---|---|
| Autosave and crash recovery | A-01 | Implemented |
| Autosave status line and recovery card in Studio | A-01b | Planned (wave 3) |
| Smart fill, assisted tracing | A-02 | Implemented |
| One-click normalization of native-space scans to MNI space: command line; card in Draw and Map; Library batch | A-03, A-03b, A-03c | Planned (wave 3) |
| *Run everything*: the cohort pipeline from lesions to an HTML report | A-04, A-04b, A-04c | Planned (wave 4) |

Connectomes are never bundled. Planned (wave 5): a connectome is downloaded only when you ask for it and
have accepted the provider's terms. Templates are downloaded only when you ask.

Known limitations today:

- **Map** needs a connectome that you provide (or the synthetic demo, which is not real data) and a lesion
  in the connectome's MNI space; normalizing native-space scans is planned (wave 3). There is no cohort
  statistics screen yet: cohort numerics run from the command line.
- **Smart fill** needs visible contrast between the lesion and its surroundings. There is nothing to find
  on the bare MNI template: use the patient's scan as the source volume. Lesions with grey-matter-like
  intensity on T1 images are ambiguous, and a click near the edge of a lesion can give a different
  region than a click well inside it. It is assisted tracing: review every result.
- **Autosave** is not Save. Modified data above 2 GB is not autosaved, very large modified data is
  autosaved at most every 10 minutes, and an autosave may contain copies of patient data. The Windows
  folder and the web version (stopping the container) have not been tried. The status line and recovery
  card in Studio are planned (wave 3): today recovery is a window at start and *File > Recover
  Autosaved Work…*.
- The navigation behaviors of Studio (page step, focus follows the mouse, left-drag window/level) have no
  settings page yet; they are the `Studio/*` keys of
  [Settings added by Slicer Studio](#settings-added-by-slicer-studio).
- The Draw inspector width is chosen from what the tools need (up to 600 px, remembered), so it can be
  wider than the 340 px default after you have used the tools.
- The *System* theme follows the light or dark setting of the operating system on Windows and with Qt 6.5
  or newer, and the desktop palette with Qt 5 on Linux (light when that is unknown). It has not been
  tried on macOS.
- Slicer Studio has been run on Linux with Qt 5.15. The macOS packages (Qt 6) passed the start-up test
  on macOS 15 with Apple Silicon only, on a disk image built before wave 2: the wave 2 features have not
  been run on macOS.

## Development

### Where things are

| Path | Contents |
|---|---|
| [Modules/Scripted/Studio](Modules/Scripted/Studio) | Studio interface (`StudioLib/`): shell, top bar, workspaces, layouts, Library page, importer, downloads, Draw (`draw.py`, `lesioncard.py`), Search (`palette.py`, `finder.py`), shortcut list (`cheatsheet.py`), in-view probe (`probe.py`), mode pill (`modes.py`), orientation letters, guided tours (`tour.py`, `tours.py`, `migration.py`); the `Studio*Test` suites in `Testing/Python` |
| [Modules/Scripted/Studio/StudioLib](Modules/Scripted/Studio/StudioLib) | Autosave and recovery: `autosave.py` (the service, menu actions), `autosave_io.py` (writing of autosave scenes), `recovery.py` (scan, recover, discard, recovery window), `autosave_settings.py` (the settings panel) |
| [Modules/Loadable/Segmentations/EditorEffects/Python/SegmentEditorEffects](Modules/Loadable/Segmentations/EditorEffects/Python/SegmentEditorEffects) | Smart fill: `SegmentEditorSmartFillEffect.py` (the effect) and `SmartFillAlgorithm.py` (the computation, numpy and VTK only) |
| [Modules/Scripted/LesionNetworkMapping](Modules/Scripted/LesionNetworkMapping) | `LesionNetworkMappingLib` and its unit tests, the Map module (`LesionNetworkMapping.py`) and its card plug-ins (`LesionNetworkMappingUI/`) |
| [Modules/Scripted/SampleData](Modules/Scripted/SampleData) | "MNI152 (neuro)" category: bundled 2 mm template, TemplateFlow downloads |
| [Base/QTGUI/qSlicerStyle.cxx](Base/QTGUI/qSlicerStyle.cxx) | Palettes, color tokens, density, accent color |
| [Libs/MRML/Core/vtkMRMLLayoutNode.cxx](Libs/MRML/Core/vtkMRMLLayoutNode.cxx), [Libs/MRML/Logic/vtkMRMLLayoutLogic.cxx](Libs/MRML/Logic/vtkMRMLLayoutLogic.cxx) | Studio layouts and their portable saving |
| [Utilities/SlicerStudioWeb](Utilities/SlicerStudioWeb) | Web browser launcher and Docker image |
| [Utilities/Scripts/SlicerStudioMacOS](Utilities/Scripts/SlicerStudioMacOS) | macOS disk image build script |
| [Utilities/Scripts](Utilities/Scripts) | `StudioTestRunner.py`, `StudioBench.py`, `StudioScreens.py`, `SlicerFileCompatibilityCheck.py`, `StudioAutosaveCrashCheck.py`, `StudioUsabilityGate.py`, `StudioReadmeCheck.py` |
| [Docs/user_guide/install_studio.md](Docs/user_guide/install_studio.md) | Install guide |

### Tests

[`StudioTestRunner.py`](Utilities/Scripts/StudioTestRunner.py) runs tests headless under `xvfb-run`
(`--no-xvfb` to turn it off) and returns the test's exit code:

```console example
SLICER=~/Slicer-SuperBuild-Release/Slicer-build/Slicer
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest Studio StudioTest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest Studio StudioAutosaveTest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest Segmentations SegmentEditorSmartFillTest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" pyunit Segmentations Testing/Python/SmartFillAlgorithmTest.py
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest Applications/SlicerApp/Testing/Python StudioLayoutPortabilityTest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" --generic-dir "$(mktemp -d)" generic Studio
```

`unittest DIR NAME` accepts a module name as `DIR`; `selftest MODULE` runs a module's self-test (for example
`selftest LesionNetworkMapping`); `pyunit DIR FILE` runs a pure Python test with `PythonSlicer`. Upstream
tests run the same way, for example `unittest Segmentations SegmentEditorLogicTest`. The Studio suites are
`StudioTest`, `StudioNavigationTest`, `StudioOrientationTest`, `StudioTopBarTest`, `StudioPaletteTest`,
`StudioLibraryTest`, `StudioImportTest`, `StudioModesTest`, `StudioTourTest`, `StudioAutosaveTest` and
`StudioUsabilityGateTest`; a new `Studio*Test.py` file in `Testing/Python` is registered without editing
the CMake file.

The LNM unit tests (271 tests in 12 files, about a minute, no network) need only `PythonSlicer`:

```console
cd Modules/Scripted/LesionNetworkMapping
PythonSlicer -m unittest Testing/Python/LesionNetworkMapping*Test.py
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
package did not pass. It reads a state file that maps work package identifiers to `passed`, `failed`, `deferred` or
`pending`. Run it after editing the README:

```console example
python3 Utilities/Scripts/StudioReadmeCheck.py --self-test
python3 Utilities/Scripts/StudioReadmeCheck.py --readme README.md --source . --state wave_state.json --run-examples
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

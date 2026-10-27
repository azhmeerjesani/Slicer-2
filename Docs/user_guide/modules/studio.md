# Slicer Studio

Slicer Studio is the default interface of this fork of 3D Slicer. It is built for one path: bring scans and lesion
masks in (**Library**), trace a lesion (**Draw**), and compute and read a lesion network map (**Map**). Every
other 3D Slicer module is still there, one search away, and the classic interface is one switch away. Nothing is
removed.

This page describes what Slicer Studio shows and which keys it uses. For installation, see
[Install Slicer Studio](../install_studio.md). If you know official 3D Slicer, [Coming from 3D Slicer](../studio_coming_from_slicer.md)
says what stays the same and where things moved; **Help > Take a tour…** in the ≡ menu walks through the screen.
The Segment Editor in Draw is described in [Segment Editor](segmenteditor.md) and the network mapping in Map in
[Lesion Network Mapping](lesionnetworkmapping.md).

:::{note}

Slicer Studio is research software. Lesion network mapping results are for research use only and not for
clinical decisions (see [Research use only](lesionnetworkmapping.md#research-use-only)).

:::

## The screen

The Studio window has three parts, and nothing else (no menu bar, toolbars, logo header, Data Probe or status bar):

- **The top bar** (44 px). On the left the **≡ menu** and the breadcrumb (the study and subject, or the name of the
  scan). In the centre the switcher **Library | Draw | Map**. On the right Undo and Redo (in Draw only), the layout
  picker, the **Search** field, the autosave status and, only when there are errors, an error badge with a count.
- **The inspector** on the right (340 px by default; drag its edge to change it). It shows the cards of the current
  workspace, or any other module you open with Search.
- **The views** on the left: the slice and 3D views, or in Library the canvas.

The window opens on **Library**. Where you are is always shown: the switcher highlights the current workspace, and
when you open another module from Search, the inspector header reads "← Draw" (or whichever workspace you came
from). That button, or **Esc**, takes you back.

### The ≡ menu

Press **F10** or click ≡. It lists the three workspaces, **Search…**, the **File**, **Edit**, **View** and **Help**
submenus (the same menus as in the classic interface), **Appearance** (Dark, Light or System, and Comfortable or
Compact density), **Settings…** (Ctrl+2), **Python console** (Ctrl+3), **Error log** (Ctrl+0),
**Keyboard shortcuts** (Ctrl+/), **Show inspector** and **Classic interface** (Ctrl+Alt+0). On Windows and Linux, Alt+F,
Alt+E and Alt+H open the ≡ menu at File, Edit and Help. Alt, then V, reaches View. On macOS the system menu bar also
stays available.

The panel header has a **?** button, which explains the current tool (in Draw: the tool, its key and the Draw keys),
and a **⋯** menu with **Show Data Probe**, **Reset panel width**, **Open in Classic** and the extra actions of the
workspace.

## Search

There is one search, called **Search**: the field in the top bar, **Ctrl+K**, **≡ > Search…**, and **Ctrl+F**,
which opens Search filtered to modules. It finds workspaces, modules, Draw tools, actions (open, save, close, place a
point, Python console and others), view commands such as layouts and the crosshair, recent files and the
[where did it go](../studio_coming_from_slicer.md) answers. Choosing a module opens it in the inspector under a
"← Draw" back button.

Results are ordered by how well they match, best first:

1. an exact name (or alias, such as "Segment Editor" for Draw);
2. an exact command synonym, then an exact module synonym;
3. a name that starts with the text, then a synonym that starts with it (from 3 characters);
4. words that start in order ("seg ed");
5. text anywhere in a name;
6. full text (only when nothing above matched).

Within a group, the entries you ran most recently come first. With an empty query Search shows **Recent tools**,
**Recent files** and **Favorites** (your classic favourite modules, read but never changed). When nothing matches, it
says so and suggests words that work, for example "save", "export", "figure", "crosshair".

## Esc: one step back at a time

Esc undoes one state per press, from the innermost out. It never stops a computation and never closes a workspace
or the application:

| Rung | State | Esc does |
| ---- | ----- | -------- |
| 0 | A menu, popup or dialog; the Search field; a text field with unsent edits; the Python console | The widget handles Esc itself |
| 1 | A popover, the shortcut list or the list of recent messages | Closes it |
| 2 | Place or window/level mode (shown in the mode pill) | Returns to navigation. A Draw tool that the mode paused is armed again |
| 3 | A maximized view (**Single**, or a double-click) | Restores the layout |
| 4 | Hidden panels | Shows them |
| 5 | An active Draw tool | Deselects it |
| 6 | A module opened from Search | Goes back to the workspace |

**Library is outside the ladder:** it is a workspace, not a state. Its visible way back is the **Continue in Draw →**
button on the Current work card. Every step has a visible twin: the ✕ of the pill, the maximize/restore button on a
view, the **Show panels** pill, the active tool tile, the "← Draw" button.

## Mode pills and Hide panels

Two overlays are allowed over the views, so that no state is invisible:

- The **mode pill** at the top centre appears when the mouse mode is not the default: *Placing a point · click in a
  view · Esc to cancel*, or *Window/level · drag in a view · Esc to exit*. Its ✕ ends the mode. A Draw tool that was active when you started
  placing resumes afterwards (the pill says "Brush resumes after").
- **Hide panels** (**F11** or **Ctrl+6**) hides the inspector and the top bar for a larger view. A **Show panels** pill
  appears at the top right. Hide only the inspector with **Ctrl+\\** or **Ctrl+5**: an edge handle "‹" then shows
  it again.

Confirmations appear as a short toast at the bottom centre. A consequence of something that happened automatically
(for example "Figure shown on the right") stays until your next input and carries one action such as **Undo** or
**Show**. **≡ > View > Recent messages** lists the latest ones. Offers and notices (downloads, recovery, questions)
go in a notice line between the panel header and the cards, never over the image.

## Workspaces

### Library

Library is where data comes in and where you choose what to work on. In Studio it replaces Welcome, Add Data,
Sample Data and, for everyday use, the Data module.

**Empty Library.** A drop zone ("Drop scans, lesion masks or a folder"; NIfTI, NRRD or DICOM folders), the buttons
**Open…**, **Open folder…** and **Start on MNI template**, and a **Recent** list whose first row has the focus, so
Enter reopens the last item. A link "Load sample data" opens the Sample Data module in the inspector.

- Dropping a **scan** (`.nii`, `.nii.gz`, `.nrrd`, `.mha`, `.mhd`) loads it directly. A **lesion mask** is recognised by
  its name (the word lesion, mask, seg, label or roi, as a whole word, for example `sub-01_lesion.nii.gz`) and
  becomes the segment "Lesion". After loading a single scan, Studio switches to Draw. `.mrb` and `.mrml` files load as
  scenes. Hold Shift while dropping, or drop something ambiguous, to get the classic Add Data dialog.
- A **folder of DICOM** files opens the DICOM module. A folder with several NIfTI files shows a card (for example "40 NIfTI
  files found", with the participants table if the folder has one) that offers **Create study**, or opening one subject at
  a time.
- **Start on MNI template** loads the bundled MNI152 2009c template (2 mm), creates an empty segmentation named
  Lesions and opens Draw with the Brush armed. A 1 mm template is offered as an optional download (see
  [Data downloaded on request](lesionnetworkmapping.md#data-downloaded-on-request)).

**With data loaded** the canvas shows the **Current work** card: the scan, the lesion with its side and volume ("Left ·
2.27 mL"; the side word only when the lesion is in MNI space), and whether it is saved ("Saved · sub-01_lesion.nii.gz ·
2 min ago", "Unsaved changes", "Not saved yet"). The main button is **Continue in Draw →**. **Close…** closes the
scene. The list of what is loaded is called **Open now**. Studio uses one word for removing something from the scene,
**Close**; "Delete" stays the word of the Data module, and "Remove from study" never deletes files.

**With a study open** Library shows a 44 px filter row (a search field, filter chips and the sort order), the subject **list** with
columns ID, Group, Lesion mL, Space, Status and QC, and an optional **grid** of cards with a lesion thumbnail on the
template (switch with the List/Grid buttons; the slider sets the card size). Thumbnails are computed in the background
and cached in the study's `.studio` folder. Each thumbnail is marked **R** at its left edge and **L** at its right edge: the patient's right is on the screen left.

| Key (in the subject list or grid) | Operation |
| --------------------------------- | --------- |
| `↑` `↓` | move between subjects |
| `Enter`, or double-click | open the subject in Draw |
| `P` / `X` / `U` | QC pass / QC fail (left out of every analysis) / not reviewed |
| `Delete` | remove from the study (files are kept; Undo in the message) |
| `Ctrl+A` | select all subjects |
| `/` | search the subjects |

The inspector shows the cards **Study** (name, folder, counts, "All changes saved"), **Subject** (paths, space chip,
lesion volume, group, scores, notes, **Open in Draw**), **Batch** (set the group or a score of the selected subjects,
remove from the study) and **Import** (add files or a folder, import a `participants.tsv`). Further cards (QC flags,
normalization, the analysis line) appear when the study needs them.

### Draw

Draw is the [Segment Editor](segmenteditor.md) module in its Studio presentation. Its inspector has, top to bottom:

1. The **Lesion card**: colour, name (click to rename), visibility, a live line "2.41 mL · 1 part · 0 gaps", and
   **Clean up**. In MNI space the line starts with the side ("Left · 2.27 mL"); in scan space no side word is shown,
   because x = 0 is not the midline there. Parts and gaps turn amber when they are more than 1 or 0, with a link to fix
   them (**Keep largest**, **Fill**).
2. **Tools**: **Brush** (P), **Outline** (D), **Smart fill** (M), **Erase** (E) and **Fill gaps** (Shift+F), plus
   **More tools**, which shows the remaining effects.
3. The **options of the active tool**. Brush and Erase have size chips 1, 3, 6 and 12 mm.
4. **Display** (collapsed): fill or outline, opacity, window/level presets, **Voxel view**.
5. A sticky footer. Without a study the main button is **Save lesion · Ctrl+Enter** and the second is **Map network →**
   (for lesions in MNI space; otherwise it explains why and offers **Normalize to MNI…**). With a study the main button
   is **Done → next · Ctrl+Enter**.

**The first click.** The Brush is armed when you came to trace: **Start on MNI template**, **Continue in Draw →**,
**Open in Draw**, a loaded lesion file or set. A scan you only dropped or opened lands in Draw **unarmed**: a
left-drag then adjusts window/level, so that looking at a scan never paints on it. Pick a tool (P) to start drawing.
**Space** pauses the active tool (and resumes it); while paused, left-drag adjusts window/level.

#### Draw keys

The keys work on the view under the mouse. The tool keys exist only in Draw.

| Key | Operation |
| --- | --------- |
| `P`, `D`, `M`, `E`, `L`, `Shift+F` | Brush, Outline, Smart fill, Erase (press again to return to the previous tool), Auto outline, Fill gaps |
| `1` … `0`, `Shift+1` … `Shift+0` | the effect at that position of the tool list (positions 1-10, 11-20) |
| `O` | switch the lesion between filled and outline |
| `N` / `Shift+N` | next / previous slice that has a gap in the lesion |
| `C` | centre all views on the lesion |
| `Ctrl+Z` / `Ctrl+Y` (also `z` / `y`) | undo / redo |
| `Space` | pause or resume the active tool |
| `Esc` | deselect the active tool (after the steps above it in the ladder) |
| `Ctrl+Shift+S` | Save lesion (re-saves silently after the first save) |
| `Ctrl+Enter` | the main button of the inspector: Save lesion, Done → next, Map network, or Run everything |
| `Alt+←` / `Alt+→` | previous / next lesion of a loaded set, or subject of a study |
| `Shift+wheel`, `[` `]`, `-` `+` | brush size (the brackets can be turned off, see [Settings](../settings.md#slicer-studio-settings)) |
| `i` | toggle masking by intensity range |

The [Segment Editor page](segmenteditor.md#keyboard-shortcuts) lists the keys of each tool. The same tables are
generated from the live key list by **Keyboard shortcuts** (**Ctrl+/**).

#### Saving the lesion

Saving a lesion and saving the scene are different things, with different keys:

| What you want | Key | What is written |
| ------------- | --- | --------------- |
| Keep the lesion as a file | **Ctrl+Shift+S**, or **Ctrl+Enter** without a study | A binary mask `<name>_lesion.nii.gz` (uint8) **on the lesion's own grid**, plus a sidecar `<name>_lesion.json` |
| Save the whole session | **Ctrl+S** | The scene (`.mrml` or `.mrb`) exactly as official 3D Slicer writes it |
| Save under another name | **Save lesion as…** in the ⋯ menu of the Lesion card, or Search | Always asks for the file |
| Go to the next subject of a study | **Ctrl+Enter** ("Done → next") | `lesions/<id>_space-<space>_desc-lesion_mask.nii.gz` and a row in `participants.tsv` |

- **The grid never changes.** The mask is written on the grid it was drawn on, never resampled to the grid of the
  scan shown behind it. A 91×109×91 mask drawn over the 2 mm template is saved as 91×109×91 with the same voxels.
- **Re-saving.** After the first save in a session, **Save lesion** and Ctrl+Shift+S write silently to the same file and
  show a toast with **Show in folder**.
- **The name** is `<sub-XX>_lesion.nii.gz` when the scan or node name has a subject label, else
  `<scan name>_lesion.nii.gz`. If the last file in the folder ends in a number (`case-001_lesion`), the next one is
  suggested (`case-002_lesion`). The folder is the last one you used, else the scan's folder, never the application
  folder.
- **An empty lesion writes nothing** ("The lesion is empty: nothing was saved").
- **The sidecar** is a small JSON file next to the mask with `Source` (the scan, as a path relative to the sidecar),
  `Space`, `Notes`, `SourceReference` and `GeneratedBy`. 3D Slicer and FSL ignore it; tools that read BIDS use it.
  When you open the lesion alone later, `Source` lets Studio offer to open the scan it was drawn on.
- **Quitting.** If only saved lesions and the template are unsaved, the message says "Your lesion is saved as
  sub-01_lesion.nii.gz. The session (scene) is not saved." with **Quit** as the default. If a lesion has changes
  since it was saved, **Save lesion…** is the default. With a study open, there is no Save: the current subject is
  written to the study first and the message only asks "All changes are saved in study <name>. Quit?".

Autosave (see [below](#autosave-and-recovery)) is not Save: it says "Autosaved", never "Saved".

### Map

Map is the [Lesion Network Mapping](lesionnetworkmapping.md) module. Its inspector shows the cards Seed, Normalize
to MNI, Connectome, Preflight, Run everything (when the seed is a cohort), Compute, Result, QA, Cohort, Clusters,
Networks and Export, and a footer that always reads "Research use only — not for clinical decisions" with the
references. The top bar shows the state of the computation next to the switcher ("No connectome", "Ready", "Mapping
34%", "Done · N=1000"). With the seed set to **Cohort**, **Ctrl+Enter** runs everything; otherwise it maps the
network. **Ctrl+E** exports. A computation is stopped only with its **Stop** button, never with Esc.

### Other modules

Any other module opens in the same inspector with a back button, "← Draw". Open them with Search. Opening a module
does not change the layout.

## Keyboard reference

### Everywhere in Studio

| Key | Operation |
| --- | --------- |
| `Ctrl+Alt+1` / `2` / `3` | Library / Draw / Map |
| `Ctrl+Alt+0` | Classic interface (and back) |
| `Ctrl+K` | Search |
| `Ctrl+F` | Search, filtered to modules |
| `Ctrl+/` | Keyboard shortcuts |
| `F10` | the ≡ menu (Windows and Linux also `Alt`, `Alt+F`, `Alt+E`, `Alt+H`) |
| `Ctrl+\` or `Ctrl+5` | show or hide the inspector (`Ctrl+5` for keyboards where backslash needs AltGr) |
| `F11` or `Ctrl+6` | Hide panels / show them |
| `F6` / `Shift+F6` | move the focus to the next / previous region: top bar, inspector, active view, canvas |
| `Tab` / `Shift+Tab` | move the focus (Tab never hides the inspector) |
| `Esc` | one step back, see [the ladder](#esc-one-step-back-at-a-time) |
| `Alt+1` … `Alt+5` | layouts: Focus, Single (maximize the view under the mouse), Four-up, Row, Focus + 3D |
| `Ctrl+Enter` | the main button of the inspector |
| `Ctrl+O` | Open… (loads the files directly) |
| `Ctrl+H` | Library (Windows and Linux) |
| `Ctrl+S` `Ctrl+W` `Ctrl+0` `Ctrl+2` `Ctrl+3` `Ctrl+4` | save the scene, close the scene, error log, settings, Python console, Extensions Manager (as in 3D Slicer) |

Inside a text field, the field keeps the keys it needs (Esc with unsent edits, typed characters). On German and French
keyboards AltGr+digit acts as Ctrl+Alt+digit in a view.

### In the views

These behaviours exist only while Studio is active; the Classic interface keeps the official behaviour. See
[Navigation in Studio](../user_interface.md#navigation-in-studio) for the table and the settings.

### Keyboard and screen readers

The top bar and the panel header can be reached with **F6** and **Tab**; arrows move inside the switcher, and Space and
Enter press a focused button. The focus ring is 2 px and always visible for keyboard focus. Consequences of automatic
actions are announced to screen readers. While the Library canvas covers the views, the hidden view widgets are
skipped by Tab.

## Left and right

Laterality is always shown, and never guessed:

- Every slice view shows **R/L**, **A/P** and **S/I** at its edges, computed from the view's orientation (also for
  oblique and flipped views). Over a figure the letters other than R/L are not shown.
- The axial and coronal views say, in words at the bottom left, "Left is on screen right" (the radiological display
  that 3D Slicer uses) or "Left is on screen left". A neurological display for the views is not part of Studio yet.
- **Hemisphere words ("Left", "Right", "Both hemispheres") appear only in MNI space**, where the midline is known.
  In scan space coordinates are shown with direction letters ("L 11.2 · P 17.8 · S 9.5 (scanner)") and a tooltip says
  they are measured from the image origin, not from the midline. The in-view readout shows MNI or scanner space
  for the coordinates; a minus sign is never used for a direction.
- When the lesion's grid is another MNI space than the template (for example MNI152NLin6Asym on a 2009c template) an
  amber line says the two differ by a few millimetres.

## Figures as references

A 2D figure (for example a published panel of a lesion) can be traced from. Drop it like any image. It loads as a
reference volume, never as something to edit, and Studio opens the **Compare** layout: the template where you draw on
the left, the figure on the right. Strokes over the figure change nothing. A question in the notice line asks which
side of the figure is the patient's right ("In this figure, the patient's right is on the Left / Right / Not sure");
until it is answered the figure is tagged "Figure · side unknown". The answer is stored in the figure's
node attributes. After saving the lesion, start a new lesion (it asks, with Undo) and drop the next figure.

## Appearance

**Application Settings > Appearance** has three choices, applied as soon as you make them (density after a restart):

- **Theme:** System, Dark or Light. Dark is the default. System follows the operating system.
- **Accent color:** the colour of selected items, links and highlighted controls (a reset button restores the default).
- **Density:** Comfortable (default) or Compact, which uses the spacing of earlier 3D Slicer versions.

The font is Inter, applied when you have not chosen another font. These choices are saved only in the Studio's own
settings keys (`Appearance/Theme`, `Appearance/Accent`, `Appearance/Density`, `Appearance/Font`); the settings that
official 3D Slicer reads (`Styles/Style`, `font`) are not rewritten. See [Settings](../settings.md#appearance).

## The study folder

A **study** is a folder of plain files (the catalog of a Lightroom, in other words). It can be opened in any file
browser, edited in a spreadsheet and backed up by copying it.

```
<study>/
  study.json            name, template, space, the rules that recognise lesion and ID names, connectomes used
  participants.tsv      one row per subject (BIDS-compatible)
  participants.json     data dictionary of the columns
  lesions/              sub-<id>_space-<space>_desc-lesion_mask.nii.gz  (+ a .json sidecar each)
  derivatives/
    lnm/                lesion network mapping outputs
    normalization/      normalized lesions of native-space subjects, one folder per subject
  sources/              figures traced from (optional)
  .studio/              thumbs/, journal/, inbox/, audit.jsonl  (application state, ignored by analyses)
```

- `participants.tsv` is the single source of truth about the subjects. `participant_id` comes first, a missing value
  is `n/a`, and paths are relative to the study folder. Its standard columns are `participant_id`, `group`
  (`case`, `control` or `n/a`), `include` (1 or 0), `status` (`untraced`, `inprogress`, `traced` or `mapped`), `qc`
  (`unreviewed`, `pass` or `fail`), `scan`, `lesion`, `space`, `volume_ml`, `n_parts`, `gaps`, `centroid_x`,
  `centroid_y`, `centroid_z`, `outside_brain_pct`, `rater`, `trace_seconds`, `modified` and `notes`. Your own columns
  (for example clinical scores) are kept after them, in order. More columns are added only when a feature first needs
  them. Open and edit the file in a spreadsheet; keep the header and the tab separators.
- **One writer.** While Studio has a study open, only Studio writes `participants.tsv`. Programs that run in the
  background post their results to `.studio/inbox/`, and Studio merges them. If you edit the file in a spreadsheet
  while the study is open, Studio keeps the cells you changed on disk, unless it changed the same cell.
- **Two windows on one study.** Opening a study that another Slicer Studio window (or a lab member on a shared drive)
  has open shows a warning; trace each subject in one window only. If both windows save the same subject's lesion, the
  later save is written and the other window's version is kept in `.studio/conflicts/`, and a message names that copy.
- **No absolute paths.** The files that are meant to be shared (the study, the tables, the sidecars) store paths
  relative to the study folder. `.studio` is application state and `derivatives/lnm/*/provenance.json` and
  `reproduce.sh` keep full local paths on purpose, because they reproduce one run on one computer.
- **Creating a study.** **Create study** on the folder card writes `study.json`, `participants.tsv` and a `lesions/` folder
  into the folder you opened; your own files are not changed or moved. Scans and lesions are paired by the ID found in
  the file names (`sub-01`) and by the lesion words listed in `study.json`. A `participants.tsv` or `.csv` in the folder
  supplies the group and the scores. Only the subject you are editing is loaded in the scene. **Import** in the inspector adds
  files, folders or a participants table later.
- **Journal.** While you draw, the lesion is written to `.studio/journal/` two seconds after the last edit, in the
  background. When you reopen a subject whose journal is newer than its lesion file, a card offers "Recover unsaved
  tracing".

## Autosave and recovery

In Studio, your work is saved as a project in the Slicer Studio folder, continuously and in place: see
[Where Slicer Studio saves your work](../studio_projects.md). This section is about the recovery copies that Slicer
Studio keeps of scenes that are not projects (in Classic, and while a study is open), so that a crash or an accidental
close does not lose them. It runs in Studio and in Classic.

- **What.** The whole scene is saved as an ordinary 3D Slicer scene (`Scene.mrml` and data files). It is incremental:
  files you loaded and did not change are referenced by their path, and only changed data is written. Data larger than
  2 GB per node is not autosaved; the recovery window lists it.
- **How often.** At most every 2 minutes, and only after 10 seconds without input, so that autosave never interrupts a
  stroke. Large nodes that take a while to write are written at most every 10 minutes, after 30 seconds of pause.
- **Where.** A private folder in the application data of your user account: `~/.local/share/slicer.org/Slicer/StudioAutosave` on
  Linux, `~/Library/Application Support/slicer.org/Slicer/StudioAutosave` on macOS and under `%LOCALAPPDATA%` on Windows. It is
  not a folder such as Documents, which is often synchronised to the cloud. You can change it in
  **Application Settings > Autosave** or with the environment variable `SLICER_STUDIO_AUTOSAVE_DIR`.
- **Retention.** The most recent 5 sessions are kept, within 2 GB, for at most 14 days. Sessions that you leave with
  "Exit (discard modifications)" are kept for that time too, so that an accidental discard can be undone.
- **Privacy.** Autosaves can contain patient data, so the folder is private to your account (mode 0700 on
  Linux and macOS). **Delete all autosaves…** in the Autosave settings removes the autosaves of every closed session for good.
- **Autosave is not Save.** The top bar shows "Autosaved · 2 min ago" (in Classic, a short message in the status
  bar). Ctrl+S, the exit prompt and the saved state of the scene keep their meaning: the exit prompt tells you that
  unsaved changes were autosaved and remain recoverable for 14 days.
- **Recovery.** After a crash, the next start shows **Recover unsaved work** (a card on the Library canvas, or a
  notice in the inspector elsewhere, and a dialog in Classic) with **Recover**, **Discard** and **Later**. With a
  study that was open it offers **Reopen study** first. **File > Recover Autosaved Work…** opens the same list at any
  time. Recovering loads the scene and points each node back at its original file, or at no file, never at the
  autosave folder.
- **Opening an autosave in official 3D Slicer.** An autosave is a normal scene. In official 3D Slicer 5.12 or 5.13
  use **File > Load Scene** on the `Scene.mrml` in the session's folder.
- **Switching it off.** Clear **Autosave the scene** in Application Settings > Autosave, set
  `Studio/Autosave/Enabled` to false, or start with the environment variable `SLICER_STUDIO_AUTOSAVE=0`. Autosave
  never runs with `--testing` or `--no-main-window`.

The keys are listed in [Settings](../settings.md#saving).

## Switching to the classic interface, and disabling Studio

- **Classic interface** (≡ menu, or **Ctrl+Alt+0**, or **⋯ > Open in Classic**): the menu bar, toolbars, module panel
  and Data Probe come back exactly as in 3D Slicer, and the window layout you had before is restored. **View >
  Studio interface** (or Ctrl+Alt+0 again) returns to Studio. Studio remembers your choice (`Studio/Mode`).
- **Start in Classic once:** set the environment variable `SLICER_CLASSIC_UI=1` before starting.
- **Disable Studio permanently:** set `Studio/Enabled` to false in `Slicer.ini` (see [Settings](../settings.md#slicer-studio-settings)), or choose Classic and leave it.
- Studio is never applied when Slicer is started with `--testing` or `--no-main-window`, so scripts, tests and
  headless runs behave exactly as in 3D Slicer.

## Compatibility with official 3D Slicer

Slicer Studio uses the same file formats and the same settings file as 3D Slicer, so both can be installed side by
side.

- **Files.** Scenes (`.mrml`, `.mrb`), volumes, segmentations (`.seg.nrrd`), markups, tables and colour tables written
  by Slicer Studio open correctly in official 3D Slicer 5.12 and 5.13, and the files written by those versions open in
  Slicer Studio. Studio adds no new node or storage file types. Extra information is stored as node attributes, which
  other versions ignore. Studio-only files (the sidecar `.json` next to a lesion, `participants.json`, `study.json`,
  `.studio/`) are extra files that other programs ignore.
- **Layouts.** The Studio layouts (Focus 7001, Focus + 3D 7002, Row 7003, Compare 7005) are saved in a scene as the
  conventional layout plus an extra attribute. Official 3D Slicer therefore shows its conventional layout with the
  slice and 3D views, and Studio restores its own layout when it loads the scene again.
- **Settings.** `Slicer.ini` is shared by every installation of your user account. Studio writes only keys that start
  with `Studio/` or `Appearance/` and never rewrites a key that official 3D Slicer reads (for example the style, the
  font or the module settings). Official versions ignore the Studio keys. When you close the window, Studio writes back
  the classic window state and a portable layout under `MainWindow/windowState` and `MainWindow/layout`, so an official
  installation that shares `Slicer.ini` opens with its normal window and layout, not with Studio's.
- **Checking it yourself.** `Utilities/Scripts/SlicerFileCompatibilityCheck.py` writes a scene with a volume, a
  segmentation, markups, a table, a colour table, a module parameter node, a 3D view and the current layout in
  each of the installations you name, and loads it in each of the others:

  ```
  python3 Utilities/Scripts/SlicerFileCompatibilityCheck.py \
      --slicer fork=/path/to/Slicer-Studio/Slicer \
      --slicer official=/path/to/Slicer-5.12.4/Slicer \
      --out compat-report --pairs all --autosave
  ```

  It compares the node classes, voxels, segments, markup coordinates, table contents and colour table entries, checks
  that the views are shown, and writes `report.txt` and `report.json` into the output folder. Use it with the
  installations you actually use before relying on the exchange of data. Each Slicer process in the check runs with
  its own temporary settings, so your own settings are not touched.

## Notices

Slicer Studio includes the following third-party components. Their full licence texts are in the source tree next
to the files.

- **Inter** (the interface font), Copyright (c) 2016 The Inter Project Authors, under the SIL Open Font License 1.1. The
  licence text is `Base/QTGUI/Resources/Fonts/OFL.txt`.
- **Lucide** (the icons), Copyright (c) Lucide Contributors 2022, under the ISC licence. Portions are Copyright (c)
  2013-2022 Cole Bemis, as part of Feather (MIT). The licence text is the file `LICENSE` in each `Icons/Lucide` folder.
- **MNI ICBM 2009c nonlinear asymmetric templates** (the bundled 2 mm template and its brain mask), Copyright (C)
  1993-2009 Louis Collins, McConnell Brain Imaging Centre, Montreal Neurological Institute, McGill University.
  Permission to use, copy, modify, and distribute this software and its documentation for any purpose and without
  fee is hereby granted, provided that the above copyright notice appear in all copies. The authors and McGill
  University make no representations about the suitability of this software for any purpose. It is provided "as is"
  without express or implied warranty. The authors are not responsible for any data loss, equipment damage, property
  loss, or injury to subjects or patients resulting from the use or misuse of this software package. Cite: Fonov et al.,
  NeuroImage 2011 (doi:[10.1016/j.neuroimage.2010.07.033](https://doi.org/10.1016/j.neuroimage.2010.07.033)), and
  TemplateFlow (Ciric et al., Nature Methods 2022). The notice is in `LICENSE-MNI152NLin2009cAsym.txt` next to the
  template.
- **MNI152NLin6Asym** (the FSL template) is **not** included. It is downloaded only when you ask for it, and the FSL
  licence terms apply (non-commercial use). See [Data downloaded on request](lesionnetworkmapping.md#data-downloaded-on-request).

The notices for 3D Slicer itself and its libraries are those of 3D Slicer (see [About](../about.md)).

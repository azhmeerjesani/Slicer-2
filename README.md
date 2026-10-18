# Slicer Studio

Slicer Studio is an edition of [3D Slicer](https://www.slicer.org) for one kind of work: drawing brain
lesions slice by slice and computing lesion network maps (LNM). It opens on a simple Library page and
puts the Segment Editor next to a large axial view in a Draw workspace; a Map workspace that computes a
lesion's network from a normative connectome is planned (wave 2). The layout borrows from photo editors
such as Lightroom: a library to choose what to work on, a focused editing space and a quiet dark
interface. Underneath it is 3D Slicer 5.13, so its files open in official 3D Slicer and the other way
round. Its packaging scripts build a macOS disk image, a Linux package and a version that runs in a web
browser on your own computer; no package has been published yet.

> [!NOTE]
> **Early development, no release yet.** Wave 1 of 4 and the packaging work are implemented: the Studio
> interface, the Library page, drawing and slicing fixes and speed-ups, the lesion network mapping
> library and command line, file compatibility checks and the packaging scripts. Planned next: the
> streamlined Draw panel and the Map workspace (wave 2), then the study workflow (wave 3). Packages are
> built on your own computer (see [Install](#install)); no disk image, Linux package or web image has
> been published yet. See [Status and roadmap](#status-and-roadmap).

> [!WARNING]
> **Research use only.** Slicer Studio, like 3D Slicer, is not a medical device. Lesion network maps must
> not be used for clinical decisions; the specificity of the method is debated (van den Heuvel et al.,
> 2026; see [References](#references)).

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
  exactly. `View > Studio interface` or `Ctrl+Alt+0` switches back. The choice is remembered.
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
  Classic interface), the name of the current data, the **Library | Draw | Map** switcher, Undo and Redo
  (in Draw), a layout picker, a search box and an error badge.
- **One inspector.** The module panel is a panel on the right, 340 px wide by default (300–480 px;
  423 px in Draw for now). A workspace is a module shown in it. Any other module, opened from the
  search box or by a script, appears there too, with a close button that returns to the workspace.
- **Dark by default.** A graphite dark theme, a light theme and System, an accent color and a
  Comfortable or Compact density, in *Application Settings > Appearance* (theme and density also in
  `≡ > Appearance`; a density change takes effect after a restart). Check boxes and radio buttons are
  redrawn for contrast in the dark theme. Icons are [Lucide](https://lucide.dev) SVGs.

| Workspace | What you do there | State |
|---|---|---|
| **Library** | Open scans, lesion masks, scenes or DICOM folders, or start on the MNI template. A drop zone, *Open…*, *Open folder…*, *Start on MNI template* and the most recent files are shown over the views. | Available |
| **Draw** | Trace the lesion with the Segment Editor. The views use the *Studio Focus* layout: a large axial view, with the coronal and sagittal views stacked on its right. | Available with the classic Segment Editor panel; the streamlined panel is planned (wave 2) |
| **Map** | Compute and read the lesion's network map. | Planned (wave 2): the tab is shown but disabled |

Keyboard (on macOS, `Ctrl` is `Cmd` and `Alt` is `Option`):

| Keys | Action |
|---|---|
| `Ctrl+Alt+1` / `2` / `3` | Library / Draw / Map |
| `Ctrl+Alt+0` | Switch between Studio and Classic |
| `Ctrl+O` | Open files, loaded directly (Classic keeps *Add Data*) |
| `Ctrl+K` | Search modules (Slicer's module finder; a command palette is planned) |
| `Alt+1` / `2` / `3` / `4` | Focus / Single / Four-up / Row layout |
| `Tab` (in a view) | Show or hide the inspector |
| `Ctrl+6` or `F11` | Focus mode: hide the inspector and the top bar |
| `Ctrl+/` | Brief reminder of the main shortcuts (a full shortcut overlay is planned, wave 3) |

Slicer's own shortcuts, such as `Ctrl+S` (save), `Ctrl+2` (settings) or `Ctrl+3` (Python console), keep
working with the menu bar hidden.

The Classic interface is also used, whatever the last choice, with `--testing`, with
`--no-main-window`, when the environment variable `SLICER_CLASSIC_UI=1` is set, or when the setting
`Studio/Enabled` is `false`.

### Drawing

These changes are in the Segment Editor itself, so they also apply in the Classic interface:

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
- In the compatibility test, the segmentation files (`.seg.nrrd`) that Slicer Studio saves are
  byte-identical to those of official 3D Slicer 5.12.4 and 5.13.

### Slicing and navigation

- **Studio layouts.** *Studio Focus* (large axial view, coronal over sagittal on the right),
  *Studio Focus + 3D* and *Studio Row* (three equal views). They are registered in every mode, so a scene
  saved with one of them reopens with it in Studio and Classic, and official Slicer can open it. The
  Classic layout menu does not list them yet, and *Studio Focus + 3D* is only available from Python
  (`slicer.app.layoutManager().setLayout(7002)`). The layout picker (`Alt+1` to `Alt+4`) offers Focus, a
  single view, Four-up and Row; *More layouts…* opens Slicer's layout menu. Each workspace remembers its
  layout, and switching workspaces changes the layout only when it differs.
- **Fewer updates per slice step.** A scroll step causes one slice-logic update instead of three. Other
  views are re-rendered only when they show something that moved (slice intersections, slabs, handles).
  A slice node change that changes nothing no longer reslices the volume. The slice controller bar
  updates only what changed. The rendered views are pixel-identical to before (416 captures compared).
- **Data Probe and slice annotation updates** are coalesced and skipped while hidden;
  `slicer.i18n.tr()` caches the source file of its caller.
- **Planned:** flat view bars that show the slice index (wave 2); PageUp/PageDown by 10 slices,
  Home/End, stepping that stops at the first and last slice, symmetric zoom, focus that follows the mouse
  and left-drag window/level (wave 3).

### Lesion network mapping library

- `LesionNetworkMappingLib`: a numpy/scipy library and command line that map one or many lesions
  against a normative functional connectome in one streaming pass, with N-aware threshold rules,
  specificity QA (connectome degree similarity, relocated-lesion null), a plain-file study model and an
  export package with provenance. It has 67 unit tests on synthetic data, without network access.
- The Map workspace (the Slicer module around the library) is planned (wave 2). Until then, use the
  command line: see [Lesion network mapping](#lesion-network-mapping).

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
  2026-10-16, for scene folders and `.mrb` bundles, in normal and `--testing` mode; the LNM outputs
  written by Slicer Studio load in all three.
- **No new formats.** No new node classes, storage node classes or file formats, and no changes to MRML
  defaults. Existing writers produce the same output, except that a layout node with a Studio layout
  also writes a `studioViewArrangement` XML attribute (see *Portable layouts*). Module state is kept in
  standard scripted module parameter nodes. Extra information is only stored as node attributes or XML
  attributes that official Slicer ignores.
- **Portable layouts.** The Studio layouts (ids 7001–7003, range 7001–7099 reserved) are registered in
  C++ in every mode. A scene saved with one of them stores `currentViewArrangement="2"` (the Conventional
  layout, which official Slicer knows) plus `studioViewArrangement="7001"`. Official Slicer ignores the
  extra attribute and shows the Conventional layout; Slicer Studio restores its own layout.
- **Shared settings.** All Slicer installations share the user settings file
  (`~/.config/slicer.org/Slicer.ini`), so recent files and other preferences carry over. Slicer Studio
  keeps its own preferences under `Studio/*` and `Appearance/*`, which official Slicer does not read, and
  does not change keys that official Slicer reads (`Styles/Style`, `font`, `Modules/*`, `Python/*`,
  `Developer/*` and others) through defaults or migrations; only your own changes in the settings do.
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
     (no network needed) and creates a "Lesions" segmentation on its grid. A banner offers the 1 mm
     template: a 13.7 MB download from TemplateFlow with progress, Cancel and a checksum check.
   - *Recent* lists the most recent files; `Enter` reopens the first one.
3. **Trace the lesion in Draw.** After a scan, a lesion or the template is loaded, Draw opens by itself
   with the "Lesion" segment selected in the Segment Editor. Use the usual effects: Paint, Draw (with
   *Add* / *Subtract* and the lasso), Level tracing, Erase, Islands, Smoothing, Margin. `Ctrl+Z` and
   `Ctrl+Y` undo and redo. *Planned:* the streamlined Draw panel (wave 2: a lesion card with its volume,
   labeled tool tiles with letter keys, brush size presets) and the study loop (wave 3: *Done → next*,
   autosave).
4. **Save.** `Ctrl+S` saves the scene as a folder or a `.mrb` file, which also opens in official
   3D Slicer. To save the lesion as a NIfTI mask, open the Segmentations module (`Ctrl+K`, type
   "Segmentations") and use *Export to files* with the NIfTI format.
5. **Map the network.** *Planned (wave 2):* the Map workspace is not available yet. You can already
   compute a map from the command line with the exported lesion, see
   [Lesion network mapping](#lesion-network-mapping). The lesion must be in the connectome's MNI space.
   Note that the template that comes with Slicer Studio (MNI152NLin2009cAsym) and the FSL MNI152 template
   used by GSP1000 (MNI152NLin6Asym) differ by a few millimeters. Normalizing native-space scans to MNI
   is planned.

## Lesion network mapping

`LesionNetworkMappingLib`
([Modules/Scripted/LesionNetworkMapping](Modules/Scripted/LesionNetworkMapping/LesionNetworkMappingLib))
uses only numpy and scipy (no Slicer, VTK or Qt imports) and runs in Slicer's own Python, `PythonSlicer`.
For every subject of a normative connectome, it correlates (Pearson r) the lesion's mean time course
with every voxel, averages over runs and applies the Fisher z transform; across subjects it computes the
mean r, the mean Fisher z and the one-sample T statistic of Fisher z, as Lead-DBS does. All lesions of
a run share one pass over the connectome: one matrix product per chunk of voxels serves all of them.

The library is not part of the built application yet. Run it from the source tree, with `PYTHONPATH`
pointing to `Modules/Scripted/LesionNetworkMapping` and the `PythonSlicer` of any Slicer Studio or
3D Slicer 5.12+ installation:

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

```console
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

```console
PythonSlicer -m LesionNetworkMappingLib.engine \
  --bundle /data/MyConnectome.lnmc \
  --lesion sub-01_lesion.nii.gz --lesion sub-02_lesion.nii.gz \
  --out results --snapshot-every 50 --null-relocations 50 --run-json results/run.json
```

| Option | Meaning |
|---|---|
| `--bundle`, `--lesion` (repeatable), `--out` | Connectome bundle, lesion NIfTI files on any grid, output folder. Lesion images are binarized (unless `--probabilistic`) and resampled onto the connectome grid through their affines, as fractional voxel occupancy. |
| `--subjects N` [`--random` `--seed S`] | Use only N subjects, for a preview. |
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

Speed, measured on synthetic data with one BLAS thread: 0.08 s per subject for 1 lesion, 0.11 s for 10
lesions and 0.21 s for 50 lesions (230,000 voxels, 240 time points, in memory); 0.13 s per subject for
10 lesions read from a float16 bundle on disk (228,093 voxels, 2 runs of 120 time points). A full pass
over a large connectome reads the whole bundle, so disk speed matters: GSP1000 is about 126 GB as a
float16 bundle (computed from 1,000 subjects, about 216 time points and 292,019 voxels).

### Thresholds and QA

Thresholds are stored as rules and resolved from the number of subjects N and of voxels V each time
they are used, so a value that depends on N is never silently reused for another N:

| Rule | Resolves to |
|---|---|
| `fwe_bonferroni` (default, alpha 0.05) | Bonferroni-corrected T: \|T\| ≥ 5.27 at N = 1000, V = 292,019 |
| `t_convention` | T = 7, defined for N ≈ 1000; not applied below N = 900 unless overridden, with a warning when N differs by more than 10% |
| `fz_abs` | An effect size on the mean Fisher z |
| `custom_t` | A T value, with an optional reference N and then the same warning |

QA, following the critique by van den Heuvel et al. (2026):

- **Degree similarity:** Pearson and Spearman correlation between the T map and the connectome degree
  map. A high value means the map may reflect connectome hubs rather than lesion-specific connectivity.
- **Relocated-lesion null:** maps of the same lesion shape moved to random places in the brain, computed
  in the same pass; reports how similar the lesion's map is to them.

Also in the library, with a user interface planned: cohort statistics (overlap, sensitivity, Welch t,
Liebermeister test, max-statistic permutation FWE, conjunction, symptom correlation, peak table) and a
plain-file study model (`study.json` plus a BIDS-style `participants.tsv`).

### Data and licensing

- **No connectome is bundled with or downloaded by Slicer Studio.** You obtain the data yourself and
  convert it once.
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
> (van den Heuvel et al., 2026, Nat Neurosci, doi:10.1038/s41593-025-02196-7).

### References

- Boes AD, Prasad S, Liu H, et al. Network localization of neurological symptoms from focal brain
  lesions. *Brain*. 2015;138(10):3061-3075. [doi:10.1093/brain/awv228](https://doi.org/10.1093/brain/awv228)
- Fox MD. Mapping symptoms to brain networks with the human connectome. *N Engl J Med*.
  2018;379(23):2237-2245. [doi:10.1056/NEJMra1706158](https://doi.org/10.1056/NEJMra1706158)
- van den Heuvel et al. *Nat Neurosci*. 2026.
  [doi:10.1038/s41593-025-02196-7](https://doi.org/10.1038/s41593-025-02196-7). A critique: it reports
  that many published lesion network maps carry a trace of connectome degree and that randomly placed
  lesions give similar maps.

## Status and roadmap

The work is organized in four waves of work packages, plus packaging. "Implemented" means written and
verified with this repository's tests, benchmarks, screenshots and compatibility check; nothing is
released yet.

| Part | Contents | State |
|---|---|---|
| Wave 1: foundation | Benchmark, screenshot and test tools; dark theme and Appearance settings; Studio interface (top bar, workspaces, Studio layouts, Classic switch); Library page with direct loading and the bundled MNI template; Segment Editor effect fixes and speed-ups; faster masked painting; faster slice updates; LNM thresholds, QA, study model and export; file compatibility | Implemented, under review |
| Packaging | Web browser launcher and Docker image; macOS disk image script; Linux package (`make package`); a GitHub Actions workflow, disabled and kept as a reference for the build steps | Implemented; packages are built locally; a macOS disk image passed the start-up test on macOS 15 (Apple Silicon), other Macs not yet tested |
| Wave 2: the streamlined editor | Draw panel (lesion card, tool tiles, letter keys, brush presets, *Done* / *Map network* button); Map workspace v1 (seed, connectome, compute with progressive maps, thresholds, QA, export, disclaimer); Inter font; flat view bars; more style work; Segment Editor improvements (segment created automatically, masking summary, brush limits); Threshold and Fill between slices fixes | Planned (next) |
| Wave 3: navigation, study loop | Slice navigation keys and mouse; menu-bar-free shortcuts and focus mode in C++; NIfTI loading fixes; brush size display and `[` `]` keys; command palette, shortcut overlay, in-view readout; studies (subject list, *Done → next*, QC flags, autosave journal); cohort statistics, connectome manager and Map integration | Planned |
| Wave 4: performance and docs | Faster Draw and Scissors apply, cheaper undo; Library grid with thumbnails; faster startup (lazy Python wrappers, linker option); user guide pages | Planned |
| Automation | Autosave of lesions, smart fill, MNI normalization of native-space scans, cohort pipeline with a report | Planned |

Known limitations today:

- The Map workspace is disabled; lesion network mapping runs from the command line.
- Draw shows the classic Segment Editor panel, so the inspector is 423 px wide in Draw, and stays that
  wide in the Library after Draw has been opened.
- `Ctrl+K` opens Slicer's module finder, not a command palette.
- The *System* theme follows the operating system on Windows only; on macOS and Linux it is light.
- The fork's default settings file (`Applications/SlicerApp/Resources/SlicerDefaultSettings.ini`) is
  not installed yet; its keys equal the built-in defaults or are not read yet (the Inter font keys,
  wave 2), so nothing changes for users.

## Development

### Where things are

| Path | Contents |
|---|---|
| [Modules/Scripted/Studio](Modules/Scripted/Studio) | Studio interface: shell, top bar, workspaces, layouts, Library page, importer, downloads; `StudioTest` |
| [Modules/Scripted/LesionNetworkMapping](Modules/Scripted/LesionNetworkMapping) | `LesionNetworkMappingLib` and its unit tests (the module itself is planned) |
| [Modules/Scripted/SampleData](Modules/Scripted/SampleData) | "MNI152 (neuro)" category: bundled 2 mm template, TemplateFlow downloads |
| [Base/QTGUI/qSlicerStyle.cxx](Base/QTGUI/qSlicerStyle.cxx) | Palettes, color tokens, density, accent color |
| [Libs/MRML/Core/vtkMRMLLayoutNode.cxx](Libs/MRML/Core/vtkMRMLLayoutNode.cxx), [Libs/MRML/Logic/vtkMRMLLayoutLogic.cxx](Libs/MRML/Logic/vtkMRMLLayoutLogic.cxx) | Studio layouts and their portable saving |
| [Utilities/SlicerStudioWeb](Utilities/SlicerStudioWeb) | Web browser launcher and Docker image |
| [Utilities/Scripts/SlicerStudioMacOS](Utilities/Scripts/SlicerStudioMacOS) | macOS disk image build script |
| [Utilities/Scripts](Utilities/Scripts) | `StudioTestRunner.py`, `StudioBench.py`, `StudioScreens.py`, `SlicerFileCompatibilityCheck.py` |
| [.github/workflows/studio-packages.yml](.github/workflows/studio-packages.yml) | Packaging workflow; disabled, kept as a reference for the build steps |
| [Docs/user_guide/install_studio.md](Docs/user_guide/install_studio.md) | Install guide |

### Tests

[`StudioTestRunner.py`](Utilities/Scripts/StudioTestRunner.py) runs tests headless under `xvfb-run`
(`--no-xvfb` to turn it off) and returns the test's exit code:

```console
SLICER=~/Slicer-SuperBuild-Release/Slicer-build/Slicer
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest Studio StudioTest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" unittest Applications/SlicerApp/Testing/Python StudioLayoutPortabilityTest
python3 Utilities/Scripts/StudioTestRunner.py --slicer "$SLICER" --generic-dir "$(mktemp -d)" generic Studio
```

`unittest DIR NAME` accepts a module name as `DIR`; `selftest MODULE` runs a module's self-test. Upstream
tests run the same way, for example `unittest Segmentations SegmentEditorLogicTest`.

The LNM unit tests (67 tests, a few seconds, no network) need only `PythonSlicer`:

```console
cd Modules/Scripted/LesionNetworkMapping
PythonSlicer -m unittest Testing/Python/LesionNetworkMappingLibTest.py Testing/Python/LesionNetworkMappingStudyTest.py
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
  --out compat-report --pairs all
```

The report is written to `report.txt` and `report.json`, with screenshots; the exit code is 1 if any
check fails. Run it after any change to what Slicer saves or loads.

### Contributing

Follow upstream's [CONTRIBUTING.md](CONTRIBUTING.md), for example its commit message prefixes (`ENH:`,
`BUG:`, `PERF:` and others). In addition, for this fork:

- Do not change what Slicer writes to files, and add no node classes or file formats. Run the
  compatibility check.
- Keep `--testing` and `--no-main-window` behaving as upstream. Studio code must work when
  `StudioLib.shell.instance()` returns `None` (Classic, testing, no main window).
- Do not rename or remove modules, settings keys, widget object names or APIs; add instead.
- Store fork settings under `Studio/*` or `Appearance/*`; never rewrite keys that official Slicer
  reads.
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
| [Lucide](https://lucide.dev) icons 0.468.0 | [Modules/Scripted/Studio/Resources/Icons/Lucide](Modules/Scripted/Studio/Resources/Icons/Lucide) | ISC; portions MIT (Feather) | 58 icons from lucide-static 0.468.0, stroke width changed from 2 to 1.75; the module icon `Resources/Icons/Studio.svg` is Lucide's *library* icon, recolored. [LICENSE](Modules/Scripted/Studio/Resources/Icons/Lucide/LICENSE) next to the icons |
| [Inter](https://rsms.me/inter/) font | Not bundled yet (planned, wave 2) | SIL Open Font License 1.1 | No font file is included yet; the application font is unchanged |
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

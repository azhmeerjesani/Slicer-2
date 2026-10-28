# Application settings

## Editing application settings

The application settings dialog allows users to customize application behavior.

After starting Slicer, it can be accessed clicking in menu: `Edit` / `Application Settings`.

### General

Application startup script can be used to launch any custom Python code when Slicer application is started.

### Modules

#### Skip loading

Select which [type of modules](https://www.slicer.org/wiki/Documentation/Nightly/Developers/Modules) to not load at startup. It is also possible to start Slicer by temporarily disabling those modules (not saved in settings) by passing the arguments in the command line.

For example, this command will start Slicer without any CLI loaded:

    Slicer.exe --disable-cli-modules

#### Show hidden modules

Some modules don't have a user interface, they are hidden from the module's list. For debugging purpose, it is possible to force their display

#### Temporary directory

Directory where modules can store their temporary outputs if needed.

#### Additional module paths

List of directories scanned at startup to load additional modules. Any CLI, Loadable or scripted modules located in these paths will be loaded.

Module folders of extensions are included in this list. To remove modules of an extension, it is recommended to use the [Extensions Manager](extensions.md#extensions-manager) instead of just removing its module paths.

It is also possible to start Slicer by temporarily adding module paths (not saved in settings) by passing the arguments in the command line.

For example this command will start Slicer trying to load CLIs found in the specified directory:

    Slicer.exe --additional-module-paths C:\path\to\lib\Slicer-X.Y\cli-modules

#### Modules

List of modules loaded, ignored or failed to load in Slicer. An unchecked checkbox indicates that module should not be loaded (ignored) next time Slicer starts. A text color code is used to describe the state of each module:

-  Black: module successfully loaded in Slicer
-  Gray: module not loaded because it has been ignored (unchecked)
-  Red: module failed to load. There are multiple reasons why a module can fail to load.

Look at startup [log outputs](https://www.slicer.org/wiki/Documentation/Nightly/SlicerApplication/ErrorLog) to have more information.
If a module is not loaded in Slicer (ignored or failed), all dependent modules won't be loaded. You can verify the dependencies of a module in the tooltip of the module.

You can filter the list of modules by untoggling in the advanced (>>) panel the "To Load", "To Ignore", "Loaded", "Ignored" and "Failed" buttons.

#### Home

Module that is shown when Slicer starts up.

#### Favorites

List of modules that appear in the Favorites toolbar:

![](https://github.com/Slicer/Slicer/releases/download/docs-resources/settings_favorites_toolbar.png)

To add a module, drag&drop it from the *Modules* list above. Then use the advanced panel (>>) to reorganize/delete the modules within the toolbar.

### Appearance

#### Style

The overall theme of Slicer is controlled by the selected Style:
- Slicer (default): it sets the style based on theme settings set by the operating system.
  For example, on Windows if [dark mode](https://blogs.windows.com/windowsexperience/2016/08/08/windows-10-tip-personalize-your-pc-by-enabling-the-dark-theme/)
  is turned on for apps, then the `Dark Slicer` style will be used upon launching Slicer. Currently, automatic detection of dark mode is not available on Linux,
  therefore use needs to manually select `Dark Slicer` style for a dark color scheme.
- Light Slicer: application window background is bright, regardless of operating system settings.
- Dark Slicer: application window background is dark, regardless of operating system settings.

#### Slicer Studio appearance

In Slicer Studio the Appearance panel has more choices, applied as soon as you make them (density after a restart).
They are saved under the fork's own keys `Appearance/*`, and none of them changes the settings that official 3D Slicer
reads, so other 3D Slicer installations that share your settings file keep their own look.

- **Theme** (`Appearance/Theme`): System, Dark or Light. Dark is the default. System follows the operating system,
  as the Style does. The Theme sets the colours of the *Slicer* style
  described above. Choosing *Dark Slicer* or *Light Slicer* as the Style in the Advanced section is an explicit
  choice of a fixed style and overrides the theme.
- **Accent color** (`Appearance/Accent`): the color of selected items, links and highlighted controls. The **Reset**
  button returns to the default accent of the theme.
- **Density** (`Appearance/Density`): *Comfortable* (default) or *Compact*, which uses the spacing of earlier
  3D Slicer versions. Takes effect after a restart.
- **Font and size** (`Appearance/Font`): the interface font is *Inter*. While you have not chosen another font, Inter
  is applied when the application starts and nothing is written to the settings; the `font` key that official 3D Slicer uses
  is not changed. A font that you choose here is saved under `Appearance/Font` and applied by Slicer Studio only.
  In the classic interface and with `--testing`, the standard font setting of 3D Slicer is used as before.

These choices have no effect when Slicer is started with `--testing`, which always uses the light theme and
the standard sizes.

### Views

The **Views** panel has the usual defaults for new slice and 3D views, and these additions:

- **Flat view bars** (`Appearance/FlatViewBars`): shows compact, flat bars above the slice and 3D views (orientation,
  slice position, link, options, maximize). Uncheck to use the classic bars. On by default in Slicer Studio, off with
  `--testing`. Takes effect after a restart.
- **3D view background** (*Top color*, *Bottom color*, *Show box*, *Box color*): defaults of new 3D views, which follow
  the theme until you change them. They are saved immediately under `Appearance/Default3DView/*`, which other
  installations ignore. **Use theme colors** returns to the theme.
- **Navigation in Studio**: three behaviours that apply only while the Studio interface is shown (see
  [Navigation in Studio](user_interface.md#navigation-in-studio)): **Left-drag adjusts window/level**
  (`Studio/LeftDragWindowLevel`, on), **Keys act on the view under the mouse** (`Studio/FocusFollowsMouse`, on) and the
  **Page Up / Page Down step** (`Studio/PageStepSlices`, 10 slices). The Classic interface, `--testing` and
  scripts keep the official behaviour whatever these say.

### Saving

The **Saving** panel (in the Application Settings of Slicer Studio and of its classic interface; the save status in
the top bar of Studio also leads to it) controls where and how often your work is saved. See
[Where Slicer Studio saves your work](studio_projects.md) for projects and
[Autosave and recovery](modules/studio.md#autosave-and-recovery) for the recovery copies. The panel has:

| Setting | Key | Default | Range |
| ------- | --- | ------- | ----- |
| **Slicer Studio folder** (**Change…**, **Show**) | `Studio/Projects/Root` (`Studio/Projects/RootChosen` once you chose it) | `Slicer Studio` in Documents | a folder |
| **Save continuously** | `Studio/Autosave/Enabled` | on | |
| **At most every** (minutes) | `Studio/Autosave/IntervalMinutes` | 2 | 1-60 |
| **or after a pause of** (seconds) | `Studio/Autosave/IdleSeconds` | 10 | 3-300 |
| **Location** | `Studio/Autosave/Directory` | empty, meaning the private folder `StudioAutosave` in the application data folder | a folder |
| **Keep** (sessions) | `Studio/Autosave/KeepSessions` | 5 | 1-50 |
| **Max size** (MB) | `Studio/Autosave/MaxDiskMB` | 2048 | 256-102400 |
| **Delete after** (days) | `Studio/Autosave/MaxAgeDays` | 14 | 1-365 |
| **Skip data larger than** (MB) | `Studio/Autosave/MaxNodeMB` | 2048 | 16-102400 |

The pause and the interval apply to projects and to recovery copies; **Location**, **Keep**, **Max size** and **Delete
after** apply to the recovery copies of scenes that are not projects (Classic, studies). **Change…** offers to move the
projects, studies and templates to the new folder. The buttons **Recover autosaved work…**, **Autosave now** and **Delete all
autosaves…** (permanent, for closed sessions) are in the panel too. The environment variable
`SLICER_STUDIO_PROJECTS_ROOT` sets the Slicer Studio folder (the first-start question is then not asked),
`SLICER_STUDIO_PROJECTS=0` turns projects off, `SLICER_STUDIO_AUTOSAVE_DIR` overrides the recovery location and
`SLICER_STUDIO_AUTOSAVE=0` turns autosave off. The panel is not present and autosave does not run with `--testing`
or `--no-main-window`. Choose a private folder: autosaves can contain patient data, and a folder that is synchronized
to the cloud or shared is not private.

### Developer

#### Developer mode

Enable the following features:

* Testing modules are loaded: modules whose name ends with `Test` or `Tests` are only loaded at application startup if developer mode is enabled. Skipping them shortens the application startup time, in particular for scripted testing modules, which would need to be imported just to be registered. Testing modules are always loaded if the application is started in testing mode, as automated tests rely on them.
* [Module selection toolbar](user_interface.md#toolbar): Modules associated with the _Testing_ category are visible by default.
* [WebServer module](modules/webserver.md): Javascript logging is enabled by default.
* [Module panel](user_interface.md#module-panel): `Reload & Test` module panel section is displayed for scripted modules. It includes controls for reloading, testing and editing scripted modules as well as restarting the application.

modules/webserver.html


## Slicer Studio settings

Slicer Studio stores its own choices under keys that start with `Studio/` or `Appearance/`. Official 3D Slicer
does not read them, so installations that share your settings file are not affected. Slicer Studio **never rewrites a
key that official 3D Slicer reads** (for example `Styles/Style`, `font`, `Modules/*` or `Views/*`); only a change you make
yourself in the Application Settings of that installation does. The one thing Studio does write back is the window: when you
close the window it stores the classic window state and a portable layout under `MainWindow/windowState` and
`MainWindow/layout`, so an official installation that shares `Slicer.ini` opens with its normal window and layout, not with Studio's. When Slicer is started with `--testing` or
`--disable-settings`, the temporary settings are used and none of the keys below is written.

Most keys are written by the application and need not be edited. The keys that you may want to set (in
`Slicer.ini`, see [Settings file location](#settings-file-location)):

| Key | Default | Meaning |
| --- | ------- | ------- |
| `Studio/Enabled` | true | Set to false to start in the classic interface always. Environment variable `SLICER_CLASSIC_UI=1` does it once |
| `Studio/Mode` | studio | `classic` when you left the Studio interface with Ctrl+Alt+0; changed by the switch |
| `Studio/StartWorkspace` | Library | The workspace the window opens on (Library, Draw or Map) |
| `Studio/LastWorkspace` | | The workspace you used last |
| `Studio/PanelWidth` | 340 | Width of the inspector in pixels (300-480; changed by dragging its edge) |
| `Studio/BrushDiameterMm` | 3.0 | Size of the brush in millimetres, remembered between sessions |
| `Studio/FollowStrokes` | true | The "Follow" option of Draw: views follow the strokes |
| `Studio/BracketKeysResizeBrush` | true (false with `--testing`) | `[` and `]` change the brush size |
| `Studio/LeftDragWindowLevel`, `Studio/FocusFollowsMouse`, `Studio/PageStepSlices` | true, true, 10 | Navigation in Studio, see above |
| `Studio/LastSaveFolder` | | The folder of the last lesion you saved |
| `Studio/RecentStudies`, `Studio/RecentFolders`, `Studio/RecentOpenTimes` | | The recent lists of Library |
| `Studio/PaletteMRU` | | The entries of Search that you ran last |
| `Studio/AllowedToolbars` | | Toolbars you chose to show with "Show toolbar…" |
| `Studio/Library/StudyView`, `Studio/Library/GridThumbSize` | list, 160 | The subject view of Library (list or grid) and the size of the grid cards |
| `Studio/Tour/Offered`, `Studio/Tour/Completed` | | Whether the guided tour was offered, and the tours you completed |
| `Studio/LazyPythonWrappers` | true | Load the Python wrappers of the modules when they are first used, which shortens the start. Set to false for the old behaviour |
| `Studio/SmartFill/*` | see [Segment Editor](modules/segmenteditor.md#smart-fill) | The defaults of Smart fill: `Mode`, `AutoTolerance`, `TolerancePercent`, `MaxRadiusMm`, `LeakGuardMm`, `FillHoles`, `StayInsideBrain`, `ApplyOnClick`. Written only when you change them; ignored with `--testing` |
| `Studio/Autosave/*` | see the table above | The autosave settings |
| `Studio/LNM/Connectomes` | | The connectomes registered in the module (their folders, in the setting only: never in a shared file) |
| `Studio/LNM/DataDirectory` | the `SlicerStudio` folder in your documents | Where network mapping data and normalization outputs go when no study is open |
| `Studio/LNM/AcceptedTerms/<sha>` | false | You ticked "I accept these data use terms" for the connectome with this manifest hash |
| `Studio/LNM/MaxJobThreads` | number of cores minus one | The most threads a background job uses |
| `Studio/LNM/MemoryBudgetGB` | the smaller of a quarter of the memory and 4 | The memory a cohort pass may use |
| `Studio/LNM/Normalize/LastOptions` | | The options of the last normalization (JSON) |
| `Studio/LNM/Pipeline/Options` | | The options of Run everything (JSON) |
| `Appearance/Theme`, `Appearance/Accent`, `Appearance/Density`, `Appearance/Font`, `Appearance/FlatViewBars`, `Appearance/Default3DView/*` | see above | The look of the interface |

Study-level choices are not in `Slicer.ini` but in the study's `study.json` (for example `normalization`, with the options
of automatic normalization), so that they travel with the study.

## Compatibility with official 3D Slicer

Slicer Studio is 3D Slicer with a different default interface, and it is built to exchange data with the official
releases.

- **Scenes and data saved by Slicer Studio load in official 3D Slicer 5.12 and 5.13, and the other way round.** Volumes, segmentations,
  markups, tables, colour tables, transforms and scenes keep their formats. No new node or storage types are added and
  the files written by the existing storage nodes are unchanged.
- **Layouts are portable.** The layouts of Slicer Studio (Focus, Focus + 3D, Row, Compare) are saved in a scene as the
  Conventional layout together with an extra attribute. Official 3D Slicer shows its Conventional layout (slice and 3D
  views), and Slicer Studio restores its own layout.
- **Settings are shared.** All installations of one user read and write `Slicer.ini`. Slicer Studio writes only its
  own keys (above) and never rewrites a key that official versions read (apart from the window state and layout written back when you close the window, above), so using Slicer Studio does not change how
  your official installation looks or behaves. (The one exception is a change that you make yourself in the
  Application Settings.)
- **Extra files are harmless.** The sidecars next to lesion masks (`.json`), the files of a study, and the maps of
  lesion network mapping are standard NIfTI, JSON and TSV files. Official 3D Slicer, FSL and FSLeyes ignore the
  sidecars.
- **Check it yourself.** `Utilities/Scripts/SlicerFileCompatibilityCheck.py` saves a test scene in each installation you
  name and loads it in every other one, then compares what was loaded. It runs each installation with its own temporary
  settings, so your settings are not touched:

      python3 Utilities/Scripts/SlicerFileCompatibilityCheck.py \
          --slicer fork=/path/to/Slicer \
          --slicer official=/path/to/official/Slicer --out compat-report

  The result is in `report.txt` and `report.json` in the output folder; the exit code is not zero if a check
  fails. See [Slicer Studio](modules/studio.md#compatibility-with-official-3d-slicer).

## Information for Advanced Users

### Settings file location

Settings are stored in *.ini files. If the settings file is found in application home directory (within organization name or domain subfolder) then that .ini file is used. This can be used for creating a portable application that contains all software and settings in a relocatable folder. Relative paths in settings files are resolved using the application home directory, and therefore are portable along with the application.

If .ini file is not found in the application home directory then it is searched in user profile:

-  Windows: `%USERPROFILE%\AppData\Roaming\slicer.org\` (typically `C:\Users\<your_user_name>\AppData\Roaming\slicer.org\`)
-  Linux: `~/.config/slicer.org/`
-  Mac: `~/.config/slicer.org/`

Deleting the *.ini files restores all the settings to default.

There are two types of settings: `user specific settings` and `user and revision specific settings`.

#### User specific settings

This file is named `Slicer.ini` and it stores settings applying to *all versions* of Slicer installed by the *current user*.

To display the exact location of this settings file, open a terminal and type:

    ./Slicer --settings-path

On Windows:

    Slicer.exe --settings-path | more

or enter the following in the Python console:

    slicer.app.slicerUserSettingsFilePath

#### User and revision specific settings

This file is named like `Slicer-<REVISION>.ini` and it stores settings applying to a *specific revision* of Slicer installed by the *current user*.

To display the exact location of this settings file, enter the following in the Python console:

    slicer.app.slicerRevisionUserSettingsFilePath

### Application startup file

Each time Slicer starts, it will look up for a startup script file named <code>.slicerrc.py</code>. Content of this file is executed automatically at each startup of Slicer.

The file is searched at multiple location and the first one that is found is used. Searched locations:
- Application home folder (`slicer.app.slicerHome`)
- Path defined in `SLICERRC` environment variable
- User profile folder (`~/.slicerrc.py`)

You can find the path to the startup script in Slicer by opening in the menu: Edit / Application Settings. ''Application startup script'' path is shown in the ''General'' section (or running `getSlicerRCFileName()` command in Slicer Python console).

### Startup performance optimization

If the application takes a long time to start up then the `--report-startup-timing` command-line option can be used to find out where that time is spent. Once the startup is complete, a report is written to the application log (it can be displayed by opening in the menu: View / Error Log), containing:
- how much time was spent before the application's entry point was reached, which is the time the operating system needed to load the application's own libraries,
- how much time each startup phase took (initializing the application, registering, instantiating and loading modules, initializing the user interface, and showing the main window),
- the slowest 15 modules, each with the time it took to instantiate and to load it,
- the total time measured from the creation of the process,
- what the startup file prefetcher did, if it is available in the build (see below).

For example:

    Slicer.exe --report-startup-timing

The option can be combined with `--exit-after-startup`, which quits the application as soon as the startup is complete, and with `--disable-modules` or `--disable-scripted-loadable-modules`, to see how much of the startup time a group of modules is responsible for.

The application libraries can be read into the file system cache on background threads while the application starts up, so that the work the operating system does when a library is first touched overlaps with the startup instead of delaying it. On Windows that work is on-access virus scanning, which is slow enough that this prefetching is enabled by default there. On Linux and macOS there is no such scanning and only the file cache is gained, which is worth a lot on a cold cache, a network file system or a spinning disk and nothing on a warm one, so prefetching is disabled by default and can be turned on with the `SLICER_STARTUP_FILE_PREFETCH` environment variable described below. Startup timing reports include how many libraries were prefetched and how many of the libraries this startup needed were predicted by the previous run, which tells if the prefetching could help at all.

Testing modules are not loaded unless [developer mode](#developer-mode) is enabled, which also shortens the startup time.

### Runtime environment variables

The following environment variables can be set before the application is started to fine-tune its behavior:
- `PYTHONNOUSERSITE`: if it is set to `1` then import of user site packages is disabled. For example, this will prevent Slicer to reuse packages downloaded/built by Anaconda.
- `QT_SCALE_FACTOR`: see [Qt documentation](https://doc.qt.io/qt-5/highdpi.html). For example, font size can be reduced by running `set QT_SCALE_FACTOR=0.5` in the command console and then starting Slicer in that console.
- `QT_ENABLE_HIGHDPI_SCALING`: see [Qt documentation](https://doc.qt.io/qt-5/highdpi.html)
- `QT_SCALE_FACTOR_ROUNDING_POLICY`: see [Qt documentation](https://doc.qt.io/qt-5/highdpi.html)
- `QTWEBENGINE_REMOTE_DEBUGGING`: port number for Qt webengine remote debugger. Default value is `1337`.
- `SLICER_OPENGL_PROFILE`: Requested OpenGL profile. Valid values are `no` (no profile), `core` (core profile),
  and `compatibility` (compatibility profile). Default value is `compatibility` on Windows systems.
- `SLICER_BACKGROUND_THREAD_PRIORITY`: Set priority for background processing tasks. On Linux, it may affect the
  entire process priority. An integer value is expected, default = `20` on Linux and macOS, and `-1` on Windows.
- `SLICER_STARTUP_FILE_PREFETCH`: If it is set to `1` then the application libraries are read into the file system cache
  while the application starts up, and if it is set to `0` then they are not. If the variable is left unset (or set to any
  other value) then prefetching is enabled on Windows, where parallel file prefetching significantly improve startup time, and
  disabled on Linux and macOS, where only the file cache is gained and whether that helps depends on the computer.
  It has an effect only in builds configured with `Slicer_BUILD_STARTUP_FILE_PREFETCH` enabled.
- `SLICER_STARTUP_FILE_PREFETCH_THREADS`: Number of threads used for prefetching the application libraries at startup.
  A positive integer value is expected, by default the number of CPU cores is used, limited to the range of `2` to `8`.
- `SLICERRC`: Custom application startup file path. Contains a full path to a Python script. By default it is `~/.slicerrc.py` (where ~ is the user profile a.k.a user home folder).
- `SLICER_EXTENSIONS_MANAGER_SERVER_URL`: URL of the extensions manager backend with the `/api` path. Default value is retrieved from the settings using the key `Extensions/ServerUrl`.
- `SLICER_EXTENSIONS_MANAGER_FRONTEND_SERVER_URL`: URL of the extension manager frontend displaying the web page. Default value is retrieved from the settings using the key `Extensions/FrontendServerUrl`.
- `SLICER_EXTENSIONS_MANAGER_SERVER_API`: Supported value is `Girder_v1`. Default value is hard-coded to `Girder_v1`.

### Qt built-in command-line options

Slicer application accepts standard Qt command-line arguments that specify how Qt interacts with the windowing system.

Examples of options:
- `-qwindowgeometry geometry`, specifies window geometry for the main window using the X11-syntax. For example: `-qwindowgeometry 100x100+50+50`.
- `-display hostname:screen_number`, switches displays on X11 and overrides the `DISPLAY` environment variable.
- `-platform windows:dpiawareness=[0|1|2]`, sets the [DPI awareness](https://doc.qt.io/qt-5/highdpi.html#microsoft-windows) on Windows.
- `-widgetcount`, prints debug message at the end about number of widgets left undestroyed and maximum number of widgets existed at the same time.
- `-reverse`, sets the application's layout direction to `Qt::RightToLeft`.

To learn about the supported options:
- https://doc.qt.io/qt-5/qapplication.html#QApplication
- https://doc.qt.io/qt-5/qguiapplication.html#supported-command-line-options

:::{note}

Since the Slicer launcher is itself a Qt application and the Qt built-in command-line options are expected to **only** be passed to
the launched application `SlicerApp-real` and not the Slicer launcher, the list of arguments to filter is specified in the [Main.cpp](https://github.com/commontk/AppLauncher/blob/master/Main.cpp#L35) found in the `commontk/AppLauncher` project.

:::

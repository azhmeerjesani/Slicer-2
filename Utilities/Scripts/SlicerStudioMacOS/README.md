# Build the Slicer Studio disk image (macOS)

`build_dmg.sh` turns this source tree into an installable macOS disk image,
`SlicerStudio-<version>-macos-<arch>.dmg`. The image holds
**Slicer Studio.app** and a link to the Applications folder. It installs next
to an official 3D Slicer without replacing it.

The script runs on the Mac. It checks the machine, installs the build tools with
Homebrew, gets Qt, builds the Slicer superbuild in Release mode, packages it
with CPack and makes the final disk image. Disk images are built on a Mac with
this script. The CI workflow file in the repository is only a reference (see
[The CI workflow file](#the-ci-workflow-file)).

## Quick start

You need:

- macOS 14 (Sonoma) or newer, on Apple Silicon (arm64) or Intel (x86_64);
- the Xcode Command Line Tools (`xcode-select --install`);
- [Homebrew](https://brew.sh);
- about 40 GB of free disk space, and several hours for the first build.

From the top of the source tree, run:

```console
Utilities/Scripts/SlicerStudioMacOS/build_dmg.sh
```

When it finishes, the disk image is in `~/SlicerStudio/dist/`. If the build stops
(an error, a reboot, Ctrl+C), run the same command again: it continues where it
stopped. To see what the script would do without changing anything, add
`--dry-run`.

Everything the script creates goes into `~/SlicerStudio`:

| Path | Content |
|---|---|
| `~/SlicerStudio/S` | superbuild directory (`--build-dir`; keep the path short and without spaces) |
| `~/SlicerStudio/dist` | final disk image and its `.sha256` checksum file (`--out-dir`) |
| `~/SlicerStudio/logs` | `configure.log`, `build.log`, `package.log`, `packageverify.log`, `smoke-<arch>.log` |
| `~/SlicerStudio/Qt-official`, `Qt-source` | Qt used by `--qt=official` or `--qt=source` |
| `~/SlicerStudio/tools` | CMake 3.31 (when no CMake 3 is installed) and the aqtinstall environment |
| `~/SlicerStudio/ccache-bin` | compiler wrappers that use [ccache](https://ccache.dev) |

## Choosing Qt

| `--qt=` | Qt | The disk image runs on | Notes |
|---|---|---|---|
| `homebrew` (default) | Homebrew's current Qt 6 | the build Mac, and Macs with the same or a newer macOS | Fastest to set up. Homebrew only provides Qt for Apple Silicon Macs with macOS 15 or newer. On other Macs, `brew install qt` compiles Qt for hours (the script warns): use `official` there. |
| `official[=6.11.2]` | official Qt binaries, downloaded with [aqtinstall](https://github.com/miurahr/aqtinstall) | macOS 14 and newer (the minimum of this source tree; Qt 6.11 itself supports macOS 13) | Use this for a disk image you share. It downloads roughly 1 to 2 GB. |
| `source[=6.11.2]` | Qt built from source by `Utilities/Scripts/Build-qt6.sh` | macOS 14 and newer | Takes hours and needs the full Xcode (Metal toolchain). Build-qt6.sh assumes Homebrew in `/opt/homebrew`. |
| `/path/to/Qt` | an existing Qt 6.8+ or Qt 5.15 installation (its prefix, or its `lib/cmake/Qt6` or `lib/cmake/Qt5` folder) | depends on that Qt | For example, a Qt from the Qt online installer: `--qt=~/Qt/6.11.2/macos`. |

The oldest macOS a build can target is set by the source tree:
`required_deployment_target` in `CMake/SlicerInitializeOSXVariables.cmake`,
currently 14.0. The script reads it from there and uses it as the deployment
target (`CMAKE_OSX_DEPLOYMENT_TARGET`). A lower `--macos-min` is refused.

The script also reads the minimum macOS version of the Qt libraries. When that
version is newer, the script raises the deployment target to match. It records
the result in the application (`LSMinimumSystemVersion`), so Finder explains why
the application cannot start on an older macOS. The build runs the programs it
builds, so the build Mac itself must run at least that macOS version.

### Packaging with Homebrew's Qt

The macOS build instructions warn about errors such as
`'@rpath/QtGui.framework/...' is not absolute` when packaging with a Qt that was
not built from source. Those errors came from CMake's `fixup_bundle()`. This
source tree no longer uses `fixup_bundle()`, for the following reasons.

- `CMake/SlicerCPackBundleFixup.cmake.in` calls
  `Utilities/Scripts/SlicerBundleCloseDeps.py`. It resolves absolute Homebrew
  install names and `@rpath` references, then copies the Qt frameworks and their
  Homebrew dependencies (ICU, glib, freetype and others) into the bundle.
- `CMake/SlicerBlockInstallQtPlugins.cmake` resolves Homebrew's symlinked Qt
  plugins.

After `make package`, the script runs `make packageverify`. That target fails if
any library in the bundle still loads from outside the bundle. The result is
shown as "bundle check" at the end of the run.

Homebrew Qt has some limits that remain:

- A disk image built with Homebrew Qt is not redistributable to older macOS
  versions.
- Homebrew's `qt` may be a newer Qt than the versions this source tree has been
  tested with. If it does not compile, use `--qt=official`.
- The script never upgrades installed Homebrew packages. Homebrew can still
  upgrade Qt later, for example during a `brew upgrade`. The next run detects
  that the Qt version changed and asks for `--clean`.

## Options

Run `build_dmg.sh --help` for the complete list. Common combinations:

| Command | Effect |
|---|---|
| `build_dmg.sh` | Build (or continue the build) and package. |
| `build_dmg.sh --qt=official` | Make a disk image for macOS 14 and newer. |
| `build_dmg.sh --skip-deps --skip-build` | Package an existing build again. |
| `build_dmg.sh --no-package` | Build only. |
| `build_dmg.sh --clean` | Delete the build directory and start again. The compiler cache keeps a rebuild short. |
| `build_dmg.sh --reconfigure --cmake-arg=-DSlicer_USE_SimpleITK:BOOL=OFF` | Change the CMake configuration of an existing build. |
| `build_dmg.sh --smoke-only` | Start the newest disk image's application as a test. |
| `build_dmg.sh --jobs 4` | Limit the number of parallel compile jobs. By default, the script uses one job per CPU, but at most one per 2 GB of memory. |
| `build_dmg.sh --ci` | Never ask questions. Build output goes to the log file, with a progress line every 5 minutes. |

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Unexpected failure |
| 2 | Usage error |
| 3 | Preflight check failed |
| 4 | Installing dependencies or Qt failed |
| 5 | Configuration failed |
| 6 | Build failed |
| 7 | Packaging failed |
| 8 | Start-up test failed |

## What the script does

1. **Preflight.** The script checks:
   - the macOS version (14 or newer) and the architecture;
   - the Xcode Command Line Tools and Homebrew;
   - Rosetta 2, on Apple Silicon only;
   - free disk space;
   - that the paths contain no spaces and stay short.

   Rosetta 2 is needed because the CTK application launcher, used by the build
   tree and by the bundled `PythonSlicer`, is only released as an x86_64
   binary.
2. **Tools.** `brew install` of git-lfs, ccache and, with `--qt=homebrew`, qt.
   Only missing packages are installed.

   The superbuild is configured with CMake 3: this source tree validates CMake
   policies up to 3.28, and the macOS build instructions use 3.31. A CMake
   3.28 to 3.x found on `PATH` is used. Otherwise, for example when only
   Homebrew's CMake 4 is installed, the script downloads the official CMake
   3.31.10 binaries from Kitware into `~/SlicerStudio/tools`, after checking
   their SHA-256 checksum. With `--skip-deps` and only CMake 4, the script uses
   it with `CMAKE_POLICY_VERSION_MINIMUM=3.5`, so that older external projects
   still configure, and warns that this is untested.
3. **Qt.** The script finds or installs Qt, as described in
   [Choosing Qt](#choosing-qt).
4. **Configure.** The superbuild is configured with these options:
   - `Release`, `BUILD_TESTING=OFF`, `Slicer_BUILD_DOCUMENTATION=OFF`;
   - `Slicer_USE_VTK_DEBUG_LEAKS=OFF`, `Slicer_BUILD_QT_DESIGNER_PLUGINS=OFF`;
   - `CMAKE_OSX_ARCHITECTURES=$(uname -m)`, `CMAKE_OSX_DEPLOYMENT_TARGET`, the SDK;
   - `Qt6_DIR` (or `Qt5_DIR`) and the matching `Slicer_REQUIRED_QT_VERSION`;
   - `Slicer_USE_SYSTEM_QT=OFF`, so that Qt is embedded in the package.

   The compilers are wrapper scripts that call ccache. The superbuild passes the
   compiler path on to every external project (VTK, ITK, CTK, Python and the
   rest), so all of them are cached.

   The script records the source, architecture, Qt and minimum macOS of the
   build directory. Reusing that build directory with a different source,
   architecture, Qt or minimum macOS is refused.
5. **Build.** `make -C <build-dir> -j<jobs>`.
6. **Package.** `make package` in `Slicer-build` (CPack DragNDrop), then
   `make packageverify`.
7. **Disk image.** The script then runs these steps:
   1. Mount the CPack disk image and copy its application as `Slicer Studio.app`.
   2. Set `CFBundleName` and `CFBundleDisplayName` to "Slicer Studio",
      `CFBundleIdentifier` to `org.slicer.slicerstudio`, and
      `LSMinimumSystemVersion` in `Info.plist`. This happens before signing,
      because `Info.plist` is sealed by the signature.
   3. Sign the bundle again ad hoc, inside out, with
      `Utilities/Scripts/SlicerSignBundleMacOS.py`. That script is more reliable
      than `codesign --deep` on a bundle this large. Then check the result with
      `codesign --verify --deep --strict`.
   4. Create the disk image with `hdiutil` (UDZO, volume name "Slicer Studio",
      with an `Applications` link), verify it, and write a SHA-256 checksum next
      to it.
8. **Start-up test.** The script mounts the final disk image and starts
   `Slicer Studio.app/Contents/MacOS/Slicer` with `--no-splash
   --disable-settings --exit-after-startup` and a short Python check (main
   window, core modules, numpy, ssl). It also runs the bundled `PythonSlicer`.
   `--disable-settings` keeps the test away from your `Slicer.ini`. Skip the test
   with `--no-smoke`.

## Next to the official 3D Slicer

The script's changes keep the two applications apart:

- The application is named **Slicer Studio.app**. Official releases install as
  "3D Slicer <version>.app" (older ones as "Slicer.app").
- The bundle identifier is `org.slicer.slicerstudio`. Official Slicer uses
  `org.slicer.slicer`, so Launch Services, the Dock and privacy permissions keep
  the two apart.

Some things stay shared on purpose:

- **User settings.** `~/.config/slicer.org/Slicer.ini` is used by every Slicer
  installation. Slicer Studio keeps its own preferences under keys that
  official Slicer does not read (`Studio/*`, `Appearance/*`). It does not
  rewrite the values official Slicer uses.
- **Per-installation files.** Revision-specific settings, extensions and Python
  packages are stored inside each application, as in official Slicer. Copy
  the application to Applications before installing extensions: a mounted
  disk image is read-only.
- **Data files.** Scenes and data files saved by Slicer Studio open in
  official 3D Slicer 5.12 and 5.13, and the other way round.

## Installing the disk image

Open the disk image and drag **Slicer Studio** to **Applications**.

The application is signed ad hoc but not notarized by Apple. When it is
downloaded, macOS blocks it the first time. Do one of the following:

- Open it once, then go to **System Settings > Privacy & Security** and click
  **Open Anyway**. On macOS 14, you can instead right-click the application
  and choose **Open**.
- Or, in Terminal, run:

  ```console
  xattr -dr com.apple.quarantine "/Applications/Slicer Studio.app"
  ```

A disk image built on your own Mac is not quarantined.

On Apple Silicon, the bundled `PythonSlicer` needs Rosetta 2:

```console
softwareupdate --install-rosetta --agree-to-license
```

The first start after installing can take a minute or two, while macOS checks
the application's libraries. Slicer shows a progress window during that time.

## The CI workflow file

`.github/workflows/studio-packages.yml` is a GitHub Actions workflow that runs
the build steps of the packages on a clean machine, using this script for the
disk image. It is disabled and stays disabled: GitHub is only used as the git
remote, and no package is built or published there. The file is kept as a
reference for the build steps. Build disk images on a Mac with this script (see
[Quick start](#quick-start)).

## Troubleshooting

- **`Bad CPU type in executable`** (PythonSlicer, launcher): install Rosetta 2
  with `softwareupdate --install-rosetta --agree-to-license`.
- **`malformed mach-o: load commands size`**: the build path is too long.
  Use a short `--build-dir`, such as `~/S`, together with `--clean`.
- **The build stopped**: `~/SlicerStudio/logs/build.log` contains the error.
  Fix the cause and run the script again. With parallel jobs, the first error
  can be far above the end of the log, so search the log for `error:`.
- **`PythonQt: missing generated wrapper sources`**: the PythonQt wrapper
  generator (run while CTK builds) found no Qt headers. Homebrew installs Qt as
  one package per module, so the script gives the generator a folder that links
  all of them (`~/SlicerStudio/pythonqt-qt`, passed as `QTDIR`) and reruns it
  when an earlier run left no wrappers: run the script again. If it still
  stops, it prints the generator's own report; build with `--qt=official
  --clean` instead.
- **`configured with a different source, architecture, Qt or minimum macOS`**:
  Homebrew upgraded Qt, or a different `--qt` or `--macos-min` was given. Run
  again with `--clean`. The compiler cache keeps the rebuild short.
- **`older than the minimum macOS of the build`**: the Qt libraries (or
  `--macos-min`) require a newer macOS than the build Mac runs. Use a lower
  `--macos-min`, or a Qt built for this macOS, such as `--qt=official`.
- **`bundle check: FAILED`**: some library in the bundle still loads from
  outside of it. `~/SlicerStudio/logs/packageverify.log` lists those libraries.
  The disk image probably works on the build Mac only. Try `--qt=official`.
- **The application is "damaged" or "can't be opened"**: see
  [Installing the disk image](#installing-the-disk-image).
- **Running the script without a Mac**: `--dry-run` prints the full plan on any
  system. On Linux, fake the platform with
  `SLICER_STUDIO_FAKE_UNAME=Darwin/arm64 build_dmg.sh --dry-run`. A third field
  sets the macOS version, as in `Darwin/x86_64/14.7`. The variable is refused
  without `--dry-run`.

# Install Slicer Studio

Slicer Studio is a redesigned edition of 3D Slicer, built from this source
tree. It comes in three forms:

- a native macOS application, as a disk image (`.dmg`);
- a Linux package, as a `.tar.gz` file;
- a web version that runs on your computer and opens in a web browser.

None of these is on download.slicer.org. You build them yourself, on your own
computer.

Slicer Studio works side by side with official 3D Slicer:

- Scenes and data files saved by Slicer Studio open in official 3D Slicer 5.12
  and 5.13, and files saved by official 3D Slicer open in Slicer Studio.
- Both applications can be installed at the same time.
- Coming from 3D Slicer? [Coming from 3D Slicer](studio_coming_from_slicer.md) shows where everything went.

## Build the packages

Each form is built on your own computer:

- **macOS disk image:** run `Utilities/Scripts/SlicerStudioMacOS/build_dmg.sh`
  on your Mac (see [macOS](#macos)).
- **Linux package:** run `make package` in the `Slicer-build` folder of a Linux
  build (see [Linux](#linux)).
- **Web version:** build a Docker image from the Linux package (see
  [In a web browser](#in-a-web-browser)).

A first build takes several hours.

## macOS

### Build the disk image

Build the disk image on your Mac. Run this command from the top of the source
tree:

```console
Utilities/Scripts/SlicerStudioMacOS/build_dmg.sh
```

Building needs macOS 14 (Sonoma) or newer, the Xcode Command Line Tools,
Homebrew, about 40 GB of free disk space and several hours. The disk image is
written to `~/SlicerStudio/dist/`. If the build is interrupted, run the same
command again to continue.

`Utilities/Scripts/SlicerStudioMacOS/README.md` explains the options, for
example how to choose the Qt version. It also explains how to make a disk image
that runs on other Macs (`--qt=official`).

Which Macs a disk image runs on depends on how it was built:

| Disk image | Runs on |
|---|---|
| Built on your Mac with the default settings (Homebrew's Qt) | Your Mac, and Macs with the same or a newer macOS |
| Built on your Mac with `--qt=official` | macOS 14 or newer, on the same kind of processor as your Mac |

On an Apple Silicon Mac with macOS 14, or on an Intel Mac, build with
`--qt=official`: Homebrew only has a ready-made Qt for Apple Silicon with
macOS 15 or newer, and compiles Qt for hours elsewhere.

### Install

1. Open the disk image.
2. Drag **Slicer Studio** to **Applications**. Applications must be writable
   for installing extensions and Python packages, so do not run the application
   from the disk image.
3. Start **Slicer Studio** from Applications.

Slicer Studio is signed ad hoc but not notarized by Apple. The first time you
open a downloaded copy, macOS blocks it. To allow it, do one of the following:

- Try to open the application, then go to **System Settings** >
  **Privacy & Security**. Click **Open Anyway** next to the message about
  Slicer Studio, and confirm with **Open**. On macOS 14, you can instead
  right-click the application and choose **Open**.
- Or, in Terminal, run:

  ```console
  xattr -dr com.apple.quarantine "/Applications/Slicer Studio.app"
  ```

  Use this command if macOS reports that the application "is damaged and can't
  be opened".

On a Mac with Apple Silicon, also install Rosetta 2 once. The bundled Python
launcher (`PythonSlicer`), used for background processing, needs it:

```console
softwareupdate --install-rosetta --agree-to-license
```

The first start can take a minute or two, while macOS checks the application's
libraries. A progress window is shown during that time.

To uninstall Slicer Studio, delete **Slicer Studio** from Applications.

### Next to official 3D Slicer

- **Separate applications.** Slicer Studio is installed as
  "Slicer Studio.app", with its own bundle identifier
  (`org.slicer.slicerstudio`). Official 3D Slicer installs as
  "3D Slicer <version>.app" (older versions as "Slicer.app"), so the two do not
  replace each other. Each application keeps its own extensions and Python
  packages.
- **Shared settings.** All Slicer installations use the same user settings
  file, `~/.config/slicer.org/Slicer.ini` (see [Settings](settings.md)), so
  recent files and other preferences carry over. Slicer Studio stores its own
  preferences under separate keys, so official 3D Slicer keeps its appearance
  and behavior. Slicer Studio does not change official 3D Slicer's preferences;
  only changes you make in the application settings do.

## Linux

The Linux package is built on Ubuntu 24.04 against the Qt 5.15 that comes with
the system. It runs on Ubuntu 24.04.

1. Install the runtime libraries:

   ```console
   sudo apt-get install libqt5core5t64 libqt5gui5t64 libqt5widgets5t64 libqt5network5t64 \
     libqt5opengl5t64 libqt5printsupport5t64 libqt5sql5t64 libqt5sql5-sqlite libqt5svg5 \
     libqt5xml5t64 libqt5xmlpatterns5 libqt5x11extras5 libqt5multimedia5 libqt5multimediawidgets5 \
     libqt5qml5 libqt5quick5 libqt5quickwidgets5 libqt5webchannel5 libqt5webengine5 \
     libqt5webenginecore5 libqt5webenginewidgets5 libqt5test5t64 \
     libglu1-mesa libxt6t64 libnss3 libasound2t64 libpulse-mainloop-glib0 libssl3t64
   ```

2. Extract the package anywhere:

   ```console
   tar -xzf Slicer-<version>-linux-amd64.tar.gz
   ```

3. Run the `Slicer` launcher in the extracted folder.

To uninstall, remove the folder.

To build the package yourself, follow the
[Linux build instructions](../developer_guide/build_instructions/linux.md),
then run `make package` in the `Slicer-build` folder.

## In a web browser

The web version runs the Linux build of Slicer Studio in a Docker container on
your computer and shows it in your browser at `http://localhost:8080`. It works
on macOS (including Apple Silicon), Windows and Linux. You need Docker Desktop
(or Docker Engine) and Python 3.

The launcher is `Utilities/SlicerStudioWeb/slicer_studio_web.py`. It uses only
the Python standard library. `Utilities/SlicerStudioWeb/README.md` covers the
details and troubleshooting.

You build the image yourself, from the Linux package (see [Linux](#linux)).
Build the image once, then start it:

```console
python3 Utilities/SlicerStudioWeb/slicer_studio_web.py build --package Slicer-<version>-linux-amd64.tar.gz
python3 Utilities/SlicerStudioWeb/slicer_studio_web.py
```

The second command starts Slicer Studio and opens the browser. Stop it with
`python3 Utilities/SlicerStudioWeb/slicer_studio_web.py stop`.

On Linux, the launcher can also run without Docker, on a local Slicer
installation or build tree (`slicer_studio_web.py local`).

## Updates

Slicer Studio updates itself from the public releases of its repository,
<https://github.com/azhmeerjesani/Slicer-2/releases>. The publisher creates each
release by hand (see [Publish a release](#publish-a-release)); nothing needs an
account or costs anything.

### How checks work

- **When.** An installed Slicer Studio (the macOS application or the Linux
  package) checks when it starts and then once a day while it runs. The startup
  waits for the check at most 5 seconds; without a network connection it starts
  as usual.
- **What is sent.** One anonymous HTTPS request to github.com for the small file
  `studio-update.json` of the latest release
  (`https://github.com/azhmeerjesani/Slicer-2/releases/latest/download/studio-update.json`).
  Nothing about you, your computer or your data is sent, and no GitHub account or
  API is used. Downloads are accepted only from github.com and GitHub's download
  servers (`*.githubusercontent.com`).
- **Turn automatic checks off** in **Settings** > **Updates** ("Check for
  updates automatically when Slicer Studio starts"). **Check now** in the same
  panel still works. The setting is stored as `Studio/Updates/AutoCheck`.
- **Development builds** (a build tree) never check by themselves and never
  install updates; **Check now** shows what the latest release is.

### Optional and required updates

- An **optional** update shows a quiet notice, "Update 1.2.0 available ·
  Update now · Skip", at the top of the Library (or in the inspector), and the
  **Updates** panel of the settings shows the version, the release notes and
  **Update now**. **Skip** hides the notice for that version. You also find the
  update in ≡ > **Check for updates…** (**Help** > **Check for Updates…** in the
  Classic interface) and by searching for "update" (`Ctrl+K`).
- A **required** update (marked so by the publisher, or because your version is
  older than the release's minimum supported version) shows a dialog when Slicer
  Studio starts, before anything else: **Update now** or **Quit**. If the check
  cannot reach GitHub, a required update found earlier is shown as a warning
  only, and you can keep working. The same warning (with **Update now** when it
  can be installed) appears when the release has no file for your computer yet,
  or when it is found while Slicer Studio is already running.

### What Update now does

1. It downloads the file of your computer (the disk image on macOS, the package
   on Linux) with a progress bar and a **Cancel** button.
2. It checks the size and the SHA-256 checksum of the download against the
   release. A file that does not match is deleted and nothing is installed.
3. It prepares the new version next to the installed one, then closes Slicer
   Studio. The usual question about unsaved changes appears; unsaved work is also
   autosaved and offered for recovery when the new version starts.
4. After Slicer Studio has closed, the new version replaces the old one and
   starts. If anything goes wrong, the previous version is put back, and the next
   start says why.

On macOS, if the Applications folder is not writable for you, macOS asks for an
administrator password; you can instead quit, open the downloaded disk image and
drag Slicer Studio to Applications. If Slicer Studio runs from the disk image or
from the Downloads folder (macOS "App Translocation"), move it to Applications
first. On Linux, the folder of the package must be writable; otherwise extract the
new package yourself. **Install when I quit** installs the downloaded update the
next time you quit, without restarting.

### Web version

`slicer_studio_web.py` checks for a new release when it starts Slicer Studio. A
required update stops it with instructions; an optional one prints a notice. To
update, run:

```console
python3 slicer_studio_web.py update
```

It downloads the launcher bundle and the Linux package of the latest release,
checks their size and SHA-256 checksum, unpacks the new launcher next to the old
one (or in the cache folder when run from a source tree) and builds the Docker
image from the verified package. Start the new version with
`python3 <new launcher folder>/slicer_studio_web.py run --recreate`.
`--no-update-check` (or the environment variable
`SLICER_STUDIO_UPDATE_FEED=off`) skips the check.

### Publish a release

Releases are made by hand. `Utilities/Scripts/SlicerStudioRelease/make_release.py`
prepares the files and prints the steps; it never contacts GitHub:

1. Raise `STUDIO_VERSION` in `Modules/Scripted/Studio/StudioLib/version.py` and
   build the packages (macOS disk images with `build_dmg.sh`, the Linux package
   with `make package`).
2. Run `python3 Utilities/Scripts/SlicerStudioRelease/make_release.py <packages>
   --notes notes.md` (add `--required` or `--minimum-supported X.Y.Z` to force the
   update). It writes `studio-update.json` with the size and SHA-256 of each
   package, and the web launcher bundle.
3. On GitHub, create the release `v<version>`, attach every listed file, mark it
   as the latest release and publish it.

`Utilities/Scripts/SlicerStudioRelease/README.md` has the details.

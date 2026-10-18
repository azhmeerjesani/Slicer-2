# Install Slicer Studio

Slicer Studio is a redesigned edition of 3D Slicer, built from this source
tree. It comes in three forms:

- a native macOS application, as a disk image (`.dmg`);
- a Linux package, as a `.tar.gz` file;
- a web version that runs on your computer and opens in a web browser.

None of these is on download.slicer.org. You get them from this repository's
GitHub Actions workflow, or you build them yourself.

Slicer Studio works side by side with official 3D Slicer:

- Scenes and data files saved by Slicer Studio open in official 3D Slicer 5.12
  and 5.13, and files saved by official 3D Slicer open in Slicer Studio.
- Both applications can be installed at the same time.

## Get the packages from GitHub

The **Slicer Studio packages** workflow (`.github/workflows/studio-packages.yml`)
builds the packages on GitHub's servers. To get them:

1. On the repository's GitHub page, open **Actions** and select
   **Slicer Studio packages**.
2. Click **Run workflow**, choose the packages, and start the run. Each package
   has its own option:
   - **macos**: the Apple Silicon disk image (on by default).
   - **macos_intel**: an Intel disk image.
   - **linux**: the Linux tarball (on by default).
   - **docker**: the web image.
3. When the run has finished, download the packages from the **Artifacts**
   section of the run page. The disk image is the
   `SlicerStudio-<version>-macos-<arch>.dmg` file itself. The Linux package is
   `Slicer-<version>-linux-amd64.tar.gz`.

A build takes several hours. If a run stops at the time limit of GitHub's
runners, run the workflow again: the compiler cache saved by the first run
lets the next run finish quickly. Artifacts are kept for 30 days.

## macOS

### Get the disk image

Download it from the workflow run (see above), or build it on your Mac. To build
it, run this command from the top of the source tree:

```console
Utilities/Scripts/SlicerStudioMacOS/build_dmg.sh
```

Building needs macOS 13 or newer, the Xcode Command Line Tools, Homebrew, about
40 GB of free disk space and several hours. The disk image is written to
`~/SlicerStudio/dist/`. If the build is interrupted, run the same command again
to continue.

`Utilities/Scripts/SlicerStudioMacOS/README.md` explains the options, for
example how to choose the Qt version. It also explains how to make a disk image
that runs on other Macs (`--qt=official`).

Which Macs a disk image runs on depends on how it was built:

| Disk image | Runs on |
|---|---|
| From the workflow (default settings) | macOS 13 or newer, on Apple Silicon. The Intel disk image runs on Intel Macs. |
| Built on your Mac with the default settings (Homebrew's Qt) | Your Mac, and Macs with the same or a newer macOS |

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
  Slicer Studio, and confirm with **Open**. On macOS 14 and older, you can
  instead right-click the application and choose **Open**.
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
     libglu1-mesa libxt6t64 libnss3 libasound2t64 libpulse-mainloop-glib0
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
details and troubleshooting. There are two ways to get the image:

- **Build it from the Linux package.** Build the image once, then start it:

  ```console
  python3 Utilities/SlicerStudioWeb/slicer_studio_web.py build --package Slicer-<version>-linux-amd64.tar.gz
  python3 Utilities/SlicerStudioWeb/slicer_studio_web.py
  ```

  The second command starts Slicer Studio and opens the browser. Stop it with
  `python3 Utilities/SlicerStudioWeb/slicer_studio_web.py stop`.

- **Pull the image built by the workflow.** When the workflow is run with the
  **docker** option, it pushes the image to
  `ghcr.io/<owner>/slicer-studio-web`. Pull the image and start it:

  ```console
  docker pull ghcr.io/<owner>/slicer-studio-web:latest
  python3 Utilities/SlicerStudioWeb/slicer_studio_web.py run --image ghcr.io/<owner>/slicer-studio-web:latest
  ```

  A newly pushed image is private. To pull it, either log in first with
  `docker login ghcr.io`, or make it public in the package settings on GitHub.

On Linux, the launcher can also run without Docker, on a local Slicer
installation or build tree (`slicer_studio_web.py local`).

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
- Your work is saved automatically, as ordinary 3D Slicer files, in a folder you
  choose the first time Slicer Studio starts: see
  [Where Slicer Studio saves your work](studio_projects.md).

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
   for installing extensions, so do not run the application from the disk
   image. To install a newer build over a build made before the profile
   existed, first rename the old one (for example to "Slicer Studio old"),
   then drag the new one in, start it once, and delete the old one afterwards
   (see [The first start of a new build](#the-first-start-of-a-new-build)).
   Over a build that already has the profile you can drag the new one over
   the old one: your settings, extensions, Python packages and saved work are
   kept (see [Your Slicer Studio profile](#your-slicer-studio-profile)).
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

The first start can take a minute or two, while macOS checks the application's
libraries. A progress window is shown during that time.

To uninstall Slicer Studio, delete **Slicer Studio** from Applications. This
keeps your profile; to remove it too, see
[Start again from nothing](#start-again-from-nothing).

### Next to official 3D Slicer

- **Separate applications.** Slicer Studio is installed as
  "Slicer Studio.app", with its own bundle identifier
  (`org.slicer.slicerstudio`). Official 3D Slicer installs as
  "3D Slicer <version>.app" (older versions as "Slicer.app"), so the two do not
  replace each other. Slicer Studio keeps its extensions and Python packages
  in its own profile, and official 3D Slicer keeps its own inside its
  application, so neither sees the other's.
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

To install a newer build, extract it anywhere (a new folder, or in place of the
old one when the old one already has the profile): your settings, extensions,
Python packages and saved work are kept (see
[Your Slicer Studio profile](#your-slicer-studio-profile)). Over a build made
before the profile, use a new folder and start the new build once before you
delete the old one (see
[The first start of a new build](#the-first-start-of-a-new-build)).

To uninstall, remove the folder. This keeps your profile; to remove it too, see
[Start again from nothing](#start-again-from-nothing).

To build the package yourself, follow the
[Linux build instructions](../developer_guide/build_instructions/linux.md),
then run `make package` in the `Slicer-build` folder.

## Your Slicer Studio profile

Everything you set up in Slicer Studio is kept outside the application, so
reinstalling it, installing a newer build, or an update (in the application or
by hand) keeps it:

| What | Where |
|---|---|
| Preferences shared with official 3D Slicer | `~/.config/slicer.org/Slicer.ini` |
| Settings of each build (installed extensions, module paths) | `~/.config/slicer.org/SlicerStudio-<number>.ini` |
| Extensions, Python packages, autosave, update state | the profile folder |

The profile folder is:

- **macOS:** `~/Library/Application Support/slicer.org/Slicer`
- **Linux:** `~/.local/share/slicer.org/Slicer` (or
  `$XDG_DATA_HOME/slicer.org/Slicer`)

It holds:

- `StudioExtensions`: the extensions you installed. Every build made from the
  same version of 3D Slicer uses them. A build based on a newer 3D Slicer needs
  extensions built for that version: it starts with none, and Settings >
  Profile lists the ones of the older builds to install again. On macOS the
  extensions also live inside the application, where the Extensions Manager
  installs them; Slicer Studio copies them into the profile as soon as you
  install, update or uninstall one, and copies them into a newly installed
  application when it starts. When you keep two builds (for example a test
  build next to "Slicer Studio"), an extension installed in one is copied into
  the other when it starts. An extension you uninstall is kept in
  `StudioExtensions-trash` for 30 days.
- `StudioPython`: the Python packages you install (with `pip_install` or in the
  Python console). They are kept for each Python version: after an update to a
  newer Python, Settings > Profile offers to install them again.
- `StudioAutosave`, `StudioUpdate`, `StudioMoveJournal.json`: autosave and
  recovery, and the state of updates.
- `StudioLegacy`: copies of what older Slicer Studio builds kept inside the
  application.
- `StudioProfile.json`: a record of what was carried from one build to the
  next.

Your projects and studies are in the Slicer Studio folder you chose (see
[Where Slicer Studio saves your work](studio_projects.md)), which reinstalling
never touches either.

### The first start of a new build

Each build has its own settings file (`SlicerStudio-<number>.ini`). At its first
start, a new build copies your settings from the previous build's file, and
the extensions of the previous build into the profile when needed. If
extensions were carried over, Slicer Studio asks to restart once, so that all
their parts are loaded. If the copy could not finish (Slicer Studio was closed
during it, or the disk was too full), it goes on at the next start, as long as
the previous build is still there; Settings > Profile lists what is left.

Builds made before the profile kept their settings and extensions inside the
application, so replacing the application deletes them. Keep the old build
until the new one has started once:

- **macOS:** rename the old application in Applications (for example to
  "Slicer Studio old"), drag the new one in, start the new one once, then
  delete the old one. The new build finds the old one in Applications (or in
  `~/Applications`) and copies its settings and extensions. Do not drag the new
  build over an old one made before the profile.
- **Linux:** extract the new build into a new folder next to the old one, start
  it once, then delete the old folder.

An update made with **Update now** keeps the previous version until its
settings and extensions are copied. Builds that already copy them by
themselves also keep a copy in `StudioLegacy` in the profile, a while after
each start. Python packages installed in an old build are not copied:
Settings > Profile names them, and **Reinstall…** installs them again.

### Settings > Profile

The **Profile** panel of the Application Settings shows the profile folder (**Show**
opens it), the size of each part, what was carried over, and extensions or
Python packages of a previous build that are not installed (**Reinstall…**).

### Move the profile

- **Change…** in Settings > Profile chooses another folder. It is used from the
  next start: the profile is copied there if the folder is empty, or the
  profile already in that folder is used as it is. A folder that holds other
  files is refused. The old folder is kept (Settings > Profile names it; delete
  it yourself afterwards). Close other Slicer Studio windows first.
- Or set the environment variable `SLICER_STUDIO_PROFILE_DIR` to an absolute
  folder (`~` is allowed) that exists. It wins over the setting.

A folder inside the application, that you cannot write, or that does not exist
(renamed, or a synced folder not restored yet) is not used: Slicer Studio uses
the default folder for that run without carrying or removing anything, and
says why in Settings > Profile and in the log.

A portable build (configured with
`Slicer_STORE_SETTINGS_IN_APPLICATION_HOME_DIR` ON) keeps its settings,
extensions and Python packages inside itself, as official 3D Slicer does, and
does not use the profile for them.

To install Python packages inside the application instead (as official 3D
Slicer does), set `Studio/Profile/PipToProfile` to `false` in `Slicer.ini`.

### Start again from nothing

Quit Slicer Studio, then delete the profile folder and the
`~/.config/slicer.org/SlicerStudio-*.ini` files. Keep `Slicer.ini` if you also
use official 3D Slicer: it holds the preferences of both.

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

Your work is saved automatically in the `Slicer Studio` folder of the data
folder (`SlicerStudioData` in your home folder unless you pass `--data`), in
`Projects` and `Studies`; the Docker version does not ask where to save (`local`
asks as the desktop application does). See
[Where Slicer Studio saves your work](studio_projects.md).

## Extensions

The Extensions Manager offers the extensions of the official 3D Slicer build
that Slicer Studio is based on. Only extensions written in Python work in
Slicer Studio. Extensions with compiled (C++) parts, such as MarkupsToModel,
were built for official 3D Slicer and cannot run here: they still install, but
the Extensions Manager shows them as incompatible, says why, and keeps them
disabled. Use official 3D Slicer for those extensions.

## Updates

Slicer Studio updates itself from the public releases of its repository,
<https://github.com/azhmeerjesani/Slicer-2/releases>. The publisher creates each
release by hand (see [Publish a release](#publish-a-release)); nothing needs an
account or costs anything. Between releases it also takes the newest Python code
of the `main` branch (see [Code updates from main](#code-updates-from-main)).

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
   starts, with your settings, extensions and Python packages (see
   [Your Slicer Studio profile](#your-slicer-studio-profile)). If anything goes wrong, the previous version is put back and starts
   instead, and it says why. While another Slicer Studio window of the same
   installation is open, the update is not installed: Update now says so, and
   you close the other window first.

On macOS, if the Applications folder is not writable for you, macOS asks for an
administrator password; you can instead quit, open the downloaded disk image and
drag Slicer Studio to Applications. If Slicer Studio runs from the disk image or
from the Downloads folder (macOS "App Translocation"), move it to Applications
first. On Linux, the folder of the package must be writable; otherwise extract the
new package yourself. When Slicer Studio cannot install an update itself, it keeps
the checked package in your Downloads folder and names its full path.
**Install when I quit** installs the downloaded update the
next time you quit, without restarting.

### Code updates from main

Between releases, an installed Slicer Studio also takes the newest code of the
`main` branch, as far as it can without a new build:

- **What it applies.** Only the Python files and resources that a build copies
  as they are into the application: the scripted modules (Studio, Lesion
  Network Mapping, ...) and the Python files in `bin/Python`. Anything that is
  compiled into Slicer Studio (C++ code, CMake files, `.ui` or `.qrc` files
  built into C++, command-line module descriptions, ...) needs a new build: when
  newer commits on `main` change such a file, nothing is applied and Slicer
  Studio says once "Newer code on main changes parts that are compiled into
  Slicer Studio (N files, for example ...). It needs a new build and will come
  with the next release." Changes to documentation, tests and the developer,
  release and web tools are ignored. Deleting a Python file applies only inside
  a package (a deleted top-level module, or a scripted module, would still be
  found in the installed files, so it needs a new build).
- **Only newer code.** A build takes code from `main` only when `main` contains
  the commit it was built from (checked against the history of `main` each
  time). A build made from another branch, or from a commit that is ahead of
  `main` (for example a bug-fix branch), takes nothing, so its own newer code is
  never replaced by older code from `main`; **Settings** > **Updates** says so.
- **When.** A few seconds after each start (and once a day while Slicer Studio
  runs), in the background; it never slows the start-up down, and without a
  network connection nothing happens. While a newer release is available, code
  from `main` is not taken: install the release.
- **What is sent.** Anonymous HTTPS requests to github.com with git's own
  protocol (which commit is the newest on `main`, and its list of files, about
  200 KB), and one request to `raw.githubusercontent.com` per changed file, at
  that exact commit. No GitHub API, no account, nothing about you or your data.
- **Checked.** The list of files must hash to the commit, and every downloaded
  file must hash to its id in that list (git's SHA-1 object ids), so the
  updated files are exactly those of the commit. The list of files of your
  build is checked the same way against the commit it was built from.
- **Applied at the next start.** The new files are prepared in your user data
  folder (`StudioUpdate/commit/` in Slicer Studio's application data folder);
  the installed application, and the signed macOS application, are never
  changed. Then a notice says "Update ready · main @ abc1234 (Nov 16) · Restart
  now · Later". **Restart now** closes Slicer Studio (it asks about unsaved
  work as usual) and starts it again; with **Later** the update applies the
  next time you start Slicer Studio. The first start with it says "Slicer
  Studio now runs code from main @ abc1234."
- **Automatic rollback.** A start with a code update counts as good only when,
  after the start-up, the Studio interface and the update service (which must
  stay able to fetch a fix from `main`) have started and every updated module
  loaded from the update. If Slicer Studio does not start properly with a code
  update (it closes or crashes before that, or Studio reports that its start-up
  failed), the next start goes back to the previous code update, or to the
  installed version, and says so. It also goes back after two crashes in a row
  within ten minutes of the start, and at once (for the rest of that session
  the installed files are used) when an updated module fails to load. Closing
  Slicer Studio while it is still starting (for example from a dialog shown at
  the start) does not count against the update. That commit is not taken again;
  a later commit on `main` is. If something goes wrong later, **Go back** in
  **Settings** > **Updates** returns to the previous code update or to the
  installed build at the next start. If Slicer Studio cannot be used at all,
  start it once with the environment variable `SLICER_STUDIO_COMMIT_UPDATES=off`
  (the installed code runs), or delete the folder `StudioUpdate/commit` in its
  application data folder.
- **Settings** > **Updates** > **Code from main** shows the installed build
  (version and commit), the code that runs, the last check, **Check main now**,
  **Restart now**, **Go back**, and the switch "Also get code updates from main
  between releases" (`Studio/Updates/CommitUpdates`, on by default). Turning it
  off brings back the installed code at the next start. The environment
  variable `SLICER_STUDIO_COMMIT_UPDATES=off` (or `SLICER_STUDIO_UPDATE_FEED=off`)
  turns it off too. (The tests also set it to a repository address on the same
  computer; any other value is ignored.)
- **Not updated:** development builds (a build tree or a source checkout), a
  build made from a source tree with uncommitted changes, the web version, runs
  under `--testing`, and the command-line tools that do not start the
  application (`PythonSlicer`, `slicer-lnm`): they run the installed code until
  the next release. Installing a release removes every code update.

**What you trust.** The code updates come straight from `main`, without a
release in between. Anyone who can change `main` of
`azhmeerjesani/Slicer-2` (a merged pull request, a collaborator's account that
was taken over, a forced push) can change the code that runs on every
computer with Slicer Studio, at its next start. Protect the branch on GitHub
(required reviews, no forced pushes) and turn the switch off if that is not
acceptable for you. Commits are not checked for signatures; what is checked is
that the files are exactly those of the newest commit on `main`, as GitHub
reports it over HTTPS.

### Web version

`slicer_studio_web.py` checks for a new release when it starts Slicer Studio. A
required update stops it with instructions; an optional one prints a notice.
While the release has no Linux package yet, a required update only prints a
warning and Slicer Studio starts. To update, run:

```console
python3 slicer_studio_web.py update
```

It downloads the launcher bundle and the Linux package of the latest release,
checks their size and SHA-256 checksum, unpacks the new launcher next to the old
one (or in the cache folder when run from a source tree) and builds the Docker
image from the verified package. The package is deleted after a successful build; with `--no-build`
it stays in the launcher's cache folder (`updates/<version>` inside it, which the command prints) until the next
`update` removes it. Start the new version with
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

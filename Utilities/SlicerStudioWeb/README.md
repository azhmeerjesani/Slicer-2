# Slicer Studio in a web browser

`slicer_studio_web.py` runs Slicer Studio on your computer and shows it in a web
browser at <http://localhost:8080>.

It is the real desktop application, not a web rewrite. The Linux build of Slicer
Studio runs on a virtual display (TigerVNC, software OpenGL), and
[noVNC](https://novnc.com) streams that display to the browser tab. Nothing is
uploaded anywhere: the application, the browser and your files all stay on this
computer.

There are two ways to run it:

| | Docker (`run`) | Local (`local`) |
|---|---|---|
| Works on | macOS (Intel and Apple Silicon), Windows, Linux | Linux only |
| Needs | Docker Desktop (or Docker Engine), Python 3.8+ | a Slicer installation or build tree, TigerVNC, websockify, Python 3.8+ |
| Slicer comes from | a Docker image built from a Slicer Linux package | this computer |

The script uses only the Python standard library: there is nothing to `pip install`.

If you want the native macOS application instead, install the DMG; this page is
about the browser version.

## Quick start (macOS)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it.
   On a Mac with Apple Silicon (M1 or later), open Docker Desktop **Settings > General** and turn on
   **Use Rosetta for x86_64/amd64 emulation on Apple Silicon**. The image is built for
   Intel/AMD (amd64) processors and runs with emulation; Rosetta makes it much faster.
2. Get the Slicer Linux package, `Slicer-<version>-linux-amd64.tar.gz` (see
   [Getting a Slicer Linux package](#getting-a-slicer-linux-package)).
3. In Terminal, in this folder (`Utilities/SlicerStudioWeb` of the source tree), build the image once:

   ```
   python3 slicer_studio_web.py build --package ~/Downloads/Slicer-5.13.0-linux-amd64.tar.gz
   ```

   This takes a few minutes. The image uses about 3 GB of disk space in Docker (about
   0.7 GB compressed), and the build needs about 2 GB more while it runs.
4. Start Slicer Studio:

   ```
   python3 slicer_studio_web.py
   ```

   Your browser opens <http://localhost:8080>, and Slicer appears a few seconds later.
   With emulation on Apple Silicon, starting takes longer.
5. When you are done, stop it:

   ```
   python3 slicer_studio_web.py stop
   ```

`python3` comes with macOS (it may offer to install the Xcode command line tools the
first time).

## Quick start (Windows)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (WSL 2 backend) and start it.
2. Install Python 3 from <https://www.python.org> or the Microsoft Store.
3. In PowerShell, in this folder:

   ```
   py slicer_studio_web.py build --package $HOME\Downloads\Slicer-5.13.0-linux-amd64.tar.gz
   py slicer_studio_web.py
   ```

4. Stop it with `py slicer_studio_web.py stop`.

## Quick start (Linux)

With Docker, the steps are the same as on macOS (install
[Docker Engine](https://docs.docker.com/engine/install/) or Docker Desktop first).

Without Docker, run a Slicer of this computer (an installation, or the
`Slicer-build` folder of a build tree) on a virtual display:

```
sudo apt install tigervnc-standalone-server tigervnc-tools websockify novnc openbox   # Debian, Ubuntu
python3 slicer_studio_web.py local --slicer ~/Slicer-SuperBuild-Release/Slicer-build/Slicer
```

Press `Ctrl+C` in the terminal to stop everything. See [Local mode](#local-mode-linux-without-docker).

## Getting a Slicer Linux package

The Docker image is built from the Linux package of Slicer Studio, a file named
like `Slicer-5.13.0-2026-10-18-linux-amd64.tar.gz`. Get it in one of these ways:

- **From a Linux build tree**: in `Slicer-build`, run `make package` (or
  `cmake --build . --target package`); the package is written to that folder.
  The script can do this for you:

  ```
  python3 slicer_studio_web.py build --slicer-build ~/Slicer-SuperBuild-Release
  ```

  Add `--reuse-package` to use the newest package already in the build tree.
- **From a web address**: `--package-url URL` downloads the package (into the cache
  folder, once) before building; add `--package-sha256 HASH` to check it.

The package must be the Linux amd64 (x86-64) package of this fork. It is built with
the Qt 5 of Ubuntu 24.04, which is why the image is based on Ubuntu 24.04.

## Commands

| Command | What it does |
|---|---|
| `run` (default) | Starts the `slicer-studio-web` container, waits until it answers and opens the browser. If it is already running, opens the browser again. |
| `build` | Builds the Docker image `slicer-studio-web:latest` from a Slicer Linux package. |
| `stop` | Stops and removes the container. Your files in the data folder and your settings are kept. |
| `status` | Shows whether Docker runs, whether the image exists, and the container state, address and data folder. |
| `logs` | Shows the output of Slicer and of the web server in the container (`-f` to follow). |
| `local` | Linux only: runs a local Slicer on a virtual display, without Docker. |

`python3 slicer_studio_web.py COMMAND --help` lists all options. The most useful:

| Option | Commands | Meaning |
|---|---|---|
| `--port PORT` | `run`, `local` | Browser port (default 8080). |
| `--data DIR` | `run` | Folder shared with Slicer as its `Documents` folder and `/data` (default `~/SlicerStudioData`). |
| `--package FILE`, `--package-url URL`, `--slicer-build DIR` | `build`, `run` | Slicer Linux package to build the image from. With `run`, the image is (re)built first. |
| `--image NAME` (alias `--tag`) | all Docker commands | Image name (default `slicer-studio-web:latest`). |
| `--name NAME` | all Docker commands | Container name (default `slicer-studio-web`); use different names and ports to run several. |
| `--geometry WxH` | `run`, `local` | Initial screen size (default 1600x1000). Once the browser connects, the screen follows the size of the browser window. |
| `--ask-password` | `run`, `local` | Require a password in the browser (or set the `VNC_PASSWORD` environment variable). |
| `--listen ADDRESS` | `run`, `local` | Network address (default `127.0.0.1`, this computer only). See [Security](#security). |
| `--no-browser` | `run`, `local` | Do not open the browser. |
| `--recreate` | `run` | Replace a running container (for example after building a new image). |
| `-- ARGS` | `run`, `local` | Everything after `--` is passed to Slicer, for example `-- --python-script /data/setup.py`. |

## Your files

- The **data folder** (`~/SlicerStudioData` unless you choose another one with
  `--data`) is the `Documents` folder of Slicer (also reachable as `/data`). The
  file dialogs of Slicer Studio start there (see
  [Lesion network mapping in the browser build](#lesion-network-mapping-in-the-browser-build));
  a dialog that opens in Slicer's home folder instead (for example in Classic
  mode) shows `Documents`, which is the same folder. It is Slicer's default
  folder for saving scenes, and the DICOM database is created there
  (`SlicerDICOMDatabase`).
- Everything else inside the container is temporary: it is lost when the
  container is removed (`stop` removes it).
- Slicer settings (and extensions) are kept between runs in the Docker volume
  `slicer-studio-web-home` (on Linux, when your user id is not 1000:
  `slicer-studio-web-home-<your user id>`, because settings files can only be
  changed by the user who wrote them). To start over with default settings, run
  `docker volume rm slicer-studio-web-home` while Slicer Studio is stopped. Use
  `--home-volume ""` to not keep settings at all, or `--home-volume NAME` to
  choose the volume.
- On Linux, the container runs with your user and group ids when they are not
  1000, so files written to the data folder belong to you. Run the script as your
  user, not with `sudo`: under `sudo`, the script uses the ids, the home folder
  and the settings volume of the user who ran `sudo`; as `root` itself, Slicer
  would run as root in the container too.

## Using Slicer in the browser

- **Keyboard**: Slicer runs on Linux, so its shortcuts use `Ctrl`, also on a Mac
  (`Ctrl+O`, not `Cmd+O`). Some shortcuts are taken by the browser before the page
  sees them (`Ctrl+W`, `Ctrl+T`, `Ctrl+N` and similar). Full-screen mode of the
  browser helps, and in Chrome or Edge you can install the page as an app (address
  bar menu > "Install page as app" / "Open as window") so that it gets its own
  window.
- **noVNC panel**: the small tab on the left edge of the page opens the noVNC panel:
  full screen, clipboard (to paste text into Slicer or copy text out of it) and
  connection settings.
- **Window size**: the Slicer screen follows the size of the browser window. Slicer
  Studio stays usable down to about 1366x768.
- **Quitting**: if you quit Slicer (File > Exit), it starts again after a few
  seconds, so the browser tab keeps working. "Restart Slicer" works as on the
  desktop. Use `stop` to stop everything.
- **Extensions**: the Extensions Manager needs a package built with the system
  OpenSSL 3 (`Slicer_USE_SYSTEM_OpenSSL=ON`). An image built from an older
  package that bundles OpenSSL 1.1 cannot download the extension catalog
  ("Failed to download extension metadata from server"); rebuild the image from
  a newer package. Downloads made by Python, such as Sample Data, work in both.
- **Display quality**: the picture is sent with the best quality noVNC offers (JPEG
  quality 100, no chroma subsampling, lossless for flat areas). It is close to, but
  not exactly, pixel-identical to a local display. Like all Slicer builds, this is
  for research use, not for clinical diagnosis.
- **Speed**: rendering uses the CPU (Mesa llvmpipe), not the graphics card. Slice
  viewing and segmentation are fine; volume rendering of large images is slow. Give
  Docker Desktop enough resources (Settings > Resources: 4 or more CPUs, 8 GB or more
  memory).

## Lesion network mapping in the browser build

- The container sets `SLICER_STUDIO_WEB=1`. Slicer Studio uses it to know that this is
  the browser build, so reports open inside Slicer (the browser of the container
  cannot be seen) and connectome downloads are checked against `/data`.
- **Connectome downloads must go to `/data`** (the data folder you chose with `--data`).
  Everything else in the container is lost when it is removed, and a connectome is
  large. Choose a folder under `/data` for "Get a connectome..." and for the data
  folder of the Map module.
- **Figures enter through "Open figure...".** The browser build has no image clipboard
  and no drag-and-drop from your computer into Slicer, so paste and drop do not work;
  use "Open figure..." and pick the file from `/data`.
- **"Show in Files" and links.** The container has no file manager and no browser, so
  "Show in Files", "Show folder", "Open folder" and link-outs (a DOI, a connectome page)
  show a small window instead: the folder path (a path under `/data` is in your `--data`
  folder on the host) or the link, with a Copy button.
- **File dialogs start in `/data`** until you choose another folder ("Save a copy as..."
  of an open project starts in its `Exports` folder, under `/data/Slicer Studio`; the container's
  start-up script sets the Studio settings `Studio/LastSaveFolder` and `Studio/LastOpenDirectory` when they are empty or point to a missing folder).
- **Apple Silicon.** The image is `linux/amd64`, so on an Apple Silicon Mac it runs under
  emulation. The heavy lesion network mapping jobs (normalization to the template,
  mapping, permutation tests) are 3 to 5 times slower than natively, and the time
  estimate shown in the app grows accordingly. For a whole cohort, the macOS disk
  image is recommended.

## Security

- By default the server listens on `127.0.0.1` only: only this computer can open
  it, and there is no password. On a computer shared with other people, any local
  user could open the page; use `--ask-password` there.
- `--listen 0.0.0.0` makes it reachable from other computers. The script then
  prints a warning and requires a password (`--ask-password` or `VNC_PASSWORD`). VNC
  passwords are limited to 8 characters, and the connection is not encrypted:
  prefer keeping the default and using an SSH tunnel from the other computer, for
  example `ssh -L 8080:localhost:8080 your-server`, then open <http://localhost:8080>
  there.
- Whoever can open the page controls Slicer, including its Python console, and can
  read and change the files of the data folder. Only the data folder of your
  computer is shared with the container, which runs as an unprivileged user (on
  Linux, with your user id; see [Your files](#your-files)).
- The password is given to the container as the environment variable
  `VNC_PASSWORD`: it does not appear in process lists, but anyone who may use
  Docker on this computer can read it (`docker inspect`). Using Docker requires
  administrator rights anyway.
- In local mode, the virtual display only accepts programs that have its key (an
  X authority cookie in a private temporary folder), so other users of the
  computer cannot reach Slicer through the display instead of the browser.

## Troubleshooting

| Problem | What to do |
|---|---|
| "Docker is not installed" / "Docker is not running" | Install Docker Desktop (or Docker Engine) and start it; wait until it reports that it is running. On Linux, "permission denied" means your user is not in the `docker` group: add it (`sudo usermod -aG docker $USER`), then log in again. |
| "Port 8080 is already in use" | Another program (or another Slicer Studio) uses the port: `--port 8081`. |
| "The Docker image ... does not exist yet" | Build it first: `build --package FILE` (see above). |
| Docker Hub "toomanyrequests" (rate limit) while building | The script retries with `mirror.gcr.io/library/ubuntu:24.04` automatically. You can also choose the base image with `--base-image`. |
| Building behind a company proxy | `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` of your environment are passed to the build. If the proxy inspects TLS, add its CA certificate: `--extra-ca-cert proxy-ca.pem`. |
| The page stays black or shows "Failed to connect" | Slicer is still starting (longer with emulation), or it failed: look at `python3 slicer_studio_web.py logs`. The page reconnects by itself. |
| Very slow on a Mac with Apple Silicon | Turn on Rosetta in Docker Desktop (see the quick start) and give Docker more CPUs and memory. |
| Cannot save into the data folder (Linux) | The folder must be writable by your user; with rootless Docker or user namespaces, files may need `chmod`. |
| Keys do not reach Slicer | Click into the Slicer picture first. Browser shortcuts cannot be overridden; see [Using Slicer in the browser](#using-slicer-in-the-browser). |
| Start over with default settings | `stop`, then `docker volume rm slicer-studio-web-home` (on Linux with a user id other than 1000: `slicer-studio-web-home-<id>`), then `run`. |
| Remove everything | `stop`, then `docker image rm slicer-studio-web:latest` and `docker volume rm slicer-studio-web-home` (see the line above). |

## Local mode (Linux without Docker)

`local` starts these processes, reachable from this computer only (unless you use
`--listen`), and stops them when you press `Ctrl+C` (or close the terminal):

- a virtual display with a VNC server: TigerVNC `Xvnc` (preferred, supports
  browser-driven resizing), or `Xvfb` plus `x11vnc`;
- the `openbox` window manager with the configuration of `docker/openbox-rc.xml`,
  when installed (without it, the Slicer window is not maximized);
- `websockify`, serving noVNC and forwarding to the VNC server (the `websockify`
  command, or the `websockify` Python module);
- Slicer: `--slicer PATH` (a `Slicer` launcher, or a folder that contains one, also
  the top folder of a superbuild), otherwise the newest of `SLICER_EXECUTABLE`,
  `Slicer` on the `PATH`, `*/Slicer-build/Slicer` next to the source tree,
  `~/Slicer*/Slicer`, `~/Slicer*/Slicer-build/Slicer`, `/opt/Slicer*/Slicer` and
  `/opt/*/Slicer-build/Slicer`.

noVNC comes from the system package (`/usr/share/novnc`) when installed. Otherwise
the script downloads noVNC 1.6.0 from GitHub into `~/.cache/slicer-studio-web` once
and checks the SHA-256 checksum of its content. `--novnc DIR` uses another copy.

Slicer uses your normal settings (`~/.config/slicer.org`), as when you start it from
the desktop. If Slicer exits, it is started again; `--no-restart` stops instead.

## Building the image without the script

The script assembles a build context folder with the `Dockerfile`, the `docker/`
folder and the package, and runs `docker build`. The same by hand (BuildKit, the
default builder since Docker 23, is required):

```
mkdir context
cp Dockerfile context/
cp -r docker context/
cp /path/to/Slicer-5.13.0-linux-amd64.tar.gz context/
docker build --file context/Dockerfile \
  --build-arg SLICER_PACKAGE=Slicer-5.13.0-linux-amd64.tar.gz \
  --build-arg BASE_IMAGE=ubuntu:24.04 \
  --tag slicer-studio-web:latest context
```

Continuous integration can call the script instead:

```
python3 Utilities/SlicerStudioWeb/slicer_studio_web.py build --package Slicer-5.13.0-linux-amd64.tar.gz --tag slicer-studio-web:5.13.0
```

Build arguments: `SLICER_PACKAGE` (file name of the package in the context,
required), `BASE_IMAGE` (default `ubuntu:24.04`; must be Ubuntu 24.04),
`EXTRA_CA_CERT_FILE` (CA certificate file in the context, optional), and the
standard proxy variables.

Run the image by hand:

```
docker run -d --name slicer-studio-web -p 127.0.0.1:8080:8080 \
  -v "$HOME/SlicerStudioData:/data" -v slicer-studio-web-home:/home/slicer \
  --shm-size 2g slicer-studio-web:latest
```

Container environment variables: `GEOMETRY` (initial screen size), `VNC_PASSWORD`
(require a password), `SLICER_ARGS` (extra Slicer arguments). Arguments after the
image name are passed to Slicer.

## Files

| File | Purpose |
|---|---|
| `slicer_studio_web.py` | The launcher (build, run, stop, status, logs, local). |
| `Dockerfile` | The image: Ubuntu 24.04, the runtime libraries of the package, TigerVNC, noVNC, websockify, openbox, Slicer in `/opt/slicer`. |
| `docker/entrypoint.sh` | Starts the virtual display, the window manager, the web server and Slicer (started again when it exits). |
| `docker/openbox-rc.xml` | Window manager settings: the main window fills the screen; dialogs can be moved. |
| `docker/index.html` | Opens the noVNC client with the right options (connect, resize, reconnect, quality). |

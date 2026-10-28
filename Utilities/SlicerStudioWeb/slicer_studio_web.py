#!/usr/bin/env python3
"""Run Slicer Studio in a web browser on this computer.

The Linux build of Slicer Studio runs on a virtual display and is shown in the
browser with noVNC (http://localhost:8080 by default). There are two ways to
run it:

- Docker (macOS, Windows and Linux): ``run`` starts the ``slicer-studio-web``
  image in a container and opens the browser. ``build`` creates that image from
  a Slicer Linux package (``Slicer-*-linux-amd64.tar.gz``).
- Local (Linux only, no Docker): ``local`` runs a Slicer installation or build
  tree of this computer on a virtual display.

Examples::

    python3 slicer_studio_web.py build --package Slicer-5.13.0-linux-amd64.tar.gz
    python3 slicer_studio_web.py              # same as "run"
    python3 slicer_studio_web.py stop
    python3 slicer_studio_web.py local --slicer ~/Slicer-SuperBuild/Slicer-build/Slicer
    python3 slicer_studio_web.py update       # download, verify and install the latest release

Updates: ``run`` checks the public latest release of Slicer Studio on GitHub (one anonymous request; no API, no
account). A required update stops it with instructions, an optional one prints a notice. ``update`` (or ``--update``)
downloads the launcher bundle and the Linux package of the latest release, checks their size and SHA-256 against
the release manifest, unpacks the launcher next to this one and builds the image from the verified package.

The script only uses the Python standard library (Python 3.8 or later). See
README.md next to this script for details.
"""

from __future__ import annotations

import argparse
import collections
import getpass
import hashlib
import http.client
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DOCKERFILE = SCRIPT_DIR / "Dockerfile"
DOCKER_FILES_DIR = SCRIPT_DIR / "docker"
OPENBOX_CONFIG = DOCKER_FILES_DIR / "openbox-rc.xml"
INDEX_HTML = DOCKER_FILES_DIR / "index.html"

DEFAULT_IMAGE = "slicer-studio-web:latest"
DEFAULT_CONTAINER = "slicer-studio-web"
DEFAULT_PORT = 8080
DEFAULT_LISTEN = "127.0.0.1"
DEFAULT_GEOMETRY = "1600x1000"
DEFAULT_DATA_DIR_NAME = "SlicerStudioData"
# The Slicer Studio folder inside the container (A-09): projects and studies are saved there, in the data folder
CONTAINER_PROJECTS_ROOT = "/data/Slicer Studio"
PROJECTS_FOLDER_NAME = "Slicer Studio"
DEFAULT_HOME_VOLUME = "slicer-studio-web-home"
CONTAINER_WEB_PORT = 8080
# User and group ids of the "slicer" user of the image.
IMAGE_USER_IDS = (1000, 1000)
# Used when Docker Hub refuses to serve the default base image (rate limit).
MIRROR_BASE_IMAGE = "mirror.gcr.io/library/ubuntu:24.04"

# noVNC release downloaded by "local" when no system package provides it. The
# checksum is computed over the extracted files (see tree_sha256), so it does
# not depend on how the server compresses the archive.
NOVNC_VERSION = "1.6.0"
NOVNC_URL = f"https://github.com/novnc/noVNC/archive/refs/tags/v{NOVNC_VERSION}.zip"
NOVNC_TREE_SHA256 = "ce861b7f5a09ce6083f546cd03a7e63763452bd85898f776c180dd18f1689a02"
SYSTEM_NOVNC_DIRS = ("/usr/share/novnc", "/usr/share/webapps/novnc", "/usr/local/share/novnc")

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
# Process names of the Slicer launcher and application.
SLICER_PROCESS_NAMES = ("Slicer", "SlicerApp-real")

# In-app updates (decision D-U1): the manifest of the latest public release (no GitHub API, no token). The
# environment variable SLICER_STUDIO_UPDATE_FEED replaces the address (tests); "off" turns update checks off.
UPDATE_REPOSITORY = "azhmeerjesani/Slicer-2"
UPDATE_FEED_URL = f"https://github.com/{UPDATE_REPOSITORY}/releases/latest/download/studio-update.json"
UPDATE_DOWNLOAD_URL = "https://github.com/{repository}/releases/download/{tag}/{name}"
UPDATE_FEED_VARIABLE = "SLICER_STUDIO_UPDATE_FEED"
UPDATE_ALLOWED_HOSTS = ("github.com",)
UPDATE_ALLOWED_HOST_SUFFIXES = (".githubusercontent.com",)
UPDATE_CHECK_TIMEOUT = 5
UPDATE_MANIFEST_MAX_BYTES = 256 * 1024
# Docker image label with the Slicer Studio version of the package the image was built from
VERSION_LABEL = "org.slicerstudio.version"
VERSION_FILE_IN_SOURCE = SCRIPT_DIR.parent.parent / "Modules" / "Scripted" / "Studio" / "StudioLib" / "version.py"

DOCKER_INSTALL_HINT = "Install Docker Desktop (macOS, Windows: https://www.docker.com/products/docker-desktop/) or Docker Engine (Linux: https://docs.docker.com/engine/install/)."


class LauncherError(Exception):
    """Error reported to the user without a traceback."""


def info(message):
    print(message, flush=True)


def warn(message):
    print(f"Warning: {message}", file=sys.stderr, flush=True)


def script_command(command=None, args=None):
    """Return how to call this script again (optionally a command for the same container), for messages."""
    text = f"{'py' if os.name == 'nt' else 'python3'} {Path(sys.argv[0]).name}"
    if command:
        text += f" {command}"
    if args is not None and getattr(args, "name", DEFAULT_CONTAINER) != DEFAULT_CONTAINER:
        text += f" --name {args.name}"
    return text


# ---------------------------------------------------------------------------
# Docker helpers


def docker_executable():
    docker = shutil.which("docker")
    if not docker:
        raise LauncherError(f"Docker is not installed (the 'docker' command was not found).\n  {DOCKER_INSTALL_HINT}")
    return docker


def docker(*args, check=True, timeout=None, env=None):
    """Run a docker command and return the completed process (output captured)."""
    result = subprocess.run([docker_executable(), *args], capture_output=True, encoding="utf-8", errors="replace", timeout=timeout, env=env, check=False)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise LauncherError(f"'docker {args[0]}' failed: {detail}")
    return result


def ensure_docker_running():
    """Check that the Docker daemon answers; return its CPU architecture."""
    try:
        result = docker("info", "--format", "{{.ServerVersion}} {{.Architecture}}", check=False, timeout=60)
    except subprocess.TimeoutExpired:
        raise LauncherError("Docker does not answer. Restart Docker Desktop (or the Docker service) and try again.")
    if result.returncode != 0 or not result.stdout.strip():
        detail = (result.stderr or result.stdout).strip().splitlines()
        if "permission denied" in result.stderr.lower():
            hint = "Your user may not use Docker: add it to the 'docker' group, then log in again."
        else:
            hint = "Start Docker Desktop (or the Docker service) and try again."
        message = f"Docker is not running or cannot be reached.\n  {hint}"
        if detail:
            message += f"\n  docker info: {detail[-1]}"
        raise LauncherError(message)
    fields = result.stdout.split()
    return fields[-1] if fields else ""


def needs_amd64_emulation(docker_architecture):
    return docker_architecture.lower() not in ("x86_64", "amd64")


def emulation_note():
    return (
        "Docker runs the Linux amd64 image with emulation on this computer (for example a Mac with Apple Silicon).\n"
        "  Slicer works, but it is slower and takes longer to start (up to a few minutes). In Docker Desktop, turn on\n"
        "  'Use Rosetta for x86_64/amd64 emulation on Apple Silicon' (Settings > General) for better speed.")


def image_exists(image):
    return docker("image", "inspect", image, check=False).returncode == 0


def pull_image(image, emulated):
    """Pull an image that names a registry or repository (such as ghcr.io/OWNER/slicer-studio-web); return True on success."""
    if "/" not in image:
        return False
    info(f"Downloading the image {image}")
    command = [docker_executable(), "pull"] + (["--platform", "linux/amd64"] if emulated else []) + [image]
    returncode, _ = run_streaming(command)
    if returncode != 0:
        warn(f"The image {image} could not be downloaded (for a private image, run 'docker login' first).")
    return returncode == 0


def container_state(name):
    """Return the container status ("running", "exited", ...) or None when it does not exist."""
    result = docker("container", "inspect", "--format", "{{.State.Status}}", name, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def container_url(name):
    """Return the browser address of a running container, from its published port."""
    result = docker("port", name, f"{CONTAINER_WEB_PORT}/tcp", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    host, _, port = result.stdout.strip().splitlines()[0].rpartition(":")
    return browser_url(host.strip("[]"), int(port))


def container_logs_tail(name, lines=30):
    """Return the last lines of the container output (its stdout and stderr in order)."""
    result = subprocess.run([docker_executable(), "logs", "--tail", str(lines), name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            encoding="utf-8", errors="replace", check=False)
    return result.stdout.strip()


def mount_option(kind, source, target):
    """Return a --mount value; the source is quoted (CSV) when it contains a comma or a quote."""
    source_field = f"source={source}"
    if "," in source_field or '"' in source_field:
        source_field = '"' + source_field.replace('"', '""') + '"'
    return f"type={kind},{source_field},target={target}"


def host_user_ids():
    """Return (uid, gid) of the user of this script on Linux (under sudo: of the user who ran sudo), else None.

    Docker Desktop on macOS and Windows maps file ownership by itself.
    """
    if not sys.platform.startswith("linux"):
        return None
    sudo_uid, sudo_gid = os.environ.get("SUDO_UID", ""), os.environ.get("SUDO_GID", "")
    if os.getuid() == 0 and sudo_uid.isdigit() and sudo_gid.isdigit():
        return int(sudo_uid), int(sudo_gid)
    return os.getuid(), os.getgid()


def default_data_dir():
    """Return ~/SlicerStudioData of the user of this script (under sudo: of the user who ran sudo)."""
    sudo_user = os.environ.get("SUDO_USER")
    if sys.platform.startswith("linux") and os.getuid() == 0 and sudo_user:
        home = os.path.expanduser(f"~{sudo_user}")
        if not home.startswith("~"):
            return Path(home) / DEFAULT_DATA_DIR_NAME
    return Path.home() / DEFAULT_DATA_DIR_NAME


def default_home_volume(user_ids):
    """Return the name of the volume that keeps the Slicer settings of this user.

    Settings files can only be changed by the user id that wrote them: on Linux,
    users other than the image user get their own volume.
    """
    if user_ids is None or user_ids[0] == IMAGE_USER_IDS[0]:
        return DEFAULT_HOME_VOLUME
    return f"{DEFAULT_HOME_VOLUME}-{user_ids[0]}"


def run_streaming(command, env=None):
    """Run a command, show its output and return (exit code, last output lines)."""
    tail = collections.deque(maxlen=200)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", env=env)
    try:
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            tail.append(line)
        return process.wait(), list(tail)
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        raise


# ---------------------------------------------------------------------------
# Slicer package (input of the image build)


def cache_dir():
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "slicer-studio-web"


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url, destination):
    """Download url to destination (through the proxy of the environment, if any)."""
    partial = destination.with_name(destination.name + ".part")
    info(f"Downloading {url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, open(partial, "wb") as stream:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            reported = -1
            for block in iter(lambda: response.read(1024 * 1024), b""):
                stream.write(block)
                received += len(block)
                if total:
                    percent = received * 100 // total
                    if percent >= reported + 10:
                        reported = percent
                        info(f"  {percent}% of {total / 1e6:.0f} MB")
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        partial.unlink(missing_ok=True)
        hint = ""
        if "CERTIFICATE_VERIFY_FAILED" in str(error):
            hint = "\n  The certificate could not be verified. With Python from python.org on macOS, run 'Install Certificates.command' of that Python."
        raise LauncherError(f"Download of {url} failed: {error}{hint}")
    partial.replace(destination)


def download_package(url, expected_sha256):
    name = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
    if not name.endswith((".tar.gz", ".tgz")):
        name = f"Slicer-package-{hashlib.sha256(url.encode()).hexdigest()[:12]}.tar.gz"
    directory = cache_dir() / "packages"
    directory.mkdir(parents=True, exist_ok=True)
    package = directory / name
    if package.exists() and (not expected_sha256 or file_sha256(package) == expected_sha256.lower()):
        info(f"Using the package downloaded before: {package}")
        return package
    download(url, package)
    if expected_sha256:
        actual = file_sha256(package)
        if actual != expected_sha256.lower():
            package.unlink()
            raise LauncherError(f"The downloaded package does not have the expected SHA-256 checksum.\n  expected {expected_sha256}\n  received {actual}")
    return package


def newest_package(directory):
    packages = [path for path in directory.glob("Slicer*.tar.gz") if path.is_file()]
    return max(packages, key=lambda path: path.stat().st_mtime) if packages else None


def package_from_build_tree(build_dir, reuse):
    """Return the Slicer package of a build tree, creating it with the "package" target unless reuse is set."""
    build_dir = Path(build_dir).expanduser().resolve()
    if not (build_dir / "CPackConfig.cmake").exists() and (build_dir / "Slicer-build" / "CPackConfig.cmake").exists():
        build_dir = build_dir / "Slicer-build"  # top-level directory of a superbuild
    if not (build_dir / "CPackConfig.cmake").exists():
        raise LauncherError(f"{build_dir} is not a Slicer build tree (CPackConfig.cmake was not found).")
    existing = newest_package(build_dir)
    if reuse:
        if not existing:
            raise LauncherError(f"There is no Slicer*.tar.gz package in {build_dir}; remove --reuse-package to create one.")
        info(f"Using the existing package {existing}")
        return existing
    cmake = shutil.which("cmake")
    if not cmake:
        raise LauncherError("cmake was not found; it is needed to create the package of the build tree.")
    info(f"Creating the Slicer package of {build_dir} (this can take a while).")
    started = time.time()
    if subprocess.call([cmake, "--build", str(build_dir), "--target", "package", "--parallel", str(os.cpu_count() or 2)]) != 0:
        raise LauncherError("Creating the package failed (see the output above).")
    package = newest_package(build_dir)
    if not package or package.stat().st_mtime < started - 1:
        raise LauncherError(f"The package target did not create a Slicer*.tar.gz file in {build_dir}.")
    return package


def resolve_package(args):
    """Return the path of the Slicer package selected by the command-line options, or None."""
    sources = [option for option in (args.package, args.package_url, args.slicer_build) if option]
    if len(sources) > 1:
        raise LauncherError("Use only one of --package, --package-url and --slicer-build.")
    if args.package:
        package = Path(args.package).expanduser().resolve()
        if not package.is_file():
            raise LauncherError(f"The package {package} does not exist.")
    elif args.package_url:
        package = download_package(args.package_url, args.package_sha256)
    elif args.slicer_build:
        package = package_from_build_tree(args.slicer_build, args.reuse_package)
    else:
        return None
    if not package.name.endswith((".tar.gz", ".tgz")):
        raise LauncherError(f"{package.name} is not a Slicer Linux package (Slicer-*-linux-amd64.tar.gz).")
    if "linux" not in package.name.lower():
        warn(f"{package.name} does not look like a Linux package; the image needs the Linux amd64 package of Slicer.")
    return package


def link_or_copy(source, destination):
    try:
        os.link(source, destination)
    except OSError:
        info(f"Copying {source.name} into the build context.")
        shutil.copy2(source, destination)


def docker_desktop_host_url(parts):
    """Return the URL (urlsplit result) with its host replaced by the address of this computer in Docker Desktop."""
    user_info, at, _ = parts.netloc.rpartition("@")
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:  # invalid port: leave the URL to the build tools
        return parts.geturl()
    return urllib.parse.urlunsplit(parts._replace(netloc=f"{user_info}{at}host.docker.internal{port}"))


def proxy_build_arguments():
    """Pass the proxy settings of this environment to the image build.

    A proxy on localhost is reached through the host network on Linux and
    through host.docker.internal with Docker Desktop.
    """
    arguments = []
    use_host_network = False
    for name in PROXY_VARIABLES:
        value = os.environ.get(name)
        if not value:
            continue
        if name.lower() != "no_proxy":
            parts = urllib.parse.urlsplit(value if "://" in value else f"http://{value}")
            if parts.hostname in LOOPBACK_HOSTS:
                if sys.platform.startswith("linux"):
                    use_host_network = True
                else:
                    value = docker_desktop_host_url(parts)
        arguments += ["--build-arg", f"{name}={value}"]
    if use_host_network:
        arguments += ["--network", "host"]
    return arguments


def is_rate_limited(output_lines):
    text = "".join(output_lines).lower()
    return any(marker in text for marker in ("toomanyrequests", "too many requests", "rate limit"))


def build_image(args, package, docker_architecture):
    if not DOCKERFILE.exists() or not DOCKER_FILES_DIR.is_dir():
        raise LauncherError(f"The Dockerfile and the docker/ folder must be next to this script ({SCRIPT_DIR}).")
    platform_arguments = []
    if args.platform:
        platform_arguments = ["--platform", args.platform]
    elif needs_amd64_emulation(docker_architecture):
        platform_arguments = ["--platform", "linux/amd64"]
        info(emulation_note())
    env = dict(os.environ, DOCKER_BUILDKIT="1")
    with tempfile.TemporaryDirectory(prefix="slicer-studio-web-build-") as temporary:
        context = Path(temporary)
        shutil.copy2(DOCKERFILE, context / "Dockerfile")
        shutil.copytree(DOCKER_FILES_DIR, context / "docker")
        link_or_copy(package, context / package.name)
        build_arguments = ["--build-arg", f"SLICER_PACKAGE={package.name}"]
        package_version = package_studio_version(package)
        if package_version:
            build_arguments += ["--label", f"{VERSION_LABEL}={package_version}"]
        if args.extra_ca_cert:
            shutil.copy2(Path(args.extra_ca_cert).expanduser(), context / "extra-ca.crt")
            build_arguments += ["--build-arg", "EXTRA_CA_CERT_FILE=extra-ca.crt"]
        command = [docker_executable(), "build", "--file", str(context / "Dockerfile"), "--tag", args.image]
        command += platform_arguments + build_arguments + proxy_build_arguments()
        if args.no_cache:
            command.append("--no-cache")
        base_images = [args.base_image] if args.base_image else [None, MIRROR_BASE_IMAGE]
        for attempt, base_image in enumerate(base_images):
            base_arguments = ["--build-arg", f"BASE_IMAGE={base_image}"] if base_image else []
            info(f"Building the image {args.image} from {package.name}" + (f" (base image {base_image})" if base_image else ""))
            returncode, output = run_streaming(command + base_arguments + [str(context)], env=env)
            if returncode == 0:
                break
            if attempt + 1 < len(base_images) and is_rate_limited(output):
                warn(f"Docker Hub refused to send the base image (rate limit); trying the mirror {MIRROR_BASE_IMAGE}.")
                continue
            raise LauncherError("Building the image failed (see the output above).")
    size = docker("image", "inspect", "--format", "{{.Size}}", args.image, check=False).stdout.strip()
    if size.isdigit():
        info(f"Image {args.image} is ready ({int(size) / 1e9:.1f} GB).")


# ---------------------------------------------------------------------------
# Network helpers


def browser_url(host, port):
    if host in ("", "0.0.0.0", "::", "127.0.0.1", "::1", "localhost"):
        host = "localhost"
    elif ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/"


def check_listen_address(listen):
    """Validate the --listen address; return True when it is reachable from other computers."""
    try:
        address = ipaddress.ip_address(listen)
    except ValueError:
        raise LauncherError(f"--listen must be an IPv4 address such as 127.0.0.1 or 0.0.0.0, not '{listen}'.")
    if address.version != 4:
        raise LauncherError("--listen supports IPv4 addresses only (for example 127.0.0.1 or 0.0.0.0).")
    return not address.is_loopback


def check_port_free(listen, port):
    in_use = False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name != "nt":
            # Like the servers: ignore connections of a previous run that are still closing
            # (a port with a listening socket still fails). On Windows this option would
            # allow binding a port in use.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((listen, port))
        except OSError:
            in_use = True
    if not in_use:
        # On macOS the bind above also succeeds when another program listens on all
        # addresses of the port: check that nothing answers there either.
        try:
            with socket.create_connection(("127.0.0.1" if listen == "0.0.0.0" else listen, port), timeout=1):
                in_use = True
        except OSError:
            pass
    if in_use:
        raise LauncherError(f"Port {port} is already in use. Close the program that uses it, or choose another port, for example: --port {port + 1}")


def free_local_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for_http(url, timeout, is_alive):
    """Wait until url answers; is_alive() returning False aborts the wait. Return True when it answered."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never use a proxy for localhost
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=5) as response:
                response.read()
                return True
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            pass
        if not is_alive():
            return False
        time.sleep(1)
    return False


def password_from_environment(args, required):
    """Return the VNC password (environment variable VNC_PASSWORD or prompt) or None."""
    password = os.environ.get("VNC_PASSWORD") or None
    if not password and args.ask_password:
        password = getpass.getpass("Password for the browser (VNC uses at most 8 characters): ") or None
    if required and not password:
        raise LauncherError("--listen makes Slicer Studio reachable from other computers: set a password with --ask-password or the VNC_PASSWORD environment variable.")
    if password and len(password) > 8:
        warn("VNC authentication only uses the first 8 characters of the password.")
    return password


def remote_access_warning(listen, port):
    warn(f"Slicer Studio will listen on {listen}:{port}: other computers that can reach this one can open it.\n"
         "  Anyone with the password can control Slicer and read and change the files of the data folder.\n"
         "  The connection is not encrypted; prefer an SSH tunnel to a localhost-only server.")


def open_browser(url, enabled):
    if enabled:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass


# ---------------------------------------------------------------------------
# Versions and updates (no GitHub API: the public "latest release" download address only)


_STUDIO_VERSION_PATTERN = re.compile(rb'^STUDIO_VERSION\s*=\s*"([^"]+)"', re.MULTILINE)
_SEMVER_PATTERN = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?$")
_ASSET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$")


def version_key(text):
    """Sort key of a semantic version (a pre-release sorts before its release); ValueError if not a version."""
    match = _SEMVER_PATTERN.match(str(text or "").strip())
    if not match:
        raise ValueError(f"not a version: {text!r}")
    pre = match.group(4)
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")) if pre else ()
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 0 if pre else 1, parts)


def launcher_version():
    """Slicer Studio version of this launcher: the VERSION file of a release bundle, else version.py of the source tree."""
    version_file = SCRIPT_DIR / "VERSION"
    if version_file.is_file():
        text = version_file.read_text(encoding="utf-8").strip()
        return text or None
    try:
        match = _STUDIO_VERSION_PATTERN.search(VERSION_FILE_IN_SOURCE.read_bytes())
    except OSError:
        return None
    return match.group(1).decode() if match else None


def package_studio_version(package):
    """STUDIO_VERSION inside a Slicer Linux package (read without extracting it), or None."""
    import tarfile

    try:
        with tarfile.open(package, "r:*") as archive:
            for member in archive:
                if member.isfile() and member.name.endswith("qt-scripted-modules/StudioLib/version.py"):
                    stream = archive.extractfile(member)
                    match = _STUDIO_VERSION_PATTERN.search(stream.read()) if stream else None
                    return match.group(1).decode() if match else None
    except (OSError, tarfile.TarError, EOFError):
        return None
    return None


def image_studio_version(image):
    """Slicer Studio version of a Docker image (its label), or None."""
    result = docker("image", "inspect", "--format", f'{{{{index .Config.Labels "{VERSION_LABEL}"}}}}', image, check=False)
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value if value and value != "<no value>" else None


def update_feed():
    value = os.environ.get(UPDATE_FEED_VARIABLE, "").strip()
    if value.lower() in ("off", "0", "none", "false"):
        return None
    return value or UPDATE_FEED_URL


def _update_url_allowed(url, feed):
    parts = urllib.parse.urlsplit(url)
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    overridden = feed != UPDATE_FEED_URL
    extra = {(urllib.parse.urlsplit(feed).hostname or "").lower()} if overridden else set()
    allowed = host in UPDATE_ALLOWED_HOSTS or host.endswith(UPDATE_ALLOWED_HOST_SUFFIXES) or host in extra
    if scheme == "https" and allowed:
        return True
    if overridden and scheme == "file":
        return True
    return overridden and scheme == "http" and allowed and host in ("localhost", "127.0.0.1", "::1")


class _UpdateRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, feed):
        urllib.request.HTTPRedirectHandler.__init__(self)
        self.feed = feed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = urllib.parse.urljoin(req.full_url, newurl)
        if not _update_url_allowed(newurl, self.feed):
            raise LauncherError(f"Refused a redirect to {urllib.parse.urlsplit(newurl).hostname}: updates come only from GitHub over HTTPS.")
        return urllib.request.HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, headers, newurl)


def _update_open(url, feed, timeout):
    if not _update_url_allowed(url, feed):
        raise LauncherError(f"Refused to download {url}: updates come only from GitHub over HTTPS.")
    opener = urllib.request.build_opener(_UpdateRedirectHandler(feed))
    request = urllib.request.Request(url, headers={"User-Agent": f"SlicerStudioWeb/{launcher_version() or 'unknown'}"})
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        raise LauncherError(f"{url}: the server answered {error.code} {error.reason}")
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
        raise LauncherError(f"Cannot reach the update server: {getattr(error, 'reason', error)}")


def _update_read(response, size, what):
    """response.read(size); a connection that drops or stalls becomes a LauncherError, not a traceback."""
    try:
        return response.read(size)
    except (http.client.HTTPException, OSError) as error:
        raise LauncherError(f"{what}: the download was interrupted ({error.__class__.__name__}: {error}).")


def fetch_update_manifest(feed=None, timeout=UPDATE_CHECK_TIMEOUT):
    """The validated manifest of the latest release (a dict). Raise LauncherError."""
    feed = feed or update_feed()
    if not feed:
        raise LauncherError(f"Update checks are turned off ({UPDATE_FEED_VARIABLE}=off).")
    with _update_open(feed, feed, timeout) as response:
        content = _update_read(response, UPDATE_MANIFEST_MAX_BYTES + 1, "The update manifest")
    if len(content) > UPDATE_MANIFEST_MAX_BYTES:
        raise LauncherError("The update manifest is too large.")
    try:
        manifest = json.loads(content.decode("utf-8"))
        version_key(manifest["version"])
        if manifest.get("minimumSupported"):
            version_key(manifest["minimumSupported"])
        if not isinstance(manifest.get("required", False), bool):
            raise ValueError("required")
        assets = manifest.get("assets") or {}
        for key in ("web", "linux-x86_64"):
            asset = assets.get(key)
            if asset is None:
                continue
            if (not _ASSET_NAME_PATTERN.match(str(asset.get("name"))) or ".." in asset["name"] or not isinstance(asset.get("size"), int)
                    or asset["size"] <= 0 or not re.match(r"^[0-9a-f]{64}$", str(asset.get("sha256", "")).lower())):
                raise ValueError(f"assets.{key}")
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as error:
        raise LauncherError(f"The update manifest is not valid ({error}).")
    return manifest


def update_status(manifest, current):
    """'none', 'optional' or 'required' for the version ``current`` (None: unknown version)."""
    if not current:
        return "optional" if manifest.get("required") else "none"
    try:
        newer = version_key(manifest["version"]) > version_key(current)
        below = bool(manifest.get("minimumSupported")) and version_key(current) < version_key(manifest["minimumSupported"])
    except ValueError:
        return "none"
    if not newer:
        return "none"
    return "required" if manifest.get("required") or below else "optional"


def check_for_updates(current, quiet_failure=True):
    """At launcher start: stop on a required update, print a notice for an optional one."""
    if update_feed() is None:
        return "off"
    try:
        manifest = fetch_update_manifest()
    except LauncherError as error:
        if not quiet_failure:
            raise
        info(f"(Could not check for updates: {error})")
        return "unknown"
    status = update_status(manifest, current)
    if status == "required":
        raise LauncherError(
            f"Slicer Studio {manifest['version']} is a required update (you have {current or 'an unknown version'}).\n"
            f"  Update with: {script_command('update')}\n"
            f"  then start it again. Release notes: https://github.com/{UPDATE_REPOSITORY}/releases/tag/v{manifest['version']}")
    if status == "optional":
        info(f"Slicer Studio {manifest['version']} is available (you have {current or 'an unknown version'}). Update with: {script_command('update')}")
    return status


def download_update_asset(manifest, key, directory, feed=None):
    """Download a release file of the manifest to directory and verify its size and SHA-256. Return its path."""
    asset = (manifest.get("assets") or {}).get(key)
    if asset is None:
        raise LauncherError(f"The release {manifest['version']} has no '{key}' file.")
    feed = feed or update_feed()
    name = asset["name"]
    if feed and feed != UPDATE_FEED_URL:
        url = urllib.parse.urljoin(feed, urllib.parse.quote(name))
    else:
        url = UPDATE_DOWNLOAD_URL.format(repository=UPDATE_REPOSITORY, tag=f"v{manifest['version']}", name=urllib.parse.quote(name))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if path.is_file() and path.stat().st_size == asset["size"] and file_sha256(path) == asset["sha256"].lower():
        info(f"Using {name} downloaded before (checked).")
        return path
    partial = path.with_name(name + ".part")
    digest = hashlib.sha256()
    received = 0
    reported = -1
    info(f"Downloading {name} ({asset['size'] / 1e6:.0f} MB)")
    try:
        with _update_open(url, feed, 60) as response, open(partial, "wb") as stream:
            for block in iter(lambda: _update_read(response, 1024 * 1024, name), b""):
                received += len(block)
                if received > asset["size"]:
                    raise LauncherError(f"{name} is larger than the release file: refused.")
                digest.update(block)
                stream.write(block)
                percent = received * 100 // asset["size"]
                if percent >= reported + 10:
                    reported = percent
                    info(f"  {percent}%")
        if received != asset["size"]:
            raise LauncherError(f"{name} has the wrong size ({received} bytes instead of {asset['size']}): refused.")
        if digest.hexdigest() != asset["sha256"].lower():
            raise LauncherError(f"{name} does not have the SHA-256 checksum of the release: refused.")
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(path)
    return path


def unpack_web_bundle(bundle, destination):
    """Unpack the launcher bundle (one top folder) into destination. Return the new launcher folder."""
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        tops = {name.split("/", 1)[0] for name in names}
        if len(tops) != 1 or any(name.startswith("/") or ".." in name.split("/") or "\\" in name for name in names):
            raise LauncherError(f"{bundle.name} is not a valid launcher bundle.")
        target = destination / tops.pop()
        if target.resolve() == SCRIPT_DIR:
            raise LauncherError(f"{target} is the folder of this launcher; choose another --dest.")
        if target.exists():
            shutil.rmtree(target)
        destination.mkdir(parents=True, exist_ok=True)
        archive.extractall(destination)
        for info_entry in archive.infolist():
            mode = (info_entry.external_attr >> 16) & 0o777
            if mode:
                os.chmod(destination / info_entry.filename, mode)
    if not (target / "slicer_studio_web.py").is_file():
        raise LauncherError(f"{bundle.name} does not contain slicer_studio_web.py.")
    return target


def command_update(args):
    """Download, verify and install the latest release of the web version."""
    feed = update_feed()
    if feed is None:
        raise LauncherError(f"Update checks are turned off ({UPDATE_FEED_VARIABLE}=off).")
    manifest = fetch_update_manifest(feed, timeout=30)
    version = manifest["version"]
    current = None
    if not args.no_build:
        try:
            ensure_docker_running()
            current = image_studio_version(args.image)
        except LauncherError:
            current = None
    current = current or launcher_version()
    status = update_status(manifest, current)
    if status == "none" and not args.force:
        info(f"Slicer Studio is up to date ({current}; the latest release is {version}).")
        return 0
    info(f"Updating to Slicer Studio {version} (you have {current or 'an unknown version'}).")
    directory = cache_dir() / "updates" / version
    package = download_update_asset(manifest, "linux-x86_64", directory, feed)
    launcher_dir = SCRIPT_DIR
    if (manifest.get("assets") or {}).get("web"):
        bundle = download_update_asset(manifest, "web", directory, feed)
        if args.dest:
            destination = Path(args.dest).expanduser().resolve()
        elif (SCRIPT_DIR / "VERSION").is_file():
            destination = SCRIPT_DIR.parent  # next to the launcher bundle this one came from
        else:
            destination = cache_dir() / "launcher"  # not inside a source checkout
        launcher_dir = unpack_web_bundle(bundle, destination)
        info(f"New launcher: {launcher_dir}")
    else:
        warn(f"The release {version} has no launcher bundle; the image is built with this launcher.")
    new_script = launcher_dir / "slicer_studio_web.py"
    if args.no_build:
        info(f"Verified package: {package}")
        info(f"Build the image with: python3 {new_script} build --package {package}")
        return 0
    command = [sys.executable, str(new_script), "build", "--package", str(package), "--image", args.image]
    info("Building the image with the new launcher.")
    if subprocess.call(command) != 0:
        raise LauncherError("Building the image of the update failed (see above); the previous image is kept.")
    info("")
    info(f"Slicer Studio {version} is installed. Start it with: python3 {new_script} run --recreate")
    return 0

# ---------------------------------------------------------------------------
# Commands: build, run, stop, logs, status


def command_build(args):
    architecture = ensure_docker_running()
    package = resolve_package(args)
    if package is None:
        raise LauncherError("Choose the Slicer Linux package: --package FILE, --package-url URL or --slicer-build DIR.")
    build_image(args, package, architecture)
    info(f"Start it with: {script_command('run')}" + (f" --image {args.image}" if args.image != DEFAULT_IMAGE else ""))


def command_run(args):
    architecture = ensure_docker_running()
    emulated = needs_amd64_emulation(architecture)
    if not args.no_update_check:
        current = (image_studio_version(args.image) if image_exists(args.image) else None) or launcher_version()
        check_for_updates(current)
    state = container_state(args.name)
    if state == "running" and not args.recreate:
        url = container_url(args.name) or browser_url(args.listen, args.port)
        info(f"Slicer Studio is already running at {url}")
        info(f"  Stop it with: {script_command('stop', args)}   (or start a new one with --recreate)")
        open_browser(url, not args.no_browser)
        return

    remote = check_listen_address(args.listen)
    password = password_from_environment(args, required=remote)
    package = resolve_package(args)
    if package is not None:
        build_image(args, package, architecture)
    elif not image_exists(args.image) and not pull_image(args.image, emulated):
        raise LauncherError(
            f"The Docker image {args.image} does not exist yet. Build it from a Slicer Linux package:\n"
            f"  {script_command('build')} --package Slicer-X.Y.Z-linux-amd64.tar.gz\n"
            f"or give the package to this command (--package, --package-url or --slicer-build). See README.md.")
    if state is not None:
        docker("rm", "--force", "--volumes", args.name)
    if remote:
        remote_access_warning(args.listen, args.port)
    check_port_free(args.listen, args.port)

    user_ids = host_user_ids()
    data_dir = (Path(args.data).expanduser() if args.data else default_data_dir()).resolve()
    # Under sudo, the folders created here belong to the user who ran sudo.
    owner_ids = user_ids if user_ids and os.getuid() == 0 else None
    created = [path for path in (data_dir, *data_dir.parents) if not path.exists()] if owner_ids else []
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        for path in created:
            os.chown(path, *owner_ids)
    except OSError as error:
        raise LauncherError(f"The data folder {data_dir} cannot be created: {error}")
    home_volume = default_home_volume(user_ids) if args.home_volume is None else args.home_volume
    command = ["run", "--detach", "--name", args.name]
    command += ["--publish", f"{args.listen}:{args.port}:{CONTAINER_WEB_PORT}"]
    command += ["--mount", mount_option("bind", data_dir, "/data")]
    # Projects are saved in the data folder, without the first-start folder question
    command += ["--env", f"SLICER_STUDIO_PROJECTS_ROOT={CONTAINER_PROJECTS_ROOT}"]
    if home_volume:
        command += ["--mount", mount_option("volume", home_volume, "/home/slicer")]
    command += ["--shm-size", "2g"]
    if args.geometry:
        command += ["--env", f"GEOMETRY={args.geometry}"]
    env = dict(os.environ)
    env.pop("VNC_PASSWORD", None)
    if password:
        env["VNC_PASSWORD"] = password
        command += ["--env", "VNC_PASSWORD"]  # the value comes from the environment, not the command line
    if emulated:
        command += ["--platform", "linux/amd64"]
    if user_ids and user_ids != IMAGE_USER_IDS:
        # Run with the ids of this user instead of those of the image user, so that
        # Slicer can write to the data folder and the files belong to this user.
        command += ["--user", f"{user_ids[0]}:{user_ids[1]}"]
        if user_ids[0] == 0:
            warn("This script runs as root, so Slicer runs as root in the container too. Run it as your user (a member of the 'docker' group) instead.")
    command.append(args.image)
    command += args.slicer_args

    if emulated:
        info(emulation_note())
    info(f"Starting Slicer Studio (data folder: {data_dir}).")
    docker(*command, env=env)

    url = browser_url(args.listen, args.port)
    probe_url = f"http://127.0.0.1:{args.port}/" if not remote or args.listen == "0.0.0.0" else url
    timeout = args.timeout or (600 if emulated else 180)
    if not wait_for_http(probe_url, timeout, lambda: container_state(args.name) == "running"):
        logs = container_logs_tail(args.name)
        if container_state(args.name) != "running":
            raise LauncherError(f"The container stopped. Last log lines:\n{logs}")
        raise LauncherError(f"Slicer Studio did not answer at {url} within {timeout} s. See: {script_command('logs', args)}")
    info("")
    info(f"Slicer Studio is running at {url}")
    info("  Slicer appears in the browser a few seconds after the page opens" + (" (longer with emulation)." if emulated else "."))
    info(f"  Data folder: {data_dir} (the Documents folder in Slicer, also /data)")
    info(f"  Your work is saved automatically in {data_dir / PROJECTS_FOLDER_NAME} (Projects and Studies).")
    info(f"  Stop it with: {script_command('stop', args)}")
    open_browser(url, not args.no_browser)


def command_stop(args):
    ensure_docker_running()
    if container_state(args.name) is None:
        info("Slicer Studio is not running.")
        return
    info("Stopping Slicer Studio.")
    docker("stop", "--time", "20", args.name, check=False)
    docker("rm", "--force", "--volumes", args.name)
    info("Stopped. Files saved in the data folder stay there.")


def command_logs(args):
    ensure_docker_running()
    if container_state(args.name) is None:
        raise LauncherError("There is no Slicer Studio container (start it with 'run').")
    command = [docker_executable(), "logs", "--tail", str(args.tail)]
    if args.follow:
        command.append("--follow")
    try:
        return subprocess.call(command + [args.name])
    except KeyboardInterrupt:
        return 0


def command_status(args):
    try:
        architecture = ensure_docker_running()
    except LauncherError as error:
        info(str(error))
        return 1
    info(f"Docker: running ({architecture})" + (" - the amd64 image runs with emulation" if needs_amd64_emulation(architecture) else ""))
    info(f"Launcher: Slicer Studio {launcher_version() or 'version unknown'}")
    result = docker("image", "inspect", "--format", "{{.Size}}|{{.Created}}", args.image, check=False)
    if result.returncode == 0:
        size, created = result.stdout.strip().split("|", 1)
        image_version = image_studio_version(args.image)
        info(f"Image: {args.image} ({int(size) / 1e9:.1f} GB, created {created[:19].replace('T', ' ')}"
             + (f", Slicer Studio {image_version}" if image_version else "") + ")")
    else:
        info(f"Image: {args.image} is not built yet (see '{script_command('build')} --help')")
    data_format = '{{.State.Status}}|{{.State.StartedAt}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}'
    result = docker("container", "inspect", "--format", data_format, args.name, check=False)
    if result.returncode != 0:
        info("Container: not running")
        return 0
    status, started, health, data_dir = result.stdout.strip().split("|", 3)
    info(f"Container: {args.name} is {status}" + (f" ({health})" if health else "") + (f", started {started[:19].replace('T', ' ')}" if status == "running" else ""))
    if status == "running":
        info(f"Address: {container_url(args.name)}")
    if data_dir:
        info(f"Data folder: {data_dir}")
    return 0


# ---------------------------------------------------------------------------
# Command: local (Linux, no Docker)


def tree_sha256(root):
    """Checksum of a directory tree: SHA-256 of the "<file SHA-256>  <relative path>" lines sorted by path."""
    files = sorted((path.relative_to(root).as_posix(), path) for path in root.rglob("*") if path.is_file())
    lines = [f"{file_sha256(path)}  {relative}\n" for relative, path in files]
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def download_novnc():
    """Download and verify the pinned noVNC release into the cache folder; return its directory."""
    directory = cache_dir()
    target = directory / f"noVNC-{NOVNC_VERSION}"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="novnc-", dir=directory) as temporary:
        temporary = Path(temporary)
        archive = temporary / "noVNC.zip"
        download(NOVNC_URL, archive)
        extracted = temporary / "extracted"
        with zipfile.ZipFile(archive) as zip_file:
            for member in zip_file.namelist():
                if member.startswith("/") or ".." in Path(member).parts:
                    raise LauncherError(f"Unexpected file name in the noVNC archive: {member}")
            zip_file.extractall(extracted)
        folders = [path for path in extracted.iterdir() if path.is_dir()]
        if len(folders) != 1 or not (folders[0] / "vnc.html").exists():
            raise LauncherError("The noVNC archive does not have the expected content.")
        checksum = tree_sha256(folders[0])
        if checksum != NOVNC_TREE_SHA256:
            raise LauncherError(f"The noVNC download does not have the expected content (checksum {checksum}).")
        if target.exists():
            shutil.rmtree(target)
        folders[0].replace(target)
    info(f"noVNC {NOVNC_VERSION} saved to {target}")
    return target


def find_novnc(requested):
    if requested:
        directory = Path(requested).expanduser()
        if not (directory / "vnc.html").exists():
            raise LauncherError(f"{directory} is not a noVNC folder (vnc.html is missing).")
        return directory
    for candidate in SYSTEM_NOVNC_DIRS:
        if (Path(candidate) / "vnc.html").exists():
            return Path(candidate)
    cached = cache_dir() / f"noVNC-{NOVNC_VERSION}"
    if (cached / "vnc.html").exists():
        return cached
    info("noVNC is not installed; downloading it.")
    return download_novnc()


def find_websockify():
    executable = shutil.which("websockify")
    if executable:
        return [executable]
    if importlib.util.find_spec("websockify") is not None:
        return [sys.executable, "-m", "websockify"]
    raise LauncherError(
        "websockify was not found. Install it, for example:\n"
        "  sudo apt install websockify          (Debian, Ubuntu)\n"
        "  sudo dnf install python3-websockify  (Fedora)\n"
        "  python3 -m pip install --user websockify")


def find_slicer(requested):
    """Return the Slicer launcher to run: the requested one, or the newest one found in usual places."""
    if requested:
        path = Path(requested).expanduser()
        for candidate in (path, path / "Slicer", path / "Slicer-build" / "Slicer"):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
        raise LauncherError(f"No Slicer executable at {path}. Give the 'Slicer' launcher of an installation or of a build tree (Slicer-build/Slicer).")
    candidates = []
    if os.environ.get("SLICER_EXECUTABLE"):
        candidates.append(Path(os.environ["SLICER_EXECUTABLE"]))
    if shutil.which("Slicer"):
        candidates.append(Path(shutil.which("Slicer")))
    # Build trees next to the source tree that contains this script (<source>/Utilities/SlicerStudioWeb).
    source_parent = SCRIPT_DIR.parents[2] if len(SCRIPT_DIR.parents) > 2 else SCRIPT_DIR
    patterns = [
        (source_parent, "*/Slicer-build/Slicer"),
        (Path.home(), "Slicer*/Slicer"),
        (Path.home(), "Slicer*/Slicer-build/Slicer"),
        (Path("/opt"), "Slicer*/Slicer"),
        (Path("/opt"), "*/Slicer-build/Slicer"),
    ]
    for base, pattern in patterns:
        if base.is_dir():
            candidates += sorted(base.glob(pattern))
    found = [path for path in candidates if path.is_file() and os.access(path, os.X_OK)]
    if not found:
        raise LauncherError("Slicer was not found. Give it with --slicer PATH (the 'Slicer' launcher of an installation or of a build tree).")
    return max(found, key=lambda path: path.stat().st_mtime).resolve()


def free_display_number():
    for number in range(20, 100):
        if not Path(f"/tmp/.X11-unix/X{number}").exists() and not Path(f"/tmp/.X{number}-lock").exists():
            return number
    raise LauncherError("No free X display number was found.")


class LocalSession:
    """Virtual display, VNC server, window manager, websockify and Slicer as local processes."""

    def __init__(self, args, slicer, novnc_dir, websockify, password):
        self.args = args
        self.slicer = slicer
        self.novnc_dir = novnc_dir
        self.websockify = websockify
        self.password = password
        self.display = free_display_number()
        self.vnc_port = free_local_port()
        self.runtime_dir = Path(tempfile.mkdtemp(prefix="slicer-studio-web-"))
        self.xauthority = self.runtime_dir / "Xauthority"
        self.services = []
        self.slicer_process = None

    @staticmethod
    def start_process(command, env=None, output=None):
        # A new session: Ctrl+C in the terminal reaches this script only, which stops everything in order.
        return subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)

    def start_service(self, command, name, env=None):
        """Start a helper process; its output goes to a log file shown when it fails."""
        with open(self.runtime_dir / f"{name}.log", "wb") as log_file:
            process = self.start_process(command, env=env, output=log_file)
        self.services.append((name, process))
        return process

    def service_failure(self, message):
        """Return a LauncherError with the end of the log of the helper processes that stopped."""
        for name, process in self.services:
            if process.poll() is not None:
                log_lines = (self.runtime_dir / f"{name}.log").read_text(errors="replace").strip().splitlines()
                message += f"\n{name} stopped (status {process.returncode}). Last output:\n  " + "\n  ".join(log_lines[-15:])
        return LauncherError(message)

    def write_xauthority(self):
        """Write an X authority file with a new cookie: only the processes started here can use the display."""

        def field(data):
            return struct.pack(">H", len(data)) + data

        # One entry: any address (family 0xFFFF), the display number, a random MIT-MAGIC-COOKIE-1.
        entry = struct.pack(">H", 0xFFFF) + field(b"") + field(str(self.display).encode()) + field(b"MIT-MAGIC-COOKIE-1") + field(os.urandom(16))
        self.xauthority.write_bytes(entry)
        self.xauthority.chmod(0o600)

    def display_env(self):
        env = dict(os.environ)
        env["DISPLAY"] = f":{self.display}"
        env["XAUTHORITY"] = str(self.xauthority)
        env.pop("VNC_PASSWORD", None)
        env.pop("WAYLAND_DISPLAY", None)  # show Qt applications on the virtual display, not on the desktop
        env["QT_QPA_PLATFORM"] = "xcb"
        return env

    def web_root(self):
        """noVNC files plus the index page that opens the client with the right options."""
        root = self.runtime_dir / "web"
        root.mkdir()
        for entry in self.novnc_dir.iterdir():
            if entry.name != "index.html":
                (root / entry.name).symlink_to(entry.resolve())
        if INDEX_HTML.exists():
            shutil.copy2(INDEX_HTML, root / "index.html")
        return root

    def start_display(self):
        geometry = self.args.geometry or DEFAULT_GEOMETRY
        socket_path = Path(f"/tmp/.X11-unix/X{self.display}")
        self.write_xauthority()
        xvnc = shutil.which("Xvnc")
        if xvnc:
            security = ["-SecurityTypes", "None"]
            if self.password:
                vncpasswd = shutil.which("vncpasswd")
                if not vncpasswd:
                    raise LauncherError("vncpasswd (TigerVNC tools) is needed to set a password.")
                password_file = self.runtime_dir / "vncpasswd"
                encoded = subprocess.run([vncpasswd, "-f"], input=(self.password + "\n").encode(), capture_output=True, check=True).stdout
                password_file.write_bytes(encoded)
                password_file.chmod(0o600)
                security = ["-SecurityTypes", "VncAuth", "-PasswordFile", str(password_file)]
            command = [xvnc, f":{self.display}", "-auth", str(self.xauthority), "-geometry", geometry, "-depth", "24", "-desktop", "Slicer Studio",
                       "-rfbport", str(self.vnc_port), "-localhost", "-nolisten", "tcp", "-AlwaysShared", *security]
            server = self.start_service(command, "Xvnc")
            x11vnc = None
        else:
            xvfb, x11vnc = shutil.which("Xvfb"), shutil.which("x11vnc")
            if not xvfb or not x11vnc:
                raise LauncherError(
                    "A virtual display with a VNC server is needed. Install TigerVNC, for example:\n"
                    "  sudo apt install tigervnc-standalone-server tigervnc-tools   (Debian, Ubuntu)\n"
                    "  sudo dnf install tigervnc-server                            (Fedora)\n"
                    "or install Xvfb and x11vnc.")
            server = self.start_service([xvfb, f":{self.display}", "-auth", str(self.xauthority), "-screen", "0", f"{geometry}x24", "-nolisten", "tcp"], "Xvfb")
        deadline = time.monotonic() + 30
        while not socket_path.exists():
            if server.poll() is not None or time.monotonic() > deadline:
                raise self.service_failure(f"The virtual display :{self.display} did not start.")
            time.sleep(0.1)
        if x11vnc:  # VNC server of the Xvfb display (no resizing from the browser)
            command = [x11vnc, "-display", f":{self.display}", "-auth", str(self.xauthority), "-rfbport", str(self.vnc_port), "-localhost", "-forever", "-shared", "-quiet",
                       "-desktop", "Slicer Studio"]
            if self.password:
                password_file = self.runtime_dir / "x11vnc-password"
                password_file.write_text(self.password + "\n")
                password_file.chmod(0o600)
                command += ["-passwdfile", str(password_file)]
            else:
                command += ["-nopw"]
            self.start_service(command, "x11vnc")

    def start(self):
        info(f"Starting the virtual display :{self.display}.")
        self.start_display()
        openbox = shutil.which("openbox")
        if openbox and OPENBOX_CONFIG.exists():
            self.start_service([openbox, "--config-file", str(OPENBOX_CONFIG)], "openbox", env=self.display_env())
        else:
            warn("openbox was not found: the Slicer window is not maximized and dialogs cannot be moved (sudo apt install openbox).")
        command = self.websockify + ["--web", str(self.web_root()), "--heartbeat", "30", f"{self.args.listen}:{self.args.port}", f"127.0.0.1:{self.vnc_port}"]
        self.start_service(command, "websockify")
        self.start_slicer()

    def start_slicer(self):
        info(f"Starting {self.slicer}")
        self.slicer_process = self.start_process([str(self.slicer), *self.args.slicer_args], env=self.display_env())

    def services_alive(self):
        return all(process.poll() is None for _, process in self.services)

    def slicer_instances(self):
        """Process ids of Slicer applications on this display (also those Slicer restarted by itself)."""
        pids = []
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                if (proc / "comm").read_text().strip() not in SLICER_PROCESS_NAMES:
                    continue
                environment = (proc / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if f"DISPLAY=:{self.display}".encode() in environment:
                pids.append(int(proc.name))
        return pids

    def supervise(self):
        """Wait while Slicer runs; start it again when it exits unless --no-restart is given."""
        quick_exits = 0
        while True:
            started = time.monotonic()
            while self.slicer_process.poll() is None:
                if not self.services_alive():
                    raise self.service_failure("The virtual display or the web server stopped.")
                time.sleep(0.5)
            # "Restart Slicer" starts a new instance and quits the current one.
            while self.slicer_instances():
                time.sleep(1)
            if not self.args.restart:
                info(f"Slicer exited (status {self.slicer_process.returncode}).")
                return
            quick_exits = quick_exits + 1 if time.monotonic() - started < 20 else 0
            delay = 1 + quick_exits * quick_exits if quick_exits < 5 else 30
            info(f"Slicer exited (status {self.slicer_process.returncode}); starting it again in {delay} s. Press Ctrl+C to stop.")
            time.sleep(delay)
            self.start_slicer()

    @staticmethod
    def terminate(process, timeout=10):
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        except ProcessLookupError:
            pass

    def stop(self):
        self.terminate(self.slicer_process)
        for pid in self.slicer_instances():
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for _, process in reversed(self.services):
            self.terminate(process)
        shutil.rmtree(self.runtime_dir, ignore_errors=True)


def command_local(args):
    if not sys.platform.startswith("linux"):
        raise LauncherError(f"'local' needs Linux. On macOS and Windows, use Docker: {script_command('run')}")
    remote = check_listen_address(args.listen)
    password = password_from_environment(args, required=remote)
    slicer = find_slicer(args.slicer)
    websockify = find_websockify()
    novnc_dir = find_novnc(args.novnc)
    if remote:
        remote_access_warning(args.listen, args.port)
    check_port_free(args.listen, args.port)

    def stop_on_signal(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_on_signal)
    signal.signal(signal.SIGHUP, stop_on_signal)  # the terminal was closed
    session = LocalSession(args, slicer, novnc_dir, websockify, password)
    try:
        session.start()
        url = browser_url(args.listen, args.port)
        if not wait_for_http(f"http://127.0.0.1:{args.port}/" if args.listen == "0.0.0.0" else url, 60, session.services_alive):
            raise session.service_failure("The web server did not start.")
        info("")
        info(f"Slicer Studio is running at {url}")
        info("  Press Ctrl+C here to stop it.")
        open_browser(url, not args.no_browser)
        session.supervise()
    except KeyboardInterrupt:
        info("Stopping.")
    finally:
        session.stop()
    return 0


# ---------------------------------------------------------------------------
# Command line


def add_package_options(parser):
    group = parser.add_argument_group("Slicer package (to build the image)")
    group.add_argument("--package", metavar="FILE", help="Slicer Linux package (Slicer-*-linux-amd64.tar.gz)")
    group.add_argument("--package-url", metavar="URL", help="download the Slicer Linux package from this address")
    group.add_argument("--package-sha256", metavar="HASH", help="expected SHA-256 checksum of the --package-url download")
    group.add_argument("--slicer-build", metavar="DIR", help="Slicer build tree: create its package (cmake --build DIR --target package)")
    group.add_argument("--reuse-package", action="store_true", help="with --slicer-build: use the newest existing package instead of creating one")
    group.add_argument("--base-image", metavar="IMAGE", help=f"base image (default ubuntu:24.04; {MIRROR_BASE_IMAGE} is tried when Docker Hub limits downloads)")
    group.add_argument("--extra-ca-cert", metavar="FILE", help="extra CA certificate (PEM) for downloads during the build, for networks with a TLS-inspecting proxy")
    group.add_argument("--platform", metavar="PLATFORM", help="image platform (default: linux/amd64 when Docker does not run on amd64)")
    group.add_argument("--no-cache", action="store_true", help="build the image without the Docker build cache")


def geometry_value(text):
    """Return a screen size given as WIDTHxHEIGHT (argparse type)."""
    match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", text)
    if not match or int(match.group(1)) == 0 or int(match.group(2)) == 0:
        raise argparse.ArgumentTypeError(f"'{text}' is not a screen size such as {DEFAULT_GEOMETRY}")
    return f"{int(match.group(1))}x{int(match.group(2))}"


def port_value(text):
    """Return a TCP port number from 1 to 65535 (argparse type)."""
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port '{text}': --port must be between 1 and 65535") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("--port must be between 1 and 65535")
    return port


def add_server_options(parser):
    parser.add_argument("--port", type=port_value, default=DEFAULT_PORT, help=f"browser port on this computer (default {DEFAULT_PORT})")
    parser.add_argument("--listen", default=DEFAULT_LISTEN, metavar="ADDRESS",
                        help=f"network address to listen on (default {DEFAULT_LISTEN}: this computer only; 0.0.0.0 allows other computers and needs a password)")
    parser.add_argument("--geometry", metavar="WxH", type=geometry_value, help=f"initial screen size (default {DEFAULT_GEOMETRY}; the browser window size is used once connected)")
    parser.add_argument("--ask-password", action="store_true", help="ask for a password to require in the browser (or set VNC_PASSWORD)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the web browser")
    parser.add_argument("slicer_args", nargs="*", metavar="-- SLICER_ARGS", help="arguments passed to Slicer, after '--'")


def build_parser():
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="Run Slicer Studio in a web browser (http://localhost:8080). Without a command, 'run' is used.",
        epilog="See README.md next to this script for details.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    docker_options = argparse.ArgumentParser(add_help=False)
    docker_options.add_argument("--name", default=DEFAULT_CONTAINER, help=f"container name (default {DEFAULT_CONTAINER})")
    docker_options.add_argument("--image", "--tag", dest="image", default=DEFAULT_IMAGE, help=f"image name (default {DEFAULT_IMAGE})")

    build = commands.add_parser("build", parents=[docker_options], help="build the Docker image from a Slicer Linux package")
    add_package_options(build)
    build.set_defaults(handler=command_build)

    run = commands.add_parser("run", parents=[docker_options], help="start Slicer Studio in Docker and open the browser (default)")
    add_server_options(run)
    run.add_argument("--data", metavar="DIR", help=f"folder shared with Slicer as its Documents folder and /data (default ~/{DEFAULT_DATA_DIR_NAME})")
    run.add_argument("--home-volume", metavar="NAME",
                     help=f"Docker volume that keeps Slicer settings between runs (default {DEFAULT_HOME_VOLUME}; on Linux, {DEFAULT_HOME_VOLUME}-UID for user ids other than 1000; empty: do not keep them)")
    run.add_argument("--recreate", action="store_true", help="replace a running container")
    run.add_argument("--timeout", type=int, metavar="SECONDS", help="how long to wait for the server (default 180, 600 with emulation)")
    run.add_argument("--no-update-check", action="store_true", help=f"do not check for a new Slicer Studio release (also {UPDATE_FEED_VARIABLE}=off)")
    add_package_options(run)
    run.set_defaults(handler=command_run)

    stop = commands.add_parser("stop", parents=[docker_options], help="stop Slicer Studio (Docker)")
    stop.set_defaults(handler=command_stop)

    logs = commands.add_parser("logs", parents=[docker_options], help="show the output of Slicer Studio (Docker)")
    logs.add_argument("--follow", "-f", action="store_true", help="keep showing new output")
    logs.add_argument("--tail", default="200", metavar="LINES", help="number of lines to show (default 200, 'all' for everything)")
    logs.set_defaults(handler=command_logs)

    status = commands.add_parser("status", parents=[docker_options], help="show the Docker image and container state")
    status.set_defaults(handler=command_status)

    update = commands.add_parser("update", parents=[docker_options], help="download, verify and install the latest Slicer Studio release (also --update)")
    update.add_argument("--dest", metavar="DIR", help="where to unpack the new launcher (default: next to this launcher bundle, else the cache folder)")
    update.add_argument("--no-build", action="store_true", help="only download and verify; print the build command")
    update.add_argument("--force", action="store_true", help="install the latest release even if it is not newer")
    update.set_defaults(handler=command_update)

    local = commands.add_parser("local", help="Linux only, without Docker: run a local Slicer on a virtual display")
    add_server_options(local)
    local.add_argument("--slicer", metavar="PATH", help="Slicer launcher (installation or Slicer-build of a build tree; default: search usual places)")
    local.add_argument("--novnc", metavar="DIR", help="noVNC folder (default: system package, else download noVNC " + NOVNC_VERSION + ")")
    local.add_argument("--no-restart", dest="restart", action="store_false", help="stop when Slicer exits instead of starting it again")
    local.set_defaults(handler=command_local)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = ("build", "run", "stop", "logs", "status", "local", "update")
    if "--update" in argv and (not argv or argv[0] not in commands):
        argv = ["update"] + [arg for arg in argv if arg != "--update"]
    if not argv or (argv[0] not in commands and argv[0] not in ("-h", "--help")):
        argv.insert(0, "run")
    # Everything after "--" goes to Slicer.
    slicer_args = []
    if "--" in argv:
        index = argv.index("--")
        argv, slicer_args = argv[:index], argv[index + 1:]
    args = build_parser().parse_args(argv)
    if getattr(args, "listen", None) == "localhost":
        args.listen = DEFAULT_LISTEN  # Docker needs an IP address
    if hasattr(args, "slicer_args"):
        args.slicer_args = list(args.slicer_args) + slicer_args
    try:
        return args.handler(args) or 0
    except LauncherError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

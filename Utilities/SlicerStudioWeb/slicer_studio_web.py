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
import os
import shutil
import signal
import socket
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
DEFAULT_DATA_DIR = Path.home() / "SlicerStudioData"
CONTAINER_WEB_PORT = 8080
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
SLICER_PROCESS_NAME = "SlicerApp-real"

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
    result = subprocess.run([docker_executable(), *args], capture_output=True, text=True, timeout=timeout, env=env, check=False)
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
            hint = "Your user may not use Docker: add it to the 'docker' group (then log in again) or run this script with sudo."
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
    result = docker("logs", "--tail", str(lines), name, check=False)
    return (result.stdout + result.stderr).strip()


def mount_option(kind, source, target):
    """Return a --mount value; the source is quoted (CSV) when it contains a comma or a quote."""
    source_field = f"source={source}"
    if "," in source_field or '"' in source_field:
        source_field = '"' + source_field.replace('"', '""') + '"'
    return f"type={kind},{source_field},target={target}"


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
            host = urllib.parse.urlsplit(value if "://" in value else f"http://{value}").hostname
            if host in LOOPBACK_HOSTS:
                if sys.platform.startswith("linux"):
                    use_host_network = True
                else:
                    value = value.replace(host, "host.docker.internal", 1)
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
    if listen == "localhost":
        return False
    try:
        address = ipaddress.ip_address(listen)
    except ValueError:
        raise LauncherError(f"--listen must be an IPv4 address such as 127.0.0.1 or 0.0.0.0, not '{listen}'.")
    if address.version != 4:
        raise LauncherError("--listen supports IPv4 addresses only (for example 127.0.0.1 or 0.0.0.0).")
    return not address.is_loopback


def check_port_free(listen, port):
    bind_host = "127.0.0.1" if listen == "localhost" else listen
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name != "nt":
            # Like the servers: ignore connections of a previous run that are still closing
            # (a port with a listening socket still fails). On Windows this option would
            # allow binding a port in use.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((bind_host, port))
        except OSError:
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

    data_dir = Path(args.data).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    command = ["run", "--detach", "--name", args.name]
    command += ["--publish", f"{args.listen}:{args.port}:{CONTAINER_WEB_PORT}"]
    command += ["--mount", mount_option("bind", data_dir, "/data")]
    if args.home_volume:
        command += ["--mount", mount_option("volume", args.home_volume, "/home/slicer")]
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
    if sys.platform.startswith("linux") and hasattr(os, "getuid") and os.getuid() != 1000:
        # The image user has uid 1000: run with the ids of this user instead, so
        # that Slicer can write to the data folder and the files belong to this user.
        # (Docker Desktop on macOS and Windows maps file ownership by itself.)
        command += ["--user", f"{os.getuid()}:{os.getgid()}"]
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
        info(f"Docker: {error}")
        return 1
    info(f"Docker: running ({architecture})" + (" - the amd64 image runs with emulation" if needs_amd64_emulation(architecture) else ""))
    result = docker("image", "inspect", "--format", "{{.Size}}|{{.Created}}", args.image, check=False)
    if result.returncode == 0:
        size, created = result.stdout.strip().split("|", 1)
        info(f"Image: {args.image} ({int(size) / 1e9:.1f} GB, created {created[:19].replace('T', ' ')})")
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

    def display_env(self):
        env = dict(os.environ)
        env["DISPLAY"] = f":{self.display}"
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
            command = [xvnc, f":{self.display}", "-geometry", geometry, "-depth", "24", "-desktop", "Slicer Studio",
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
            server = self.start_service([xvfb, f":{self.display}", "-screen", "0", f"{geometry}x24", "-nolisten", "tcp"], "Xvfb")
        deadline = time.monotonic() + 30
        while not socket_path.exists():
            if server.poll() is not None or time.monotonic() > deadline:
                raise self.service_failure(f"The virtual display :{self.display} did not start.")
            time.sleep(0.1)
        if x11vnc:  # VNC server of the Xvfb display (no resizing from the browser)
            command = [x11vnc, "-display", f":{self.display}", "-rfbport", str(self.vnc_port), "-localhost", "-forever", "-shared", "-quiet", "-desktop", "Slicer Studio"]
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
                if (proc / "comm").read_text().strip() != SLICER_PROCESS_NAME[:15]:
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


def add_server_options(parser):
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"browser port on this computer (default {DEFAULT_PORT})")
    parser.add_argument("--listen", default=DEFAULT_LISTEN, metavar="ADDRESS",
                        help=f"network address to listen on (default {DEFAULT_LISTEN}: this computer only; 0.0.0.0 allows other computers and needs a password)")
    parser.add_argument("--geometry", metavar="WxH", help=f"initial screen size (default {DEFAULT_GEOMETRY}; the browser window size is used once connected)")
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
    run.add_argument("--data", default=str(DEFAULT_DATA_DIR), metavar="DIR", help=f"folder shared with Slicer as its Documents folder and /data (default {DEFAULT_DATA_DIR})")
    run.add_argument("--home-volume", default="slicer-studio-web-home", metavar="NAME",
                     help="Docker volume that keeps Slicer settings between runs (default slicer-studio-web-home; empty: do not keep them)")
    run.add_argument("--recreate", action="store_true", help="replace a running container")
    run.add_argument("--timeout", type=int, metavar="SECONDS", help="how long to wait for the server (default 180, 600 with emulation)")
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

    local = commands.add_parser("local", help="Linux only, without Docker: run a local Slicer on a virtual display")
    add_server_options(local)
    local.add_argument("--slicer", metavar="PATH", help="Slicer launcher (installation or Slicer-build of a build tree; default: search usual places)")
    local.add_argument("--novnc", metavar="DIR", help="noVNC folder (default: system package, else download noVNC " + NOVNC_VERSION + ")")
    local.add_argument("--no-restart", dest="restart", action="store_false", help="stop when Slicer exits instead of starting it again")
    local.set_defaults(handler=command_local)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = ("build", "run", "stop", "logs", "status", "local")
    if not argv or (argv[0] not in commands and argv[0] not in ("-h", "--help")):
        argv.insert(0, "run")
    # Everything after "--" goes to Slicer.
    slicer_args = []
    if "--" in argv:
        index = argv.index("--")
        argv, slicer_args = argv[:index], argv[index + 1:]
    args = build_parser().parse_args(argv)
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

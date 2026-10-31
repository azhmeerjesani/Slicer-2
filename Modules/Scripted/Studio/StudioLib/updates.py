"""In-app updates of Slicer Studio (A-08, decision D-U1).

Updates are the public GitHub releases of the repository ``azhmeerjesani/Slicer-2``, created and uploaded by hand by
the publisher with the files that ``Utilities/Scripts/SlicerStudioRelease/make_release.py`` prepares. Each release
carries a manifest, ``studio-update.json``::

    {"version": "1.2.0", "required": false, "minimumSupported": null, "published": "2026-10-22",
     "notes": "plain text or Markdown",
     "assets": {"macos-arm64": {"name": "SlicerStudio-1.2.0-macos-arm64.dmg", "size": 123, "sha256": "…"}, …}}

**Check.** One anonymous HTTPS GET of ``https://github.com/azhmeerjesani/Slicer-2/releases/latest/download/
studio-update.json`` (the public "latest release" download address: no GitHub API, no account, no token). Redirects are
followed only to ``github.com`` and ``*.githubusercontent.com``; the answer is at most ``MANIFEST_MAX_BYTES``; it runs
in a worker thread and the startup waits for it at most ``CHECK_TIMEOUT_S`` seconds. Nothing runs under ``--testing``,
without main window or in a scripted run. Only the environment variable ``SLICER_STUDIO_UPDATE_FEED`` (tests) changes the
address; ``off`` turns every check off.

**Optional and required.** A release is *required* when the publisher marks it so (``"required": true``) and it is newer
than this version, or when this version is older than ``minimumSupported``. A required update shows a blocking dialog at
startup (Update now · Quit), before the recovery offer (``Studio.py`` starts autosave when the check is done) and the
first-run cards. An optional update shows a quiet notice ("Update 1.2.0 available · Update now · Skip") in Studio, and
the Updates settings panel in Studio and Classic. When the check fails, a cached manifest that says *required* shows a
non-blocking warning only.

**Channels.** A build tree or a source checkout is the *development* channel: it never checks by itself and never
installs (Check now shows what the latest release is). The web version (``SLICER_STUDIO_WEB=1``) is updated by its
launcher (``slicer_studio_web.py update``).

**Download and install.** The release file of this platform is streamed to a private folder with progress and Cancel,
its size and SHA-256 are checked against the manifest before anything is installed, and ``updates_install`` swaps the
application after it exits (and puts the previous version back if anything fails). Unsaved work is autosaved: the
session closed for the update is offered for recovery by the new version (``finishInstall()``).

Settings (fork-only, SPEC 9.7 rule 5): ``Studio/Updates/AutoCheck`` (default true), ``Studio/Updates/LastCheck``,
``Studio/Updates/LastManifest`` (the cached manifest) and ``Studio/Updates/SkippedVersion``. The defaults live in code.

The functions above the "Service" section use no Qt and no Slicer object (the tests use them with local files).
"""

import hashlib
import http.client
import importlib
import json
import logging
import os
import platform
import re
import sys
import threading
import time
import traceback
import urllib.parse

try:
    from slicer.i18n import tr as _translate
except ImportError:  # plain Python (tests of the release script)
    _translate = None


def _(text):
    """Translate on the main thread only (checks and downloads run in a worker thread, which does not call Qt)."""
    if _translate is None or threading.current_thread() is not threading.main_thread():
        return text
    return _translate(text)


from . import version as versionModule


class _LazyModule:
    """A module imported on first use: the update service starts with the application, before the first paint, and
    needs ``urllib.request`` and the installer (about 45 ms of imports) only when it checks or installs.
    """

    def __init__(self, name):
        self._name = name

    def __getattr__(self, attribute):
        return getattr(importlib.import_module(self._name), attribute)


updates_install = _LazyModule(f"{__package__}.updates_install" if __package__ else "updates_install")

#: Repository whose public releases are the updates (D-U1)
REPOSITORY = "azhmeerjesani/Slicer-2"
MANIFEST_NAME = "studio-update.json"
DEFAULT_FEED_URL = f"https://github.com/{REPOSITORY}/releases/latest/download/{MANIFEST_NAME}"
RELEASE_DOWNLOAD_URL = "https://github.com/{repository}/releases/download/{tag}/{name}"
RELEASE_PAGE_URL = "https://github.com/{repository}/releases/tag/{tag}"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
#: Environment variable that replaces the feed address (tests; "off" turns checks off). Not a settings key on purpose.
FEED_ENVIRONMENT_VARIABLE = "SLICER_STUDIO_UPDATE_FEED"
#: Hosts that the feed and the downloads may be served from (after redirects)
ALLOWED_HOSTS = ("github.com",)
ALLOWED_HOST_SUFFIXES = (".githubusercontent.com",)
#: Platform keys of the manifest
PLATFORM_KEYS = ("macos-arm64", "macos-x86_64", "linux-x86_64", "web")

#: Network timeout of a check (seconds)
CHECK_TIMEOUT_S = 5
#: The startup waits at most this long for the check (seconds): within CHECK_TIMEOUT_S with the 50 ms polling
STARTUP_WAIT_S = 4.8
#: Largest manifest accepted (bytes)
MANIFEST_MAX_BYTES = 256 * 1024
#: Longest time a check may take in all (seconds): a server that sends its answer slowly is given up
CHECK_DEADLINE_S = 2 * CHECK_TIMEOUT_S
#: Longest release notes kept (characters)
NOTES_MAX_CHARS = 20000
#: A download that receives no byte for this long (seconds) fails with "timed out" (also the timeout of connecting)
DOWNLOAD_TIMEOUT_S = 60
#: Largest block taken from the connection at once (bytes); a read returns what has arrived, it does not wait for a full block
DOWNLOAD_CHUNK = 256 * 1024
#: The download looks at Cancel at least this often (seconds), also while the server is slow or silent
DOWNLOAD_POLL_S = 0.5
#: While the application runs, a check is made when the last one is older than this (seconds)
RECHECK_INTERVAL_S = 24 * 3600
#: The optional-update notice is shown at the earliest this long after startup (ms), when no other prompt is shown
NOTICE_DELAY_MS = 4000
NOTICE_RETRY_MS = 5000
NOTICE_MAX_RETRIES = 24

SETTINGS_GROUP = "Studio/Updates"
DEFAULT_SETTINGS = {"AutoCheck": True, "LastCheck": "", "LastManifest": "", "SkippedVersion": ""}

#: Object names (stable once released, SPEC 9.2)
REQUIRED_DIALOG_OBJECT_NAME = "StudioRequiredUpdateDialog"
UPDATE_DIALOG_OBJECT_NAME = "StudioUpdateDialog"
NOTICE_OBJECT_NAME = "StudioUpdateNotice"
HELP_ACTION_OBJECT_NAME = "StudioCheckForUpdatesAction"
NOTICE_KEY = "updates"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ASSET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$")


class UpdateError(Exception):
    """A check or a download failed; the message is shown to the user."""


#
# Feed, platform and channel
#


def feedOverride():
    """The feed address set by ``SLICER_STUDIO_UPDATE_FEED`` (None when not set; "off" when turned off)."""
    value = os.environ.get(FEED_ENVIRONMENT_VARIABLE, "").strip()
    if not value:
        return None
    return "off" if value.lower() in ("off", "0", "none", "false") else value


def feedUrl():
    """The address of the manifest of the latest release, or None if checks are turned off."""
    override = feedOverride()
    if override == "off":
        return None
    return override or DEFAULT_FEED_URL


def platformKey(environ=None, systemPlatform=None, machine=None):
    """The manifest key of this installation ("macos-arm64", "macos-x86_64", "linux-x86_64", "web") or None."""
    environ = os.environ if environ is None else environ
    if environ.get("SLICER_STUDIO_WEB") == "1":
        return "web"
    systemPlatform = sys.platform if systemPlatform is None else systemPlatform
    machine = (platform.machine() if machine is None else machine).lower()
    if systemPlatform == "darwin":
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x86_64"
    if systemPlatform.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux-x86_64"
    return None


def _isLoopback(host):
    import ipaddress

    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _hostAllowed(host, extraHosts=()):
    host = (host or "").lower().rstrip(".")
    return host in ALLOWED_HOSTS or any(host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES) or host in extraHosts


def _overrideHosts(feed):
    """Hosts allowed in addition to GitHub: the host of an overridden feed (tests, mirrors)."""
    if feed and feed != DEFAULT_FEED_URL:
        host = urllib.parse.urlsplit(feed).hostname
        return (host.lower(),) if host else ()
    return ()


def checkUrl(url, extraHosts=(), allowInsecureLoopback=False, allowFile=False):
    """Raise ``UpdateError`` unless ``url`` may be fetched: HTTPS to an allowed host (plain HTTP only to this computer
    and ``file:`` only for a feed set by ``SLICER_STUDIO_UPDATE_FEED``).
    """
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme == "file" and allowFile:
        return
    host = parts.hostname or ""
    if scheme == "https" and _hostAllowed(host, extraHosts):
        return
    if scheme == "http" and allowInsecureLoopback and _isLoopback(host) and _hostAllowed(host, extraHosts):
        return
    raise UpdateError(_("Refused to contact {address}: updates come only from GitHub over HTTPS.").format(address=f"{scheme}://{host}"))


def _checkedRedirectHandler(policy):
    """A redirect handler that follows a redirect only to an allowed host (``checkUrl``)."""
    import urllib.request

    class CheckedRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            newurl = urllib.parse.urljoin(req.full_url, newurl)
            checkUrl(newurl, **policy)
            return urllib.request.HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, headers, newurl)

    return CheckedRedirectHandler()


def _sslContext():
    import ssl

    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(name)
        if path and os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _policy(feed):
    overridden = feed != DEFAULT_FEED_URL
    return {"extraHosts": _overrideHosts(feed), "allowInsecureLoopback": overridden, "allowFile": overridden}


def _open(url, feed, timeout, notFound=None):
    """Open ``url`` with the redirect and host rules of ``feed``. Return the response.

    A 404 means "no release with an update manifest" when the manifest is fetched; ``notFound`` is the message for
    another file (the release file of a download), where the manifest was found and the file is missing.
    """
    import urllib.error
    import urllib.request

    policy = _policy(feed)
    checkUrl(url, **policy)
    handlers = [_checkedRedirectHandler(policy)]
    if urllib.parse.urlsplit(url).scheme.lower() == "https":
        handlers.append(urllib.request.HTTPSHandler(context=_sslContext()))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": f"SlicerStudio/{versionModule.STUDIO_VERSION}", "Accept": "*/*"})
    try:
        return opener.open(request, timeout=timeout)
    except UpdateError:
        raise
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise UpdateError(notFound or _("No release with an update manifest was found ({address}).").format(address=url))
        raise UpdateError(_("The server answered {code} {reason}.").format(code=error.code, reason=error.reason))
    except (urllib.error.URLError, OSError, ValueError) as error:
        reason = getattr(error, "reason", error)
        raise UpdateError(_("Cannot reach the update server: {reason}").format(reason=reason))
    except http.client.HTTPException:
        # not an OSError: a reply that is not HTTP (BadStatusLine), cut off or malformed
        raise UpdateError(_("Cannot reach the update server: it did not send a valid answer."))


#
# Manifest
#


class Manifest:
    """A validated ``studio-update.json``."""

    def __init__(self, data):
        self.data = data
        self.version = str(versionModule.parse(data["version"]))
        self.required = bool(data.get("required", False))
        minimum = data.get("minimumSupported")
        self.minimumSupported = str(versionModule.parse(minimum)) if minimum else None
        self.published = str(data.get("published") or "")
        self.notes = str(data.get("notes") or "")[:NOTES_MAX_CHARS]
        self.assets = data.get("assets") or {}

    @property
    def tag(self):
        return f"v{self.version}"

    def asset(self, key):
        """``{"name", "size", "sha256"}`` of the release file for the platform ``key``, or None."""
        return self.assets.get(key) if key else None

    def releasePageUrl(self):
        return RELEASE_PAGE_URL.format(repository=REPOSITORY, tag=self.tag)

    def toJson(self):
        return json.dumps(self.data, sort_keys=True, separators=(",", ":"))

    def __repr__(self):
        return f"Manifest(version={self.version!r}, required={self.required}, minimumSupported={self.minimumSupported!r})"


def parseManifest(content):
    """Return the ``Manifest`` of ``content`` (bytes, text or dict), or raise ``UpdateError`` if it is not valid."""
    try:
        data = content if isinstance(content, dict) else json.loads(content.decode("utf-8") if isinstance(content, (bytes, bytearray)) else content)
    except (ValueError, UnicodeDecodeError) as error:
        raise UpdateError(_("The update manifest is not valid JSON: {error}").format(error=error))
    if not isinstance(data, dict):
        raise UpdateError(_("The update manifest is not a JSON object."))
    problems = []
    if not versionModule.isValid(data.get("version")):
        problems.append("version")
    if not isinstance(data.get("required", False), bool):
        problems.append("required")
    minimum = data.get("minimumSupported")
    if minimum is not None and not versionModule.isValid(minimum):
        problems.append("minimumSupported")
    if not isinstance(data.get("notes", ""), str) or not isinstance(data.get("published", ""), (str, type(None))):
        problems.append("notes/published")
    assets = data.get("assets", {})
    if not isinstance(assets, dict):
        problems.append("assets")
        assets = {}
    cleanAssets = {}
    for key, asset in assets.items():
        if key not in PLATFORM_KEYS:
            continue  # a platform of a later version
        if (not isinstance(asset, dict) or not isinstance(asset.get("name"), str) or not _ASSET_NAME_PATTERN.match(asset["name"])
                or ".." in asset["name"] or not isinstance(asset.get("size"), int) or isinstance(asset.get("size"), bool)
                or asset["size"] <= 0 or not isinstance(asset.get("sha256"), str) or not _SHA256_PATTERN.match(asset["sha256"].lower())):
            problems.append(f"assets.{key}")
            continue
        cleanAssets[key] = {"name": asset["name"], "size": asset["size"], "sha256": asset["sha256"].lower()}
    if problems:
        raise UpdateError(_("The update manifest is not valid ({fields}).").format(fields=", ".join(problems)))
    data = dict(data, assets=cleanAssets)
    return Manifest(data)


def fetchManifest(feed=None, timeout=CHECK_TIMEOUT_S):
    """Download and validate the manifest of the latest release. Raise ``UpdateError``."""
    feed = feed or feedUrl()
    if not feed:
        raise UpdateError(_("Update checks are turned off ({variable}=off).").format(variable=FEED_ENVIRONMENT_VARIABLE))
    deadline = time.monotonic() + CHECK_DEADLINE_S
    with _open(feed, feed, timeout) as response:
        length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
        if length and length.isdigit() and int(length) > MANIFEST_MAX_BYTES:
            raise UpdateError(_("The update manifest is too large."))
        # read1() returns what one read gives, so the total deadline also holds for a server that sends its answer
        # slowly (the network timeout applies to each read only)
        read = getattr(response, "read1", None) or response.read
        blocks, size = [], 0
        while True:
            try:
                block = read(16 * 1024)
            except OSError as error:
                raise UpdateError(_("Cannot reach the update server: {reason}").format(reason=error))
            except http.client.HTTPException:  # not an OSError: a chunked or other reply that is cut off or malformed
                raise UpdateError(_("Cannot reach the update server: the connection was interrupted."))
            if not block:
                break
            blocks.append(block)
            size += len(block)
            if size > MANIFEST_MAX_BYTES:
                raise UpdateError(_("The update manifest is too large."))
            if time.monotonic() > deadline:
                raise UpdateError(_("The update server answers too slowly."))
    return parseManifest(b"".join(blocks))


class Status:
    """What a manifest means for this version: ``kind`` is "none", "optional" or "required"."""

    def __init__(self, manifest, currentVersion, skippedVersion=""):
        self.manifest = manifest
        self.currentVersion = str(currentVersion)
        self.newer = manifest is not None and versionModule.isNewer(manifest.version, currentVersion)
        self.belowMinimum = bool(manifest is not None and manifest.minimumSupported
                                 and versionModule.compare(currentVersion, manifest.minimumSupported) < 0)
        if not self.newer:
            # Nothing newer to install (also when a manifest names a minimum above its own version)
            self.kind = "none"
        elif manifest.required or self.belowMinimum:
            self.kind = "required"
        else:
            self.kind = "optional"
        self.skipped = self.kind == "optional" and bool(skippedVersion) and skippedVersion == manifest.version

    @property
    def available(self):
        return self.kind != "none"

    @property
    def version(self):
        return self.manifest.version if self.manifest is not None else None

    def __repr__(self):
        return f"Status({self.kind!r}, {self.version!r}, skipped={self.skipped})"


def evaluate(manifest, currentVersion=None, skippedVersion=""):
    """The ``Status`` of ``manifest`` for ``currentVersion`` (default: this Slicer Studio)."""
    return Status(manifest, currentVersion or versionModule.STUDIO_VERSION, skippedVersion)


#
# Download and verification
#


def assetUrl(manifest, asset, feed=None):
    """Address of a release file: the tag of the release on GitHub, or next to an overridden feed (tests)."""
    feed = feed or feedUrl()
    name = urllib.parse.quote(asset["name"])
    if feed and feed != DEFAULT_FEED_URL:
        return urllib.parse.urljoin(feed, name)
    return RELEASE_DOWNLOAD_URL.format(repository=REPOSITORY, tag=manifest.tag, name=name)


def fileSha256(path, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def verifyFile(path, size, sha256):
    """Raise ``UpdateError`` unless the file has the size and SHA-256 of the manifest."""
    actual = os.path.getsize(path)
    if actual != size:
        raise UpdateError(_("The download has the wrong size ({actual} bytes instead of {expected}): it was refused.").format(actual=actual, expected=size))
    digest = fileSha256(path)
    if digest != sha256.lower():
        raise UpdateError(_("The download does not have the SHA-256 checksum of the release: it was refused."))


class _BodyReader:
    """Reads the body of ``response`` in a helper thread, so that the download can look at Cancel while the server is
    slow or silent (a read of the connection cannot be interrupted, and one that is cut by a timeout cannot be resumed).
    ``get(timeout)`` returns the next block (``b""`` at the end), or None when nothing came within ``timeout``, or raises
    the error of the connection. ``stop()`` ends the thread: it closes the connection for reading, which wakes a read
    that waits.
    """

    def __init__(self, response, size):
        import queue

        self.response = response
        self.size = size
        self.blocks = queue.Queue(maxsize=4)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="StudioDownload", daemon=True)
        self.thread.start()

    def _put(self, item):
        import queue

        while not self.stopped.is_set():
            try:
                self.blocks.put(item, timeout=0.2)
                return
            except queue.Full:
                pass

    def _run(self):
        read = getattr(self.response, "read1", self.response.read)  # what has arrived: not a wait for a full block
        try:
            while not self.stopped.is_set():
                block = read(self.size)
                self._put(("data", block))
                if not block:
                    return
        except Exception as error:
            self._put(("error", error))

    def get(self, timeout):
        import queue

        try:
            kind, value = self.blocks.get(timeout=timeout)
        except queue.Empty:
            return None
        if kind == "error":
            raise value
        return value

    def stop(self):
        import socket

        self.stopped.set()
        if self.thread.is_alive():
            try:
                self.response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
            self.thread.join(2)


def downloadAsset(manifest, key, directory, feed=None, progress=None, cancelled=None):
    """Download the release file of the platform ``key`` to ``directory`` and verify it. Return its path.

    ``progress(received, total)`` is called from this thread; ``cancelled()`` returning True stops the download.
    The file is written as ``<name>.part`` and renamed only after its size and SHA-256 matched the manifest; a file
    that does not match is deleted. Raise ``UpdateError``.
    """
    asset = manifest.asset(key)
    if asset is None:
        raise UpdateError(_("This release has no file for this computer ({platform}).").format(platform=key or _("unknown")))
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, asset["name"])
    part = path + ".part"
    size = asset["size"]
    if os.path.exists(path):
        try:
            verifyFile(path, size, asset["sha256"])
            if progress:
                progress(size, size)
            return path
        except (UpdateError, OSError):
            os.remove(path)
    feed = feed or feedUrl()
    url = assetUrl(manifest, asset, feed)
    digest = hashlib.sha256()
    received = 0
    try:
        missing = _("The release {version} has no downloadable file {name} yet (404: {address}).").format(
            version=manifest.version, name=asset["name"], address=url)
        with _open(url, feed, DOWNLOAD_TIMEOUT_S, notFound=missing) as response, open(part, "wb") as stream:
            length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
            if length and length.isdigit() and int(length) != size:
                raise UpdateError(_("The download has the wrong size ({actual} bytes instead of {expected}): it was refused.").format(actual=int(length), expected=size))
            if progress:
                progress(0, size)
            reader = _BodyReader(response, DOWNLOAD_CHUNK)
            try:
                while True:
                    if cancelled is not None and cancelled():
                        raise UpdateError(_("The download was cancelled."))
                    block = reader.get(DOWNLOAD_POLL_S)
                    if block is None:
                        continue  # nothing arrived yet: look at Cancel again (the connection gives up after DOWNLOAD_TIMEOUT_S)
                    if not block:
                        break
                    received += len(block)
                    if received > size:
                        raise UpdateError(_("The download is larger than the release file ({expected} bytes): it was refused.").format(expected=size))
                    digest.update(block)
                    stream.write(block)
                    if progress:
                        progress(received, size)
            finally:
                reader.stop()
        if received != size:
            raise UpdateError(_("The download has the wrong size ({actual} bytes instead of {expected}): it was refused.").format(actual=received, expected=size))
        if digest.hexdigest() != asset["sha256"]:
            raise UpdateError(_("The download does not have the SHA-256 checksum of the release: it was refused."))
        os.replace(part, path)
    except UpdateError:
        _removeQuietly(part)
        raise
    except OSError as error:
        _removeQuietly(part)
        raise UpdateError(_("The download failed: {error}").format(error=error))
    except http.client.HTTPException:  # not an OSError: a chunked reply that is cut off or malformed (the network dropped)
        _removeQuietly(part)
        raise UpdateError(_("The download failed: the connection was interrupted."))
    return path


#: Written when an update is prepared: which run staged it (the staging folder is removed by the next start if that run died)
STAGED_MARKER = "staged.json"


def _processAlive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, but belongs to another user
    return True


def _removeQuietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def formatSize(numberOfBytes):
    if numberOfBytes >= 1 << 30:
        return f"{numberOfBytes / (1 << 30):.1f} GB"
    if numberOfBytes >= 1 << 20:
        return f"{numberOfBytes / (1 << 20):.0f} MB"
    return f"{max(1, numberOfBytes >> 10)} KB"


#
# Settings (fork-only keys Studio/Updates/*)
#


def settingsValue(name):
    import qt

    value = qt.QSettings().value(f"{SETTINGS_GROUP}/{name}")
    default = DEFAULT_SETTINGS.get(name)
    if value is None:
        return default
    if isinstance(default, bool):
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    return str(value)


def setSettingsValue(name, value):
    import qt

    qt.QSettings().setValue(f"{SETTINGS_GROUP}/{name}", value)


def cachedManifest():
    """The manifest of the last successful check (``Studio/Updates/LastManifest``), or None."""
    text = settingsValue("LastManifest")
    if not text:
        return None
    try:
        return parseManifest(text)
    except UpdateError:
        return None


def stateDirectory():
    """Private folder of the update state (install record, helper log, downloads)."""
    import qt

    if stateDirectoryOverride:
        return stateDirectoryOverride
    base = qt.QStandardPaths.writableLocation(qt.QStandardPaths.AppLocalDataLocation)
    return os.path.join(base, "StudioUpdate")


def downloadDirectory():
    return os.path.join(stateDirectory(), "downloads")


#
# Service
#

_service = None
#: Tests: True lets a development build install (into a temporary folder given by ``installTargetOverride``)
_installableOverride = None
installTargetOverride = None
#: Tests: replaces ``versionModule.STUDIO_VERSION`` as the running version
currentVersionOverride = None
#: Tests: replaces the private state folder (install record, downloads)
stateDirectoryOverride = None


def instance():
    """The update service, or None (testing, no main window, scripted run)."""
    return _service


def currentVersion():
    return currentVersionOverride or versionModule.STUDIO_VERSION


def channel():
    """"release" for an installed package, "development" for a build tree or a source checkout."""
    import slicer

    installed = getattr(slicer.app, "isInstalled", False)
    installed = installed() if callable(installed) else installed
    return "release" if installed else "development"


def isDisabledByEnvironment():
    """True when no update UI and no check may run in this process (SPEC 9.1)."""
    import slicer

    if slicer.app.testingEnabled():
        return True
    options = slicer.app.commandOptions()
    if options.noMainWindow or options.exitAfterStartup or getattr(options, "runPythonAndExit", False):
        return True
    return slicer.util.mainWindow() is None


def onStartupCompleted(then=None):
    """Start the update service (called by the Studio module). ``then()`` is called once, when the startup may go on:
    at once without an automatic check, or when the check has finished (at most ``CHECK_TIMEOUT_S`` seconds), unless a
    required update is shown (the application then quits to update, or quits).
    """
    global _service
    if _service is not None or isDisabledByEnvironment():
        if then is not None:
            then()
        return _service
    _service = UpdateService()
    _service.start(then)
    return _service


def _shell():
    shellModule = sys.modules.get("StudioLib.shell")
    return shellModule.instance() if shellModule is not None else None


def _isAlive(qobject):
    try:
        qobject.objectName
    except Exception:
        return False
    return True


def _mainWindow():
    import slicer

    return slicer.util.mainWindow()


class _Task:
    """A function run in a worker thread; ``poll()`` on the main thread calls ``onDone(result, error)`` once."""

    def __init__(self, function, onDone, timeoutS=None, onTimeout=None):
        import qt

        self.function = function
        self.onDone = onDone
        self.onTimeout = onTimeout
        self.result = None
        self.error = None
        self.finished = False
        self.reported = False
        self.timedOut = False
        self.cancelEvent = threading.Event()
        self.progress = (0, 0)
        self.started = time.monotonic()
        self.timeoutS = timeoutS
        self.timer = qt.QTimer()
        self.timer.setInterval(50)
        self.timer.connect("timeout()", self.poll)
        self.thread = threading.Thread(target=self._run, name="StudioUpdate", daemon=True)

    def start(self):
        self.thread.start()
        self.timer.start()
        return self

    def _run(self):
        try:
            self.result = self.function(self)
        except UpdateError as error:
            self.error = error
        except Exception as error:
            if isinstance(error, updates_install.InstallError):
                self.error = error
            else:
                logging.error("Updates: " + traceback.format_exc())
                self.error = UpdateError(str(error))
        self.finished = True

    def cancel(self):
        self.cancelEvent.set()

    def cancelled(self):
        return self.cancelEvent.is_set()

    def setProgress(self, received, total):
        self.progress = (received, total)

    def poll(self):
        if self.finished and not self.reported:
            self.reported = True
            self.timer.stop()
            if not self.timedOut:
                self.onDone(self.result, self.error)
            elif self.onTimeout is not None:
                # The answer came after the startup went on: reported as a late result
                self.onTimeout(self.result, self.error, late=True)
            return
        if not self.timedOut and self.timeoutS is not None and time.monotonic() - self.started > self.timeoutS:
            self.timedOut = True
            if self.onTimeout is not None:
                self.onTimeout(None, UpdateError(_("The update server did not answer within {seconds} s.").format(seconds=self.timeoutS)), late=False)


class UpdateService:
    """Checks, the startup decision, the notice, the dialogs, the menu and Search entries."""

    def __init__(self, force=False):
        self.force = force
        self.status = None
        self.lastError = None
        self.checking = False
        self.job = None
        self.prepared = None
        self.requiredDialog = None
        self.updateDialog = None
        self.notice = None
        self.noticeVersion = None
        self.noticeWhere = None
        self.banner = None
        self.helpAction = None
        self._then = None
        self._checkTask = None
        self._checkCallbacks = []
        #: True from the startup check until the startup decision is made: the startup shows the result itself
        self._startupPending = False
        self._listeners = []
        self._noticeRetries = 0
        self._noticeTimer = None
        self._recheckTimer = None
        self._tourHoldTimer = None
        self._tourHeld = None
        self._quitConnected = False
        self._discardConnected = False
        self._relaunch = True
        #: Staged copies of dropped updates, removed by the next download (``_discardPrepared()``)
        self._staleStaging = []
        self._finishedMessage = None
        self._startTime = time.monotonic()
        #: How long the startup waited for the check before going on (ms; None until then, or when a required
        #: update is shown)
        self.startupWaitMs = None
        self._paletteRegistered = False
        manifest = cachedManifest()
        if manifest is not None:
            self.status = evaluate(manifest, currentVersion(), settingsValue("SkippedVersion"))

    #
    # Life cycle
    #

    def start(self, then=None):
        """Finish an update installed by the previous run, then check (or not) and decide."""
        import qt

        self._then = then
        try:
            self.finishInstall()
        except Exception:
            logging.error("Updates: finishing the last update failed:\n" + traceback.format_exc())
        qt.QTimer.singleShot(1200, self.installUserInterface)
        if self.automaticChecksEnabled():
            self._holdTourOffer()
            self._startupCheck(STARTUP_WAIT_S)
        else:
            # No check: a cached required release is shown with Update now (only a failed check means offline)
            self._continueStartup()
        self._recheckTimer = qt.QTimer()
        self._recheckTimer.setInterval(3600 * 1000)
        self._recheckTimer.connect("timeout()", self._maybeRecheck)
        self._recheckTimer.start()

    def dispose(self):
        """Remove the user interface (tests)."""
        global _service
        for timer in (self._noticeTimer, self._recheckTimer, self._tourHoldTimer):
            if timer is not None:
                timer.stop()
        if self._checkTask is not None:
            self._checkTask.timer.stop()
        if self.job is not None:
            self.job.cancel()
            self.job.timer.stop()
        self.hideNotice()
        self.hideBanner()
        for dialog in (self.requiredDialog, self.updateDialog):
            if dialog is not None:
                dialog.dispose()
        self.requiredDialog = self.updateDialog = None
        if self.helpAction is not None and _isAlive(self.helpAction):
            self.helpAction.setObjectName("")
            menu = _mainWindow().findChild("QMenu", "HelpMenu") if _mainWindow() is not None else None
            if menu is not None:
                menu.removeAction(self.helpAction)
            self.helpAction.deleteLater()
        self.helpAction = None
        if self._paletteRegistered:
            try:
                from StudioLib import palette

                palette.unregisterProvider(self.paletteItems)
            except Exception:
                pass
        self._disconnectQuit()
        if _service is self:
            _service = None

    def installUserInterface(self):
        """The Updates settings panel, the Help menu item (Classic) and the Search entries."""
        try:
            from StudioLib import updates_settings

            updates_settings.install(self)
            self._installHelpAction()
            self._registerPaletteProvider()
        except Exception:
            logging.error("Updates: installing the user interface failed:\n" + traceback.format_exc())

    def _installHelpAction(self):
        import qt

        mainWindow = _mainWindow()
        menu = mainWindow.findChild("QMenu", "HelpMenu") if mainWindow is not None else None
        if menu is None or self.helpAction is not None:
            return
        self.helpAction = qt.QAction(_("Check for Updates…"), mainWindow)
        self.helpAction.setObjectName(HELP_ACTION_OBJECT_NAME)
        self.helpAction.connect("triggered()", lambda: self.showUpdates(check=True))
        menu.addSeparator()
        menu.addAction(self.helpAction)
        # Studio has ≡ > Check for updates…; the Help item is for Classic (the Help menu is shared)
        menu.connect("aboutToShow()", self._updateHelpAction)

    def _updateHelpAction(self):
        if self.helpAction is not None and _isAlive(self.helpAction):
            self.helpAction.setVisible(_shell() is None)

    def _registerPaletteProvider(self):
        import importlib.util

        try:
            if importlib.util.find_spec("StudioLib.palette") is None:
                return
            from StudioLib import palette
        except Exception:
            return
        if hasattr(palette, "registerProvider"):
            palette.registerProvider(self.paletteItems)
            self._paletteRegistered = True

    def paletteItems(self, shell=None):
        status = self.status
        available = status is not None and status.available
        items = [
            {"id": "updates.check", "title": _("Check for updates"), "category": "Help", "callback": lambda: self.showUpdates(check=True),
             "synonyms": ["update", "updates", "upgrade", "new version", "release", "version"],
             "description": _("Slicer Studio {version}").format(version=currentVersion())},
            {"id": "updates.install", "title": _("Update now"), "category": "Help", "callback": self.updateNow,
             "synonyms": ["update", "upgrade", "install update", "new version"],
             "enabled": lambda: self.status is not None and self.status.available,
             "description": (_("Install Slicer Studio {version}").format(version=status.version) if available else _("No update available"))},
        ]
        return items

    #
    # Listeners (settings panel)
    #

    def addListener(self, callback):
        if callback not in self._listeners:
            self._listeners.append(callback)

    def removeListener(self, callback):
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self):
        for callback in list(self._listeners):
            try:
                callback(self)
            except Exception:
                logging.error("Updates: listener failed:\n" + traceback.format_exc())

    #
    # Checks
    #

    def automaticChecksEnabled(self):
        """Automatic checks: the AutoCheck setting, a feed, and a release installation (or a feed set for tests)."""
        if feedUrl() is None or not settingsValue("AutoCheck"):
            return False
        return channel() == "release" or feedOverride() is not None

    def canInstall(self):
        """True if this installation can install an update itself (a release package on macOS or Linux)."""
        if _installableOverride is not None:
            return bool(_installableOverride)
        return channel() == "release" and platformKey() in ("macos-arm64", "macos-x86_64", "linux-x86_64")

    def releaseHasFile(self, status=None):
        """True if the release of ``status`` (default: the current status) has a file for this computer."""
        status = status if status is not None else self.status
        return bool(status is not None and status.manifest is not None and status.manifest.asset(platformKey()) is not None)

    def canInstallRelease(self, status=None):
        """True if this installation can install the release of ``status`` now (it can update itself, and the release
        has its file). A required release that cannot be installed here never blocks: it shows a warning.
        """
        return self.canInstall() and self.releaseHasFile(status)

    def installTarget(self):
        import slicer

        if installTargetOverride:
            return installTargetOverride
        return updates_install.installTarget(platformKey(), slicer.app.slicerHome)

    def check(self, onDone=None, timeoutS=None):
        """Check the feed in a worker thread; ``onDone(status, error)`` on the main thread. Return False if turned off."""
        if onDone is not None:
            self._checkCallbacks.append(onDone)
        if self.checking:
            return True
        feed = feedUrl()
        if feed is None:
            self.lastError = UpdateError(_("Update checks are turned off ({variable}=off).").format(variable=FEED_ENVIRONMENT_VARIABLE))
            self._reportCheck(None, self.lastError)
            return False
        self.checking = True
        self._notify()
        self._checkTask = _Task(lambda task: fetchManifest(feed), self._onChecked, timeoutS=timeoutS, onTimeout=self._onCheckTimeout).start()
        return True

    def _startupCheck(self, timeoutS):
        """The check of the startup: its result is shown by the startup decision (_onStartupCheck), not by _onChecked."""
        self._startupPending = True
        return self.check(onDone=self._onStartupCheck, timeoutS=timeoutS)

    def checkNow(self):
        """Check now (Settings, Search, menus), whatever the channel and the AutoCheck setting."""
        return self.check()

    def _onChecked(self, manifest, error):
        startup = self._startupPending  # the startup decision shows the result of the startup check itself
        self.checking = False
        self._checkTask = None
        if error is None:
            setSettingsValue("LastManifest", manifest.toJson())
            setSettingsValue("LastCheck", time.strftime("%Y-%m-%dT%H:%M:%S"))
            self.status = evaluate(manifest, currentVersion(), settingsValue("SkippedVersion"))
            self.lastError = None
        else:
            logging.info(f"Updates: check failed: {error}")
            self.lastError = error
        self._reportCheck(self.status if error is None else None, error)
        if error is None and not startup:
            # A check after the startup (Check again on the banner, Settings, Search, the daily check, a late answer):
            # bring the banner and the notice up to date, for example "Connect to the internet and check again"
            # becomes "is required (you have X)" with Update now, and the banner of a release that is no longer
            # required goes
            self._showAvailability(startup=False)

    def _onCheckTimeout(self, manifest, error, late):
        if not late:
            # The startup goes on; the answer, if any, is handled as a late result
            callbacks, self._checkCallbacks = self._checkCallbacks, []
            for callback in callbacks:
                callback(None, error)
            return
        self._onChecked(manifest, error)

    def _reportCheck(self, status, error):
        callbacks, self._checkCallbacks = self._checkCallbacks, []
        for callback in callbacks:
            try:
                callback(status, error)
            except Exception:
                logging.error("Updates: " + traceback.format_exc())
        self._notify()

    def _maybeRecheck(self):
        if not self.automaticChecksEnabled() or self.checking:
            return
        last = settingsValue("LastCheck")
        try:
            lastTime = time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%S")) if last else 0
        except ValueError:
            lastTime = 0
        if time.time() - lastTime >= RECHECK_INTERVAL_S:
            self.check()

    #
    # Startup decision
    #

    def _onStartupCheck(self, status, error):
        self._startupPending = False
        if error is None and status is not None and status.kind == "required" and self.canInstallRelease(status):
            self.showRequiredDialog()
            return
        self._continueStartup(offline=error is not None)

    def _holdTourOffer(self):
        """Keep the first-start tour offer back while the startup check runs, so that a required update dialog is never
        shown over it. The offer is scheduled again when the startup goes on.
        """
        import qt

        self._tourHoldTimer = qt.QTimer()
        self._tourHoldTimer.setInterval(100)
        self._tourHoldTimer.connect("timeout()", self._tourHoldTick)
        self._tourHoldTick()
        self._tourHoldTimer.start()

    def _tourHoldTick(self):
        try:
            from StudioLib import tour

            live = tour.plugin()
            offerTimer = getattr(live, "_offerTimer", None) if live is not None else None
            if offerTimer is not None and offerTimer.isActive() and not tour.isOffered():
                offerTimer.stop()
                self._tourHeld = live
        except Exception:
            pass

    def _releaseTourOffer(self, offer=True):
        timer, self._tourHoldTimer = self._tourHoldTimer, None
        if timer is not None:
            timer.stop()
        live, self._tourHeld = self._tourHeld, None
        if not offer or live is None:
            return
        try:
            from StudioLib import tour

            if tour.plugin() is live and not tour.isOffered() and hasattr(live, "scheduleOffer"):
                live.scheduleOffer()
        except Exception:
            logging.error("Updates: " + traceback.format_exc())

    def _continueStartup(self, offline=False):
        """Run the rest of the startup (autosave and its recovery offer) and show what the check found."""
        self._releaseTourOffer()
        then, self._then = self._then, None
        if self.startupWaitMs is None:
            self.startupWaitMs = (time.monotonic() - self._startTime) * 1000.0
        if then is not None:
            try:
                then()
            except Exception:
                logging.error("Updates: " + traceback.format_exc())
        self._showAvailability(startup=True, offline=offline)

    def _showAvailability(self, startup=False, offline=False):
        status = self.status
        if status is None or not status.available:
            self.hideNotice()
            self.hideBanner()
            return
        if channel() != "release" and feedOverride() is None and _installableOverride is None:
            # A development build shows what is available in Settings > Updates only
            return
        if status.kind == "required":
            # Not installable here (development build, web version, no file for this computer), offline (cached
            # manifest), checks off (cached manifest) or found after the startup
            self.hideNotice()
            self.showBanner(offline=offline)
        elif not status.skipped and not self.installChosen(status.version):
            if self.notice is not None and self.noticeVersion != status.version:
                # A later check found another version: the notice names the version that Update now and Skip act on
                self.hideNotice()
            self.scheduleNotice()
        else:
            self.hideNotice()

    #
    # Optional update: the notice
    #

    def scheduleNotice(self):
        import qt

        self._noticeRetries = 0
        if self._noticeTimer is None:
            self._noticeTimer = qt.QTimer()
            self._noticeTimer.setSingleShot(True)
            self._noticeTimer.connect("timeout()", self._tryNotice)
        elapsedMs = (time.monotonic() - self._startTime) * 1000.0
        self._noticeTimer.start(int(max(0, NOTICE_DELAY_MS - elapsedMs)))

    def studioEntered(self):
        """Studio was applied in the middle of the session (from Classic): announce an optional update that is waiting.

        The notice is scheduled once at startup and gives up when there is no Studio shell then (a start in Classic),
        so the switch to Studio schedules it again, under the same conditions as ``_showAvailability()``.
        """
        status = self.status
        if status is None or not status.available or status.kind != "optional" or status.skipped or self.notice is not None:
            return
        if self.installChosen(status.version):
            return
        if channel() != "release" and feedOverride() is None and _installableOverride is None:
            return
        self.scheduleNotice()

    def _otherPromptShown(self):
        import slicer

        if slicer.app.activeModalWidget() is not None or slicer.app.activePopupWidget() is not None:
            return True
        try:
            from StudioLib import tour

            if tour.activeTour() is not None:
                return True
            live = tour.plugin()
            offerTimer = getattr(live, "_offerTimer", None) if live is not None else None
            if (live is not None and not tour.isOffered() and live.offerWidget() is None and offerTimer is not None
                    and offerTimer.isActive()):
                # The first-start tour offer is about to be shown: it comes first
                return True
        except Exception:
            pass
        return False

    def _tryNotice(self):
        shell = _shell()
        status = self.status
        if shell is None or status is None or status.kind != "optional" or status.skipped or self.notice is not None:
            return
        if self.installChosen(status.version):
            # Its install is already chosen (Install when I quit): Skip could not take it back, so there is nothing
            # to offer
            return
        if self.canInstall() and not self.releaseHasFile(status):
            # The release has no file for this computer yet: Update now could not work (Settings > Updates shows why)
            return
        if self._otherPromptShown() or not shell.mainWindow.isVisible() or self.showNotice() is None:
            # Another prompt is shown (or the inspector notice slot is taken): try again later
            self._noticeRetries += 1
            if self._noticeRetries < NOTICE_MAX_RETRIES:
                self._noticeTimer.start(NOTICE_RETRY_MS)

    def showNotice(self):
        """Show "Update X available · Update now · Skip" (Library canvas, else the inspector notice slot). Studio only."""
        from StudioLib import ui

        shell = _shell()
        status = self.status
        if shell is None or status is None or not status.available:
            return None
        self.hideNotice()
        widget = ui.noticeLine(_("Update {version} available").format(version=status.version),
                               [(_("Update now"), self.updateNow), (_("Skip"), self.skip)])
        widget.setObjectName(NOTICE_OBJECT_NAME)
        widget.setToolTip(_("Slicer Studio {version} is available (you have {current}). Settings > Updates has the release notes.").format(
            version=status.version, current=currentVersion()))
        where = self._placeNotice(shell, widget, NOTICE_KEY)
        if where is None:
            widget.deleteLater()
            return None
        self.notice, self.noticeWhere, self.noticeVersion = widget, where, status.version
        return widget

    def _placeNotice(self, shell, widget, key):
        if shell.currentWorkspace() == "library" and shell.canvas() is not None:
            try:
                from StudioLib import library

                library.showNotice(key, widget)
                return "library"
            except Exception:
                logging.error("Updates: " + traceback.format_exc())
        current = shell.inspectorNotice()
        if current is not None and _isAlive(current) and shell.inspectorNoticeKey() != key:
            # The slot holds one notice: never replace another one
            return None
        shell.setInspectorNotice(widget, key)
        return "inspector"

    def hideNotice(self):
        widget, where = self.notice, self.noticeWhere
        self.notice = self.noticeWhere = self.noticeVersion = None
        if widget is None:
            return
        self._removePlaced(widget, where, NOTICE_KEY)

    def _removePlaced(self, widget, where, key):
        shell = _shell()
        if where == "library":
            try:
                from StudioLib import library

                library.closeNotice(key)
            except Exception:
                pass
        elif where == "inspector" and shell is not None:
            shell.clearInspectorNotice(key)
        if _isAlive(widget):
            widget.hide()
            widget.deleteLater()

    def installChosen(self, version=None):
        """True when the prepared update (of ``version``, if given) is set to install: Install when I quit, a restart
        that waits for the quit, or its install helper already runs (administrator rights given).
        """
        prepared = self.prepared
        if prepared is None or not (self._quitConnected or prepared.launched):
            return False
        return version is None or prepared.version == version

    def skip(self):
        """Skip of the notice: this version is not offered again (Settings > Updates still shows it)."""
        if self.status is not None and self.status.version:
            setSettingsValue("SkippedVersion", self.status.version)
            self.status.skipped = True
        self.hideNotice()
        shell = _shell()
        if shell is not None:
            shell.toast(_("Update skipped · Settings > Updates installs it later"), 3000)
        self._notify()

    #
    # Required update that cannot be installed now: a non-blocking warning
    #

    def showBanner(self, offline=False):
        import qt

        status = self.status
        if status is None or status.kind != "required":
            return None
        self.hideBanner()
        key = platformKey()
        installNow = False
        if key == "web":
            text = _("Slicer Studio {version} is required. Update it with the web launcher (slicer_studio_web.py update).")
        elif not self.canInstall():
            if channel() != "release":
                text = _("Release {version} is marked as required (this development build is not updated).")
            else:
                text = _("Slicer Studio {version} is required. There is no package of it for this computer: see {url}.")
        elif not self.releaseHasFile(status):
            # Short: Details (Settings > Updates) and the Classic message add where to download it
            text = _("Slicer Studio {version} is required, but its release has no file for this computer yet.")
        elif offline:
            text = _("Slicer Studio {version} is required. Connect to the internet and check again to update.")
        else:
            # Found after the startup, or cached while automatic checks are off: it can be installed now
            text = _("Slicer Studio {version} is required (you have {current}).")
            installNow = True
        text = text.format(version=status.version, current=status.currentVersion, platform=key or _("unknown"), url=RELEASES_URL)
        details = (_("Details"), lambda: self.showUpdates(check=False))
        shell = _shell()
        if shell is not None:
            from StudioLib import ui

            actions = [(_("Update now"), self.updateNow), details] if installNow else [(_("Check again"), lambda: self.showUpdates(check=True)), details]
            widget = ui.noticeLine(text, actions)
            widget.setObjectName("StudioRequiredUpdateBanner")
            where = self._placeNotice(shell, widget, NOTICE_KEY + "-required")
            if where is None:
                widget.deleteLater()
                return None
            self.banner = (widget, where)
            return widget
        # Classic: a non-modal message, once
        box = qt.QMessageBox(_mainWindow())
        box.setObjectName("StudioRequiredUpdateBanner")
        box.setIcon(qt.QMessageBox.Warning)
        box.setWindowTitle(_("Update required"))
        box.setText(text)
        if self.canInstall() and not self.releaseHasFile(status):
            box.setInformativeText(self.unavailableReason() or "")
        box.setStandardButtons(qt.QMessageBox.Ok)
        if installNow:
            updateButton = box.addButton(_("Update now"), qt.QMessageBox.AcceptRole)
            updateButton.setObjectName("StudioRequiredUpdateBannerUpdateButton")
            updateButton.connect("clicked()", self.updateNow)
        box.setModal(False)
        box.show()
        self.banner = (box, "window")
        return box

    def hideBanner(self):
        banner, self.banner = self.banner, None
        if banner is None:
            return
        widget, where = banner
        if where == "window":
            if _isAlive(widget):
                widget.close()
                widget.deleteLater()
            return
        self._removePlaced(widget, where, NOTICE_KEY + "-required")

    #
    # Required update: the blocking dialog
    #

    def showRequiredDialog(self):
        """The blocking dialog (Update now · Quit). Shown once the main window is up and no other modal is open."""
        import qt
        import slicer

        mainWindow = _mainWindow()
        if mainWindow is None:
            return None
        if not mainWindow.isVisible() or slicer.app.activeModalWidget() is not None:
            qt.QTimer.singleShot(250, self.showRequiredDialog)
            return None
        if self.requiredDialog is None:
            self.requiredDialog = RequiredUpdateDialog(self, mainWindow)
        self.requiredDialog.show()
        # No first-start tour offer in this run: the application quits to update (or quits)
        self._releaseTourOffer(offer=False)
        return self.requiredDialog

    #
    # Update now
    #

    def updateNow(self):
        """Update now (notice, Settings, Search, menu): download, verify, prepare, then restart (asked first)."""
        self.hideNotice()
        status = self.status
        if status is None or not status.available:
            self.showUpdates(check=True)
            return None
        # A later check found another version than the one prepared: Update now acts on the version it names
        stale = self.prepared is not None and self.prepared.version != status.version
        if stale and self._discardPrepared():
            stale = False
        if self.updateDialog is None or not _isAlive(self.updateDialog.dialog):
            self.updateDialog = UpdateDialog(self, _mainWindow())
        self.updateDialog.show()
        if self.job is None and self.prepared is None:
            self.updateDialog.begin()
        elif stale and self.job is None:
            # Its install helper already runs (administrator rights given): it cannot be taken back, so say so
            self.updateDialog.preparedInstead(self.prepared.version, status.version)
        return self.updateDialog

    def _discardPrepared(self):
        """Drop the prepared update (and its Install when I quit) so that another version can be downloaded. Return
        False if it cannot be dropped: its install helper already runs (administrator rights given).
        """
        import slicer

        prepared = self.prepared
        if prepared is None:
            return True
        if prepared.launched:
            return False
        self.prepared = None
        if self._quitConnected:
            try:
                slicer.app.disconnect("aboutToQuit()", self._onAboutToQuit)
            except Exception:
                pass
            self._quitConnected = False
        self._relaunch = True
        if self.updateDialog is not None:
            # Its title and its ready state name the dropped version
            self.updateDialog.dispose()
            self.updateDialog = None
        # The staged copy is removed by the next download before it prepares (same folder), else by the next start
        self._staleStaging.append(os.path.dirname(prepared.staged))
        package = prepared.package
        if package and os.path.isfile(package) and os.path.dirname(os.path.abspath(package)) == os.path.abspath(downloadDirectory()):
            _removeQuietly(package)
        self._notify()
        return True

    def unavailableReason(self):
        """Why this installation cannot update itself (shown with the manual way), or None."""
        key = platformKey()
        if key == "web":
            return _("The web version is updated by its launcher: on the computer that runs it, run "
                     "'python3 slicer_studio_web.py update' in the Utilities/SlicerStudioWeb folder, then start it again.")
        if self.canInstall():
            if self.status is not None and self.status.manifest is not None and not self.releaseHasFile():
                return _("Slicer Studio {version} has no file for this computer ({platform}) yet. Download it from {url} when it "
                         "is available.").format(version=self.status.version, platform=key or _("unknown"), url=RELEASES_URL)
            return None
        if channel() != "release":
            return _("This is a development build: it is not updated. Install a release package to get updates.")
        return _("There is no package of Slicer Studio for this computer. Download it from {url}.").format(url=RELEASES_URL)

    def startJob(self, onUpdate):
        """Download, verify and prepare the update of this platform in a worker thread. Return the task."""
        status = self.status
        manifest = status.manifest
        key = platformKey()
        target = self.installTarget()
        feed = feedUrl()
        directory = downloadDirectory()
        if target is None and key in ("macos-arm64", "macos-x86_64", "linux-x86_64"):
            raise updates_install.InstallError(_("The installed application was not found."))
        unavailable = _("This computer cannot install the update.")
        stale, self._staleStaging = self._staleStaging, []
        # From now on this run owns the download and staging folders: another Slicer Studio that starts with the same
        # state folder must not clean them up (the marker names this run until it ends or its update is registered)
        self._writeStagedMarker(None)

        def run(task):
            for root in stale:
                # The staged copy of a dropped update (a later check found another version)
                if os.path.isdir(root) and os.path.basename(root).endswith(updates_install.STAGING_SUFFIX):
                    updates_install._removeTree(root)
            task.phase = "download"
            path = downloadAsset(manifest, key, directory, feed=feed, progress=task.setProgress, cancelled=task.cancelled)
            if task.cancelled():
                raise UpdateError(_("The download was cancelled."))
            task.phase = "prepare"
            if key == "linux-x86_64":
                return updates_install.prepareLinux(path, target, manifest.version)
            if key in ("macos-arm64", "macos-x86_64"):
                return updates_install.prepareMacOS(path, target, manifest.version, stagingParent=None)
            raise UpdateError(unavailable)

        def done(prepared, error):
            self.job = None
            if error is None:
                self.prepared = prepared
                self._registerStaged(prepared)
            else:
                self._removeOwnStagedMarker()
            onUpdate(prepared, error)
            self._notify()

        self.job = _Task(run, done)
        self.job.phase = "download"
        return self.job.start()

    def installOnQuit(self, relaunch=True):
        """Swap in the prepared update when the application exits (and relaunch it). With administrator rights the
        helper starts now (the password is asked now). Raise ``InstallError``.
        """
        import slicer

        prepared = self.prepared
        if prepared is None:
            return False
        self._relaunch = relaunch
        if prepared.needsAdmin and not prepared.launched:
            prepared.launch(stateDirectory(), relaunch=relaunch, record=self._installRecord())
        elif not self._quitConnected:
            slicer.app.connect("aboutToQuit()", self._onAboutToQuit)
            self._quitConnected = True
        if self.notice is not None and self.noticeVersion == prepared.version:
            # A notice placed meanwhile (another check) would offer Skip of a version that installs anyway
            self.hideNotice()
        return True

    #
    # A prepared update that is not installed
    #

    def _writeStagedMarker(self, root):
        """Write the marker that names this run as the owner of the download and the staged copy (``root``, or None
        while the update is still downloading or being prepared).
        """
        try:
            os.makedirs(stateDirectory(), exist_ok=True)
            with open(os.path.join(stateDirectory(), STAGED_MARKER), "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "root": root}, f)
        except OSError:
            logging.warning("Updates: could not write the staging marker:\n" + traceback.format_exc())

    def _removeOwnStagedMarker(self):
        """The download or the preparation failed or was cancelled: this run no longer owns anything."""
        path = os.path.join(stateDirectory(), STAGED_MARKER)
        try:
            with open(path, encoding="utf-8") as f:
                marker = json.load(f)
            if isinstance(marker, dict) and marker.get("pid") == os.getpid():
                _removeQuietly(path)
        except (OSError, ValueError):
            pass

    def _registerStaged(self, prepared):
        """Remember the staged copy (for the cleanup of a run that dies) and remove it at exit unless the user chose to
        install it (Restart now, Install when I quit).
        """
        import slicer

        self._writeStagedMarker(os.path.dirname(prepared.staged))
        if not self._discardConnected:
            slicer.app.connect("aboutToQuit()", self._onQuitDiscard)
            self._discardConnected = True

    def _onQuitDiscard(self):
        prepared = self.prepared
        if prepared is None or prepared.launched or self._quitConnected:
            return  # nothing prepared, or the install is chosen: the helper (or the next start) takes over
        try:
            root = os.path.dirname(prepared.staged)
            if os.path.isdir(root) and os.path.basename(root).endswith(updates_install.STAGING_SUFFIX):
                updates_install._removeTree(root)
            package = prepared.package
            if package and os.path.isfile(package) and os.path.dirname(os.path.abspath(package)) == os.path.abspath(downloadDirectory()):
                _removeQuietly(package)
            _removeQuietly(os.path.join(stateDirectory(), STAGED_MARKER))
        except Exception:
            logging.error("Updates: removing the unused update failed:\n" + traceback.format_exc())

    def _removeAbandonedStaging(self):
        """At start, when no install record exists: remove what a run that did not install its prepared update (crash,
        killed) left: the staged copy next to the installation (or in the temporary folder) and the downloaded package.
        """
        stateDir = stateDirectory()
        markerPath = os.path.join(stateDir, STAGED_MARKER)
        roots = []
        target = self.installTarget()
        if target:
            roots.append(updates_install.stagingRoot(target))
        try:
            with open(markerPath, encoding="utf-8") as f:
                marker = json.load(f)
        except (OSError, ValueError):
            marker = None
        if isinstance(marker, dict):
            pid = marker.get("pid")
            if isinstance(pid, int) and pid != os.getpid() and _processAlive(pid):
                return  # another running Slicer Studio prepared it and may still install it
            root = marker.get("root")
            if isinstance(root, str) and os.path.basename(root).endswith(updates_install.STAGING_SUFFIX):
                roots.append(root)
        elif not target:
            return
        for root in dict.fromkeys(roots):
            if os.path.isdir(root):
                updates_install.removeInBackground(root)
        downloads = downloadDirectory()
        if os.path.isdir(downloads):
            for name in os.listdir(downloads):
                updates_install.removeInBackground(os.path.join(downloads, name))
        _removeQuietly(markerPath)

    def _disconnectQuit(self):
        import slicer

        if self._discardConnected:
            try:
                slicer.app.disconnect("aboutToQuit()", self._onQuitDiscard)
            except Exception:
                pass
            self._discardConnected = False

        if self._quitConnected:
            try:
                slicer.app.disconnect("aboutToQuit()", self._onAboutToQuit)
            except Exception:
                pass
            self._quitConnected = False

    def _installRecord(self):
        record = {"from": currentVersion(), "package": self.prepared.package if self.prepared is not None else None}
        try:
            from StudioLib import autosave

            service = autosave.instance()
            if service is not None and getattr(service, "sessionDir", None):
                record["autosaveSession"] = service.sessionDir
        except Exception:
            pass
        return record

    def _onAboutToQuit(self):
        prepared = self.prepared
        if prepared is None or prepared.launched:
            return
        try:
            prepared.launch(stateDirectory(), relaunch=self._relaunch, record=self._installRecord())
        except Exception:
            logging.error("Updates: starting the install helper failed:\n" + traceback.format_exc())

    def restartToUpdate(self):
        """Close the application (its own exit prompt asks about unsaved work); the update installs and relaunches.

        Return True if the application is closing, False if the user cancelled the exit (the update then installs
        when the application exits, and relaunches it as Restart now asked).
        """
        mainWindow = _mainWindow()
        self.installOnQuit(relaunch=True)
        if mainWindow is None:
            return False
        mainWindow.close()
        if mainWindow.isVisible():
            # Exit cancelled (for example to save a file): install and relaunch when the application exits later
            return False
        return True

    #
    # Settings
    #

    def showUpdates(self, check=False):
        """Open Settings > Updates (and check now)."""
        import qt
        import slicer

        if check:
            self.check()
        dialog = slicer.app.settingsDialog()
        mainWindow = _mainWindow()
        if dialog is None or mainWindow is None:
            return
        dialog.setCurrentPanel(_("Updates"))
        if dialog.isVisible():
            return
        action = mainWindow.findChild(qt.QAction, "EditApplicationSettingsAction")
        if action is not None:
            action.trigger()

    #
    # After an update
    #

    def finishInstall(self):
        """Read the result of the update installed when the previous run exited: tell the user, delete the previous
        version, and offer the unsaved work of the session closed for the update for recovery.
        """
        stateDir = stateDirectory()
        if not os.path.exists(os.path.join(stateDir, "install.json")):  # updates_install.RECORD_FILE, without importing it
            # No update was installed: a prepared update that the previous run did not install is of no use any more
            self._removeAbandonedStaging()
            return None
        record, result = updates_install.readRecord(stateDir)
        if record is None:
            return None
        target = self.installTarget()
        if not target or os.path.realpath(str(record.get("target") or "")) != os.path.realpath(target):
            # The update of another installation (for example a package, while this is a build tree)
            return None
        updates_install.clearRecord(stateDir)
        newVersion = str(record.get("version") or "")
        succeeded = result == "ok" and versionModule.tryParse(newVersion) is not None and versionModule.compare(newVersion, currentVersion()) == 0
        if succeeded:
            backup = record.get("backup")
            if backup and os.path.isdir(backup):
                updates_install.removeInBackground(backup)
            self._finishedMessage = ("ok", _("Slicer Studio was updated to {version}.").format(version=newVersion))
        else:
            reason = result[len("failed"):].strip() if result and result.startswith("failed") else _("the installation did not finish")
            staged = record.get("staged")
            if staged and os.path.isdir(os.path.dirname(staged)) and os.path.basename(os.path.dirname(staged)).endswith(updates_install.STAGING_SUFFIX):
                updates_install.removeInBackground(os.path.dirname(staged))
            self._finishedMessage = ("warn", _("The update to {version} could not be installed: {reason}. Slicer Studio {current} was kept. "
                                               "Details: {log}").format(version=newVersion or "?", reason=reason, current=currentVersion(),
                                                                      log=record.get("log") or ""))
        package = record.get("package")
        if package and os.path.isfile(package) and os.path.dirname(os.path.abspath(package)) == os.path.abspath(downloadDirectory()):
            _removeQuietly(package)
        session = record.get("autosaveSession")
        if session:
            self._offerSessionForRecovery(session)
        import qt

        qt.QTimer.singleShot(2500, self._showFinishedMessage)
        return succeeded

    def _offerSessionForRecovery(self, sessionDir):
        """The session closed for the update with unsaved work is offered for recovery like a crashed one (it is marked
        as closed for the update, so that the recovery dialog does not say that Slicer crashed).
        """
        try:
            from StudioLib import recovery

            root = os.path.dirname(os.path.normpath(sessionDir))
            for session in recovery.scan(root):
                if os.path.normpath(session.path) == os.path.normpath(sessionDir) and session.status == "closed-unsaved":
                    recovery.markSession(session, "running", synchronous=True, closedForUpdate=True)
                    return True
        except Exception:
            logging.error("Updates: " + traceback.format_exc())
        return False

    def _showFinishedMessage(self):
        import slicer

        message, self._finishedMessage = self._finishedMessage, None
        if message is None:
            return
        level, text = message
        logging.info(f"Updates: {text}")
        shell = _shell()
        if shell is not None:
            shell.toast(text, 6000, sticky=level == "warn", level="consequence" if level == "warn" else "info")
        elif level == "warn":
            slicer.util.warningDisplay(text, windowTitle=_("Update"))
        else:
            mainWindow = _mainWindow()
            if mainWindow is not None and mainWindow.statusBar() is not None:
                mainWindow.statusBar().showMessage(text, 6000)


#
# Dialogs
#


class _EscapeCloseFilter:
    """Event filter that ignores Esc and the window's close button (the required dialog has Update now and Quit only)."""

    @staticmethod
    def create():
        import qt

        class Filter(qt.QObject):
            def eventFilter(self, obj, event):
                if event.type() == qt.QEvent.Close:
                    if not obj.property("studioAllowClose"):
                        event.ignore()
                        return True
                if event.type() == qt.QEvent.KeyPress and event.key() == qt.Qt.Key_Escape:
                    return True
                return False

        return Filter()


def _notesBrowser(notes, maximumHeight=220):
    import qt

    from . import projects

    browser = qt.QTextBrowser()
    browser.setObjectName("StudioUpdateNotes")
    projects.routeLinks(browser)
    if notes and hasattr(browser, "setMarkdown"):
        browser.setMarkdown(notes)
        _flattenHeadings(browser)
    else:
        browser.setPlainText(notes or _("No release notes."))
    browser.setMaximumHeight(maximumHeight)
    return browser


def _flattenHeadings(browser):
    """Show the headings of Markdown release notes at the body size in the semibold weight: the default sizes (up to twice the
    body) would make "What's new" larger than the title of the dialog.
    """
    import qt

    document = browser.document
    block = document.begin()
    while block.isValid():
        if block.blockFormat().headingLevel() > 0:
            cursor = qt.QTextCursor(block)
            cursor.movePosition(qt.QTextCursor.EndOfBlock, qt.QTextCursor.KeepAnchor)
            charFormat = qt.QTextCharFormat()
            charFormat.setProperty(qt.QTextFormat.FontSizeAdjustment, 0)
            charFormat.setFontWeight(qt.QFont.DemiBold)
            cursor.mergeCharFormat(charFormat)
        block = block.next()


def _dialogStyleSheet():
    """The rules of the update dialogs inside the dialog chrome."""
    from StudioLib import ui

    return (f"QTextBrowser#StudioUpdateNotes {{ background-color: {ui.token('card-low').name()}; border: 1px solid {ui.token('hairline').name()};"
            " border-radius: 10px; padding: 6px 8px; }"
            f"QLabel#StudioUpdateStatus {{ color: {ui.token('text2').name()}; }}")


def _progressText(received, total):
    if total:
        return _("Downloading… {received} of {total}").format(received=formatSize(received), total=formatSize(total))
    return _("Downloading…")


class RequiredUpdateDialog:
    """Blocking dialog of a required update: version, notes, Update now and Quit (``StudioRequiredUpdateDialog``)."""

    def __init__(self, service, parent):
        import qt

        from StudioLib import ui

        self.service = service
        self.task = None
        status = service.status
        self.dialog = qt.QDialog(parent)
        self.dialog.setObjectName(REQUIRED_DIALOG_OBJECT_NAME)
        self.dialog.setWindowTitle(_("Update required"))
        self.dialog.setModal(True)
        self.dialog.setMinimumWidth(480)
        layout = qt.QVBoxLayout(self.dialog)
        layout.setContentsMargins(24, 24, 24, 22)
        layout.setSpacing(14)
        if status.belowMinimum:
            text = _("This version ({current}) is no longer supported. Slicer Studio {version} is required to continue.")
        else:
            text = _("Slicer Studio {version} is a required update (you have {current}).")
        body = text.format(version=status.version, current=status.currentVersion) + " " + _(
            "Slicer Studio downloads it, checks it and restarts. Your settings and autosaved work are kept.")
        # Header (VISUAL 4.7): the icon tile beside the title and the body text
        header, self.titleLabel, self.text = ui.dialogHeader(
            "download", _("Update to Slicer Studio {version}").format(version=status.version), body, "StudioRequiredUpdate")
        layout.addLayout(header)
        layout.addWidget(_notesBrowser(status.manifest.notes))
        self.progress = qt.QProgressBar()
        self.progress.setObjectName("StudioUpdateProgress")
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.statusLabel = qt.QLabel()
        self.statusLabel.setObjectName("StudioUpdateStatus")
        self.statusLabel.setWordWrap(True)
        self.statusLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        self.statusLabel.setVisible(False)
        layout.addWidget(self.statusLabel)
        # Footer (VISUAL 4.7): the link on the left, the single primary button on the right
        buttons = qt.QHBoxLayout()
        buttons.setSpacing(10)
        self.quitButton = ui.linkButtonWidget(_("Quit"), self.quit, "StudioRequiredUpdateQuitButton")
        buttons.addWidget(self.quitButton)
        buttons.addStretch(1)
        self.updateButton = qt.QPushButton(_("Update now"))
        self.updateButton.setObjectName("StudioRequiredUpdateNowButton")
        self.updateButton.setDefault(True)
        self.updateButton.setMinimumHeight(36)
        self.updateButton.setMinimumWidth(96)
        ui.setRole(self.updateButton, "primary")
        self.updateButton.connect("clicked()", self.onUpdateClicked)
        buttons.addWidget(self.updateButton)
        layout.addLayout(buttons)
        # Dialog chrome (VISUAL 4.7): frameless, rounded, bordered, over a backdrop
        ui.dialogChrome(self.dialog, styleSheet=_dialogStyleSheet)
        self._filter = _EscapeCloseFilter.create()
        self.dialog.installEventFilter(self._filter)
        # Esc and the close button are filtered; anything else that ends the dialog (reject(), done()) shows it again
        self.dialog.connect("finished(int)", self._onFinished)
        self.state = "offer"

    def _onFinished(self, result):
        import qt

        def showAgain():
            if _isAlive(self.dialog) and not self.dialog.property("studioAllowClose") and not self.dialog.isVisible():
                self.show()

        if not self.dialog.property("studioAllowClose"):
            qt.QTimer.singleShot(0, showAgain)

    def show(self):
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def isVisible(self):
        return _isAlive(self.dialog) and self.dialog.isVisible()

    def _setStatus(self, text, error=False):
        self.statusLabel.setText(text)
        self.statusLabel.setVisible(bool(text))

    def onUpdateClicked(self):
        if self.state == "downloading":
            if self.task is not None:
                self.task.cancel()
            return
        if self.state == "ready":
            self._restart()
            return
        reason = self.service.unavailableReason()
        if reason:
            self._setStatus(reason, error=True)
            return
        self.state = "downloading"
        self.updateButton.setText(_("Cancel"))
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self._setStatus(_("Downloading…"))
        try:
            self.task = self.service.startJob(self._onJobDone)
        except updates_install.InstallError as error:
            self._onJobDone(None, error)
            return
        self._progressTimer = self.task.timer
        self.task.timer.connect("timeout()", self._updateProgress)

    def _updateProgress(self):
        task = self.task
        if task is None:
            return
        received, total = task.progress
        if getattr(task, "phase", "") == "prepare":
            self.progress.setRange(0, 0)
            self._setStatus(_("Checked. Preparing the new version…"))
        elif total:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(received * 1000 / total))
            self._setStatus(_progressText(received, total))

    def _onJobDone(self, prepared, error):
        self.task = None
        self.progress.setVisible(False)
        if error is not None:
            self.state = "offer"
            self.updateButton.setText(_("Try again"))
            manual = getattr(error, "manualSteps", None)
            self._setStatus(str(error) + (" " + manual if manual else ""), error=True)
            return
        self.state = "ready"
        self.updateButton.setText(_("Restart now"))
        self._setStatus(_("Slicer Studio {version} is ready. It restarts to finish the update.").format(version=prepared.version))
        self._restart()

    def _restart(self):
        try:
            self.service.installOnQuit(relaunch=True)
        except updates_install.InstallError as error:
            self.state = "offer"
            self.updateButton.setText(_("Try again"))
            self._setStatus(str(error), error=True)
            return
        self._closeApplication()

    def quit(self):
        if self.task is not None:
            self.task.cancel()
        if self.state != "ready":
            self.service.prepared = None
            self.service._disconnectQuit()
        self._closeApplication()

    def _closeApplication(self):
        import qt

        mainWindow = _mainWindow()
        self.dialog.setProperty("studioAllowClose", True)
        self.dialog.hide()
        if mainWindow is not None:
            mainWindow.close()
            if mainWindow.isVisible():
                # Exit cancelled (unsaved data): the dialog comes back
                self.dialog.setProperty("studioAllowClose", False)
                qt.QTimer.singleShot(0, self.show)
                return
        else:
            import slicer

            slicer.app.quit()

    def dispose(self):
        if _isAlive(self.dialog):
            self.dialog.removeEventFilter(self._filter)
            self.dialog.setProperty("studioAllowClose", True)
            self.dialog.hide()
            self.dialog.deleteLater()


class UpdateDialog:
    """Non-modal progress of an optional update (``StudioUpdateDialog``): download, check, then Restart now or
    Install when I quit.
    """

    def __init__(self, service, parent):
        import qt

        from StudioLib import ui

        self.service = service
        self.task = None
        self.state = "idle"
        status = service.status
        self.dialog = qt.QDialog(parent)
        self.dialog.setObjectName(UPDATE_DIALOG_OBJECT_NAME)
        self.dialog.setWindowTitle(_("Update Slicer Studio"))
        self.dialog.setMinimumWidth(460)
        layout = qt.QVBoxLayout(self.dialog)
        self._layout = layout
        layout.setContentsMargins(24, 24, 24, 22)
        layout.setSpacing(14)
        header, self.title, self.text = ui.dialogHeader(
            "download", _("Slicer Studio {version}").format(version=status.version if status else "?"), " ", "StudioUpdate")
        self.text.setObjectName("StudioUpdateStatus")
        self.text.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        layout.addLayout(header)
        self.progress = qt.QProgressBar()
        self.progress.setObjectName("StudioUpdateProgress")
        layout.addWidget(self.progress)
        buttons = qt.QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        self.secondaryButton = qt.QPushButton(_("Cancel"))
        self.secondaryButton.setObjectName("StudioUpdateSecondaryButton")
        self.secondaryButton.setMinimumHeight(36)
        ui.setRole(self.secondaryButton, "secondary")
        self.secondaryButton.connect("clicked()", self.onSecondary)
        buttons.addWidget(self.secondaryButton)
        self.primaryButton = qt.QPushButton(_("Restart now"))
        self.primaryButton.setObjectName("StudioUpdatePrimaryButton")
        self.primaryButton.setMinimumHeight(36)
        self.primaryButton.setMinimumWidth(96)
        ui.setRole(self.primaryButton, "primary")
        self.primaryButton.connect("clicked()", self.onPrimary)
        buttons.addWidget(self.primaryButton)
        layout.addLayout(buttons)
        # Dialog chrome (VISUAL 4.7). Non-modal, so without the backdrop: the main window stays usable behind it
        ui.dialogChrome(self.dialog, backdrop=False, styleSheet=_dialogStyleSheet)

    def show(self):
        self.dialog.show()
        self._fitToText()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _setText(self, text):
        if self.text.text != text:
            self.text.setText(text)
            self._fitToText()

    def _fitToText(self):
        """Give the shown dialog the height its wrapped status text needs at the dialog's width. A window's layout keeps
        only its minimum size, which ignores height-for-width, so the dialog would otherwise keep the height it got for
        its first, one-line text and cut a longer status (a failure reason with the manual steps) under the buttons.
        """
        dialog = self.dialog
        if not _isAlive(dialog) or not dialog.isVisible():
            # Showing the window sizes it for its text (QWidget::adjustSize() includes height-for-width)
            return
        self._layout.activate()
        needed = max(self._layout.totalHeightForWidth(dialog.width), self._layout.totalMinimumSize().height())
        if needed > 0 and needed != dialog.height:
            dialog.resize(dialog.width, needed)

    def begin(self):
        reason = self.service.unavailableReason()
        if reason:
            self._setState("unavailable", reason)
            return
        try:
            self.task = self.service.startJob(self._onJobDone)
        except updates_install.InstallError as error:
            self._onJobDone(None, error)
            return
        self.task.timer.connect("timeout()", self._updateProgress)
        self._setState("downloading", _("Downloading…"))

    def _setState(self, state, text):
        self.state = state
        self.text.setText(text)
        self.progress.setVisible(state == "downloading")
        self.primaryButton.setVisible(state in ("ready", "failed"))
        self.primaryButton.setText(_("Restart now") if state == "ready" else _("Try again"))
        self.secondaryButton.setText({"downloading": _("Cancel"), "ready": _("Install when I quit")}.get(state, _("Close")))
        if state == "ready":
            self.primaryButton.setDefault(True)
        self._fitToText()

    def _updateProgress(self):
        task = self.task
        if task is None:
            return
        received, total = task.progress
        if getattr(task, "phase", "") == "prepare":
            self.progress.setRange(0, 0)
            self._setText(_("Checked. Preparing the new version…"))
        elif total:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(received * 1000 / total))
            self._setText(_progressText(received, total))

    def _onJobDone(self, prepared, error):
        self.task = None
        if error is not None:
            manual = getattr(error, "manualSteps", None)
            self._setState("failed", str(error) + (" " + manual if manual else "") + " " + _("Slicer Studio was not changed."))
            return
        self._setState("ready", _("Slicer Studio {version} was downloaded and checked. Slicer Studio restarts to install it; "
                                  "you are asked to save unsaved work, and autosaved work is offered for recovery after the restart.").format(
            version=prepared.version))

    def preparedInstead(self, preparedVersion, version):
        """The update shown is ready but is not the latest one, and it cannot be taken back."""
        self.title.setText(_("Slicer Studio {version}").format(version=preparedVersion))
        self._setState("ready", _("Slicer Studio {prepared} is ready and installs when you quit Slicer Studio; it cannot be taken back "
                                  "any more. Update again after that to get Slicer Studio {version}.").format(prepared=preparedVersion, version=version))

    def onPrimary(self):
        if self.state == "failed":
            self.begin()
            return
        if self.state != "ready":
            return
        try:
            closing = self.service.restartToUpdate()
        except updates_install.InstallError as error:
            self._setState("failed", str(error))
            return
        if not closing:
            self._setState("ready", _("The update installs when you quit Slicer Studio; it then starts again."))

    def onSecondary(self):
        if self.state == "downloading":
            if self.task is not None:
                self.task.cancel()
            return
        if self.state == "ready":
            try:
                self.service.installOnQuit(relaunch=False)
            except updates_install.InstallError as error:
                self._setState("failed", str(error))
                return
            shell = _shell()
            if shell is not None:
                shell.toast(_("The update installs when you quit"), 3000)
        self.dialog.hide()

    def dispose(self):
        if self.task is not None:
            self.task.cancel()
        if _isAlive(self.dialog):
            self.dialog.hide()
            self.dialog.deleteLater()


#
# Module functions
#


def checkNow():
    """Check for updates now (no-op without the service)."""
    return _service.checkNow() if _service is not None else False


def availableUpdate():
    """The version of an available update (optional or required), or None."""
    status = _service.status if _service is not None else None
    return status.version if status is not None and status.available else None


def showUpdates(check=False):
    if _service is not None:
        _service.showUpdates(check=check)


def updateNow():
    return _service.updateNow() if _service is not None else None

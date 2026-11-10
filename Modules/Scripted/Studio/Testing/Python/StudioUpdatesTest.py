"""In-app updates of Slicer Studio (StudioLib/updates.py, updates_install.py, updates_settings.py, version.py; A-08).

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioUpdatesTest'); slicer.app.exit(0)"

Nothing contacts GitHub: every test uses a local feed (an HTTP server on 127.0.0.1 in this process, through the
``SLICER_STUDIO_UPDATE_FEED`` environment variable or an explicit feed address) and temporary folders. The update
service never starts by itself under --testing; the tests create ``UpdateService(force=True)`` and dispose it.

- (a) versions: parsing, pre-releases, comparison; (b) manifests: validation and optional / required /
  minimumSupported / skipped / not newer;
- (c) the check: local feed, 404, a redirect to a foreign host refused, HTTPS only to GitHub, size cap, total deadline
  for a server that answers slowly, no GitHub API address in the code;
- (d) downloads: verified file kept; wrong SHA-256 and wrong size refused and deleted; cancel;
- (e) Linux swap end to end in a temporary folder with relaunch; failure restores the old version; finishInstall()
  reports and removes the previous version, and offers the session closed for the update for recovery;
- (f) macOS: prepareMacOS() and the helper with fake hdiutil, ditto, xattr and open; wrong application refused;
  App Translocation refused;
- (g) startup decisions: no update, optional (then() runs), required (blocking dialog, then() does not run; Esc,
  the close button, reject() and done() ignored; Quit), minimumSupported, required but not installable (warning
  only), required without a file for this computer (warning only), required found online after the startup (warning
  with Update now), offline with a cached required manifest (warning only), a slow server (the startup goes on after
  the timeout), the first-start tour offer held during the startup check;
- (h) Update now up to the install step (download, verify, prepare; the installed folder untouched), a tampered
  release refused in the dialog, and Cancel while the update is prepared (both dialogs);
- (i) Studio surfaces: the notice on the Library canvas, Skip, the ≡ menu item, the Search entries, the settings
  panel; Classic: the Help menu item;
- (j) make_release.py output round-trips through the application's manifest parser and download check; the web
  launcher reads the same manifest, refuses foreign redirects and unpacks its bundle;
- (k) nothing runs under --testing (onStartupCompleted() only calls then()).
"""

import contextlib
import functools
import hashlib
import http.server
import importlib.util
import io
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest

import qt

import slicer

SOURCE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))


class _Handler(http.server.SimpleHTTPRequestHandler):
    redirects = {}
    slowPaths = set()
    tricklePaths = set()
    stallPaths = set()
    silentPaths = set()
    stallRelease = threading.Event()
    notHttpPaths = set()
    cutChunkedPaths = set()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in self.notHttpPaths:
            # A reply that is not HTTP (http.client.BadStatusLine, which is not an OSError)
            self.wfile.write(b"NOT HTTP AT ALL\r\n\r\n")
            self.close_connection = True
            return
        if path in self.cutChunkedPaths:
            # A chunked body that stops in the middle of a chunk, as when the network drops (http.client.IncompleteRead)
            self.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n1000\r\n" + b"x" * 100)
            self.wfile.flush()
            self.close_connection = True
            return
        if path in self.redirects:
            self.send_response(302)
            self.send_header("Location", self.redirects[path])
            self.end_headers()
            return
        if path in self.tricklePaths:
            # A server that sends its answer one byte at a time (each read is quick; the whole is slow)
            body = b'{"version": "1.2.0", "assets": {}}' + b" " * 200
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for i in range(len(body)):
                    self.wfile.write(body[i:i + 1])
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path in self.silentPaths:
            # A server that accepted the connection and read the request but sends no answer (until the test releases it)
            self.stallRelease.wait(30)
            self.close_connection = True
            return
        if path in self.stallPaths:
            # A server that sends the headers and a few bytes, then nothing (until the test releases it)
            with open(os.path.join(self.directory, path.lstrip("/")), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body[:100])
                self.wfile.flush()
                self.stallRelease.wait(30)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path in self.slowPaths:
            time.sleep(3)
        try:
            return http.server.SimpleHTTPRequestHandler.do_GET(self)
        except (BrokenPipeError, ConnectionResetError):
            # The client stopped reading (a cancelled or refused download)
            return None

    def log_message(self, *args):
        pass


def _fakeLinuxPackage(path, version, top="Slicer-5.13.0-linux-amd64", complete=True):
    with tarfile.open(path, "w:gz") as archive:
        def add(name, data, mode=0o644):
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            info.mode = mode
            archive.addfile(info, io.BytesIO(data))
        add("Slicer", b'#!/bin/sh\necho "$0" > "$(dirname "$0")/../relaunched.txt"\n', 0o755)
        if complete:
            add("bin/SlicerApp-real", b"\x7fELF test", 0o755)
        add("lib/Slicer-5.13/qt-scripted-modules/StudioLib/version.py", f'STUDIO_VERSION = "{version}"\n'.encode())


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def updates_settings_panelName():
    from StudioLib import updates_settings

    return updates_settings.PANEL_OBJECT_NAME


class StudioUpdatesTest(unittest.TestCase):
    """In-app updates with a local feed. Needs the main window (dialogs, settings dialog)."""

    def setUp(self):
        from StudioLib import updates, updates_install, version

        self.updates = updates
        self.install = updates_install
        self.version = version
        self.tempDir = tempfile.mkdtemp(prefix="StudioUpdatesTest-")
        self.feedDir = os.path.join(self.tempDir, "feed")
        os.makedirs(self.feedDir)
        _Handler.redirects = {}
        _Handler.slowPaths = set()
        _Handler.tricklePaths = set()
        _Handler.stallPaths = set()
        _Handler.silentPaths = set()
        _Handler.stallRelease = threading.Event()
        _Handler.notHttpPaths = set()
        _Handler.cutChunkedPaths = set()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Handler, directory=self.feedDir))
        self.serverThread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.serverThread.start()
        self.root = f"http://127.0.0.1:{self.server.server_address[1]}/"
        self.feed = self.root + "studio-update.json"
        self.savedEnvironment = os.environ.get(updates.FEED_ENVIRONMENT_VARIABLE)
        os.environ[updates.FEED_ENVIRONMENT_VARIABLE] = self.feed
        self.savedSettings = {name: qt.QSettings().value(f"{updates.SETTINGS_GROUP}/{name}") for name in updates.DEFAULT_SETTINGS}
        qt.QSettings().remove(updates.SETTINGS_GROUP)
        self.stateDir = os.path.join(self.tempDir, "state")
        updates.stateDirectoryOverride = self.stateDir
        updates.currentVersionOverride = "0.9.0"
        self.services = []
        self.addCleanup(self._assertNoParentlessPanels)  # registered first, so it runs after every deleteLater cleanup

    def _assertNoParentlessPanels(self):
        # A parentless settings panel alive at exit makes Slicer exit abnormally (code 1) although every test passed
        qt.QApplication.sendPostedEvents(None, 52)  # QEvent.DeferredDelete
        leaked = [w for w in slicer.app.topLevelWidgets() if w.objectName == updates_settings_panelName()]
        self.assertEqual(leaked, [], "a test left a parentless UpdatesSettingsPanel alive (add self.addCleanup(panel.deleteLater))")

    def tearDown(self):
        updates = self.updates
        for service in self.services:
            service.dispose()
        updates._installableOverride = None
        updates.installTargetOverride = None
        updates.currentVersionOverride = None
        updates.stateDirectoryOverride = None
        _Handler.stallRelease.set()
        self.server.shutdown()
        self.server.server_close()
        if self.savedEnvironment is None:
            os.environ.pop(updates.FEED_ENVIRONMENT_VARIABLE, None)
        else:
            os.environ[updates.FEED_ENVIRONMENT_VARIABLE] = self.savedEnvironment
        settings = qt.QSettings()
        settings.remove(updates.SETTINGS_GROUP)
        for name, value in self.savedSettings.items():
            if value is not None:
                settings.setValue(f"{updates.SETTINGS_GROUP}/{name}", value)
        shutil.rmtree(self.tempDir, ignore_errors=True)

    #
    # Helpers
    #

    def _pump(self, seconds=0.1):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            slicer.app.processEvents()
            time.sleep(0.01)

    def _waitFor(self, condition, timeout=20.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return True
            slicer.app.processEvents()
            time.sleep(0.02)
        return condition()

    def _publish(self, version, required=False, minimum=None, notes="Notes", package=True, tamper=None):
        """Write a release (a fake Linux package and the manifest) to the feed folder. Return the manifest dict."""
        assets = {}
        if package:
            name = f"Slicer-5.13.0-studio{version}-linux-amd64.tar.gz"
            path = os.path.join(self.feedDir, name)
            _fakeLinuxPackage(path, version)
            assets["linux-x86_64"] = {"name": name, "size": os.path.getsize(path), "sha256": _sha256(path)}
            if tamper == "sha256":
                assets["linux-x86_64"]["sha256"] = "0" * 64
            elif tamper == "size":
                assets["linux-x86_64"]["size"] += 10
        data = {"version": version, "required": required, "minimumSupported": minimum, "published": "2026-10-22", "notes": notes, "assets": assets}
        with open(os.path.join(self.feedDir, "studio-update.json"), "w") as f:
            json.dump(data, f)
        return data

    def _service(self):
        service = self.updates.UpdateService(force=True)
        self.services.append(service)
        return service

    def _fakeLinuxInstall(self, version="0.9.0"):
        packages = os.path.join(self.tempDir, "packages")
        os.makedirs(packages, exist_ok=True)
        package = os.path.join(packages, "installed.tar.gz")
        _fakeLinuxPackage(package, version)
        installRoot = os.path.join(self.tempDir, "opt dir")
        os.makedirs(installRoot, exist_ok=True)
        with tarfile.open(package) as archive:
            archive.extractall(installRoot, filter="data") if hasattr(tarfile, "data_filter") else archive.extractall(installRoot)
        return os.path.join(installRoot, "Slicer-5.13.0-linux-amd64")

    def _sleeper(self, seconds=0.5):
        """A process that exits after ``seconds``; it stands for the application that the helper waits for. It is reaped
        when it exits (an exited child that is not reaped still answers 'kill -0').
        """
        process = subprocess.Popen(["/bin/sh", "-c", f"sleep {seconds}"])
        reaper = threading.Thread(target=process.wait, daemon=True)
        reaper.start()
        self.addCleanup(reaper.join, 10)
        return process

    #
    # (a) (b) versions and manifests
    #

    def test_Versions(self):
        v = self.version
        self.assertTrue(v.parse("1.2.0-rc.1") < v.parse("1.2.0"))
        self.assertTrue(v.parse("1.2.0-alpha") < v.parse("1.2.0-alpha.1") < v.parse("1.2.0-beta"))
        self.assertTrue(v.parse("1.2.0-rc.2") < v.parse("1.2.0-rc.10"))
        self.assertEqual(v.compare("v1.10.0", "1.9.9"), 1)
        self.assertEqual(v.compare("1.0.0+build.5", "1.0.0"), 0)
        self.assertTrue(v.isNewer("0.10.0", "0.9.0"))
        for bad in ("1.2", "01.2.3", "1.2.3.4", "", None, "latest"):
            self.assertIsNone(v.tryParse(bad), bad)
        self.assertTrue(v.isValid(v.STUDIO_VERSION))
        self.assertEqual(v.readVersionFile(os.path.join(os.path.dirname(v.__file__), "version.py")), v.STUDIO_VERSION)

    def test_Manifests(self):
        updates = self.updates
        good = {"version": "1.2.0", "required": False, "minimumSupported": None, "notes": "n",
                "assets": {"linux-x86_64": {"name": "a-linux-amd64.tar.gz", "size": 10, "sha256": "A" * 64}, "windows-x86_64": {"x": 1}}}
        manifest = updates.parseManifest(json.dumps(good))
        self.assertEqual(manifest.version, "1.2.0")
        self.assertEqual(list(manifest.assets), ["linux-x86_64"])  # an unknown platform is ignored
        self.assertEqual(manifest.asset("linux-x86_64")["sha256"], "a" * 64)
        self.assertEqual(manifest.tag, "v1.2.0")
        self.assertIn("github.com/azhmeerjesani/Slicer-2/releases/tag/v1.2.0", manifest.releasePageUrl())
        for change in ({"version": "1.2"}, {"required": "yes"}, {"minimumSupported": "x"},
                       {"assets": {"linux-x86_64": {"name": "../evil", "size": 1, "sha256": "a" * 64}}},
                       {"assets": {"linux-x86_64": {"name": "dir/evil", "size": 1, "sha256": "a" * 64}}},
                       {"assets": {"linux-x86_64": {"name": "ok.tgz", "size": 0, "sha256": "a" * 64}}},
                       {"assets": {"linux-x86_64": {"name": "ok.tgz", "size": True, "sha256": "a" * 64}}},
                       {"assets": {"linux-x86_64": {"name": "ok.tgz", "size": 1, "sha256": "abc"}}}):
            with self.assertRaises(updates.UpdateError, msg=str(change)):
                updates.parseManifest(dict(good, **change))
        with self.assertRaises(updates.UpdateError):
            updates.parseManifest(b"not json")
        evaluate = updates.evaluate
        self.assertEqual(evaluate(manifest, "0.9.0").kind, "optional")
        self.assertEqual(evaluate(manifest, "1.2.0").kind, "none")
        self.assertEqual(evaluate(manifest, "1.3.0").kind, "none")
        self.assertEqual(evaluate(updates.parseManifest(dict(good, required=True)), "0.9.0").kind, "required")
        self.assertEqual(evaluate(updates.parseManifest(dict(good, required=True)), "1.2.0").kind, "none")
        self.assertEqual(evaluate(updates.parseManifest(dict(good, minimumSupported="1.0.0")), "0.9.0").kind, "required")
        status = evaluate(updates.parseManifest(dict(good, minimumSupported="1.0.0")), "0.9.0")
        self.assertTrue(status.belowMinimum)
        self.assertEqual(evaluate(updates.parseManifest(dict(good, minimumSupported="0.8.0")), "0.9.0").kind, "optional")
        self.assertTrue(evaluate(manifest, "0.9.0", skippedVersion="1.2.0").skipped)
        self.assertFalse(evaluate(manifest, "0.9.0", skippedVersion="1.1.0").skipped)
        # A required release is never skipped
        self.assertFalse(evaluate(updates.parseManifest(dict(good, required=True)), "0.9.0", skippedVersion="1.2.0").skipped)
        # Platform keys
        self.assertEqual(updates.platformKey({}, "darwin", "arm64"), "macos-arm64")
        self.assertEqual(updates.platformKey({}, "darwin", "x86_64"), "macos-x86_64")
        self.assertEqual(updates.platformKey({}, "linux", "x86_64"), "linux-x86_64")
        self.assertEqual(updates.platformKey({"SLICER_STUDIO_WEB": "1"}, "linux", "x86_64"), "web")
        self.assertIsNone(updates.platformKey({}, "win32", "AMD64"))

    #
    # (c) the check
    #

    def test_Check(self):
        updates = self.updates
        self._publish("1.2.0")
        self.assertEqual(updates.fetchManifest(self.feed).version, "1.2.0")
        # The default address: the public "latest release" download of the repository (no API)
        self.assertEqual(updates.DEFAULT_FEED_URL, "https://github.com/azhmeerjesani/Slicer-2/releases/latest/download/studio-update.json")
        self.assertEqual(updates.feedUrl(), self.feed)
        with self.assertRaises(updates.UpdateError):
            updates.fetchManifest(self.root + "missing/studio-update.json")
        # A redirect to another host is refused (localhost is not the feed host 127.0.0.1)
        port = self.server.server_address[1]
        _Handler.redirects = {"/moved.json": f"http://localhost:{port}/studio-update.json"}
        with self.assertRaisesRegex(updates.UpdateError, "Refused"):
            updates.fetchManifest(self.root + "moved.json")
        # The same redirect within the feed host is followed
        _Handler.redirects = {"/moved.json": f"http://127.0.0.1:{port}/studio-update.json"}
        self.assertEqual(updates.fetchManifest(self.root + "moved.json").version, "1.2.0")
        # HTTPS only, and only to GitHub, for the real feed
        policy = updates._policy(updates.DEFAULT_FEED_URL)
        updates.checkUrl("https://github.com/x", **policy)
        updates.checkUrl("https://objects.githubusercontent.com/x", **policy)
        for url in ("http://github.com/x", "https://evil.example/x", "https://github.com.evil.example/x", "https://api.github.com/x",
                    "file:///etc/passwd", "https://githubusercontent.com.evil.example/x"):
            if url == "https://api.github.com/x":
                continue  # a GitHub host, but never used: see below
            with self.assertRaises(updates.UpdateError, msg=url):
                updates.checkUrl(url, **policy)
        # Size cap
        with open(os.path.join(self.feedDir, "big.json"), "w") as f:
            f.write(" " * (updates.MANIFEST_MAX_BYTES + 10) + "{}")
        with self.assertRaisesRegex(updates.UpdateError, "too large"):
            updates.fetchManifest(self.root + "big.json")
        # A total deadline: a server that sends its answer slowly is given up (each read alone is within the timeout)
        _Handler.tricklePaths = {"/trickle.json"}
        savedDeadline = updates.CHECK_DEADLINE_S
        updates.CHECK_DEADLINE_S = 1
        try:
            start = time.monotonic()
            with self.assertRaisesRegex(updates.UpdateError, "too slowly"):
                updates.fetchManifest(self.root + "trickle.json")
            self.assertLess(time.monotonic() - start, 4)
        finally:
            updates.CHECK_DEADLINE_S = savedDeadline
        # Turned off
        os.environ[updates.FEED_ENVIRONMENT_VARIABLE] = "off"
        self.assertIsNone(updates.feedUrl())
        with self.assertRaises(updates.UpdateError):
            updates.fetchManifest()

    def test_NoGitHubApi(self):
        files = [os.path.join(os.path.dirname(self.updates.__file__), name) for name in ("updates.py", "updates_install.py", "updates_settings.py")]
        files += [os.path.join(SOURCE_DIR, "Utilities", "Scripts", "SlicerStudioRelease", "make_release.py"),
                  os.path.join(SOURCE_DIR, "Utilities", "SlicerStudioWeb", "slicer_studio_web.py")]
        for path in files:
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("api.github.com", text, path)
            self.assertNotIn("Authorization", text, path)
            self.assertNotIn("GITHUB_TOKEN", text, path)

    #
    # (d) downloads
    #

    def test_Download(self):
        updates = self.updates
        data = self._publish("1.2.0")
        manifest = updates.parseManifest(data)
        downloads = os.path.join(self.tempDir, "downloads")
        progress = []
        path = updates.downloadAsset(manifest, "linux-x86_64", downloads, feed=self.feed, progress=lambda r, t: progress.append((r, t)))
        self.assertEqual(_sha256(path), data["assets"]["linux-x86_64"]["sha256"])
        self.assertEqual(progress[-1][0], progress[-1][1])
        os.remove(path)
        for tamper, message in (("sha256", "SHA-256"), ("size", "size")):
            tampered = updates.parseManifest(self._publish("1.2.0", tamper=tamper))
            with self.assertRaisesRegex(updates.UpdateError, message):
                updates.downloadAsset(tampered, "linux-x86_64", downloads, feed=self.feed)
            self.assertEqual(os.listdir(downloads), [], "a refused download is deleted")
        with self.assertRaisesRegex(updates.UpdateError, "cancelled"):
            updates.downloadAsset(manifest, "linux-x86_64", downloads, feed=self.feed, cancelled=lambda: True)
        self.assertEqual(os.listdir(downloads), [])
        with self.assertRaises(updates.UpdateError):
            updates.downloadAsset(manifest, "macos-arm64", downloads, feed=self.feed)
        # The real feed downloads from the tag of the release
        self.assertEqual(updates.assetUrl(manifest, manifest.asset("linux-x86_64"), updates.DEFAULT_FEED_URL),
                         f"https://github.com/azhmeerjesani/Slicer-2/releases/download/v1.2.0/{data['assets']['linux-x86_64']['name']}")

    def test_DownloadMissingReleaseFile(self):
        """The manifest is there but the release file it names is not (404): the message says the file is missing, not
        that no release with a manifest was found. A missing manifest keeps its own message.
        """
        updates = self.updates
        data = self._publish("1.2.0")
        manifest = updates.parseManifest(data)
        name = data["assets"]["linux-x86_64"]["name"]
        os.remove(os.path.join(self.feedDir, name))
        downloads = os.path.join(self.tempDir, "downloads")
        with self.assertRaises(updates.UpdateError) as caught:
            updates.downloadAsset(manifest, "linux-x86_64", downloads, feed=self.feed)
        message = str(caught.exception)
        self.assertIn("1.2.0", message)
        self.assertIn(name, message)
        self.assertIn("404", message)
        self.assertNotIn("update manifest", message)
        self.assertEqual(os.listdir(downloads), [], "no partial file is left")
        os.remove(os.path.join(self.feedDir, "studio-update.json"))
        with self.assertRaisesRegex(updates.UpdateError, "No release with an update manifest"):
            updates.fetchManifest(self.feed)

    def test_NetworkProtocolErrors(self):
        """A reply that is not HTTP, or a chunked download that is cut, is an UpdateError with a plain message (not a
        raw http.client exception, which is no OSError), and no partial file is left.
        """
        updates = self.updates
        data = self._publish("1.2.0")
        manifest = updates.parseManifest(data)
        name = data["assets"]["linux-x86_64"]["name"]
        _Handler.notHttpPaths = {"/studio-update.json"}
        with self.assertRaises(updates.UpdateError) as caught:
            updates.fetchManifest(self.feed)
        self.assertNotIn("NOT HTTP", str(caught.exception))
        self.assertIn("valid answer", str(caught.exception))
        _Handler.notHttpPaths = set()
        _Handler.cutChunkedPaths = {"/" + name}
        downloads = os.path.join(self.tempDir, "downloads")
        with self.assertRaisesRegex(updates.UpdateError, "connection was interrupted"):
            updates.downloadAsset(manifest, "linux-x86_64", downloads, feed=self.feed)
        self.assertEqual(os.listdir(downloads), [], "the partial file is deleted")
        # The manifest itself cut in the middle of a chunk
        _Handler.cutChunkedPaths = {"/studio-update.json"}
        with self.assertRaisesRegex(updates.UpdateError, "connection was interrupted"):
            updates.fetchManifest(self.feed)

    def test_NonManifestAnswerIsPlainSentence(self):
        """A proxy block page, a login page, an empty answer or binary data is reported in plain words, not with the
        JSON parser's text.
        """
        updates = self.updates
        for body in (b"<html><body>Blocked by proxy</body></html>", b"", b"\xff\xfe", "Please log in"):
            with self.assertRaises(updates.UpdateError) as caught:
                updates.parseManifest(body)
            text = str(caught.exception)
            self.assertIn("did not send an update manifest", text)
            self.assertTrue(text.endswith("and try again."), text)
            for raw in ("JSON", "Expecting", "line 1", "codec", "char 0"):
                self.assertNotIn(raw, text)

    def test_OfflineMessagesArePlainSentences(self):
        """No connection (a closed port, a name that cannot be resolved, a timeout) is a plain message without the error
        number or the library's text, and it ends in a full stop so that the next sentence can follow it.
        """
        import errno
        import socket
        import urllib.error

        updates = self.updates
        raw = ("Errno", "refused", "nodename", "Tunnel")
        with self.assertRaises(updates.UpdateError) as caught:
            updates.fetchManifest("http://127.0.0.1:1/studio-update.json")  # nothing listens
        text = str(caught.exception)
        self.assertEqual(text, "Cannot reach the update server (no connection). Check your internet connection and try again.")
        for error in (ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
                      socket.gaierror(8, "nodename nor servname provided, or not known"),
                      urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
                      OSError("Tunnel connection failed: 502 Bad Gateway"),
                      TimeoutError("timed out")):
            message = str(updates.cannotReach(error))
            self.assertTrue(message.startswith("Cannot reach the update server ("), message)
            self.assertTrue(message.endswith("Check your internet connection and try again."), message)
            for word in raw:
                self.assertNotIn(word, message)
        self.assertIn("no answer in time", str(updates.cannotReach(TimeoutError("timed out"))))
        # a download that fails the same way, and a local failure that is not about the network
        data = self._publish("1.2.0")
        manifest = updates.parseManifest(data)
        downloads = os.path.join(self.tempDir, "downloads-offline")
        saved = updates._open
        try:
            def refuse(*args, **kwargs):
                raise updates.cannotReach(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))

            updates._open = refuse
            with self.assertRaises(updates.UpdateError) as caught:
                updates.downloadAsset(manifest, "linux-x86_64", downloads, feed=self.feed)
            self.assertNotIn("Errno", str(caught.exception))
        finally:
            updates._open = saved
        # Settings > Updates and the update dialog run the message and the next sentence apart
        from StudioLib import updates_settings

        os.environ[updates.FEED_ENVIRONMENT_VARIABLE] = "http://127.0.0.1:1/studio-update.json"
        service = self._service()
        done = []
        service.check(onDone=lambda status, error: done.append(error))
        self.assertTrue(self._waitFor(lambda: done, 15))
        panel = updates_settings.UpdatesSettingsPanel(service)
        self.addCleanup(panel.deleteLater)
        text = panel.statusText()
        self.assertIn("The last check failed. Cannot reach the update server (no connection). "
                      "Check your internet connection and try again.", text)
        self.assertNotIn("Errno", text)
        dialog = updates.UpdateDialog(service, slicer.util.mainWindow())
        dialog.wanted = True  # an open dialog shows the failure itself (a closed one hands it to a notice)
        self.addCleanup(dialog.dialog.close)
        dialog._onJobDone(None, updates.cannotReach(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")))
        self.assertEqual(dialog.state, "failed")
        self.assertIn("try again. Slicer Studio was not changed.", dialog.text.text)
        self.assertNotIn("Errno", dialog.text.text)
        dialog._onJobDone(None, updates.UpdateError("Something went wrong"))
        self.assertIn("Something went wrong. Slicer Studio was not changed.", dialog.text.text)

    def test_DownloadCancelWhileStalled(self):
        """Cancel stops a download at once when the server stops sending (it does not wait for the next block or the timeout)."""
        updates = self.updates
        data = self._publish("1.2.0")
        manifest = updates.parseManifest(data)
        _Handler.stallPaths = {"/" + data["assets"]["linux-x86_64"]["name"]}
        downloads = os.path.join(self.tempDir, "downloads")
        cancel = threading.Event()
        received = []
        outcome = {}

        def work():
            try:
                updates.downloadAsset(manifest, "linux-x86_64", downloads, feed=self.feed,
                                      progress=lambda r, t: received.append(r), cancelled=cancel.is_set)
                outcome["error"] = None
            except updates.UpdateError as error:
                outcome["error"] = str(error)
            outcome["end"] = time.monotonic()

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.assertTrue(self._waitFor(lambda: received and received[-1] >= 100, 10), "the first bytes arrive")
        self.assertEqual(received[0], 0, "the progress is reported from the start")
        self._pump(1.0)
        self.assertNotIn("end", outcome, "the stalled download is still running")
        cancelAt = time.monotonic()
        cancel.set()
        self.assertTrue(self._waitFor(lambda: "end" in outcome, 8), "Cancel is noticed while the server is silent")
        self.assertLess(outcome["end"] - cancelAt, 5)
        self.assertIn("cancelled", outcome["error"])
        self.assertEqual(os.listdir(downloads), [], "the partial file is deleted")
        thread.join(5)

    def test_DownloadCancelBeforeServerAnswers(self):
        """Cancel stops a download at once when the server has accepted the connection but sent no headers yet."""
        updates = self.updates
        data = self._publish("1.2.0")
        manifest = updates.parseManifest(data)
        _Handler.silentPaths = {"/" + data["assets"]["linux-x86_64"]["name"]}
        downloads = os.path.join(self.tempDir, "downloads")
        cancel = threading.Event()
        outcome = {}

        def work():
            try:
                updates.downloadAsset(manifest, "linux-x86_64", downloads, feed=self.feed, cancelled=cancel.is_set)
                outcome["error"] = None
            except updates.UpdateError as error:
                outcome["error"] = str(error)
            outcome["end"] = time.monotonic()

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self._pump(1.5)
        self.assertNotIn("end", outcome, "the download waits for the silent server")
        cancelAt = time.monotonic()
        cancel.set()
        self.assertTrue(self._waitFor(lambda: "end" in outcome, 8), "Cancel is noticed while the server has not answered")
        self.assertLess(outcome["end"] - cancelAt, 3)
        self.assertEqual(outcome["error"], "The download was cancelled.")
        self.assertEqual([n for n in os.listdir(downloads) if n.endswith(".part")], [], "no partial file is left")
        _Handler.stallRelease.set()
        thread.join(5)

    def test_DownloadWriteFailureMessage(self):
        """BB-1: a download folder that cannot be written said "Cannot write <path>.nii.gz.part: Permission denied" (the internal
        .part name and the full path); it says "Permission denied" and names the folder.
        """
        from StudioLib import downloads

        data = self._publish("1.2.0")
        name = data["assets"]["linux-x86_64"]["name"]
        dest = os.path.join(self.tempDir, "StudioTemplates", name)
        realOpen = open

        def denied(path, *args, **kwargs):
            if str(path).endswith(".part"):
                raise PermissionError(13, "Permission denied", str(path))
            return realOpen(path, *args, **kwargs)

        download = downloads.Download(self.root + name, None, dest)
        messages = []
        download.failed.connect(messages.append)
        downloads.open = denied
        try:
            download.start()
        finally:
            del downloads.open
        self.assertEqual(messages, ["Cannot save the download in the folder StudioTemplates: Permission denied"])
        self.assertNotIn(".part", messages[0])
        self.assertNotIn(self.tempDir, messages[0])
        self.assertEqual(download.state, "failed")

    def _downloadOnFullDisk(self, failOn):
        """Download a 3 MB file whose writes (or whose final close) fail with ENOSPC; return what the Download did."""
        from StudioLib import downloads

        name = "tpl-big.bin"
        payload = os.urandom(3 * 1024 * 1024)
        with open(os.path.join(self.feedDir, name), "wb") as f:
            f.write(payload)
        realOpen = open
        attempts = []

        class FullDisk:
            def __init__(self, path, *args):
                self._f = realOpen(path, *args)

            def write(self, data):
                attempts.append(len(data))
                if failOn == "write":
                    raise OSError(28, "No space left on device")
                return self._f.write(data)

            def close(self):
                self._f.close()
                if failOn == "close":
                    raise OSError(28, "No space left on device")

        dest = os.path.join(self.tempDir, "StudioTemplates-" + failOn, name)
        download = downloads.Download(self.root + name, hashlib.sha256(payload).hexdigest(), dest)
        messages, done, tracebacks = [], [], []
        download.failed.connect(messages.append)
        download.finished.connect(done.append)
        previousHook = sys.excepthook
        sys.excepthook = lambda *args: tracebacks.append(args)
        downloads.open = FullDisk
        try:
            download.start()
            deadline = time.time() + 30
            while download.state == "running" and time.time() < deadline:
                slicer.app.processEvents()
                time.sleep(0.01)
            for _ in range(20):
                slicer.app.processEvents()
                time.sleep(0.01)
        finally:
            del downloads.open
            sys.excepthook = previousHook
        return download, dest, len(payload), attempts, messages, done, tracebacks

    def test_DownloadDiskFullStopsWithMessage(self):
        """BB-1: on a full disk every write raised an OSError traceback, the transfer ran on to the end and the notice said
        "damaged (checksum mismatch)"; the download stops at the first failed write and says the disk is full.
        """
        for failOn in ("write", "close"):
            download, dest, size, attempts, messages, done, tracebacks = self._downloadOnFullDisk(failOn)
            with self.subTest(failOn=failOn):
                self.assertEqual(download.state, "failed")
                self.assertEqual(messages, ["Cannot save the download in the folder StudioTemplates-%s: No space left on device" % failOn])
                self.assertNotIn("damaged", messages[0])
                self.assertEqual(done, [])
                self.assertEqual(tracebacks, [], "no traceback")
                if failOn == "write":
                    self.assertEqual(len(attempts), 1, "the transfer stops at the first failed write")
                    self.assertLess(download.bytesReceived, size, "the rest of the file is not downloaded")
                self.assertFalse(os.path.exists(dest))
                self.assertFalse(os.path.exists(dest + ".part"))

    def test_DownloadTimeoutMessage(self):
        """A download that stalls until the transfer timeout says so, not 'Operation canceled' (the user cancelled nothing)."""
        from StudioLib import downloads

        data = self._publish("1.2.0")
        name = data["assets"]["linux-x86_64"]["name"]
        _Handler.stallPaths = {"/" + name}
        savedTimeout = downloads.TRANSFER_TIMEOUT_MS
        downloads.TRANSFER_TIMEOUT_MS = 1500
        download = downloads.Download(self.root + name, None, os.path.join(self.tempDir, "downloads", name))
        messages = []
        download.failed.connect(messages.append)
        try:
            download.start()
            self.assertTrue(self._waitFor(lambda: messages, 20), "the stalled download ends at the transfer timeout")
        finally:
            downloads.TRANSFER_TIMEOUT_MS = savedTimeout
            download.cancel()
        self.assertEqual(download.state, "failed")
        self.assertIn("did not answer", messages[0])
        self.assertNotIn("canceled", messages[0].lower())
        self.assertFalse(os.path.exists(download.partPath), "the partial file is deleted")
        # A real cancel keeps its own text
        _Handler.stallPaths = {"/" + name}
        other = downloads.Download(self.root + name, None, os.path.join(self.tempDir, "downloads", "b-" + name))
        canceled = []
        other.failed.connect(canceled.append)
        other.start()
        other.cancel()
        self.assertEqual(canceled, ["Download canceled"])

    #
    # (e) Linux swap
    #

    def test_LinuxSwap(self):
        updates, install = self.updates, self.install
        target = self._fakeLinuxInstall("0.9.0")
        self.assertEqual(install.installTarget("linux-x86_64", target), os.path.realpath(target))
        self.assertEqual(install.linuxPackageVersion(target), "0.9.0")
        package = os.path.join(self.tempDir, "new.tar.gz")
        _fakeLinuxPackage(package, "1.2.0")
        with self.assertRaisesRegex(install.InstallError, "not 1.3.0"):
            install.prepareLinux(package, target, "1.3.0")
        self.assertFalse(os.path.exists(install.stagingRoot(target)), "a refused package leaves nothing")
        incomplete = os.path.join(self.tempDir, "incomplete.tar.gz")
        _fakeLinuxPackage(incomplete, "1.2.0", complete=False)
        with self.assertRaisesRegex(install.InstallError, "SlicerApp-real"):
            install.prepareLinux(incomplete, target, "1.2.0")
        prepared = install.prepareLinux(package, target, "1.2.0")
        self.assertEqual(install.linuxPackageVersion(prepared.staged), "1.2.0")
        self.assertEqual(install.linuxPackageVersion(target), "0.9.0", "the installed version is untouched until the swap")
        process = self._sleeper(0.5)
        prepared.launch(self.stateDir, pid=process.pid, relaunch=True, record={"from": "0.9.0"})
        self.assertEqual(install.waitForResult(self.stateDir, 30), "ok")
        self.assertEqual(install.linuxPackageVersion(target), "1.2.0")
        self.assertEqual(install.linuxPackageVersion(prepared.backup), "0.9.0")
        self.assertFalse(os.path.exists(install.stagingRoot(target)))
        relaunched = os.path.join(os.path.dirname(target), "relaunched.txt")
        self.assertTrue(self._waitFor(lambda: os.path.exists(relaunched), 10), "the new version is started")
        # The new version: finishInstall() reports, removes the previous version and clears the record
        updates.currentVersionOverride = "1.2.0"
        updates.installTargetOverride = target
        service = self._service()
        self.assertTrue(service.finishInstall())
        self.assertEqual(service._finishedMessage[0], "ok")
        self.assertTrue(self._waitFor(lambda: not os.path.exists(prepared.backup), 10))
        self.assertEqual(install.readRecord(self.stateDir), (None, None))

    def test_SecondStartLeavesPendingInstallAlone(self):
        """Slicer Studio is started again while the window that chose the update (Install when I quit, Restart now) is
        still exiting: the helper still waits, so the second start must leave the record and the staged copy alone
        (it used to report a failed install and delete the staged copy, so the chosen update was lost).
        """
        updates, install = self.updates, self.install
        target = self._fakeLinuxInstall("0.9.0")
        package = os.path.join(self.tempDir, "new.tar.gz")
        _fakeLinuxPackage(package, "1.2.0")
        prepared = install.prepareLinux(package, target, "1.2.0")
        exiting = self._sleeper(60)  # the first window, still exiting
        self.addCleanup(exiting.kill)
        helper = prepared.launch(self.stateDir, pid=exiting.pid, relaunch=False, record={"from": "0.9.0"})
        threading.Thread(target=helper.wait, daemon=True).start()
        record, result = install.readRecord(self.stateDir)
        self.assertEqual((record["pid"], record["helperPid"], result), (exiting.pid, helper.pid, None), "the record names the waited-for process")
        updates.installTargetOverride = target
        second = self._service()
        self.assertIsNone(second.finishInstall(), "the install is still pending")
        self.assertIsNone(second._finishedMessage, "no failure is reported")
        self._pump(0.5)
        self.assertIsNotNone(install.readRecord(self.stateDir)[0], "the install record is kept")
        self.assertEqual(install.linuxPackageVersion(prepared.staged), "1.2.0", "the staged copy is kept")
        # The first window has exited: the update chosen there is installed, and the new version reports it
        exiting.kill()
        self.assertEqual(install.waitForResult(self.stateDir, 30), "ok")
        self.assertEqual(install.linuxPackageVersion(target), "1.2.0")
        updates.currentVersionOverride = "1.2.0"
        self.assertTrue(self._service().finishInstall())
        self.assertEqual(install.readRecord(self.stateDir), (None, None))
        # Pending only while a recorded process runs, no result is written, and not long after the helper started
        other = self._sleeper(30)
        self.addCleanup(other.kill)
        alive = {"version": "1.2.0", "pid": other.pid, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.assertTrue(updates._installPending(alive, None))
        self.assertFalse(updates._installPending(alive, "failed the new version is incomplete"))
        self.assertFalse(updates._installPending(dict(alive, pid=os.getpid()), None), "the record of this run")
        self.assertFalse(updates._installPending(dict(alive, pid=exiting.pid), None), "the exited window")
        self.assertFalse(updates._installPending({"version": "1.2.0", "started": alive["started"]}, None), "a record without pids")
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - install.EXIT_WAIT_S - updates.INSTALL_PENDING_GRACE_S - 60))
        self.assertFalse(updates._installPending(dict(alive, started=old), None), "a recorded pid long after the helper gave up")

    def test_LinuxSwapFailureRestores(self):
        updates, install = self.updates, self.install
        target = self._fakeLinuxInstall("0.9.0")
        package = os.path.join(self.tempDir, "new.tar.gz")
        _fakeLinuxPackage(package, "1.2.0")
        prepared = install.prepareLinux(package, target, "1.2.0")
        # The staged version is damaged after the check: the helper refuses it and keeps the installed version
        os.remove(os.path.join(prepared.staged, "Slicer"))
        process = self._sleeper(0.2)
        prepared.launch(self.stateDir, pid=process.pid, relaunch=True)
        result = install.waitForResult(self.stateDir, 30)
        self.assertTrue(result.startswith("failed"), result)
        self.assertEqual(install.linuxPackageVersion(target), "0.9.0")
        self.assertTrue(os.path.exists(os.path.join(target, "Slicer")))
        self.assertFalse(os.path.exists(prepared.backup))
        # The new version is moved in but is not complete there (its launcher is a link that only resolves in the
        # staging folder): the helper removes it and puts the previous version back
        other = os.path.join(self.tempDir, "other.tar.gz")
        _fakeLinuxPackage(other, "1.2.0", top="Slicer-new-linux-amd64")
        prepared2 = install.prepareLinux(other, target, "1.2.0")
        launcher = os.path.join(prepared2.staged, "Slicer")
        os.rename(launcher, os.path.join(prepared2.staged, "real-Slicer"))
        os.symlink(os.path.join("..", os.path.basename(prepared2.staged), "real-Slicer"), launcher)
        self.assertTrue(os.path.exists(launcher))
        process = self._sleeper(0.2)
        prepared2.launch(self.stateDir, pid=process.pid, relaunch=True)
        result = install.waitForResult(self.stateDir, 30)
        self.assertIn("restored", result)
        self.assertEqual(install.linuxPackageVersion(target), "0.9.0")
        self.assertTrue(os.path.isfile(os.path.join(target, "Slicer")))
        self.assertFalse(os.path.exists(prepared2.backup))
        # The old version starts again: finishInstall() explains, and the stale staging folder is removed
        install.writeRecord(self.stateDir, {"version": "1.2.0", "target": target, "staged": prepared2.staged, "backup": prepared2.backup,
                                            "log": "install.log"})
        updates.installTargetOverride = target
        service = self._service()
        self.assertFalse(service.finishInstall())
        self.assertEqual(service._finishedMessage[0], "warn")
        self.assertIn("0.9.0 was kept", service._finishedMessage[1])
        self.assertTrue(self._waitFor(lambda: not os.path.exists(install.stagingRoot(target)), 10))
        # The record of another installation is left alone
        install.writeRecord(self.stateDir, {"version": "1.2.0", "target": "/elsewhere/Slicer"})
        self.assertIsNone(service.finishInstall())
        self.assertIsNotNone(install.readRecord(self.stateDir)[0])

    def test_FailedInstallToastKeepsLogPathReachable(self):
        """The sticky toast is one elided line: the failed-install message must carry the full text as its tooltip and
        a "Show log" action, so the log path ("Details: ...") is never only a cut-off tail.
        """
        calls = []

        class FakeShell:
            def toast(self, text, ms=1600, action=None, actionText=None, sticky=False, level="info", toolTip=None):
                calls.append(dict(text=text, action=action, actionText=actionText, sticky=sticky, toolTip=toolTip))

        service = self._service()
        log = os.path.join(self.tempDir, "a-long-folder-name", "StudioUpdate", "install.log")
        service._finishedMessage = ("warn", "The update to 1.2.0 could not be installed. Details: " + log)
        service._finishedLog = log
        updates = self.updates
        original = updates._shell
        updates._shell = FakeShell
        try:
            service._showFinishedMessage()
        finally:
            updates._shell = original
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["sticky"])
        self.assertIn(log, calls[0]["toolTip"])
        self.assertTrue(callable(calls[0]["action"]))
        self.assertTrue(calls[0]["actionText"])

    def test_LinuxFailedInstallRelaunchesKeptVersion(self):
        """Restart now promises a restart: when the helper fails after the application exited, it still starts the kept
        previous version (so the failure message shows at once); when the application never exits it starts nothing.
        """
        install = self.install
        target = self._fakeLinuxInstall("0.9.0")
        relaunched = os.path.join(os.path.dirname(target), "relaunched.txt")
        package = os.path.join(self.tempDir, "new.tar.gz")
        _fakeLinuxPackage(package, "1.2.0")
        # The staged version is damaged: nothing is changed, and the installed version is started again
        prepared = install.prepareLinux(package, target, "1.2.0")
        os.remove(os.path.join(prepared.staged, "Slicer"))
        process = self._sleeper(0.2)
        prepared.launch(self.stateDir, pid=process.pid, relaunch=True)
        self.assertTrue(install.waitForResult(self.stateDir, 30).startswith("failed"))
        self.assertTrue(self._waitFor(lambda: os.path.exists(relaunched), 10), "the kept version is started after a failed install")
        self.assertEqual(install.linuxPackageVersion(target), "0.9.0")
        # The new version is not complete after the swap: the previous one is put back and started
        os.remove(relaunched)
        other = os.path.join(self.tempDir, "other.tar.gz")
        _fakeLinuxPackage(other, "1.2.0", top="Slicer-new-linux-amd64")
        prepared2 = install.prepareLinux(other, target, "1.2.0")
        launcher = os.path.join(prepared2.staged, "Slicer")
        os.rename(launcher, os.path.join(prepared2.staged, "real-Slicer"))
        os.symlink(os.path.join("..", os.path.basename(prepared2.staged), "real-Slicer"), launcher)
        process = self._sleeper(0.2)
        prepared2.launch(self.stateDir, pid=process.pid, relaunch=True)
        self.assertIn("restored", install.waitForResult(self.stateDir, 30))
        self.assertTrue(self._waitFor(lambda: os.path.exists(relaunched), 10), "the restored version is started")
        # relaunch=False (quit without restarting) starts nothing, even after a failure
        os.remove(relaunched)
        prepared3 = install.prepareLinux(package, target, "1.2.0")
        os.remove(os.path.join(prepared3.staged, "Slicer"))
        process = self._sleeper(0.2)
        prepared3.launch(self.stateDir, pid=process.pid, relaunch=False)
        self.assertTrue(install.waitForResult(self.stateDir, 30).startswith("failed"))
        time.sleep(1)
        self.assertFalse(os.path.exists(relaunched))
        # The application does not exit in time: the helper gives up without starting a second instance
        savedScript = install.HELPER_SCRIPT
        self.addCleanup(setattr, install, "HELPER_SCRIPT", savedScript)
        install.HELPER_SCRIPT = savedScript.replace('"$waited" -gt 1200', '"$waited" -gt 1')
        self.assertNotEqual(install.HELPER_SCRIPT, savedScript)
        prepared4 = install.prepareLinux(package, target, "1.2.0")
        process = self._sleeper(6)
        prepared4.launch(self.stateDir, pid=process.pid, relaunch=True)
        self.assertIn("did not exit", install.waitForResult(self.stateDir, 30))
        time.sleep(1)
        self.assertFalse(os.path.exists(relaunched), "nothing is started while the application still runs")
        self.assertEqual(install.linuxPackageVersion(target), "0.9.0")

    def test_RecoveryOfferedAfterUpdate(self):
        """The session closed (with unsaved work) for the update is offered for recovery by the new version."""
        from StudioLib import autosave_io, recovery

        updates = self.updates
        root = os.path.join(self.tempDir, "autosave")
        sessionDir = os.path.join(root, "20261023-120000-1-abcd")
        os.makedirs(os.path.join(sessionDir, "scene-1"))
        with open(os.path.join(sessionDir, "scene-1", "Scene.mrml"), "w") as f:
            f.write("<MRML></MRML>")
        autosave_io.atomicWriteJson(os.path.join(sessionDir, autosave_io.SESSION_FILE), {
            "format": autosave_io.FORMAT, "version": autosave_io.VERSION, "sessionId": "20261023-120000-1-abcd", "state": "closed",
            "unsavedAtExit": True, "presentedCount": 0, "updated": "2026-10-23T12:00:00", "heartbeat": "2026-10-23T12:00:00",
            "slots": [{"name": "scene-1", "state": "current", "unsaved": True, "generation": 1, "nodes": 1, "bytes": 10}]})
        before = next(s for s in recovery.scan(root) if s.path == sessionDir)
        if not before.slots:
            self.skipTest("the session fixture has no recoverable slot in this autosave format")
        self.assertEqual(before.status, "closed-unsaved")
        self.assertFalse(before.presentable())
        service = self._service()
        self.assertTrue(service._offerSessionForRecovery(sessionDir))
        after = next(s for s in recovery.scan(root) if s.path == sessionDir)
        self.assertEqual(after.status, "crashed")
        self.assertTrue(after.presentable())
        # The recovery dialog does not say that Slicer crashed: the user chose to close it for the update
        self.assertTrue(after.closedForUpdate)
        self.assertEqual(after.slots[0].status(), "closed-for-update")
        dialog = recovery.RecoveryDialog()
        try:
            dialog.setSessions([after], startup=True, root=root)
            self.assertIn("closed to install an update", dialog.textLabel.text)
            self.assertNotIn("did not close normally", dialog.textLabel.text)
            self.assertEqual(dialog.list.topLevelItem(0).text(3), "closed for the update")
        finally:
            dialog.close()
            dialog.deleteLater()
        # A crashed session (no update) keeps the old wording
        data = dict(after.data)
        data.pop("closedForUpdate", None)
        autosave_io.atomicWriteJson(os.path.join(sessionDir, autosave_io.SESSION_FILE), data)
        crashed = next(s for s in recovery.scan(root) if s.path == sessionDir)
        self.assertFalse(crashed.closedForUpdate)
        self.assertEqual(crashed.slots[0].status(), "crashed")
        self.assertIn("did not close normally", recovery.RecoveryDialog._introText([crashed], True))

    #
    # (f) macOS helper with fake tools
    #

    def _fakeTools(self):
        tools = os.path.join(self.tempDir, "fake bin")
        os.makedirs(tools)
        log = os.path.join(self.tempDir, "tools.log")
        scripts = {
            "hdiutil": f"""#!/bin/sh
echo "hdiutil $*" >> "{log}"
if [ "$1" = attach ]; then
  mp=""; prev=""
  for a in "$@"; do [ "$prev" = "-mountpoint" ] && mp="$a"; prev="$a"; last="$a"; done
  cp -R "$last"/. "$mp"/
elif [ "$1" = detach ]; then
  for a in "$@"; do last="$a"; done
  find "$last" -mindepth 1 -delete
fi
""",
            "ditto": f'#!/bin/sh\necho "ditto $*" >> "{log}"\ncp -R "$1" "$2"\n',
            "xattr": f'#!/bin/sh\necho "xattr $*" >> "{log}"\n',
            "open": f'#!/bin/sh\necho "open $*" >> "{log}"\n',
        }
        for name, text in scripts.items():
            path = os.path.join(tools, name)
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o755)
        env = dict(os.environ, PATH=tools + os.pathsep + os.environ.get("PATH", ""))
        return env, log

    def _fakeApp(self, path, version, identifier="org.slicer.slicerstudio"):
        os.makedirs(os.path.join(path, "Contents", "MacOS"))
        with open(os.path.join(path, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": identifier, "CFBundleShortVersionString": version}, f)
        with open(os.path.join(path, "Contents", "MacOS", "Slicer"), "w") as f:
            f.write(version)

    def test_MacOSHelperMocked(self):
        install = self.install
        env, log = self._fakeTools()
        applications = os.path.join(self.tempDir, "Applications")
        target = os.path.join(applications, "Slicer Studio.app")
        self._fakeApp(target, "0.9.0")
        self.assertEqual(install.installTarget("macos-arm64", os.path.join(target, "Contents")), os.path.realpath(target))
        dmg = os.path.join(self.tempDir, "SlicerStudio-1.2.0-macos-arm64.dmg")  # a folder stands for the image
        self._fakeApp(os.path.join(dmg, "Slicer Studio.app"), "1.2.0")
        os.symlink("/Applications", os.path.join(dmg, "Applications"))
        with self.assertRaisesRegex(install.InstallError, "not 1.3.0"):
            install.prepareMacOS(dmg, target, "1.3.0", env=env)
        prepared = install.prepareMacOS(dmg, target, "1.2.0", env=env)
        self.assertFalse(prepared.needsAdmin)
        with open(log) as f:
            calls = f.read()
        self.assertIn("hdiutil attach -nobrowse -readonly", calls)
        self.assertIn("hdiutil detach", calls)
        self.assertIn("ditto", calls)
        process = self._sleeper(0.3)
        prepared.launch(self.stateDir, pid=process.pid, relaunch=True, env=env)
        self.assertEqual(install.waitForResult(self.stateDir, 30), "ok")
        with open(os.path.join(target, "Contents", "Info.plist"), "rb") as f:
            self.assertEqual(plistlib.load(f)["CFBundleShortVersionString"], "1.2.0")
        with open(os.path.join(prepared.backup, "Contents", "Info.plist"), "rb") as f:
            self.assertEqual(plistlib.load(f)["CFBundleShortVersionString"], "0.9.0")
        self.assertTrue(self._waitFor(lambda: "open -n" in open(log).read(), 10))
        with open(log) as f:
            calls = f.read()
        self.assertIn(f"xattr -dr com.apple.quarantine {target}", calls)
        self.assertIn(f"open -n {target}", calls)
        # Another application in the image is refused
        other = os.path.join(self.tempDir, "Other.dmg")
        self._fakeApp(os.path.join(other, "3D Slicer 5.13.app"), "1.2.0", identifier="org.slicer.slicer")
        with self.assertRaisesRegex(install.InstallError, "another application"):
            install.prepareMacOS(other, target, "1.2.0", env=env)
        # Running from a translocated location is refused (with the manual steps)
        translocated = os.path.join(self.tempDir, "AppTranslocation", "X", "Slicer Studio.app")
        self._fakeApp(translocated, "0.9.0")
        with self.assertRaises(install.InstallError) as context:
            install.prepareMacOS(dmg, translocated, "1.2.0", env=env)
        self.assertIn("drag", context.exception.manualSteps)
        # Administrator rights: the helper command goes through osascript (not run here)
        popenCalls = []

        class _FakeProcess:
            returncode = 0

            def communicate(self):
                return b"", b""

            def wait(self):
                return 0

        prepared.needsAdmin = True
        prepared.launched = False
        prepared.launch(self.stateDir, pid=1, relaunch=True, env=env, popen=lambda args, **kw: popenCalls.append(args) or _FakeProcess())
        self.assertEqual(popenCalls[0][0], "osascript")
        self.assertIn("with administrator privileges", popenCalls[0][2])
        # As root, the helper removes the previous version itself (keepBackup 0); the log stays this user's file
        self.assertIn(" 0 >> ", popenCalls[0][2])
        self.assertTrue(os.path.isfile(os.path.join(self.stateDir, install.LOG_FILE)))

    #
    # (g) startup decisions
    #

    def _startup(self, service, timeoutS=5):
        called = []
        service._then = lambda: called.append(time.monotonic())
        service._startupCheck(timeoutS)
        return called

    def test_StartupNoUpdate(self):
        self._publish("0.9.0")
        service = self._service()
        called = self._startup(service)
        self.assertTrue(self._waitFor(lambda: called, 10))
        self.assertEqual(service.status.kind, "none")
        self.assertIsNone(service.requiredDialog)
        self.assertIsNone(service.banner)
        self.assertLess(service.startupWaitMs, 5000)
        self.assertTrue(self.updates.settingsValue("LastCheck"))
        self.assertEqual(self.updates.cachedManifest().version, "0.9.0")

    def test_StartupOptional(self):
        self._publish("1.2.0")
        service = self._service()
        called = self._startup(service)
        self.assertTrue(self._waitFor(lambda: called, 10))
        self.assertEqual(service.status.kind, "optional")
        self.assertIsNone(service.requiredDialog)
        # Skip: not offered again for that version
        service.skip()
        self.assertEqual(self.updates.settingsValue("SkippedVersion"), "1.2.0")
        service2 = self._service()
        self.assertTrue(service2.status.skipped)

    def test_StartupRequired(self):
        self._publish("1.2.1", required=True, notes="## Required\n\nFix.")
        self.updates._installableOverride = True
        service = self._service()
        called = self._startup(service)
        self.assertTrue(self._waitFor(lambda: service.requiredDialog is not None and service.requiredDialog.isVisible(), 10))
        self.assertEqual(called, [], "the startup (autosave, recovery offer) does not go on")
        dialog = service.requiredDialog
        self.assertEqual(dialog.dialog.objectName, "StudioRequiredUpdateDialog")
        self.assertTrue(dialog.dialog.isModal())
        buttons = [b.text for b in dialog.dialog.findChildren("QPushButton") if b.isVisible()]
        self.assertEqual(sorted(buttons), ["Quit", "Update now"])
        # Esc and the window's close button do not dismiss it
        qt.QApplication.sendEvent(dialog.dialog, qt.QKeyEvent(qt.QEvent.KeyPress, qt.Qt.Key_Escape, qt.Qt.NoModifier))
        dialog.dialog.close()
        self._pump(0.2)
        self.assertTrue(dialog.isVisible())
        # reject() and done() (no user path today) show it again
        dialog.dialog.reject()
        self._pump(0.2)
        self.assertTrue(dialog.isVisible())
        dialog.dialog.done(0)
        self._pump(0.2)
        self.assertTrue(dialog.isVisible())
        # Quit closes the application (recorded here instead)
        closes = []
        dialog._closeApplication = lambda: closes.append(True)
        dialog.quitButton.click()
        self.assertEqual(closes, [True])
        self.assertIsNone(service.prepared)

    def test_DialogChrome(self):
        """VISUAL 4.7: the required-update dialog and the update progress dialog have the Studio dialog chrome, a 40 px icon
        tile beside an 18/600 title, one primary button, and Markdown headings of the notes do not outgrow the title.
        """
        from StudioLib import ui

        self._publish("1.2.1", required=True, notes="# Release\n\n## What's new\n\n- Fix.")
        self.updates._installableOverride = True
        service = self._service()
        called = self._startup(service)
        self.assertTrue(self._waitFor(lambda: service.requiredDialog is not None and service.requiredDialog.isVisible(), 10))
        dialog = service.requiredDialog
        qd = dialog.dialog
        optional = self.updates.UpdateDialog(service, qd.parentWidget())
        self.addCleanup(optional.dispose)
        for chromed in (qd, optional.dialog):
            self.assertTrue(chromed.property("studioDialogChrome"), chromed.objectName)
            self.assertTrue(chromed.windowFlags() & qt.Qt.FramelessWindowHint, chromed.objectName)
            tile = [w for w in chromed.findChildren("QLabel") if w.objectName.endswith("Icon")]
            self.assertEqual(len(tile), 1, chromed.objectName)
            self.assertEqual((tile[0].width, tile[0].height), (40, 40))
            self.assertEqual(tile[0].property("slicerRole"), "iconTile")
        title = qd.findChild(qt.QLabel, "StudioRequiredUpdateTitle")
        self.assertEqual(title.font.pixelSize(), ui.fontPx(18))
        self.assertEqual(dialog.updateButton.property("slicerRole"), "primary")
        # The notes: no heading is larger than the title of the dialog
        notes = qd.findChild(qt.QTextBrowser, "StudioUpdateNotes")
        block = notes.document.begin()
        headings = 0
        while block.isValid():
            if block.blockFormat().headingLevel() > 0:
                headings += 1
                cursor = qt.QTextCursor(block)
                cursor.movePosition(qt.QTextCursor.NextCharacter)
                self.assertLessEqual(cursor.charFormat().font().pointSizeF(), notes.font.pointSizeF() + 0.01, block.text())
            block = block.next()
        self.assertEqual(headings, 2)
        # Esc and close stay filtered with the frameless window, and it comes back
        qt.QApplication.sendEvent(qd, qt.QKeyEvent(qt.QEvent.KeyPress, qt.Qt.Key_Escape, qt.Qt.NoModifier))
        qd.close()
        self._pump(0.2)
        self.assertTrue(dialog.isVisible())
        self.assertEqual(called, [])

    def test_UpdateDialogFitsStatusText(self):
        """The update progress dialog grows with its status text: the ready text, a failure reason and the manual-install
        steps are not cut under the buttons (the dialog is created with a one-line text).
        """
        import types

        service = types.SimpleNamespace(status=types.SimpleNamespace(version="1.2.0"), unavailableReason=lambda: None)
        dialog = self.updates.UpdateDialog(service, slicer.util.mainWindow())
        self.addCleanup(dialog.dispose)
        dialog.show()
        self._pump(0.3)
        cases = {
            "ready": (types.SimpleNamespace(version="1.2.0"), None),
            "missing file": (None, self.install.InstallError(
                "The release 1.2.0 has no downloadable file Slicer-5.13.0-studio1.2.0-linux-amd64.tar.gz yet "
                "(404: http://127.0.0.1:18811/missingasset/Slicer-5.13.0-studio1.2.0-linux-amd64.tar.gz).")),
            "manual steps": (None, self.install.InstallError(
                "The downloaded package is Slicer Studio 1.1.0, not 1.2.0 as the release says.",
                manualSteps="To install it by hand, extract Slicer-5.13.0-studio1.2.0-linux-amd64.tar.gz and replace the folder "
                            "/opt/some place/Slicer-5.13.0-linux-amd64 with the extracted folder.")),
            "downloading": None,
        }
        for name, case in cases.items():
            if case is None:
                dialog._setState("downloading", "Downloading…")
            else:
                dialog._onJobDone(*case)
            self._pump(0.3)
            text = dialog.text
            needed = text.heightForWidth(text.width)
            self.assertGreaterEqual(text.height, needed, f"{name}: the status text is cut ({text.height} px of {needed} px)")
            textBottom = text.mapTo(dialog.dialog, qt.QPoint(0, 0)).y() + needed
            buttonTop = dialog.secondaryButton.mapTo(dialog.dialog, qt.QPoint(0, 0)).y()
            self.assertLessEqual(textBottom, buttonTop, f"{name}: the status text runs under the buttons")
            self.assertGreaterEqual(dialog.dialog.width, 460, name)
        # Back to a short text: the dialog does not keep the empty space of a long failure
        self.assertLess(dialog.dialog.height, 240, "the dialog shrinks again for a one-line status")

    def test_StartupMinimumSupported(self):
        self._publish("1.3.0", minimum="1.0.0")
        self.updates._installableOverride = True
        service = self._service()
        called = self._startup(service)
        self.assertTrue(self._waitFor(lambda: service.requiredDialog is not None, 10))
        self.assertTrue(service.status.belowMinimum)
        self.assertIn("no longer supported", service.requiredDialog.text.text)
        self.assertEqual(called, [])

    def test_StartupRequiredNotInstallable(self):
        """A development build (or the web version) shows a warning only, and the startup goes on."""
        self._publish("1.2.1", required=True)
        self.updates._installableOverride = False
        service = self._service()
        called = self._startup(service)
        self.assertTrue(self._waitFor(lambda: called, 10))
        self.assertIsNone(service.requiredDialog)
        self.assertIsNotNone(service.banner)
        self.assertEqual(service.banner[0].objectName, "StudioRequiredUpdateBanner")
        self.assertFalse(service.banner[0].isModal())
        service.hideBanner()

    def test_StartupRequiredNoFileForThisComputer(self):
        """A required release without a file for this computer never blocks: a warning, and the startup goes on."""
        self._publish("1.2.1", required=True, package=False)
        self.updates._installableOverride = True
        service = self._service()
        called = self._startup(service)
        self.assertTrue(self._waitFor(lambda: called, 10), "the startup goes on")
        self.assertEqual(service.status.kind, "required")
        self.assertIsNone(service.requiredDialog)
        self.assertFalse(service.canInstallRelease())
        self.assertIsNotNone(service.banner)
        self.assertIn("no file for this computer", self._bannerText(service))
        self.assertIn("no file for this computer", service.unavailableReason())
        service.hideBanner()
        # Update now (Settings, Search) explains it instead of failing in the download
        dialog = service.updateNow()
        self.assertEqual(dialog.state, "unavailable")
        self.assertIsNone(service.job)

    def test_UpdateDialogFromModalSettingsIsUsable(self):
        """Settings runs its own event loop (application-modal), and Qt ignores the clicks on every window that is not
        Settings or its child. Update now in Settings > Updates therefore opens the update dialog as a child of
        Settings, and it goes back to the main window (open) when Settings closes.
        """
        updates = self.updates
        self._publish("1.2.0")
        updates._installableOverride = False  # no download: the dialog says why (any state shows the same window rules)
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        mainWindow = slicer.util.mainWindow()
        settings = slicer.app.settingsDialog()
        self.assertIsNotNone(settings)
        seen = {}

        def probe():
            try:
                seen["modal"] = slicer.app.activeModalWidget() == settings
                dialog = service.updateNow()
                seen["dialog"] = dialog
                seen["visible"] = dialog.dialog.isVisible()
                seen["child"] = dialog.dialog.parentWidget() == settings
                # Reused while Settings is still open: the same window, still usable
                seen["again"] = service.updateNow() is dialog
                seen["childAgain"] = dialog.dialog.parentWidget() == settings
            except Exception as error:  # a failure in a timer callback is not reported by the test
                seen["error"] = repr(error)
            finally:
                settings.reject()

        qt.QTimer.singleShot(300, probe)
        slicer.app.openSettingsDialog()
        self.assertNotIn("error", seen, seen.get("error"))
        self.assertTrue(seen["modal"], "Settings is the application-modal window")
        self.assertTrue(seen["visible"])
        self.assertTrue(seen["child"], "the update dialog is a child of the modal Settings dialog, so it receives clicks")
        self.assertTrue(seen["again"])
        self.assertTrue(seen["childAgain"])
        # Settings closed: the update dialog is back over the main window and open
        dialog = seen["dialog"]
        self.assertTrue(self._waitFor(dialog.dialog.isVisible, 5), "still open after Settings closes")
        self.assertTrue(dialog.dialog.parentWidget() == mainWindow)
        # Outside Settings it stays a child of the main window; closing it by the user keeps it closed
        self.assertIs(service.updateNow(), dialog)
        self.assertTrue(dialog.dialog.parentWidget() == mainWindow)
        dialog.secondaryButton.click()
        self.assertFalse(dialog.dialog.isVisible())
        self.assertFalse(dialog.wanted)

    def test_ClassicRequiredBoxFromModalSettingsIsUsable(self):
        """Classic: the 'Update required' box shown while the application-modal Settings is open is a child of Settings
        (Qt ignores clicks on any other window), and goes back to the main window, still visible, when Settings closes.
        """
        updates = self.updates
        self._publish("1.2.1", required=True)
        updates._installableOverride = True
        saved = updates._shell
        updates._shell = lambda: None
        self.addCleanup(setattr, updates, "_shell", saved)
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        mainWindow = slicer.util.mainWindow()
        settings = slicer.app.settingsDialog()
        seen = {}

        def probe():
            try:
                seen["modal"] = slicer.app.activeModalWidget() == settings
                box = service.showBanner()
                seen["box"] = box
                seen["visible"] = box.isVisible()
                seen["child"] = box.parentWidget() == settings
            except Exception as error:
                seen["error"] = repr(error)
            finally:
                settings.reject()

        qt.QTimer.singleShot(300, probe)
        slicer.app.openSettingsDialog()
        self.assertNotIn("error", seen, seen.get("error"))
        self.assertTrue(seen["modal"])
        self.assertTrue(seen["visible"])
        self.assertTrue(seen["child"], "the box is a child of the modal Settings dialog, so it receives clicks")
        box = seen["box"]
        self.assertTrue(self._waitFor(lambda: box.parentWidget() == mainWindow and box.isVisible(), 5))
        service.hideBanner()

    def test_ClassicRequiredBoxDismissedInSettingsStaysClosed(self):
        """Classic: the 'Update required' box dismissed with OK while Settings is open does not come back when Settings
        closes (it is no longer the banner).
        """
        updates = self.updates
        self._publish("1.2.1", required=True)
        updates._installableOverride = True
        saved = updates._shell
        updates._shell = lambda: None
        self.addCleanup(setattr, updates, "_shell", saved)
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        settings = slicer.app.settingsDialog()
        seen = {}

        def probe():
            try:
                box = service.showBanner()
                seen["box"] = box
                seen["visibleBefore"] = box.isVisible()
                box.button(qt.QMessageBox.Ok).click()
                slicer.app.processEvents()
                seen["visibleAfterOK"] = box.isVisible()
                seen["bannerAfterOK"] = service.banner
            except Exception as error:
                seen["error"] = repr(error)
            finally:
                settings.reject()

        qt.QTimer.singleShot(300, probe)
        slicer.app.openSettingsDialog()
        self.assertNotIn("error", seen, seen.get("error"))
        self.assertTrue(seen["visibleBefore"])
        self.assertFalse(seen["visibleAfterOK"], "OK closes the box")
        self.assertIsNone(seen["bannerAfterOK"], "a dismissed box is no longer the banner")
        box = seen["box"]
        for _i in range(10):
            slicer.app.processEvents()
            self.assertFalse(updates._isAlive(box) and box.isVisible(), "the dismissed box does not come back after Settings closes")
            time.sleep(0.05)
        self.assertIsNone(service.banner)

    def test_RequiredFoundOnline(self):
        """A required release that can be installed but is shown as a warning (found after the startup, or cached while
        automatic checks are off) offers Update now, not 'Connect to the internet'.
        """
        updates = self.updates
        updates._installableOverride = True
        service = self._service()
        service.status = updates.evaluate(updates.parseManifest(self._publish("1.2.1", required=True)), "0.9.0")
        service._showAvailability(startup=False)
        self.assertIsNotNone(service.banner)
        text = self._bannerText(service)
        self.assertNotIn("Connect to the internet", text)
        self.assertIn("Update now", [b.text for b in service.banner[0].findChildren("QAbstractButton")])
        service.hideBanner()
        # After a failed check it says so
        service.showBanner(offline=True)
        self.assertIn("Connect to the internet", self._bannerText(service))
        service.hideBanner()

    def test_RequiredBannerUpdatedByCheckAgain(self):
        """The offline banner of a cached required release ("Connect to the internet and check again") is rebuilt by
        a successful check (Check again): "is required (you have X)" with Update now. A release that is no longer
        required takes the banner away.
        """
        updates = self.updates
        cached = updates.parseManifest(self._publish("1.2.1", required=True))
        updates.setSettingsValue("LastManifest", cached.toJson())
        os.environ[updates.FEED_ENVIRONMENT_VARIABLE] = "http://127.0.0.1:9/studio-update.json"  # nothing listens
        updates._installableOverride = True
        service = self._service()
        called = self._startup(service)
        self.assertTrue(self._waitFor(lambda: called, 10))
        self.assertIn("Connect to the internet", self._bannerText(service))
        # The feed answers now: Check again
        os.environ[updates.FEED_ENVIRONMENT_VARIABLE] = self.feed
        done = []
        self.assertTrue(service.check(onDone=lambda status, error: done.append(error)))
        self.assertTrue(self._waitFor(lambda: done, 10))
        self.assertIsNone(service.lastError)
        self.assertIsNotNone(service.banner)
        text = self._bannerText(service)
        self.assertNotIn("Connect to the internet", text)
        self.assertIn("is required (you have", text)
        self.assertIsNone(service.requiredDialog, "found after the startup: a warning, not a blocking dialog")
        self.assertIn("Update now", [b.text for b in service.banner[0].findChildren("QAbstractButton")])
        # The release is not required any more: no banner that says "required"
        self._publish("0.9.0")
        service.checkNow()
        self.assertTrue(self._waitFor(lambda: not service.checking, 10))
        self.assertEqual(service.status.kind, "none")
        self.assertIsNone(service.banner)

    def test_RequiredBannerGoesWhenLatestIsOptional(self):
        """A later check that finds an optional release (or one that was skipped) removes the banner of the earlier
        required result: Settings and the notice say 1.3.0 is available, so a banner about 1.2.1 contradicts them.
        """
        updates = self.updates
        self._publish("1.2.1", required=True)
        updates._installableOverride = False  # a development build: the banner says it is not updated
        service = self._service()
        called = self._startup(service)
        self.assertTrue(self._waitFor(lambda: called, 10))
        self.assertIsNotNone(service.banner)
        self.assertIn("1.2.1", self._bannerText(service))
        # The latest release is optional now
        self._publish("1.3.0")
        service.checkNow()
        self.assertTrue(self._waitFor(lambda: not service.checking, 10))
        self.assertEqual(service.status.kind, "optional")
        self.assertEqual(service.status.version, "1.3.0")
        self.assertIsNone(service.banner, "the banner of a release that is no longer required goes")
        # A skipped optional release takes the other branch: the banner goes there too
        self._publish("1.2.1", required=True)
        service.checkNow()
        self.assertTrue(self._waitFor(lambda: not service.checking, 10))
        self.assertIsNotNone(service.banner)
        self._publish("1.3.0")
        updates.setSettingsValue("SkippedVersion", "1.3.0")
        service.checkNow()
        self.assertTrue(self._waitFor(lambda: not service.checking, 10))
        self.assertTrue(service.status.skipped)
        self.assertIsNone(service.banner)
        service.hideNotice()

    def _bannerText(self, service):
        widget = service.banner[0]
        if widget.metaObject().className() == "QMessageBox":
            return widget.text
        return " ".join(label.text for label in widget.findChildren("QLabel"))

    def test_TourOfferHeldDuringStartupCheck(self):
        """The first-start tour offer waits for the startup check (a required dialog is never shown over it)."""
        from StudioLib import tour

        self._publish("1.2.0")
        scheduled = []

        class FakeTour:
            def __init__(self):
                self._offerTimer = qt.QTimer()
                self._offerTimer.setSingleShot(True)
                self._offerTimer.start(60000)

            def scheduleOffer(self):
                scheduled.append(True)

        fake = FakeTour()
        savedPlugin, savedOffered = tour.plugin, tour.isOffered
        tour.plugin, tour.isOffered = (lambda: fake), (lambda: False)
        try:
            _Handler.slowPaths = {"/studio-update.json"}
            service = self._service()
            called = []
            service._then = lambda: called.append(True)
            service._holdTourOffer()
            service._startupCheck(5)
            self._pump(0.3)
            self.assertFalse(fake._offerTimer.isActive(), "held while the check runs")
            self.assertEqual(scheduled, [])
            self.assertTrue(self._waitFor(lambda: called, 10))
            self.assertEqual(scheduled, [True], "offered again once the startup goes on")
            self.assertIsNone(service._tourHoldTimer)
        finally:
            tour.plugin, tour.isOffered = savedPlugin, savedOffered
            fake._offerTimer.stop()

    def test_StartupOfflineCachedRequired(self):
        updates = self.updates
        cached = updates.parseManifest(self._publish("1.2.1", required=True))
        updates.setSettingsValue("LastManifest", cached.toJson())
        os.environ[updates.FEED_ENVIRONMENT_VARIABLE] = "http://127.0.0.1:9/studio-update.json"  # nothing listens
        updates._installableOverride = True
        service = self._service()
        self.assertEqual(service.status.kind, "required")
        called = self._startup(service)
        self.assertTrue(self._waitFor(lambda: called, 10))
        self.assertIsNone(service.requiredDialog, "offline: no blocking dialog")
        self.assertIsNotNone(service.banner)
        self.assertIsNotNone(service.lastError)
        self.assertIn("Connect to the internet", self._bannerText(service))
        service.hideBanner()

    def test_StartupSlowServer(self):
        """The startup goes on after the timeout; the late answer is still used."""
        self._publish("1.2.0")
        _Handler.slowPaths = {"/studio-update.json"}
        service = self._service()
        called = self._startup(service, timeoutS=1)
        self.assertTrue(self._waitFor(lambda: called, 10))
        self.assertLess(service.startupWaitMs, 2500)
        self.assertTrue(self._waitFor(lambda: not service.checking, 15))
        self.assertEqual(service.status.kind, "optional")

    #
    # (h) Update now
    #

    def test_UpdateNowUpToInstall(self):
        updates, install = self.updates, self.install
        self._publish("1.2.0")
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        dialog = service.updateNow()
        self.assertEqual(dialog.dialog.objectName, "StudioUpdateDialog")
        self.assertFalse(dialog.dialog.isModal(), "an optional update never blocks")
        self.assertTrue(self._waitFor(lambda: dialog.state in ("ready", "failed"), 30), dialog.state)
        self.assertEqual(dialog.state, "ready", dialog.text.text)
        self.assertEqual(install.linuxPackageVersion(service.prepared.staged), "1.2.0")
        self.assertEqual(install.linuxPackageVersion(target), "0.9.0", "nothing installed before the restart")
        # "Install when I quit" registers the swap for the exit; nothing is started now
        dialog.secondaryButton.click()
        self.assertTrue(service._quitConnected)
        self.assertFalse(service.prepared.launched)
        self.assertFalse(service._relaunch)

    def test_UpdateNowAfterNewerReleaseReplacesPrepared(self):
        """1.2.0 is prepared (Install when I quit), then a later check finds 1.3.0: Update now acts on 1.3.0, never on
        the prepared 1.2.0 (which is dropped with its Install when I quit).
        """
        updates, install = self.updates, self.install
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        old = self._publish("1.2.0")
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        dialog = service.updateNow()
        self.assertTrue(self._waitFor(lambda: dialog.state in ("ready", "failed"), 30), dialog.state)
        self.assertEqual(dialog.state, "ready", dialog.text.text)
        dialog.secondaryButton.click()  # Install when I quit
        self.assertTrue(service._quitConnected)
        oldPackage = os.path.join(updates.downloadDirectory(), old["assets"]["linux-x86_64"]["name"])
        self.assertTrue(os.path.isfile(oldPackage))
        # A later check finds 1.3.0
        new = self._publish("1.3.0")
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        dialog2 = service.updateNow()
        self.assertEqual(dialog2.title.text, "Slicer Studio 1.3.0")
        self.assertNotEqual(dialog2.state, "ready", "the prepared 1.2.0 is not offered for 1.3.0")
        self.assertIsNotNone(service.job, "1.3.0 is downloaded")
        self.assertFalse(service._quitConnected, "1.2.0 no longer installs when the application quits")
        self.assertFalse(os.path.exists(oldPackage), "the download of 1.2.0 is removed")
        self.assertTrue(self._waitFor(lambda: dialog2.state in ("ready", "failed"), 30), dialog2.state)
        self.assertEqual(dialog2.state, "ready", dialog2.text.text)
        self.assertIn("1.3.0", dialog2.text.text)
        self.assertEqual(service.prepared.version, "1.3.0")
        self.assertEqual(install.linuxPackageVersion(service.prepared.staged), "1.3.0")
        self.assertEqual(sorted(os.listdir(updates.downloadDirectory())), [new["assets"]["linux-x86_64"]["name"]])
        self.assertEqual(install.linuxPackageVersion(target), "0.9.0", "nothing installed before the restart")
        # Once the install helper of a prepared update runs (administrator rights given) it cannot be taken back:
        # Update now says which version installs instead of downloading the newer one
        service.prepared.launched = True
        self._publish("1.4.0")
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        dialog3 = service.updateNow()
        self.assertIsNone(service.job)
        self.assertEqual(service.prepared.version, "1.3.0")
        self.assertEqual(dialog3.state, "ready")
        self.assertEqual(dialog3.title.text, "Slicer Studio 1.3.0")
        self.assertIn("1.4.0", dialog3.text.text)

    def test_UpdateNowAfterNewerReleaseReplacesRunningDownload(self):
        """1.2.0 downloads, then a later check finds 1.3.0: Update now (Update to 1.3.0) stops the 1.2.0 download and
        downloads, checks and prepares 1.3.0; it never prepares 1.2.0.
        """
        updates, install = self.updates, self.install
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        old = self._publish("1.2.0")
        oldPath = "/" + old["assets"]["linux-x86_64"]["name"]
        _Handler.stallPaths = {oldPath}  # the 1.2.0 download is still running when 1.3.0 is found
        self.addCleanup(_Handler.stallRelease.set)
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        dialog = service.updateNow()
        self.assertEqual(dialog.state, "downloading")
        oldJob = service.job
        self.assertIsNotNone(oldJob)
        self.assertTrue(self._waitFor(lambda: oldJob.progress[0] >= 100, 10), "the 1.2.0 download has started")
        # A later check finds 1.3.0 while 1.2.0 downloads
        new = self._publish("1.3.0")
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        self.assertEqual(service.status.version, "1.3.0")
        dialog2 = service.updateNow()
        self.assertIs(dialog2, dialog)
        self.assertEqual(dialog2.title.text, "Slicer Studio 1.3.0")
        self.assertTrue(oldJob.cancelled(), "the 1.2.0 download is stopped")
        self.assertTrue(self._waitFor(lambda: service.job is not None and service.job is not oldJob, 15),
                        "1.3.0 is downloaded once the 1.2.0 download has stopped")
        self.assertEqual(service.job.version, "1.3.0")
        self.assertTrue(self._waitFor(lambda: dialog2.state in ("ready", "failed"), 30), dialog2.state)
        self.assertEqual(dialog2.state, "ready", dialog2.text.text)
        self.assertIn("1.3.0", dialog2.text.text)
        self.assertNotIn("1.2.0", dialog2.text.text)
        self.assertEqual(service.prepared.version, "1.3.0")
        self.assertEqual(install.linuxPackageVersion(service.prepared.staged), "1.3.0")
        self.assertEqual(sorted(os.listdir(updates.downloadDirectory())), [new["assets"]["linux-x86_64"]["name"]],
                         "the partial 1.2.0 download is removed")
        self.assertEqual(install.linuxPackageVersion(target), "0.9.0", "nothing installed before the restart")
        # Update now again for the same version only shows the dialog (no new download)
        self.assertIs(service.updateNow(), dialog2)
        self.assertIsNone(service.job)
        self.assertEqual(dialog2.state, "ready")

    def test_CancelWhileReplacingRunningDownloadStartsNothing(self):
        """Cancel pressed while Update now stops an older download for a newer one: nothing is downloaded after it."""
        updates = self.updates
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        old = self._publish("1.2.0")
        _Handler.stallPaths = {"/" + old["assets"]["linux-x86_64"]["name"]}
        self.addCleanup(_Handler.stallRelease.set)
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        dialog = service.updateNow()
        oldJob = service.job
        self._publish("1.3.0")
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        service.updateNow()
        self.assertEqual(dialog.state, "downloading")
        dialog.secondaryButton.click()  # Cancel
        self.assertTrue(self._waitFor(lambda: dialog.state != "downloading", 15), dialog.state)
        self._pump(0.5)
        self.assertIsNone(service.job, "no download starts after Cancel")
        self.assertIsNone(service.prepared)
        self.assertTrue(oldJob.cancelled())
        self.assertEqual(dialog.state, "failed")
        self.assertIn("cancelled", dialog.text.text)

    def _prepareStaged(self, target):
        """A prepared update as the service leaves it: the staged copy next to the installation and the package in the
        download folder (nothing is installed).
        """
        downloads = self.updates.downloadDirectory()
        os.makedirs(downloads, exist_ok=True)
        package = os.path.join(downloads, "new.tar.gz")
        _fakeLinuxPackage(package, "1.2.0")
        return self.install.prepareLinux(package, target, "1.2.0")

    def test_AbandonedPreparedUpdateRemovedAtStart(self):
        updates, install = self.updates, self.install
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        prepared = self._prepareStaged(target)
        root = install.stagingRoot(target)
        downloads = updates.downloadDirectory()
        self.assertTrue(os.path.isdir(root))
        self.assertEqual(os.listdir(downloads), ["new.tar.gz"])
        # The previous run quit (or crashed) while the update was ready and not chosen: no install record exists.
        # Another Slicer Studio that is still running may install what it prepared: it is left alone
        other = self._sleeper(30)
        self.addCleanup(other.kill)
        with open(os.path.join(self.stateDir, updates.STAGED_MARKER), "w") as f:
            json.dump({"pid": other.pid, "root": root}, f)
        service = self._service()
        self.assertIsNone(service.finishInstall())
        self._pump(0.5)
        self.assertTrue(os.path.isdir(root))
        self.assertEqual(os.listdir(downloads), ["new.tar.gz"])
        other.kill()
        other.wait()
        self.assertIsNone(service.finishInstall())
        self.assertTrue(self._waitFor(lambda: not os.path.exists(root) and not os.listdir(downloads), 10), os.listdir(os.path.dirname(target)))
        self.assertFalse(os.path.exists(os.path.join(self.stateDir, updates.STAGED_MARKER)))
        self.assertEqual(install.linuxPackageVersion(target), "0.9.0", "the installation is untouched")
        self.assertEqual(sorted(os.listdir(os.path.dirname(target))), [os.path.basename(target)], "nothing is left next to the installation")
        # Without a marker (the run died before writing it, or an older version) the same cleanup applies
        prepared = self._prepareStaged(target)
        self.assertIsNone(service.finishInstall())
        self.assertTrue(self._waitFor(lambda: not os.path.exists(root) and not os.listdir(downloads), 10))

    def test_SecondInstanceLeavesRunningDownloadAlone(self):
        """A second Slicer Studio that starts (same state folder) while the first one downloads must not delete the
        download (the first run would fail with a missing file).
        """
        updates, install = self.updates, self.install
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        data = self._publish("1.2.0")
        _Handler.stallPaths = {"/" + data["assets"]["linux-x86_64"]["name"]}
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        dialog = service.updateNow()
        downloads = updates.downloadDirectory()
        part = os.path.join(downloads, data["assets"]["linux-x86_64"]["name"] + ".part")
        self.assertTrue(self._waitFor(lambda: os.path.isfile(part), 10), "the download is running")
        markerPath = os.path.join(self.stateDir, updates.STAGED_MARKER)
        self.assertTrue(os.path.isfile(markerPath), "the running download is marked with the pid of its run")
        # The marker as the other (first) instance wrote it: a live process that is not this one
        other = self._sleeper(60)
        self.addCleanup(other.kill)
        with open(markerPath) as f:
            marker = json.load(f)
        self.assertEqual(marker["pid"], os.getpid())
        marker["pid"] = other.pid
        with open(markerPath, "w") as f:
            json.dump(marker, f)
        second = self._service()
        self.assertIsNone(second.finishInstall())
        self._pump(0.5)
        self.assertTrue(os.path.isfile(part), "the second instance left the running download alone")
        marker["pid"] = os.getpid()
        with open(markerPath, "w") as f:
            json.dump(marker, f)
        # The stalled server then ends its short reply: the download is refused for its size, not for a missing file
        _Handler.stallRelease.set()
        self.assertTrue(self._waitFor(lambda: dialog.state in ("ready", "failed"), 30), dialog.state)
        self.assertEqual(dialog.state, "failed")
        self.assertIn("wrong size", dialog.text.text)
        self.assertNotIn("No such file", dialog.text.text)
        other.kill()

    def test_SecondWindowLeavesUpdateChosenInFirstWindowAlone(self):
        """Another running Slicer Studio of the same user has prepared the update (staged copy next to the installation,
        package in the download folder): a second window that also asks for the update must not replace it, and its
        exit must not delete it (the first window chose Install when I quit).
        """
        updates, install = self.updates, self.install
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        self._publish("1.2.0")
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        root = install.stagingRoot(target)
        first = self._service()
        first.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        dialog = first.updateNow()
        self.assertTrue(self._waitFor(lambda: dialog.state in ("ready", "failed"), 30), dialog.state)
        self.assertEqual(dialog.state, "ready", dialog.text.text)
        dialog.secondaryButton.click()  # Install when I quit
        self.assertTrue(first._quitConnected)
        downloads = updates.downloadDirectory()
        packages = os.listdir(downloads)
        # The marker as the first window (another process) wrote it
        other = self._sleeper(60)
        self.addCleanup(other.kill)
        markerPath = os.path.join(self.stateDir, updates.STAGED_MARKER)
        with open(markerPath) as f:
            marker = json.load(f)
        marker["pid"] = other.pid
        with open(markerPath, "w") as f:
            json.dump(marker, f)
        staged = os.stat(root).st_ino
        second = self._service()
        second.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        again = second.updateNow()
        self.assertTrue(self._waitFor(lambda: again.state in ("ready", "failed"), 30), again.state)
        self.assertEqual(again.state, "failed", "the update is prepared in the other window")
        self.assertIn("another Slicer Studio window", again.text.text)
        self.assertIsNone(second.prepared)
        self.assertEqual(os.stat(root).st_ino, staged, "the staged copy of the other window is not replaced")
        self.assertEqual(install.linuxPackageVersion(first.prepared.staged), "1.2.0")
        self.assertEqual(os.listdir(downloads), packages)
        with open(markerPath) as f:
            self.assertEqual(json.load(f)["pid"], other.pid, "the marker still names the other window")
        # Whatever this window does at its exit, the other window's update stays
        second._onQuitDiscard()
        first._quitConnected = False  # as if this window had prepared it but not chosen to install it
        first._onQuitDiscard()
        self.assertTrue(os.path.isdir(root))
        self.assertEqual(os.listdir(downloads), packages)
        self.assertTrue(os.path.isfile(markerPath))
        # The other window is gone: its marker no longer blocks, and the update can be prepared again
        other.kill()
        other.wait()
        third = self._service()
        third.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        retry = third.updateNow()
        self.assertTrue(self._waitFor(lambda: retry.state in ("ready", "failed"), 30), retry.state)
        self.assertEqual(retry.state, "ready", retry.text.text)

    def test_FailedDownloadReleasesStagedMarker(self):
        updates = self.updates
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        self._publish("1.2.0", tamper="sha256")
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        dialog = service.updateNow()
        self.assertTrue(self._waitFor(lambda: dialog.state in ("ready", "failed"), 30))
        self.assertEqual(dialog.state, "failed")
        self.assertFalse(os.path.exists(os.path.join(self.stateDir, updates.STAGED_MARKER)))

    def _closedDuringDownload(self, version="1.2.0", tamper=None, cancel=False):
        """Update now with a slow download, then Esc (reject) on the dialog while it downloads. Return the toasts shown
        (text, action, actionText) until the job ended, with the service and the dialog.
        """
        updates = self.updates
        self._publish(version, tamper=tamper)
        _Handler.slowPaths = {"/" + self.updates_assetName(version)}
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        toasts = []

        class FakeShell:
            def toast(self, text, ms=1600, action=None, actionText=None, sticky=False, level="info", toolTip=None):
                toasts.append((text, action, actionText))

        saved = updates._shell
        updates._shell = FakeShell
        self.addCleanup(setattr, updates, "_shell", saved)
        dialog = service.updateNow()
        self.assertEqual(dialog.state, "downloading")
        if cancel:
            dialog.secondaryButton.click()
        dialog.dialog.reject()  # Esc
        self.assertFalse(dialog.wanted)
        self.assertIsNotNone(service.job, "the download goes on")
        self.assertTrue(self._waitFor(lambda: service.job is None, 30))
        return toasts, service, dialog

    def updates_assetName(self, version):
        return f"Slicer-5.13.0-studio{version}-linux-amd64.tar.gz"

    def test_UpdateDialogClosedDuringDownloadSaysWhenReady(self):
        """Esc on the update dialog during the download leaves the job running: the user is told when the update is
        ready, with a button that shows the dialog (Restart now / Install when I quit) again.
        """
        if self.updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        toasts, service, dialog = self._closedDuringDownload()
        self.assertEqual(dialog.state, "ready", dialog.text.text)
        self.assertEqual(service.prepared.version, "1.2.0")
        self.assertEqual(len(toasts), 1, toasts)
        text, action, actionText = toasts[0]
        self.assertIn("1.2.0", text)
        self.assertIn("ready", text)
        self.assertEqual(actionText, "Show")
        action()
        self.assertTrue(dialog.dialog.isVisible())
        self.assertEqual(dialog.state, "ready")
        self.assertTrue(dialog.primaryButton.isVisible())

    def test_UpdateDialogClosedDuringDownloadSaysWhenFailed(self):
        if self.updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        toasts, _service, dialog = self._closedDuringDownload(tamper="sha256")
        self.assertEqual(dialog.state, "failed")
        self.assertEqual(len(toasts), 1, toasts)
        self.assertIn("could not be downloaded", toasts[0][0])
        self.assertEqual(toasts[0][2], "Show")
        toasts[0][1]()
        self.assertTrue(dialog.dialog.isVisible())
        self.assertEqual(dialog.state, "failed", "the dialog shows the failure again")

    def _closedNoticeInClassic(self, tamper=None):
        """As _closedDuringDownload, in Classic (no Studio shell, so no toast). Return the visible notice and the dialog."""
        import slicer

        updates = self.updates
        self._publish("1.2.0", tamper=tamper)
        _Handler.slowPaths = {"/" + self.updates_assetName("1.2.0")}
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        saved = updates._shell
        updates._shell = lambda: None
        self.addCleanup(setattr, updates, "_shell", saved)
        dialog = service.updateNow()
        self.assertEqual(dialog.state, "downloading")
        dialog.dialog.reject()  # Esc
        self.assertTrue(self._waitFor(lambda: service.job is None, 30))
        slicer.app.processEvents()
        notices = [w for w in slicer.app.topLevelWidgets()
                   if w.objectName == "StudioUpdateClosedNotice" and w.isVisible()]
        self.assertEqual(len(notices), 1, "Classic tells the user how the download ended")
        return notices[0], service, dialog

    def test_UpdateDialogClosedDuringDownloadSaysWhenReadyInClassic(self):
        """Classic has no toast: Esc on the update dialog during the download must not prepare the update silently (it
        would be thrown away at quit). A non-modal message says it is ready, with Show that shows the dialog again.
        """
        import qt
        import slicer

        if self.updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        notice, service, dialog = self._closedNoticeInClassic()
        self.assertEqual(dialog.state, "ready", dialog.text.text)
        self.assertEqual(service.prepared.version, "1.2.0")
        self.assertIn("1.2.0", notice.text)
        self.assertIn("ready", notice.text)
        self.assertFalse(notice.isModal())
        self.assertFalse(dialog.dialog.isVisible())
        showButton = notice.findChild(qt.QPushButton, "StudioUpdateClosedNoticeShowButton")
        self.assertIsNotNone(showButton)
        self.assertEqual(showButton.text, "Show")
        showButton.click()
        slicer.app.processEvents()
        self.assertTrue(dialog.dialog.isVisible())
        self.assertEqual(dialog.state, "ready")
        self.assertTrue(dialog.primaryButton.isVisible())
        self.assertFalse(notice.isVisible(), "the notice closes once the dialog is shown again")

    def test_UpdateDialogClosedDuringDownloadSaysWhenFailedInClassic(self):
        import qt
        import slicer

        if self.updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        notice, _service, dialog = self._closedNoticeInClassic(tamper="sha256")
        self.assertEqual(dialog.state, "failed")
        self.assertIn("could not be downloaded", notice.text)
        notice.findChild(qt.QPushButton, "StudioUpdateClosedNoticeShowButton").click()
        slicer.app.processEvents()
        self.assertTrue(dialog.dialog.isVisible())
        self.assertEqual(dialog.state, "failed", "the dialog shows the failure again")

    def test_UpdateDialogOpenOrCancelledDownloadShowsNoToast(self):
        """Nothing extra is announced while the dialog is open (it shows the result itself), nor after the user's own Cancel."""
        if self.updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        toasts, service, _dialog = self._closedDuringDownload(cancel=True)
        self.assertEqual(toasts, [], "the user cancelled it themselves")
        toasts2 = []

        class FakeShell:
            def toast(self, text, *args, **kwargs):
                toasts2.append(text)

        self.updates._shell = FakeShell
        service2 = self._service()
        service2.status = service.status
        service2.updateDialog = None
        dialog2 = service2.updateNow()
        self.assertTrue(self._waitFor(lambda: dialog2.state in ("ready", "failed"), 30), dialog2.state)
        self.assertEqual(toasts2, [], "the dialog stayed open and shows the result")

    def test_ReusedUpdateDialogTitleNamesVersionBeingInstalled(self):
        """Update now for 1.2.0 fails and the dialog is closed; a later check finds 1.3.0 and Update now reuses the dialog:
        its title names 1.3.0, not the version of the failed attempt.
        """
        updates = self.updates
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        self._publish("1.2.0", tamper="sha256")
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        dialog = service.updateNow()
        self.assertEqual(dialog.title.text, "Slicer Studio 1.2.0")
        self.assertTrue(self._waitFor(lambda: dialog.state in ("ready", "failed"), 30), dialog.state)
        self.assertEqual(dialog.state, "failed")
        dialog.secondaryButton.click()  # Close
        self._publish("1.3.0")
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        dialog2 = service.updateNow()
        self.assertIs(dialog2, dialog, "the dialog is reused")
        self.assertEqual(dialog2.title.text, "Slicer Studio 1.3.0")
        self.assertTrue(self._waitFor(lambda: dialog2.state in ("ready", "failed"), 30), dialog2.state)
        self.assertEqual(dialog2.state, "ready", dialog2.text.text)
        self.assertEqual(dialog2.title.text, "Slicer Studio 1.3.0")
        self.assertIn("1.3.0", dialog2.text.text)

    def test_AbandonedPreparedUpdateRemovedAtQuit(self):
        updates, install = self.updates, self.install
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        self._publish("1.2.0")
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        root = install.stagingRoot(target)
        for chosen in (False, True):
            service = self._service()
            service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
            dialog = service.updateNow()
            self.assertTrue(self._waitFor(lambda dialog=dialog: dialog.state in ("ready", "failed"), 30), dialog.state)
            self.assertEqual(dialog.state, "ready", dialog.text.text)
            self.assertTrue(os.path.isdir(root))
            self.assertTrue(os.listdir(updates.downloadDirectory()))
            if chosen:
                dialog.secondaryButton.click()  # Install when I quit
            # Esc on the dialog only hides it; the application quits
            service._onQuitDiscard()
            if chosen:
                self.assertTrue(os.path.isdir(root), "the update chosen for the exit is kept for the helper")
                self.assertTrue(os.listdir(updates.downloadDirectory()))
            else:
                self.assertFalse(os.path.exists(root))
                self.assertEqual(os.listdir(updates.downloadDirectory()), [])
                self.assertFalse(os.path.exists(os.path.join(self.stateDir, updates.STAGED_MARKER)))
            service.dispose()
            self.services.remove(service)

    def test_UpdateNowTamperedRefused(self):
        updates = self.updates
        self._publish("1.2.0", tamper="sha256")
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        dialog = service.updateNow()
        self.assertTrue(self._waitFor(lambda: dialog.state in ("ready", "failed"), 30))
        self.assertEqual(dialog.state, "failed")
        self.assertIn("refused", dialog.text.text)
        self.assertIn("not changed", dialog.text.text)
        self.assertIsNone(service.prepared)
        self.assertEqual(self.install.linuxPackageVersion(target), "0.9.0")
        self.assertFalse(os.path.exists(self.install.stagingRoot(target)))
        self.assertEqual([n for n in os.listdir(updates.downloadDirectory()) if not n.startswith(".")], [])

    def test_UpdateNowDevelopmentBuild(self):
        updates = self.updates
        self._publish("1.2.0")
        updates._installableOverride = False
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        dialog = service.updateNow()
        self.assertEqual(dialog.state, "unavailable")
        self.assertIsNone(service.job)

    #
    # (i) Studio and Classic surfaces
    #

    def test_StudioSurfaces(self):
        from StudioLib import library, shell, updates_settings

        updates = self.updates
        self._publish("1.2.0")
        mainWindow = slicer.util.mainWindow()
        geometry = mainWindow.saveGeometry()

        def restore():
            live = shell._shell
            if live is not None:
                live.dispose()
            mainWindow.restoreGeometry(geometry)
            self._pump(0.3)

        self.addCleanup(restore)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump(0.5)
        studioShell.switchTo("library")
        service = self._service()
        previous = updates._service
        updates._service = service
        self.addCleanup(lambda: setattr(updates, "_service", previous))
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        notice = service.showNotice()
        self.assertIsNotNone(notice)
        self.assertEqual(service.noticeWhere, "library")
        self.assertIn(updates.NOTICE_KEY, library.notices())
        self.assertEqual(notice.findChild("QLabel", "StudioNoticeText").text, "Update 1.2.0 available")
        self.assertEqual([b.text for b in notice.findChildren("QToolButton")], ["Update now", "Skip"])
        # ≡ menu item
        appMenu = studioShell.topBar.appMenu
        appMenu._update()
        self.assertEqual(appMenu.updatesAction.text, "Update to 1.2.0…")
        # Search entries
        titles = [item["title"] for item in service.paletteItems()]
        self.assertEqual(titles, ["Check for updates", "Update now"])
        # Skip removes the notice
        next(b for b in notice.findChildren("QToolButton") if b.text == "Skip").click()
        self._pump(0.1)
        self.assertNotIn(updates.NOTICE_KEY, library.notices())
        self.assertIsNone(service.notice)
        # Outside Library, in the inspector notice slot (never over another notice)
        studioShell.switchTo("draw")
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        notice = service.showNotice()
        self.assertEqual(service.noticeWhere, "inspector")
        self.assertEqual(studioShell.inspectorNoticeKey(), updates.NOTICE_KEY)
        service.hideNotice()
        self.assertIsNone(studioShell.inspectorNotice())
        # Settings panel
        panel = updates_settings.UpdatesSettingsPanel(service)
        self.addCleanup(panel.deleteLater)
        panel.refresh()
        self.assertIn("0.9.0", panel.versionLabel.text)
        self.assertIn("1.2.0", panel.latestLabel.text)
        self.assertIn("releases/tag/v1.2.0", panel.latestLabel.text)
        self.assertEqual(panel.objectName, "StudioUpdatesSettingsPanel")

    def test_SettingsNotesHeadingsFlattened(self):
        """The release notes in Settings > Updates show Markdown headings at the body size, like the update dialogs: the
        default heading sizes would make "What's new" larger than the title of the page.
        """
        import types

        from StudioLib import updates_settings

        updates = self.updates
        notes = "## Slicer Studio 1.2.0\n\nBody text\n\n### Fixes\n\n- one\n"
        manifest = types.SimpleNamespace(notes=notes, version="1.2.0", published="2026-10-01", releasePageUrl=lambda: "https://example.invalid/r")
        status = types.SimpleNamespace(manifest=manifest, kind="optional", skipped=False, available=True, version="1.2.0")
        service = types.SimpleNamespace(status=status, checking=False, lastError=None, addListener=lambda listener: None,
                                        unavailableReason=lambda: None)

        def headingAdjustments(browser):
            found = []
            block = browser.document.begin()
            while block.isValid():
                if block.blockFormat().headingLevel() > 0:
                    adjustment = qt.QTextCursor(block).charFormat().property(qt.QTextFormat.FontSizeAdjustment)
                    found.append((block.text(), adjustment))
                block = block.next()
            return found

        panel = updates_settings.UpdatesSettingsPanel(service)
        self.addCleanup(panel.deleteLater)
        panel.refresh()
        settingsHeadings = headingAdjustments(panel.notesBrowser)
        self.assertEqual([text for text, _adjustment in settingsHeadings], ["Slicer Studio 1.2.0", "Fixes"])
        for text, adjustment in settingsHeadings:
            self.assertFalse(adjustment, f"the heading {text!r} is larger than the body text in Settings")
        dialogBrowser = updates._notesBrowser(notes)
        self.addCleanup(dialogBrowser.deleteLater)
        self.assertEqual(settingsHeadings, headingAdjustments(dialogBrowser))

    def test_NoticeFollowsLaterChecks(self):
        """The notice names the version that Update now and Skip act on: a later check replaces or removes it."""
        from StudioLib import library, shell

        updates = self.updates
        self._publish("1.2.0")
        mainWindow = slicer.util.mainWindow()
        geometry = mainWindow.saveGeometry()

        def restore():
            live = shell._shell
            if live is not None:
                live.dispose()
            mainWindow.restoreGeometry(geometry)
            self._pump(0.3)

        self.addCleanup(restore)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump(0.5)
        studioShell.switchTo("library")
        updates._installableOverride = False
        self.addCleanup(lambda: setattr(updates, "_installableOverride", None))
        service = self._service()
        previous = updates._service
        updates._service = service
        self.addCleanup(lambda: setattr(updates, "_service", previous))
        service._startTime = time.monotonic() - 1000  # long after the startup: no delay for the notice

        def noticeText():
            return service.notice.findChild("QLabel", "StudioNoticeText").text if service.notice is not None else None

        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        service._showAvailability()
        self.assertTrue(self._waitFor(lambda: service.notice is not None, 5))
        self.assertEqual(noticeText(), "Update 1.2.0 available")
        # A later check finds 1.3.0: the notice is replaced
        self._publish("1.3.0")
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        service._showAvailability()
        self.assertTrue(self._waitFor(lambda: noticeText() == "Update 1.3.0 available", 5), noticeText())
        self.assertIn("1.3.0", service.notice.toolTip)
        self.assertEqual(len(library.notices()), 1)
        # A later check finds nothing newer: the notice goes
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "1.3.0")
        service._showAvailability()
        self.assertIsNone(service.notice)
        self.assertNotIn(updates.NOTICE_KEY, library.notices())

    def test_NoticeWaitsForLongPrompt(self):
        """The notice waits while another prompt (the 2-minute first-start tour, a dialog) is shown, however long that
        lasts, and appears when it ends: the retries are not used up while a prompt is shown.
        """
        from StudioLib import shell

        updates = self.updates
        self._publish("1.2.0")
        mainWindow = slicer.util.mainWindow()
        geometry = mainWindow.saveGeometry()

        def restore():
            live = shell._shell
            if live is not None:
                live.dispose()
            mainWindow.restoreGeometry(geometry)
            self._pump(0.3)

        self.addCleanup(restore)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump(0.5)
        studioShell.switchTo("library")
        updates._installableOverride = False
        self.addCleanup(lambda: setattr(updates, "_installableOverride", None))
        service = self._service()
        previous = updates._service
        updates._service = service
        self.addCleanup(lambda: setattr(updates, "_service", previous))
        service._startTime = time.monotonic() - 1000  # long after the startup: no delay for the notice
        # Fast retries; the prompt lasts for more ticks than the retries allow
        originalRetryMs = updates.NOTICE_RETRY_MS
        self.addCleanup(lambda: setattr(updates, "NOTICE_RETRY_MS", originalRetryMs))
        updates.NOTICE_RETRY_MS = 10
        ticks = []
        promptShown = [True]

        def otherPromptShown():
            ticks.append(1)
            return promptShown[0]

        service._otherPromptShown = otherPromptShown
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        service._showAvailability()
        self.assertTrue(self._waitFor(lambda: len(ticks) > updates.NOTICE_MAX_RETRIES + 5, 30))
        self.assertIsNone(service.notice, "no notice while the prompt is shown")
        self.assertTrue(service._noticeTimer.isActive(), "the notice still waits for the prompt to end")
        promptShown[0] = False
        self.assertTrue(self._waitFor(lambda: service.notice is not None, 10), "the notice appears when the prompt ends")
        self.assertEqual(service.notice.findChild("QLabel", "StudioNoticeText").text, "Update 1.2.0 available")

    def test_NoNoticeForUpdateThatInstallsAtQuit(self):
        """After Install when I quit, a later check does not offer the same version again: its Skip could not take the
        install back (it said "Update skipped" and the update installed at quit anyway). A newer version is offered.
        """
        from StudioLib import library, shell

        updates = self.updates
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        self._publish("1.2.0")
        target = self._fakeLinuxInstall("0.9.0")
        mainWindow = slicer.util.mainWindow()
        geometry = mainWindow.saveGeometry()

        def restore():
            live = shell._shell
            if live is not None:
                live.dispose()
            mainWindow.restoreGeometry(geometry)
            self._pump(0.3)

        self.addCleanup(restore)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump(0.5)
        studioShell.switchTo("library")
        updates._installableOverride = True
        updates.installTargetOverride = target
        service = self._service()
        previous = updates._service
        updates._service = service
        self.addCleanup(lambda: setattr(updates, "_service", previous))
        service._startTime = time.monotonic() - 1000  # long after the startup: no delay for the notice
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        service._showAvailability()
        self.assertTrue(self._waitFor(lambda: service.notice is not None, 5))
        dialog = service.updateNow()
        self.assertIsNone(service.notice)
        self.assertTrue(self._waitFor(lambda: dialog.state in ("ready", "failed"), 30), dialog.state)
        self.assertEqual(dialog.state, "ready", dialog.text.text)
        dialog.secondaryButton.click()  # Install when I quit
        self.assertTrue(service._quitConnected)
        self.assertEqual(service.prepared.version, "1.2.0")
        # A later check (Check now, the daily check) finds 1.2.0 again: no notice, so no Skip that would not hold
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        service._showAvailability()
        self._pump(1.0)
        self.assertIsNone(service.notice, "1.2.0 already installs when the application quits")
        self.assertNotIn(updates.NOTICE_KEY, library.notices())
        service._tryNotice()
        self.assertIsNone(service.notice)
        service.studioEntered()
        self._pump(0.5)
        self.assertIsNone(service.notice)
        self.assertEqual(updates.settingsValue("SkippedVersion"), "")
        # A notice placed before the install was chosen goes when it is chosen
        self.assertIsNotNone(service.showNotice())
        service.installOnQuit(relaunch=False)
        self.assertIsNone(service.notice)
        self.assertTrue(service._quitConnected, "the install at quit stays chosen")
        # A newer release is offered (Update now on it replaces the prepared 1.2.0)
        self._publish("1.3.0")
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        service._showAvailability()
        self.assertTrue(self._waitFor(lambda: service.notice is not None, 5))
        self.assertEqual(service.notice.findChild("QLabel", "StudioNoticeText").text, "Update 1.3.0 available")

    def test_NoNoticeWhileUpdateDownloadsOrWaits(self):
        """A check during the download (or after it, before the choice in the dialog) finding the same version does not
        bring the notice back: its Skip would not stop a download that is offered for install anyway.
        """
        from StudioLib import shell

        updates = self.updates
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        self._publish("1.2.0")
        target = self._fakeLinuxInstall("0.9.0")
        mainWindow = slicer.util.mainWindow()
        geometry = mainWindow.saveGeometry()

        def restore():
            live = shell._shell
            if live is not None:
                live.dispose()
            mainWindow.restoreGeometry(geometry)
            self._pump(0.3)

        self.addCleanup(restore)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump(0.5)
        studioShell.switchTo("library")
        updates._installableOverride = True
        updates.installTargetOverride = target
        service = self._service()
        previous = updates._service
        updates._service = service
        self.addCleanup(lambda: setattr(updates, "_service", previous))
        service._startTime = time.monotonic() - 1000  # long after the startup: no delay for the notice
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        service._showAvailability()
        self.assertTrue(self._waitFor(lambda: service.notice is not None, 5))
        dialog = service.updateNow()
        self.assertIsNone(service.notice)
        self.assertIsNotNone(service.job, "the download of 1.2.0 runs")
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        service._showAvailability()
        self._pump(0.5)
        self.assertIsNone(service.notice, "1.2.0 downloads: nothing to offer")
        service._tryNotice()
        service.studioEntered()
        self._pump(0.3)
        self.assertIsNone(service.notice)
        self.assertTrue(self._waitFor(lambda: dialog.state in ("ready", "failed"), 30), dialog.state)
        self.assertEqual(dialog.state, "ready", dialog.text.text)
        # Prepared but not yet chosen
        self.assertFalse(service.installChosen("1.2.0"))
        service._showAvailability()
        service._tryNotice()
        service.studioEntered()
        self._pump(0.5)
        self.assertIsNone(service.notice, "1.2.0 is ready: nothing to offer")
        self.assertEqual(updates.settingsValue("SkippedVersion"), "")

    def test_NoticeAfterSwitchFromClassic(self):
        """A start in Classic gives up the notice (no Studio shell); entering Studio later announces the update."""
        from StudioLib import library, shell

        updates = self.updates
        self._publish("1.2.0")
        mainWindow = slicer.util.mainWindow()
        geometry = mainWindow.saveGeometry()

        def restore():
            live = shell._shell
            if live is not None:
                live.dispose()
            mainWindow.restoreGeometry(geometry)
            self._pump(0.3)

        self.addCleanup(restore)
        updates._installableOverride = False
        self.addCleanup(lambda: setattr(updates, "_installableOverride", None))
        service = self._service()
        previous = updates._service
        updates._service = service
        self.addCleanup(lambda: setattr(updates, "_service", previous))
        service._startTime = time.monotonic() - 1000  # long after the startup: no delay for the notice
        studioShell = shell.Shell(force=True)  # Classic: the shell exists but is not applied
        self.assertIsNone(shell.instance())
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        service._showAvailability(startup=True)
        self._pump(1.0)
        self.assertIsNone(service.notice, "Classic has no Studio notice")
        self.assertFalse(service._noticeTimer.isActive())
        # Switching to Studio schedules the notice again
        studioShell.apply(midSession=True)
        self.assertTrue(self._waitFor(lambda: service.notice is not None, 10))
        self.assertEqual(service.notice.findChild("QLabel", "StudioNoticeText").text, "Update 1.2.0 available")
        self.assertIn(updates.NOTICE_KEY, library.notices())
        # A skipped version is not announced when Studio is entered again
        service.skip()
        studioShell.classic()
        studioShell.apply(midSession=True)
        self._pump(1.0)
        self.assertIsNone(service.notice)

    def test_ClassicHelpAction(self):
        service = self._service()
        service._installHelpAction()
        mainWindow = slicer.util.mainWindow()
        action = mainWindow.findChild("QAction", "StudioCheckForUpdatesAction")
        self.assertIsNotNone(action)
        helpMenu = mainWindow.findChild("QMenu", "HelpMenu")
        self.assertIn(action, helpMenu.actions())
        service._updateHelpAction()
        self.assertTrue(action.visible, "Classic (no shell): the Help item is shown")
        service.dispose()
        self.services.remove(service)
        self.assertNotIn(action, helpMenu.actions())

    #
    # (j) release script and web launcher
    #

    def _importScript(self, relativePath, name):
        path = os.path.join(SOURCE_DIR, relativePath)
        if not os.path.exists(path):
            self.skipTest(f"{relativePath} is not in this tree")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        # No __pycache__ in the source tree
        dontWrite, sys.dont_write_bytecode = sys.dont_write_bytecode, True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = dontWrite
        return module

    def test_MakeReleaseRoundTrip(self):
        updates = self.updates
        makeRelease = self._importScript(os.path.join("Utilities", "Scripts", "SlicerStudioRelease", "make_release.py"), "make_release_test")
        version = makeRelease.read_studio_version()
        self.assertEqual(version, self.version.STUDIO_VERSION)
        built = os.path.join(self.feedDir)
        linux = os.path.join(built, "Slicer-5.13.0-linux-amd64.tar.gz")
        _fakeLinuxPackage(linux, version)
        dmg = os.path.join(built, f"SlicerStudio-{version}-macos-arm64.dmg")
        with open(dmg, "wb") as f:
            f.write(os.urandom(4096))
        out = os.path.join(self.tempDir, "release")
        code = makeRelease.main([linux, dmg, "--required", "--minimum-supported", "0.1.0", "--notes-text", "Notes **bold**",
                                 "--out-dir", out, "--published", "2026-10-22"])
        self.assertEqual(code, 0)
        with open(os.path.join(out, "studio-update.json"), "rb") as f:
            manifest = updates.parseManifest(f.read())
        self.assertEqual(manifest.version, version)
        self.assertTrue(manifest.required)
        self.assertEqual(manifest.minimumSupported, "0.1.0")
        self.assertEqual(sorted(manifest.assets), ["linux-x86_64", "macos-arm64", "web"])
        for key, path in (("linux-x86_64", linux), ("macos-arm64", dmg)):
            self.assertEqual(manifest.asset(key)["sha256"], _sha256(path))
            self.assertEqual(manifest.asset(key)["size"], os.path.getsize(path))
        # The application downloads and accepts the files of the release (served as the feed)
        shutil.copy(os.path.join(out, "studio-update.json"), self.feedDir)
        shutil.copy(os.path.join(out, manifest.asset("web")["name"]), self.feedDir)
        fetched = updates.fetchManifest(self.feed)
        for key in ("linux-x86_64", "macos-arm64", "web"):
            path = updates.downloadAsset(fetched, key, os.path.join(self.tempDir, "dl"), feed=self.feed)
            self.assertTrue(os.path.isfile(path))
        # Mistakes are refused
        self.assertEqual(makeRelease.main([linux, "--version", "99.0.0", "--out-dir", out]), 1)
        self.assertEqual(makeRelease.main([linux, "--minimum-supported", "99.0.0", "--out-dir", out]), 1)
        for badMinimum in ("", " "):  # an empty value must not become a manifest that the app refuses
            self.assertEqual(makeRelease.main([linux, "--minimum-supported", badMinimum, "--out-dir", out]), 1)
        spaced = os.path.join(built, "Slicer 5.13 linux-amd64.tar.gz")
        shutil.copy(linux, spaced)
        self.assertEqual(makeRelease.main([spaced, "--out-dir", out]), 1)
        other = os.path.join(built, "other-linux-amd64.tar.gz")
        _fakeLinuxPackage(other, "0.0.1")
        self.assertEqual(makeRelease.main([other, "--out-dir", out]), 1)
        self.assertEqual(makeRelease.main([os.path.join(built, "unknown.bin"), "--out-dir", out]), 1)

    def test_MakeReleaseUnreadableNotes(self):
        """An unreadable --notes file is an 'Error: ...' line and exit 1, never a traceback."""
        makeRelease = self._importScript(os.path.join("Utilities", "Scripts", "SlicerStudioRelease", "make_release.py"), "make_release_test3")
        linux = os.path.join(self.feedDir, "Slicer-5.13.0-linux-amd64.tar.gz")
        _fakeLinuxPackage(linux, makeRelease.read_studio_version())
        cp1252 = os.path.join(self.tempDir, "notes-cp1252.md")
        with open(cp1252, "wb") as f:
            f.write(b"Caf\xe9 \x93q\x94\n")
        utf8 = os.path.join(self.tempDir, "notes-utf8.md")
        with open(utf8, "wb") as f:
            f.write("Café ok\n".encode())
        folder = os.path.join(self.tempDir, "notes-folder")
        os.makedirs(folder, exist_ok=True)
        out = os.path.join(self.tempDir, "release-notes")
        for notes, expected in ((cp1252, "UTF-8"), (os.path.join(self.tempDir, "missing.md"), "Cannot read the notes file"),
                                (folder, "Cannot read the notes file")):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = makeRelease.main([linux, "--notes", notes, "--no-web", "--out-dir", out])
            self.assertEqual(code, 1, notes)
            self.assertTrue(stderr.getvalue().startswith("Error: "), stderr.getvalue())
            self.assertIn(expected, stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse(os.path.exists(os.path.join(out, "studio-update.json")))
        self.assertEqual(makeRelease.main([linux, "--notes", utf8, "--no-web", "--out-dir", out]), 0)
        with open(os.path.join(out, "studio-update.json"), encoding="utf-8") as f:
            self.assertIn("Café ok", json.load(f)["notes"])

    def test_MakeReleasePublishedDate(self):
        """--published is checked as a real YYYY-MM-DD date: an impossible one is an 'Error: ...' line and exit 1."""
        makeRelease = self._importScript(os.path.join("Utilities", "Scripts", "SlicerStudioRelease", "make_release.py"), "make_release_test4")
        linux = os.path.join(self.feedDir, "Slicer-5.13.0-linux-amd64.tar.gz")
        _fakeLinuxPackage(linux, makeRelease.read_studio_version())
        out = os.path.join(self.tempDir, "release-published")
        for bad in ("2026-13-45", "2026-02-30", "banana", "20261022", "2026-1-5", ""):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = makeRelease.main([linux, "--published", bad, "--no-web", "--out-dir", out])
            self.assertEqual(code, 1, bad)
            self.assertTrue(stderr.getvalue().startswith("Error: "), stderr.getvalue())
            self.assertIn("YYYY-MM-DD", stderr.getvalue())
            self.assertFalse(os.path.exists(os.path.join(out, "studio-update.json")), bad)
        self.assertEqual(makeRelease.main([linux, "--published", "2024-02-29", "--no-web", "--out-dir", out]), 0)
        with open(os.path.join(out, "studio-update.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["published"], "2024-02-29")

    def test_MakeReleaseMinimumSupportedSummary(self):
        """--minimum-supported without --required: the closing summary says older versions are blocked, not 'optional'."""
        makeRelease = self._importScript(os.path.join("Utilities", "Scripts", "SlicerStudioRelease", "make_release.py"), "make_release_minsup")
        linux = os.path.join(self.feedDir, "Slicer-5.13.0-linux-amd64.tar.gz")
        _fakeLinuxPackage(linux, makeRelease.read_studio_version())
        out = os.path.join(self.tempDir, "release-minsup")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = makeRelease.main([linux, "--minimum-supported", "0.1.0", "--no-web", "--out-dir", out])
        self.assertEqual(code, 0)
        self.assertIn("older than 0.1.0 show a blocking 'Update required' dialog", stdout.getvalue())
        self.assertNotIn("This release is optional", stdout.getvalue())

    def test_MakeReleaseHugeNotes(self):
        """Notes past the app's limits: a manifest over MANIFEST_MAX_BYTES is refused (never written), notes over
        NOTES_MAX_CHARS give a warning.
        """
        makeRelease = self._importScript(os.path.join("Utilities", "Scripts", "SlicerStudioRelease", "make_release.py"), "make_release_huge")
        self.assertEqual(makeRelease.MANIFEST_MAX_BYTES, self.updates.MANIFEST_MAX_BYTES)
        self.assertEqual(makeRelease.NOTES_MAX_CHARS, self.updates.NOTES_MAX_CHARS)
        linux = os.path.join(self.feedDir, "Slicer-5.13.0-linux-amd64.tar.gz")
        _fakeLinuxPackage(linux, makeRelease.read_studio_version())
        out = os.path.join(self.tempDir, "release-huge")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = makeRelease.main([linux, "--no-web", "--out-dir", out, "--notes-text", "x" * (self.updates.MANIFEST_MAX_BYTES + 10)])
        self.assertEqual(code, 1)
        self.assertTrue(stderr.getvalue().startswith("Error: "), stderr.getvalue())
        self.assertIn("too large", stderr.getvalue())
        self.assertFalse(os.path.exists(os.path.join(out, "studio-update.json")))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = makeRelease.main([linux, "--no-web", "--out-dir", out, "--notes-text", "y" * (self.updates.NOTES_MAX_CHARS + 1)])
        self.assertEqual(code, 0)
        self.assertIn("Warning", stderr.getvalue())
        self.assertIn(str(self.updates.NOTES_MAX_CHARS), stderr.getvalue())
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = makeRelease.main([linux, "--no-web", "--out-dir", out, "--notes-text", "short"])
        self.assertEqual(code, 0)
        self.assertNotIn("Warning: the release notes", stderr.getvalue())

    def test_ReleaseScriptsOutputPathIsFile(self):
        """--out-dir and the launcher's update --dest naming a file (or under one) are errors, never a traceback."""
        makeRelease = self._importScript(os.path.join("Utilities", "Scripts", "SlicerStudioRelease", "make_release.py"), "make_release_test5")
        web = self._importScript(os.path.join("Utilities", "SlicerStudioWeb", "slicer_studio_web.py"), "slicer_studio_web_test5")
        linux = os.path.join(self.feedDir, "Slicer-5.13.0-linux-amd64.tar.gz")
        _fakeLinuxPackage(linux, makeRelease.read_studio_version())
        afile = os.path.join(self.tempDir, "afile")
        with open(afile, "w") as f:
            f.write("x")
        for out in (afile, os.path.join(afile, "sub")):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = makeRelease.main([linux, "--no-web", "--out-dir", out])
            self.assertEqual(code, 1, out)
            self.assertTrue(stderr.getvalue().startswith("Error: Cannot create the output folder"), stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
        bundle = makeRelease.make_web_bundle("1.2.0", self.feedDir)
        for destination in (afile, os.path.join(afile, "sub")):
            with self.assertRaisesRegex(web.LauncherError, "Cannot unpack the launcher"):
                web.unpack_web_bundle(web.Path(bundle), web.Path(destination))
        self.assertEqual(open(afile).read(), "x")

    def test_WebLauncherMistypedCommand(self):
        """A word that is not a command is refused (never started as 'run' with the word handed to Slicer);
        only arguments after '--' go to Slicer.
        """
        web = self._importScript(os.path.join("Utilities", "SlicerStudioWeb", "slicer_studio_web.py"), "slicer_studio_web_typo")
        calls = []
        saved = web.command_run, web.command_local
        web.command_run = lambda args: calls.append(("run", list(args.slicer_args))) or 0
        web.command_local = lambda args: calls.append(("local", list(args.slicer_args))) or 0
        try:
            for argv in (["stauts", "--no-browser"], ["stp"], ["staus"], ["run", "stauts"], ["local", "stauts"], ["run", "--no-browser", "file.mrb"]):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    web.main(argv)
                self.assertEqual(raised.exception.code, 2, argv)
                self.assertEqual(calls, [], argv)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                web.main(["stauts"])
            self.assertIn("unknown command 'stauts'", stderr.getvalue())
            for command in ("build", "run", "stop", "logs", "status", "local", "update"):
                self.assertIn(command, stderr.getvalue())
            # Valid forms still work: default command, explicit command, and Slicer arguments after "--"
            self.assertEqual(web.main(["--no-browser"]), 0)
            self.assertEqual(web.main(["run", "--no-browser", "--", "--python-script", "x.py"]), 0)
            self.assertEqual(web.main(["--no-browser", "--", "file.mrb"]), 0)
            self.assertEqual(web.main(["local", "--no-browser", "--", "-v"]), 0)
            self.assertEqual(calls, [("run", []), ("run", ["--python-script", "x.py"]), ("run", ["file.mrb"]), ("local", ["-v"])])
        finally:
            web.command_run, web.command_local = saved

    def test_WebLauncherRequiredWithoutLinuxPackage(self):
        """A required release without the Linux package (for example only the macOS disk image so far) does not stop
        'run': 'update' could not install it. The launcher warns and starts, like the app on a computer whose file the
        release does not have; 'update' says the package is not published yet. With the package, 'run' stops and names
        --no-update-check.
        """
        web = self._importScript(os.path.join("Utilities", "SlicerStudioWeb", "slicer_studio_web.py"), "slicer_studio_web_rnl")
        reached = []

        def containerState(name):
            reached.append(name)
            raise web.LauncherError("container start reached")

        web.ensure_docker_running = lambda: "x86_64"
        web.needs_amd64_emulation = lambda architecture: False
        web.image_exists = lambda image: False
        web.image_studio_version = lambda image: None
        web.launcher_version = lambda: "0.9.0"
        web.container_state = containerState
        macAsset = {"name": "SlicerStudio-1.2.1-macos-arm64.dmg", "size": 10, "sha256": "a" * 64}
        for required, minimum in ((True, None), (False, "1.0.0")):
            data = self._publish("1.2.1", required=required, minimum=minimum, package=False)
            data["assets"]["macos-arm64"] = macAsset
            with open(os.path.join(self.feedDir, "studio-update.json"), "w") as f:
                json.dump(data, f)
            stderr, stdout = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
                self.assertEqual(web.check_for_updates("0.9.0"), "required")
                code = web.main(["run", "--no-browser"])
            self.assertEqual(code, 1)
            self.assertIn("container start reached", stderr.getvalue(), "run went on to start the container")
            self.assertEqual(reached, [web.DEFAULT_CONTAINER])
            reached.clear()
            self.assertIn("no Linux package yet", stderr.getvalue())
            self.assertNotIn("is a required update", stderr.getvalue())
            with self.assertRaisesRegex(web.LauncherError, "no Linux package yet"):
                web.command_update(web.build_parser().parse_args(["update", "--no-build"]))
        # An optional release without the package names no 'update' command that would fail
        data = self._publish("1.2.1", package=False)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(web.check_for_updates("0.9.0"), "optional")
        self.assertIn("not published yet", stdout.getvalue())
        self.assertNotIn("Update with", stdout.getvalue())
        # With the Linux package, a required update still stops 'run', and the message names the way to start anyway
        self._publish("1.2.1", required=True)
        with self.assertRaises(web.LauncherError) as raised:
            web.check_for_updates("0.9.0")
        self.assertIn("is a required update", str(raised.exception))
        self.assertIn("--no-update-check", str(raised.exception))

    def test_WebLauncherPlainErrorTexts(self):
        """The web launcher's update check says what the app says for the same feeds, without the parser's, the error
        number's, the exception class's or the server's own text.
        """
        web = self._importScript(os.path.join("Utilities", "SlicerStudioWeb", "slicer_studio_web.py"), "slicer_studio_web_plain")
        raw = ("Expecting value", "Errno", "IncompleteRead", "NOT HTTP", "Connection refused", "char 0")

        def failure(feed):
            with self.assertRaises(web.LauncherError) as raised:
                web.fetch_update_manifest(feed)
            for text in raw:
                self.assertNotIn(text, str(raised.exception))
            return str(raised.exception)

        # A proxy or login page: HTML where the manifest should be
        with open(os.path.join(self.feedDir, "login.json"), "w") as f:
            f.write("<html><body>Sign in to the Wi-Fi</body></html>")
        self.assertIn("a proxy or login page may be in the way", failure(self.root + "login.json"))
        # No server (a port that was just closed)
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        self.assertIn("Cannot reach the update server (no connection)", failure(f"http://127.0.0.1:{port}/studio-update.json"))
        # A reply that is not HTTP, and a download that is cut off
        _Handler.notHttpPaths = {"/garbage.json"}
        _Handler.cutChunkedPaths = {"/cut.json"}
        try:
            self.assertIn("did not send a valid answer", failure(self.root + "garbage.json"))
            self.assertIn("the connection was interrupted", failure(self.root + "cut.json"))
        finally:
            _Handler.notHttpPaths = set()
            _Handler.cutChunkedPaths = set()

    def test_WebLauncher(self):
        web = self._importScript(os.path.join("Utilities", "SlicerStudioWeb", "slicer_studio_web.py"), "slicer_studio_web_test")
        makeRelease = self._importScript(os.path.join("Utilities", "Scripts", "SlicerStudioRelease", "make_release.py"), "make_release_test2")
        self.assertEqual(web.launcher_version(), self.version.STUDIO_VERSION)
        data = self._publish("1.2.0")
        bundle = makeRelease.make_web_bundle("1.2.0", self.feedDir)
        data["assets"]["web"] = {"name": os.path.basename(bundle), "size": os.path.getsize(bundle), "sha256": _sha256(bundle)}
        with open(os.path.join(self.feedDir, "studio-update.json"), "w") as f:
            json.dump(data, f)
        manifest = web.fetch_update_manifest(self.feed)
        self.assertEqual(web.update_status(manifest, "0.9.0"), "optional")
        self.assertEqual(web.update_status(manifest, "1.2.0"), "none")
        self.assertEqual(web.update_status(dict(manifest, required=True), "0.9.0"), "required")
        self.assertEqual(web.update_status(dict(manifest, minimumSupported="1.0.0"), "0.9.0"), "required")
        # A required update stops the launcher; an optional one does not
        data["required"] = True
        with open(os.path.join(self.feedDir, "studio-update.json"), "w") as f:
            json.dump(data, f)
        with self.assertRaisesRegex(web.LauncherError, "required update"):
            web.check_for_updates("0.9.0")
        self.assertEqual(web.check_for_updates("1.2.0"), "none")
        # Foreign redirects are refused
        port = self.server.server_address[1]
        _Handler.redirects = {"/moved.json": f"http://localhost:{port}/studio-update.json"}
        with self.assertRaisesRegex(web.LauncherError, "Refused"):
            web.fetch_update_manifest(self.root + "moved.json")
        self.assertFalse(web._update_url_allowed("https://evil.example/x", web.UPDATE_FEED_URL))
        self.assertTrue(web._update_url_allowed("https://objects.githubusercontent.com/x", web.UPDATE_FEED_URL))
        # Download, verification and the new launcher folder
        directory = web.Path(self.tempDir) / "web-dl"
        downloaded = web.download_update_asset(manifest, "web", directory, self.feed)
        launcher = web.unpack_web_bundle(downloaded, web.Path(self.tempDir) / "launchers")
        self.assertTrue((launcher / "slicer_studio_web.py").is_file())
        self.assertEqual((launcher / "VERSION").read_text().strip(), "1.2.0")
        self.assertTrue(os.access(launcher / "slicer_studio_web.py", os.X_OK))
        tampered = dict(manifest, assets=dict(manifest["assets"], web=dict(manifest["assets"]["web"], sha256="0" * 64)))
        with self.assertRaisesRegex(web.LauncherError, "SHA-256"):
            web.download_update_asset(tampered, "web", web.Path(self.tempDir) / "web-dl2", self.feed)
        self.assertEqual(list((web.Path(self.tempDir) / "web-dl2").iterdir()), [])
        # A connection that drops during the download (or the manifest) is a LauncherError, not a traceback
        _Handler.cutChunkedPaths = {"/" + data["assets"]["web"]["name"], "/studio-update.json"}
        with self.assertRaisesRegex(web.LauncherError, "interrupted"):
            web.download_update_asset(manifest, "web", web.Path(self.tempDir) / "web-dl3", self.feed)
        self.assertEqual(list((web.Path(self.tempDir) / "web-dl3").iterdir()), [])
        with self.assertRaisesRegex(web.LauncherError, "interrupted"):
            web.fetch_update_manifest(self.feed)
        _Handler.cutChunkedPaths = set()
        # A manifest whose assets (or one asset) is not an object is "not valid", not an AttributeError traceback
        # (also at the start of 'run': the check stays quiet and the launcher starts)
        for badAssets in ([1, 2], "abc", {"linux-x86_64": "x"}, {"web": 5}, {"web": [1]}):
            with open(os.path.join(self.feedDir, "studio-update.json"), "w") as f:
                json.dump(dict(data, assets=badAssets), f)
            with self.assertRaisesRegex(web.LauncherError, "not valid", msg=repr(badAssets)):
                web.fetch_update_manifest(self.feed)
            self.assertEqual(web.check_for_updates("0.9.0"), "unknown", repr(badAssets))
        with open(os.path.join(self.feedDir, "studio-update.json"), "w") as f:
            json.dump(data, f)
        self.assertEqual(web.package_studio_version(os.path.join(self.feedDir, data["assets"]["linux-x86_64"]["name"])), "1.2.0")
        # --port outside 1-65535 (or not a number) is an argument error, not a traceback or a 60 s wait
        parser = web.build_parser()
        for command in ("local", "run"):
            self.assertEqual(parser.parse_args([command, "--port", "65535"]).port, 65535)
            self.assertEqual(parser.parse_args([command, "--port", "1"]).port, 1)
            for bad in ("0", "-5", "65536", "99999", "abc", ""):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    parser.parse_args([command, "--port", bad])
                self.assertEqual(raised.exception.code, 2, f"{command} --port {bad!r}")
                self.assertIn("--port must be between 1 and 65535", stderr.getvalue())

    def test_LastCheckSurvivesTimeZoneChange(self):
        """'Last checked' and the daily re-check are written in UTC with Z: a time zone change (travel, DST) neither puts
        the last check in the future nor delays the re-check; an old value without a zone is still read as local time.
        """
        from StudioLib import updates_settings

        updates = self.updates
        savedZone = os.environ.get("TZ")
        realTime = time.time

        def setZone(name):
            os.environ["TZ"] = name
            time.tzset()

        def restoreZone():
            if savedZone is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = savedZone
            time.tzset()

        self.addCleanup(restoreZone)
        self._publish("0.9.0")
        service = self._service()
        service.automaticChecksEnabled = lambda: True
        setZone("Asia/Tokyo")
        done = []
        service.check(onDone=lambda status, error: done.append(error))
        self.assertTrue(self._waitFor(lambda: done, 10))
        self.assertIsNone(done[0])
        checkedAt = realTime()
        written = updates.settingsValue("LastCheck")
        self.assertTrue(written.endswith("Z"), written)
        self.assertLess(abs(updates.parseTime(written) - checkedAt), 5)
        # Same instant, other zone (16 h behind Tokyo in November): not in the future, shown in local time
        setZone("America/Los_Angeles")
        lastTime = updates.parseTime(written)
        self.assertLessEqual(lastTime, realTime() + 1)
        panel = updates_settings.UpdatesSettingsPanel(service)
        self.addCleanup(panel.deleteLater)
        self.assertIn("Last checked " + time.strftime("%Y-%m-%d %H:%M", time.localtime(lastTime)) + ".", panel.statusText())
        checks = []
        service.check = lambda *args, **kwargs: checks.append(1)
        service.checking = False
        try:
            time.time = lambda: checkedAt + 23 * 3600
            service._maybeRecheck()
            self.assertEqual(checks, [], "no re-check 23 h after the real check")
            time.time = lambda: checkedAt + 25 * 3600
            service._maybeRecheck()
            self.assertEqual(checks, [1], "re-check 25 h after the real check")
            # A value written by an earlier version (local time, no zone) is read as local time
            checks.clear()
            time.time = realTime
            updates.setSettingsValue("LastCheck", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(checkedAt - 25 * 3600)))
            service._maybeRecheck()
            self.assertEqual(checks, [1])
            updates.setSettingsValue("LastCheck", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(checkedAt - 3600)))
            service._maybeRecheck()
            self.assertEqual(checks, [1])
        finally:
            time.time = realTime

    #
    # (k) testing
    #

    def test_ManualInstallKeepsPackageWhereStepsSay(self):
        """When Slicer Studio cannot replace itself, the manual steps name the full path of the verified package, in a
        folder the user knows (not the private download folder), and the next start does not delete it.
        """
        updates, install = self.updates, self.install
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        data = self._publish("1.2.0")
        name = data["assets"]["linux-x86_64"]["name"]
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        keepDir = os.path.join(self.tempDir, "Downloads")
        updates.manualPackageDirectoryOverride = keepDir
        self.addCleanup(setattr, updates, "manualPackageDirectoryOverride", None)
        # The folder of the installation is not writable (for example /opt, owned by root)
        savedIsWritable = install.isWritable
        install.isWritable = lambda path: False
        self.addCleanup(setattr, install, "isWritable", savedIsWritable)
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        dialog = service.updateNow()
        self.assertTrue(self._waitFor(lambda: dialog.state in ("ready", "failed"), 30), dialog.state)
        self.assertEqual(dialog.state, "failed")
        text = dialog.text.text
        self.assertIn("cannot replace itself", text)
        kept = os.path.join(keepDir, name)
        self.assertIn(kept, text, "the steps name the full path of the package")
        self.assertEqual(_sha256(kept), data["assets"]["linux-x86_64"]["sha256"])
        self.assertFalse(os.path.exists(os.path.join(updates.downloadDirectory(), name)), "not left in the private folder")
        self.assertEqual(install.linuxPackageVersion(target), "0.9.0", "Slicer Studio was not changed")
        # The next start cleans up what a previous run left in the private folders: the kept package stays
        self.assertIsNone(self._service().finishInstall())
        self._pump(1.0)
        self.assertTrue(os.path.isfile(kept), "the package that the steps name is kept")
        # Try again: the same package is not kept twice
        dialog.onPrimary()
        self.assertTrue(self._waitFor(lambda: dialog.state in ("ready", "failed"), 30), dialog.state)
        self.assertIn(kept, dialog.text.text)
        self.assertEqual(os.listdir(keepDir), [name])
        # A different file of the same name in that folder is never overwritten
        other = os.path.join(self.tempDir, "other")
        os.makedirs(other)
        with open(os.path.join(other, name), "wb") as f:
            f.write(b"another file")
        self.assertEqual(updates.keepPackage(kept, other), os.path.join(other, name.replace(".tar.gz", " (2).tar.gz")))
        with open(os.path.join(other, name), "rb") as f:
            self.assertEqual(f.read(), b"another file")

    def test_PrepareLinuxCancelled(self):
        """prepareLinux() looks at Cancel before each file it extracts: it stops and removes what it extracted."""
        install = self.install
        target = self._fakeLinuxInstall("0.9.0")
        package = os.path.join(self.tempDir, "new.tar.gz")
        _fakeLinuxPackage(package, "1.2.0")
        calls = []

        def cancelled():
            calls.append(True)
            return len(calls) > 2  # cancelled after the first file

        with self.assertRaises(install.InstallCancelled):
            install.prepareLinux(package, target, "1.2.0", cancelled=cancelled)
        self.assertFalse(os.path.exists(install.stagingRoot(target)), "the extracted files are removed")
        self.assertEqual(install.linuxPackageVersion(target), "0.9.0")
        # Not cancelled: prepared as before
        prepared = install.prepareLinux(package, target, "1.2.0", cancelled=lambda: False)
        self.assertEqual(install.linuxPackageVersion(prepared.staged), "1.2.0")

    def _holdPrepare(self):
        """Hold the preparation of an update (in the worker thread) until the test releases it. Return (entered, release)."""
        install = self.install
        original = install.prepareLinux
        entered, release = threading.Event(), threading.Event()

        def held(*args, **kwargs):
            entered.set()
            release.wait(30)
            return original(*args, **kwargs)

        install.prepareLinux = held
        self.addCleanup(setattr, install, "prepareLinux", original)
        self.addCleanup(release.set)
        return entered, release

    def test_CancelWhilePreparing(self):
        """Cancel while the update is being prepared ("Checked. Preparing the new version…") stops the update: the
        optional dialog says it was cancelled, the required dialog goes back to Update now and never quits to install.
        """
        updates, install = self.updates, self.install
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        self._publish("1.2.0")
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        root = install.stagingRoot(target)
        # Optional update dialog
        entered, release = self._holdPrepare()
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        dialog = service.updateNow()
        task = dialog.task
        self.assertTrue(self._waitFor(lambda: entered.is_set() and task.phase == "prepare", 30), dialog.text.text)
        self._pump(0.2)
        self.assertEqual(dialog.secondaryButton.text, "Cancel")
        dialog.secondaryButton.click()
        self.assertTrue(task.cancelled())
        self.assertFalse(dialog.secondaryButton.enabled, "Cancel is not clicked twice while the update stops")
        self.assertIn("Cancelling", dialog.text.text)
        release.set()
        self.assertTrue(self._waitFor(lambda: dialog.state in ("ready", "failed"), 30), dialog.state)
        self.assertEqual(dialog.state, "failed", dialog.text.text)
        self.assertIn("cancelled", dialog.text.text)
        self.assertTrue(dialog.secondaryButton.enabled)
        self.assertIsNone(service.prepared)
        self.assertFalse(os.path.exists(root), "the staged copy is removed")
        self.assertFalse(os.path.exists(os.path.join(self.stateDir, updates.STAGED_MARKER)))
        self.assertEqual(install.linuxPackageVersion(target), "0.9.0")
        # Try again: the update is prepared
        dialog.onPrimary()
        self.assertTrue(self._waitFor(lambda: dialog.state in ("ready", "failed"), 30), dialog.state)
        self.assertEqual(dialog.state, "ready", dialog.text.text)
        self.assertEqual(install.linuxPackageVersion(service.prepared.staged), "1.2.0")
        service.dispose()

        # Required update dialog
        self._publish("1.2.1", required=True)
        entered, release = self._holdPrepare()
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        required = updates.RequiredUpdateDialog(service, slicer.util.mainWindow())
        self.addCleanup(required.dispose)
        closes = []
        required._closeApplication = lambda: closes.append(True)
        required.onUpdateClicked()
        task = required.task
        self.assertTrue(self._waitFor(lambda: entered.is_set() and task.phase == "prepare", 30), required.statusLabel.text)
        self._pump(0.2)
        self.assertEqual(required.updateButton.text, "Cancel")
        required.updateButton.click()
        self.assertTrue(task.cancelled())
        self.assertFalse(required.updateButton.enabled)
        release.set()
        self.assertTrue(self._waitFor(lambda: required.task is None, 30))
        self._pump(0.2)
        self.assertEqual(closes, [], "Cancel never quits Slicer Studio to install the update")
        self.assertEqual(required.state, "offer")
        self.assertEqual(required.updateButton.text, "Update now")
        self.assertTrue(required.updateButton.enabled)
        self.assertIn("cancelled", required.statusLabel.text)
        self.assertIsNone(service.prepared)
        self.assertFalse(service._quitConnected, "nothing installs at the next quit")
        self.assertFalse(os.path.exists(root), "the staged copy is removed")
        self.assertEqual(install.linuxPackageVersion(target), "0.9.0")

        # Cancel after the worker finished but before its result was reported (the main thread was busy)
        required.onUpdateClicked()
        task = required.task
        release.set()
        end = time.monotonic() + 30
        while not task.finished and time.monotonic() < end:
            time.sleep(0.02)  # no event processing: the result is not reported yet
        self.assertTrue(task.finished)
        self.assertIsNone(task.error, str(task.error))
        required.onUpdateClicked()  # Cancel
        self.assertTrue(self._waitFor(lambda: required.task is None, 30))
        self._pump(0.5)
        self.assertEqual(closes, [])
        self.assertEqual(required.state, "offer")
        self.assertEqual(required.updateButton.text, "Update now")
        self.assertIsNone(service.prepared)
        self.assertFalse(service._quitConnected)
        self.assertTrue(self._waitFor(lambda: not os.path.exists(root), 10), "the staged copy is removed")

    def test_RequiredDialogQuitAfterReadyDoesNotRelaunch(self):
        """Required update, Update now, ready (Restart now asked for a restart), the exit is cancelled and the dialog
        comes back: Quit must not start the application again (the update still installs at the exit).
        """
        updates = self.updates
        if updates.platformKey() != "linux-x86_64":
            self.skipTest("the Linux package path is tested on Linux")
        self._publish("1.2.1", required=True)
        target = self._fakeLinuxInstall("0.9.0")
        updates._installableOverride = True
        updates.installTargetOverride = target
        service = self._service()
        service.status = updates.evaluate(updates.fetchManifest(self.feed), "0.9.0")
        required = updates.RequiredUpdateDialog(service, slicer.util.mainWindow())
        self.addCleanup(required.dispose)
        closes = []
        required._closeApplication = lambda: closes.append(True)
        required.onUpdateClicked()
        self.assertTrue(self._waitFor(lambda: required.state in ("ready", "failed"), 30), required.statusLabel.text)
        self.assertEqual(required.state, "ready", required.statusLabel.text)
        self.assertEqual(closes, [True], "Restart now closes the application")
        self.assertTrue(service._relaunch, "Restart now asked for a relaunch")
        self.assertTrue(service._quitConnected)
        # The exit was cancelled (the dialog is back with Restart now): Quit
        required.quit()
        self.assertEqual(closes, [True, True])
        self.assertFalse(service._relaunch, "Quit does not start the application again")
        self.assertTrue(service._quitConnected, "the update installs at the exit")
        self.assertIsNotNone(service.prepared)
        # What the exit starts: the install helper without relaunch
        launches = []
        service.prepared.launch = lambda stateDir, relaunch=True, record=None, **kw: launches.append(relaunch)
        service._onAboutToQuit()
        self.assertEqual(launches, [False])
        service.prepared = None
        service.dispose()

    def test_NothingUnderTesting(self):
        updates = self.updates
        self.assertTrue(updates.isDisabledByEnvironment())
        called = []
        previous = updates._service
        updates._service = None
        try:
            self.assertIsNone(updates.onStartupCompleted(lambda: called.append(True)))
            self.assertEqual(called, [True])
            self.assertIsNone(updates.instance())
        finally:
            updates._service = previous
        mainWindow = slicer.util.mainWindow()
        self.assertIsNone(mainWindow.findChild("QAction", "StudioCheckForUpdatesAction"))
        dialog = slicer.app.settingsDialog()
        if dialog is not None:
            self.assertIsNone(dialog.findChild("QWidget", "StudioUpdatesSettingsPanel"))


if __name__ == "__main__":
    unittest.main()

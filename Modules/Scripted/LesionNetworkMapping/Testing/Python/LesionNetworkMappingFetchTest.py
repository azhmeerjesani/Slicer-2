"""Tests of the fetch/cache layer and the atlas labelling service (fetch.py, atlases.py, catalog/*.json; addendum 4.2 and 4.4).

No network is used: a local http.server in a thread serves the test files (with Range support, interrupted
reads, corrupted bodies), and the atlases are synthetic. The catalogs are checked for completeness and
consistency only.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingFetchTest.py -v
"""

import contextlib
import gzip
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import zipfile

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import atlases, fetch, geometry, jobs, nifti

_SAVED_ENV = {}


def setUpModule():
    # the local test server must never go through a proxy; retries must not wait
    for name in ("NO_PROXY", "no_proxy", fetch.OFFLINE_ENV):
        _SAVED_ENV[name] = os.environ.get(name)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    os.environ.pop(fetch.OFFLINE_ENV, None)
    _SAVED_ENV["delays"] = fetch.RETRY_DELAYS_S
    fetch.RETRY_DELAYS_S = (0.0,)


def tearDownModule():
    fetch.RETRY_DELAYS_S = _SAVED_ENV.pop("delays")
    for name, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves ``server.files`` with Range support. ``server.behavior[path]`` may hold ``cut`` (a list of byte counts: each
    request sends that many bytes of its payload and drops the connection), ``ignore_range`` and ``sequence`` (bodies
    served one per request before the normal file).
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, payload=b"", headers=()):
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_GET(self):
        server = self.server
        server.log.append((self.path, self.headers.get("Range")))
        behavior = server.behavior.get(self.path, {})
        body = server.files.get(self.path)
        if behavior.get("sequence"):
            body = behavior["sequence"].pop(0)
        if body is None:
            return self._send(404)
        start, status = 0, 200
        requested = self.headers.get("Range")
        if requested and not behavior.get("ignore_range"):
            start = int(re.match(r"bytes=(\d+)-", requested).group(1))
            if start >= len(body):
                return self._send(416, headers=[("Content-Range", f"bytes */{len(body)}")])
            status = 206
        payload = body[start:]
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        self.end_headers()
        cuts = behavior.get("cut")
        if cuts:
            self.wfile.write(payload[: cuts.pop(0)])
            self.wfile.flush()
            self.close_connection = True
            return None
        self.wfile.write(payload)
        return None


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # clients that cancel or drop a connection reset it: not an error of the test

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.files = {}
        self.behavior = {}
        self.log = []

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


def dead_url(name="/file.bin"):
    """A URL on a port nobody listens on (connection refused)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}{name}"


def entry_for(url, body, **fields):
    entry = {
        "id": fields.pop("id", "test-file"),
        "title": "Test file",
        "kind": "atlas",
        "urls": [url],
        "filename": fields.pop("filename", "file.bin"),
        "size": len(body),
        "checksum": {"algo": "sha256", "value": sha256(body)},
        "licence": "CC0-1.0",
        "badge": "green",
        "citation": "test",
        "space": "MNI152NLin6Asym",
        "family": "fsl6",
    }
    entry.update(fields)
    return entry


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-fetch-test-")
        self.cache = os.path.join(self.tmp, "cache")
        self.server = _Server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.body = np.random.default_rng(1).integers(0, 256, 600_000, dtype=np.uint8).tobytes()

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def serve(self, path, body, **behavior):
        self.server.files[path] = body
        if behavior:
            self.server.behavior[path] = behavior
        return self.server.base + path

    def requests_for(self, path):
        return [entry for entry in self.server.log if entry[0] == path]


class FetchTest(_Base):
    def test_download_verifies_caches_and_reuses(self):
        url = self.serve("/file.bin", self.body)
        entry = entry_for(url, self.body)
        path = fetch.fetch(entry, self.cache)
        self.assertEqual(read_bytes(path), self.body)
        self.assertTrue(path.startswith(self.cache))
        self.assertEqual(len(self.requests_for("/file.bin")), 1)
        record = fetch.manifest_record(entry, self.cache)
        self.assertEqual(record["sha256"], sha256(self.body))
        self.assertEqual(record["size"], len(self.body))
        self.assertFalse(record["recorded_on_first_download"])
        self.assertEqual(fetch.cached_path(entry, self.cache), path)
        self.assertEqual(fetch.fetch(entry, self.cache), path)  # reused: no second request
        self.assertEqual(len(self.requests_for("/file.bin")), 1)
        self.assertFalse(os.path.exists(path + ".part"))

    def test_md5_and_sha1_checksums(self):
        url = self.serve("/file.bin", self.body)
        for algo in ("md5", "sha1"):
            entry = entry_for(url, self.body, id=f"hash-{algo}", checksum={"algo": algo, "value": hashlib.new(algo, self.body).hexdigest()})
            self.assertTrue(os.path.isfile(fetch.fetch(entry, os.path.join(self.cache, algo))))
        entry = entry_for(url, self.body, checksum={"algo": "md5", "value": "0" * 32})
        with self.assertRaises(fetch.ChecksumError) as caught:
            fetch.fetch(entry, os.path.join(self.cache, "bad"))
        self.assertEqual(caught.exception.algo, "md5")

    def test_resume_after_interrupted_read(self):
        # the first response is cut after 250,000 bytes: the retry asks for the rest with a Range header
        url = self.serve("/file.bin", self.body, cut=[250_000])
        entry = entry_for(url, self.body)
        path = fetch.fetch(entry, self.cache, retries=2)
        self.assertEqual(read_bytes(path), self.body)
        requests = self.requests_for("/file.bin")
        self.assertEqual(len(requests), 2)
        self.assertIsNone(requests[0][1])
        start = int(re.match(r"bytes=(\d+)-", requests[1][1]).group(1))
        self.assertTrue(0 < start < len(self.body), start)

    def test_partial_file_resumes_in_a_later_call(self):
        url = self.serve("/file.bin", self.body, cut=[300_000])
        entry = entry_for(url, self.body)
        with self.assertRaises(fetch.FetchError) as caught:
            fetch.fetch(entry, self.cache, retries=0)
        self.assertEqual(caught.exception.code, "interrupted")
        part = fetch.download_path(entry, self.cache) + ".part"
        kept = os.path.getsize(part)
        self.assertTrue(0 < kept < len(self.body))
        self.assertIsNone(fetch.cached_path(entry, self.cache))
        path = fetch.fetch(entry, self.cache)
        self.assertEqual(read_bytes(path), self.body)
        self.assertEqual(self.requests_for("/file.bin")[-1][1], f"bytes={kept}-")

    def test_server_that_ignores_range_restarts_cleanly(self):
        url = self.serve("/file.bin", self.body, ignore_range=True)
        entry = entry_for(url, self.body)
        part = fetch.download_path(entry, self.cache) + ".part"
        os.makedirs(os.path.dirname(part))
        with open(part, "wb") as f:
            f.write(b"junk" * 1000)  # a stale partial file
        path = fetch.fetch(entry, self.cache)
        self.assertEqual(read_bytes(path), self.body)

    def test_checksum_mismatch_retries_once_then_raises(self):
        url = self.serve("/file.bin", self.body)
        entry = entry_for(url, self.body, checksum={"algo": "sha256", "value": "ab" * 32})
        with self.assertRaises(fetch.ChecksumError) as caught:
            fetch.fetch(entry, self.cache)
        error = caught.exception
        self.assertEqual(len(self.requests_for("/file.bin")), 2)  # one retry, each a whole download
        self.assertEqual((error.expected, error.actual), ("ab" * 32, sha256(self.body)))
        self.assertEqual(error.code, "checksum")
        dest = fetch.download_path(entry, self.cache)
        self.assertFalse(os.path.exists(dest))
        self.assertFalse(os.path.exists(dest + ".part"))
        self.assertEqual(error.path, dest + ".unverified")
        self.assertIsNone(fetch.manifest_record(entry, self.cache))
        # the caller's override: the file is kept and the manifest records it
        path = fetch.accept_unverified(entry, self.cache)
        self.assertEqual(read_bytes(path), self.body)
        self.assertTrue(fetch.manifest_record(entry, self.cache)["checksum_override"])
        self.assertEqual(fetch.cached_path(entry, self.cache), path)

    def test_corrupted_first_download_is_replaced_by_the_retry(self):
        corrupted = bytes([self.body[0] ^ 1]) + self.body[1:]
        url = self.serve("/file.bin", self.body, sequence=[corrupted])
        path = fetch.fetch(entry_for(url, self.body), self.cache)
        self.assertEqual(read_bytes(path), self.body)
        self.assertEqual(len(self.requests_for("/file.bin")), 2)

    def test_http_error_is_not_retried(self):
        entry = entry_for(self.server.base + "/missing.bin", self.body)
        with self.assertRaises(fetch.FetchError) as caught:
            fetch.fetch(entry, self.cache)
        self.assertEqual(caught.exception.code, "http")
        self.assertEqual(len(self.requests_for("/missing.bin")), 1)

    def test_second_url_is_used_when_the_first_fails(self):
        url = self.serve("/good.bin", self.body)
        entry = entry_for(self.server.base + "/missing.bin", self.body, urls=[self.server.base + "/missing.bin", url])
        self.assertEqual(read_bytes(fetch.fetch(entry, self.cache)), self.body)

    def test_offline_errors_and_cached_files(self):
        entry = entry_for(dead_url(), self.body)
        with self.assertRaises(fetch.OfflineError) as caught:
            fetch.fetch(entry, self.cache, retries=1)
        self.assertEqual(caught.exception.code, "offline")
        with self.assertRaises(fetch.OfflineError):  # offline mode: not even an attempt
            fetch.fetch(entry_for(self.server.base + "/file.bin", self.body), self.cache, offline=True)
        self.assertEqual(self.requests_for("/file.bin"), [])
        # a cached file is returned without touching the network (the server is gone)
        url = self.serve("/file.bin", self.body)
        cached = entry_for(url, self.body)
        path = fetch.fetch(cached, self.cache)
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual(fetch.fetch(cached, self.cache), path)
        self.assertEqual(fetch.fetch(cached, self.cache, offline=True), path)
        os.environ[fetch.OFFLINE_ENV] = "1"
        try:
            self.assertEqual(fetch.fetch(cached, self.cache), path)
            with self.assertRaises(fetch.OfflineError):
                fetch.fetch(entry_for(url, self.body, id="other", filename="other.bin", urls=[url + "?other"]), self.cache)
        finally:
            os.environ.pop(fetch.OFFLINE_ENV, None)

    def test_record_on_first_download(self):
        url = self.serve("/file.bin", self.body)
        entry = entry_for(url, self.body, checksum=fetch.RECORD_ON_FIRST_DOWNLOAD)
        path = fetch.fetch(entry, self.cache)
        record = fetch.manifest_record(entry, self.cache)
        self.assertTrue(record["recorded_on_first_download"])
        self.assertEqual(record["sha256"], sha256(self.body))
        with open(fetch.manifest_path(self.cache), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["format"], fetch.MANIFEST_FORMAT)
        self.assertIn(fetch._relpath(self.cache, path), manifest["files"])
        self.assertEqual(fetch.fetch(entry, self.cache), path)
        self.assertEqual(len(self.requests_for("/file.bin")), 1)
        # verified on reuse: a damaged cached file is not trusted and is downloaded again
        with open(path, "r+b") as f:
            f.write(b"\x00\x01\x02")
        self.assertIsNone(fetch.cached_path(entry, self.cache))
        self.assertEqual(read_bytes(fetch.fetch(entry, self.cache)), self.body)
        self.assertEqual(len(self.requests_for("/file.bin")), 2)
        # the source changed since the first download: the recorded sha256 refuses it
        self.server.files["/file.bin"] = self.body[::-1]
        with open(path, "r+b") as f:
            f.write(b"\x00\x01\x02")
        with self.assertRaises(fetch.ChecksumError) as caught:
            fetch.fetch(entry, self.cache)
        self.assertEqual(caught.exception.expected, sha256(self.body))

    def test_a_changed_catalog_checksum_invalidates_the_cache(self):
        url = self.serve("/file.bin", self.body)
        entry = entry_for(url, self.body)
        fetch.fetch(entry, self.cache)
        newer = dict(entry, checksum={"algo": "sha256", "value": "cd" * 32})
        self.assertIsNone(fetch.cached_path(newer, self.cache))

    def test_large_cached_files_use_size_and_mtime(self):
        url = self.serve("/file.bin", self.body)
        entry = entry_for(url, self.body)
        path = fetch.fetch(entry, self.cache)
        saved = fetch.FAST_VERIFY_BYTES
        fetch.FAST_VERIFY_BYTES = 1000
        try:
            self.assertEqual(fetch.cached_path(entry, self.cache), path)
            stat = os.stat(path)
            with open(path, "r+b") as f:
                first = f.read(1)
                f.seek(0)
                f.write(bytes([first[0] ^ 0xFF]))
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
            self.assertIsNone(fetch.cached_path(entry, self.cache))  # modified: hashed again, rejected
        finally:
            fetch.FAST_VERIFY_BYTES = saved

    def test_cancel_keeps_the_partial_file(self):
        url = self.serve("/file.bin", self.body)
        entry = entry_for(url, self.body)
        cancel = threading.Event()
        with self.assertRaises(jobs.Cancelled):
            fetch.fetch(entry, self.cache, progress=lambda done, total: cancel.set(), cancel=cancel)
        part = fetch.download_path(entry, self.cache) + ".part"
        kept = os.path.getsize(part)
        self.assertTrue(0 < kept < len(self.body))
        self.assertEqual(read_bytes(fetch.fetch(entry, self.cache)), self.body)
        self.assertEqual(self.requests_for("/file.bin")[-1][1], f"bytes={kept}-")

    def test_progress_reports_bytes(self):
        url = self.serve("/file.bin", self.body)
        seen = []
        fetch.fetch(entry_for(url, self.body), self.cache, progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(seen[-1], (len(self.body), len(self.body)))
        self.assertEqual([done for done, _ in seen], sorted(done for done, _ in seen))

    def test_zip_members(self):
        buffer = io.BytesIO()
        members = {"Pack/3T/S1.nii.gz": b"image-one" * 50, "Pack/3T/S2.nii.gz": b"image-two" * 50, "Pack/3T/S1_label.txt": b"a\nb\n", "__MACOSX/Pack/3T/._S1.nii.gz": b"junk"}
        with zipfile.ZipFile(buffer, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
        archive = buffer.getvalue()
        url = self.serve("/pack.zip", archive)
        first = entry_for(url, archive, id="s1", filename="pack.zip", unzip_member="Pack/3T/S1.nii.gz", member_sha256=sha256(members["Pack/3T/S1.nii.gz"]))
        second = entry_for(url, archive, id="s2", filename="pack.zip", unzip_member="S2.nii.gz")  # matched by file name
        path_one = fetch.fetch(first, self.cache)
        path_two = fetch.fetch(second, self.cache)
        self.assertEqual(read_bytes(path_one), members["Pack/3T/S1.nii.gz"])
        self.assertEqual(read_bytes(path_two), members["Pack/3T/S2.nii.gz"])
        self.assertEqual(len(self.requests_for("/pack.zip")), 1)  # one archive serves both
        self.assertEqual(fetch.cached_path(first, self.cache), path_one)
        self.assertTrue(fetch.manifest_record(second, self.cache)["recorded_on_first_download"])  # no member checksum: recorded
        # the label table is a member of the same archive
        table = dict(first, label_table={"format": "lines", "unzip_member": "Pack/3T/S1_label.txt", "member_sha256": sha256(members["Pack/3T/S1_label.txt"])})
        labels = fetch.fetch_labels(table, self.cache)
        self.assertEqual(read_bytes(labels), members["Pack/3T/S1_label.txt"])
        self.assertEqual(len(self.requests_for("/pack.zip")), 1)
        # a wrong member checksum is refused; a missing or ambiguous member is an error
        wrong = entry_for(url, archive, id="wrong", filename="pack.zip", unzip_member="Pack/3T/S1.nii.gz", member_sha256="00" * 32)
        os.remove(fetch.primary_path(wrong, self.cache))
        with self.assertRaises(fetch.ChecksumError):
            fetch.fetch(wrong, self.cache)
        with self.assertRaises(fetch.FetchError) as caught:
            fetch.fetch(entry_for(url, archive, id="missing", filename="pack.zip", unzip_member="Pack/3T/S9.nii.gz"), self.cache)
        self.assertEqual(caught.exception.code, "member")
        with self.assertRaises(fetch.FetchError):
            fetch._find_member(["a/x.nii", "b/x.nii"], "x.nii")

    def test_a_damaged_archive_is_removed(self):
        archive = b"this is not a zip file" * 10
        url = self.serve("/pack.zip", archive)
        entry = entry_for(url, archive, id="bad-zip", filename="bad.zip", unzip_member="S1.nii.gz")
        with self.assertRaises(fetch.FetchError) as caught:
            fetch.fetch(entry, self.cache)
        self.assertEqual(caught.exception.code, "archive")
        self.assertFalse(os.path.exists(fetch.download_path(entry, self.cache)))

    def test_link_out_derived_and_adopt(self):
        link = entry_for("https://example.org/download-page", self.body, id="link", link_out=True, filename="manual.bin")
        with self.assertRaises(fetch.NotDownloadableError) as caught:
            fetch.fetch(link, self.cache)
        self.assertEqual(caught.exception.code, "link_out")
        derived = {"id": "derived", "title": "Derived", "kind": "prior", "urls": [], "derive": {"from": "x"}, "licence": "derived", "badge": "amber"}
        with self.assertRaises(fetch.NotDownloadableError) as caught:
            fetch.fetch(derived, self.cache)
        self.assertEqual(caught.exception.code, "derive")
        manual = os.path.join(self.tmp, "browser-download.bin")
        with open(manual, "wb") as f:
            f.write(self.body)
        path = fetch.adopt(link, manual, self.cache)
        self.assertEqual(read_bytes(path), self.body)
        self.assertEqual(fetch.fetch(link, self.cache), path)  # now cached
        with open(manual, "wb") as f:
            f.write(b"not the file")
        with self.assertRaises(fetch.ChecksumError):
            fetch.adopt(entry_for("https://example.org/p", self.body, id="link2", link_out=True, filename="other.bin"), manual, self.cache)
        recorded = entry_for("https://example.org/p", b"", id="link3", link_out=True, filename="third.bin", checksum=fetch.RECORD_ON_FIRST_DOWNLOAD)
        path = fetch.adopt(recorded, manual, self.cache)
        self.assertTrue(fetch.manifest_record(recorded, self.cache)["recorded_on_first_download"])
        self.assertEqual(fetch.cached_path(recorded, self.cache), path)

    def test_entries_with_the_same_url_share_the_file(self):
        url = self.serve("/file.bin", self.body)
        atlas = entry_for(url, self.body, id="atlas-entry")
        labels = entry_for(url, self.body, id="labels-entry", kind="labels")
        self.assertEqual(fetch.fetch(atlas, self.cache), fetch.fetch(labels, self.cache))
        self.assertEqual(len(self.requests_for("/file.bin")), 1)

    def test_lock_of_a_live_download(self):
        url = self.serve("/file.bin", self.body)
        entry = entry_for(url, self.body)
        dest = fetch.download_path(entry, self.cache)
        with fetch._PartLock(dest), self.assertRaises(fetch.FetchError) as caught:
            with fetch._PartLock(dest):
                pass
        self.assertEqual(caught.exception.code, "locked")
        with open(dest + ".lock", "w") as f:  # a stale lock of a dead process is taken over
            f.write("999999999")
        self.assertEqual(read_bytes(fetch.fetch(entry, self.cache)), self.body)


class CommandLineTest(_Base):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = jobs.run_job(fetch.main, list(argv), job="fetch", orphan=False, exit=False, signals=False)
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        return code, events

    def write_catalog(self, entries):
        path = os.path.join(self.tmp, "catalog.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"format": fetch.CATALOG_FORMAT, "version": 1, "catalog": "test", "entries": entries}, f)
        return path

    def test_get_emits_json_events_and_fetches_labels(self):
        url = self.serve("/file.bin", self.body)
        table = b"index\tname\n1\tAlpha\n"
        table_url = self.serve("/labels.tsv", table)
        entry = entry_for(url, self.body, label_table={"format": "tsv", "urls": [table_url], "filename": "labels.tsv", "size": len(table), "checksum": {"algo": "sha256", "value": sha256(table)}})
        catalog = self.write_catalog([entry])
        code, events = self.run_cli("get", "--catalog", catalog, "--id", "test-file", "--cache", self.cache)
        self.assertEqual(code, 0, events)
        self.assertEqual(events[0]["event"], "start")
        self.assertEqual(events[0]["job"], "fetch")
        self.assertEqual(events[-1]["event"], "done")
        self.assertEqual(len(events[-1]["outputs"]), 2)
        results = [event for event in events if event["event"] == "result"]
        self.assertEqual([event["part"] for event in results], ["atlas", "labels"])
        self.assertEqual(results[0]["badge"], "green")
        self.assertEqual(results[0]["licence"], "CC0-1.0")
        self.assertEqual(results[0]["sha256"], sha256(self.body))
        self.assertIn("progress", [event["event"] for event in events])
        code, events = self.run_cli("get", "--catalog", catalog, "--id", "test-file", "--cache", self.cache)
        self.assertEqual(code, 0)
        self.assertTrue(all(event["cached"] for event in events if event["event"] == "result"))
        self.assertEqual(len(self.requests_for("/file.bin")), 1)

    def test_error_events(self):
        url = self.serve("/file.bin", self.body)
        catalog = self.write_catalog([entry_for(url, self.body), entry_for(dead_url(), self.body, id="dead", filename="dead.bin")])
        code, events = self.run_cli("get", "--catalog", catalog, "--id", "nope", "--cache", self.cache)
        self.assertEqual((code, events[-1]["event"], events[-1]["code"]), (1, "error", "unknown_id"))
        code, events = self.run_cli("get", "--catalog", catalog, "--id", "dead", "--cache", self.cache, "--retries", "0")
        self.assertEqual((code, events[-1]["code"]), (1, "offline"))
        code, events = self.run_cli("get", "--catalog", catalog, "--id", "test-file", "--cache", os.path.join(self.tmp, "c2"), "--offline")
        self.assertEqual((code, events[-1]["code"]), (1, "offline"))
        self.assertNotIn("/file.bin", [path for path, _ in self.server.log])
        code, events = self.run_cli("get", "--catalog", "nonexistent", "--id", "x")
        self.assertEqual((code, events[-1]["code"]), (1, "catalog"))

    def test_offline_environment_variable_applies_to_the_command_line(self):
        url = self.serve("/file.bin", self.body)
        catalog = self.write_catalog([entry_for(url, self.body)])
        previous = os.environ.get(fetch.OFFLINE_ENV)
        os.environ[fetch.OFFLINE_ENV] = "1"

        def restore():
            if previous is None:
                os.environ.pop(fetch.OFFLINE_ENV, None)
            else:
                os.environ[fetch.OFFLINE_ENV] = previous

        self.addCleanup(restore)
        code, events = self.run_cli("get", "--catalog", catalog, "--id", "test-file", "--cache", self.cache)
        self.assertEqual((code, events[-1]["event"], events[-1]["code"]), (1, "error", "offline"))
        self.assertEqual(self.requests_for("/file.bin"), [])
        os.environ[fetch.OFFLINE_ENV] = "0"
        code, events = self.run_cli("get", "--catalog", catalog, "--id", "test-file", "--cache", self.cache)
        self.assertEqual(code, 0, events)

    def test_checksum_error_and_override(self):
        url = self.serve("/file.bin", self.body)
        catalog = self.write_catalog([entry_for(url, self.body, checksum={"algo": "sha256", "value": "11" * 32})])
        code, events = self.run_cli("get", "--catalog", catalog, "--id", "test-file", "--cache", self.cache)
        self.assertEqual((code, events[-1]["code"]), (1, "checksum"))
        self.assertEqual(events[-1]["expected"], "11" * 32)
        self.assertEqual(events[-1]["actual"], sha256(self.body))
        self.assertTrue(events[-1]["path"].endswith(".unverified"))
        code, events = self.run_cli("get", "--catalog", catalog, "--id", "test-file", "--cache", self.cache, "--accept-unverified")
        self.assertEqual(code, 0, events)
        self.assertIn("checksum_override", [event.get("code") for event in events if event["event"] == "warning"])
        result = next(event for event in events if event["event"] == "result")
        self.assertTrue(result["checksum_override"])

    def test_record_on_first_download_warns(self):
        url = self.serve("/file.bin", self.body)
        catalog = self.write_catalog([entry_for(url, self.body, checksum=fetch.RECORD_ON_FIRST_DOWNLOAD)])
        code, events = self.run_cli("get", "--catalog", catalog, "--id", "test-file", "--cache", self.cache)
        self.assertEqual(code, 0)
        warning = next(event for event in events if event["event"] == "warning")
        self.assertEqual(warning["code"], "checksum_recorded")
        self.assertEqual(warning["sha256"], sha256(self.body))

    def test_list_and_adopt(self):
        manual = os.path.join(self.tmp, "manual.bin")
        with open(manual, "wb") as f:
            f.write(self.body)
        catalog = self.write_catalog([entry_for("https://example.org/page", self.body, link_out=True)])
        code, events = self.run_cli("list", "--catalog", catalog, "--cache", self.cache)
        self.assertEqual(code, 0)
        rows = [event for event in events if event["event"] == "result"]
        self.assertEqual([(row["id"], row["cached"], row["link_out"], row["badge"]) for row in rows], [("test-file", False, True, "green")])
        code, events = self.run_cli("get", "--catalog", catalog, "--id", "test-file", "--cache", self.cache)
        self.assertEqual((code, events[-1]["code"]), (1, "link_out"))
        code, events = self.run_cli("adopt", "--catalog", catalog, "--id", "test-file", "--file", manual, "--cache", self.cache)
        self.assertEqual(code, 0, events)
        code, events = self.run_cli("list", "--catalog", catalog, "--cache", self.cache)
        self.assertTrue(next(event for event in events if event["event"] == "result")["cached"])


class ManifestAndContentTest(_Base):
    def test_concurrent_manifest_writers_lose_no_record(self):
        threads = [threading.Thread(target=lambda n=n: [fetch._record_file(self.cache, f"files/t{n}/f{i}", {"id": f"{n}-{i}", "sha256": "0" * 64}) for i in range(20)]) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(fetch.read_manifest(self.cache)["files"]), 120)
        self.assertFalse(os.path.exists(f"{fetch.manifest_path(self.cache)}.lock"))

    def test_stale_manifest_lock_is_taken_over(self):
        os.makedirs(self.cache)
        lock = f"{fetch.manifest_path(self.cache)}.lock"
        with open(lock, "w"):
            pass
        old = time.time() - 3600
        os.utime(lock, (old, old))
        fetch._record_file(self.cache, "files/x/y", {"id": "x"})
        self.assertIn("files/x/y", fetch.read_manifest(self.cache)["files"])

    def test_first_download_refuses_a_web_page(self):
        page = b"<!DOCTYPE html><html><body>Please log in to the network</body></html>"
        url = self.serve("/atlas.nii.gz", page)
        entry = entry_for(url, page, filename="atlas.nii.gz", checksum=fetch.RECORD_ON_FIRST_DOWNLOAD)
        with self.assertRaises(fetch.FetchError) as raised:
            fetch.fetch(entry, self.cache, retries=0)
        self.assertEqual(raised.exception.code, "content")
        self.assertIsNone(fetch.cached_path(entry, self.cache))
        self.assertEqual(fetch.read_manifest(self.cache)["files"], {})
        self.assertFalse(os.path.exists(fetch.download_path(entry, self.cache) + ".part"))

    def test_first_download_checks_the_format_of_the_file_name(self):
        text = b"not gzip data at all " * 10
        entry = entry_for(self.serve("/atlas.nii.gz", text), text, filename="atlas.nii.gz", checksum=fetch.RECORD_ON_FIRST_DOWNLOAD)
        with self.assertRaises(fetch.FetchError) as raised:
            fetch.fetch(entry, self.cache, retries=0)
        self.assertEqual(raised.exception.code, "content")
        data = gzip.compress(b"x" * 1000)
        good = entry_for(self.serve("/good.nii.gz", data), data, id="good", filename="good.nii.gz", checksum=fetch.RECORD_ON_FIRST_DOWNLOAD)
        self.assertTrue(os.path.isfile(fetch.fetch(good, self.cache, retries=0)))


class CatalogTest(unittest.TestCase):
    EXPECTED_ATLASES = (
        "schaefer2018-400-7n-2mm", "schaefer2018-400-17n-2mm", "schaefer2018-1000-7n-2mm", "yeo2011-7n", "yeo2011-17n",
        "tian-s1-2mm", "tian-s2-2mm", "tian-s3-2mm", "tian-s4-2mm", "harvard-oxford-cortical-th25-2mm", "harvard-oxford-subcortical-th25-2mm",
        "buckner2011-cerebellum-7n-1mm", "buckner2011-cerebellum-17n-1mm", "mial67-thalamic-nuclei-1mm", "massp20-1mm",
        "harvard-aan-v2", "aal3v1", "hcp-mmp1-volumetric", "brainnetome-246",
    )  # fmt: skip
    EXPECTED_TEMPLATES = (
        "nlin6asym-res01-t1w", "nlin6asym-res02-t1w", "nlin6asym-res01-brain-mask", "nlin6asym-res02-brain-mask", "nlin2009casym-res01-t1w",
        "nlin2009casym-res02-gm-probseg", "nlin2009casym-res02-wm-probseg", "nlin2009casym-res02-csf-probseg", "xfm-nlin6asym-from-2009c",
        "xfm-2009c-from-nlin6asym", "nlin6asym-hospa-ventricles",
    )  # fmt: skip

    def test_catalogs_load_and_every_entry_is_complete(self):
        for name, expected in (("atlases", self.EXPECTED_ATLASES), ("templates", self.EXPECTED_TEMPLATES)):
            catalog = fetch.load_catalog(name)
            self.assertEqual(catalog.data["format"], "slicer-lnm-catalog")
            self.assertEqual(catalog.data["version"], 1)
            for resource_id in expected:
                self.assertIn(resource_id, catalog, resource_id)
            for resource in catalog:
                self.assertEqual(fetch.validate_entry(resource), [], resource.id)
                self.assertIn(resource.badge, fetch.BADGES)  # every entry has a licence badge
                self.assertTrue(resource.licence)
                self.assertTrue(resource.get("citation"))

    def test_badges_follow_the_licence(self):
        self.assertEqual(fetch.badge_for("CC0-1.0"), "green")
        self.assertEqual(fetch.badge_for("MIT"), "green")
        self.assertEqual(fetch.badge_for("CC-BY-4.0"), "green")
        self.assertEqual(fetch.badge_for("CC-BY-SA-4.0"), "amber")
        self.assertEqual(fetch.badge_for("FSL-terms"), "amber")
        self.assertEqual(fetch.badge_for("custom"), "amber")
        self.assertEqual(fetch.badge_for("unknown"), "grey")
        self.assertEqual(fetch.badge_for(None), "grey")
        atlases_ = fetch.load_catalog("atlases")
        self.assertEqual(atlases_.get("harvard-oxford-cortical-th25-2mm").badge, "amber")
        self.assertEqual(atlases_.get("buckner2011-cerebellum-7n-1mm").badge, "grey")
        self.assertEqual(atlases_.get("schaefer2018-400-7n-2mm").badge, "green")
        self.assertEqual(atlases_.get("harvard-aan-v2").licence, "CC0-1.0")
        self.assertEqual(fetch.load_catalog("templates").get("nlin6asym-res02-t1w").badge, "amber")

    def test_no_gsp_derived_item(self):
        for name in fetch.CATALOG_NAMES:
            with open(os.path.join(fetch.CATALOG_DIR, f"{name}.json"), encoding="utf-8") as f:
                text = f.read().lower()
            for word in ("gsp", "dataverse", "ilxiks", "kktjqc", "tractogram", "connectome.json", ".lnmc"):
                self.assertNotIn(word, text, f"{word} in {name}.json")

    def test_entries_carry_url_size_checksum_space_and_label_source(self):
        for name in fetch.CATALOG_NAMES:
            for resource in fetch.load_catalog(name):
                if resource.derive:
                    self.assertTrue(resource.derive.get("from") and resource.derive.get("transform"))
                    continue
                self.assertTrue(resource.urls)
                self.assertTrue(resource.get("family") is None or resource.get("family") == geometry.space_family(resource.get("space")), resource.id)
                grid = resource.get("grid")
                self.assertTrue(grid is None or grid in geometry.GRID_BY_ID, resource.id)
                if grid:
                    self.assertEqual(geometry.GRID_BY_ID[grid].family, resource.get("family"), resource.id)
                if not resource.record_on_first_download:
                    self.assertEqual(len(resource.checksum[1]), {"md5": 32, "sha1": 40, "sha256": 64}[resource.checksum[0]])
                    self.assertTrue(resource.size, resource.id)
                if resource.kind == "atlas":
                    self.assertTrue(resource.get("label_table"), resource.id)

    def test_known_sizes_and_policy_entries(self):
        atlases_ = fetch.load_catalog("atlases")
        templates = fetch.load_catalog("templates")
        self.assertEqual(atlases_.get("tian-s1-2mm").size, 17_983_522)
        self.assertEqual(atlases_.get("tian-s1-2mm").member, "Tian2020MSA/3T/Subcortex-Only/Tian_Subcortex_S1_3T.nii.gz")
        self.assertEqual(atlases_.get("harvard-aan-v2").get("dataset_size"), 187_759_909)
        self.assertTrue(atlases_.get("brainnetome-246").link_out)
        self.assertTrue(atlases_.get("harvard-aan-v2").link_out)  # Dryad only serves signed-in users: adopted after a manual download
        self.assertEqual(atlases_.get("hcp-mmp1-volumetric").licence, "CC-BY-4.0")
        self.assertIn("great care", atlases_.get("hcp-mmp1-volumetric").get("warning"))
        self.assertEqual(templates.get("xfm-nlin6asym-from-2009c").size, 204_735_104)
        self.assertEqual(templates.get("xfm-2009c-from-nlin6asym").size, 204_735_104)
        self.assertEqual(templates.get("nlin2009casym-res01-t1w").get("sampledata"), "MNI152NLin2009cAsym 1mm T1w")
        self.assertEqual(templates.get("nlin6asym-res02-gm-probseg").derive["from"], "nlin2009casym-res02-gm-probseg")
        self.assertEqual(templates.get("nlin6asym-hospa-ventricles").get("labels_of_interest"), {"left_lateral_ventricle": 3, "right_lateral_ventricle": 14})
        # the 2009c and NLin6 atlases are of their own families
        self.assertEqual({atlases_.get(i).get("family") for i in ("mial67-thalamic-nuclei-1mm", "massp20-1mm")}, {"2009"})
        complete = atlases_.get("harvard-oxford-cortical-th25-2mm").get("label_table")["complete"]  # TemplateFlow's table lists 52 of 96 labels
        self.assertEqual((len(complete["names"]), complete["sides"]), (48, ["Left", "Right"]))
        self.assertEqual(atlases_.get("schaefer2018-400-7n-2mm").get("family"), "fsl6")

    def test_no_multipart_etag_is_a_checksum(self):
        for name in fetch.CATALOG_NAMES:
            for resource in fetch.load_catalog(name):
                if resource.checksum:
                    self.assertNotIn("-", resource.checksum[1], resource.id)

    def test_validate_entry_finds_problems(self):
        problems = fetch.validate_entry({"id": "x", "kind": "atlas", "badge": "green", "licence": "CC-BY-SA-4.0", "urls": ["ftp://x"], "checksum": {"algo": "crc32", "value": "zz"}})
        self.assertTrue(any("badge" in p for p in problems))
        self.assertTrue(any("http" in p for p in problems))
        self.assertTrue(any("algorithm" in p for p in problems))
        self.assertTrue(any("hexadecimal" in p for p in problems))
        with self.assertRaises(fetch.CatalogError):
            fetch.Catalog({"format": "other", "version": 1, "entries": []})
        with self.assertRaises(fetch.UnknownResourceError):
            fetch.load_catalog("atlases").get("no-such-id")

    def test_cache_dir(self):
        saved = os.environ.get(fetch.CACHE_ENV)
        try:
            os.environ.pop(fetch.CACHE_ENV, None)
            self.assertTrue(fetch.cache_dir().endswith(os.path.join(".cache", "SlicerStudio", "LNM")))
            os.environ[fetch.CACHE_ENV] = "/tmp/lnm-env-cache"
            self.assertEqual(fetch.cache_dir(), os.path.abspath("/tmp/lnm-env-cache"))
            self.assertEqual(fetch.cache_dir("/tmp/explicit"), os.path.abspath("/tmp/explicit"))
        finally:
            if saved is None:
                os.environ.pop(fetch.CACHE_ENV, None)
            else:
                os.environ[fetch.CACHE_ENV] = saved


# Synthetic atlases


def las_affine(shape, spacing=2.0, origin=(0.0, 0.0, 0.0)):
    """A left-handed (LAS) grid whose voxel (0, 0, 0) is the voxel with the largest x: the layout of FSL's MNI files."""
    x0 = origin[0] + spacing * (shape[0] - 1)
    return np.array([[-spacing, 0, 0, x0], [0, spacing, 0, origin[1]], [0, 0, spacing, origin[2]], [0, 0, 0, 1.0]])


def halves_atlas(family="fsl6", **kwargs):
    """10x10x10 voxels of 2 mm (LAS, world x from -1 to 19, y and z from -1 to 19). Label 1: world x > 9, label 2: x < 9."""
    shape = (10, 10, 10)
    affine = las_affine(shape)
    data = np.zeros(shape, dtype=np.int32)
    data[:5] = 1  # i < 5 is the large-x half in an LAS grid
    data[5:] = 2
    return atlases.Atlas("halves", data, affine, {1: "7Networks_LH_Vis_1", 2: "7Networks_RH_Default_PCC_1"}, family=family, space="MNI152NLin6Asym", networks="schaefer", **kwargs)


class NetworkNameTest(unittest.TestCase):
    def test_schaefer_names(self):
        self.assertEqual(atlases.network_of("7Networks_LH_Vis_1"), "Vis")
        self.assertEqual(atlases.network_of("7Networks_RH_SomMot_12"), "SomMot")
        self.assertEqual(atlases.network_of("7Networks_LH_SalVentAttn_FrOperIns_1"), "SalVentAttn")
        self.assertEqual(atlases.network_of("7Networks_LH_Default_PCC_10"), "Default")
        self.assertEqual(atlases.network_of("7Networks_RH_Cont_Cing_2"), "Cont")
        self.assertEqual(atlases.network_of("7Networks_LH_Limbic_OFC_1"), "Limbic")
        self.assertEqual(atlases.network_of("17Networks_LH_DefaultA_PFCm_1"), "DefaultA")
        self.assertEqual(atlases.network_of("17Networks_RH_VisCent_ExStr_3"), "VisCent")
        self.assertEqual(atlases.network_of("17Networks_LH_TempPar_1"), "TempPar")
        self.assertEqual(atlases.network_of("LH_Vis_1"), "Vis")
        self.assertIsNone(atlases.network_of("Left Thalamus"))
        self.assertIsNone(atlases.network_of(""))
        self.assertIsNone(atlases.network_of(None))

    def test_yeo7_of_17_network_names(self):
        self.assertEqual(atlases.to_yeo7("DefaultB"), "Default")
        self.assertEqual(atlases.to_yeo7("TempPar"), "Default")
        self.assertEqual(atlases.to_yeo7("VisPeri"), "Vis")
        self.assertEqual(atlases.to_yeo7("SalVentAttnA"), "SalVentAttn")
        self.assertEqual(atlases.to_yeo7("ContC"), "Cont")
        self.assertEqual(atlases.to_yeo7("Vis"), "Vis")
        self.assertIsNone(atlases.to_yeo7("HIP"))

    def test_tian_and_buckner_tables(self):
        for name, structure in (("aHIP-rh", "HIP"), ("pHIP-lh", "HIP"), ("lAMY-rh", "AMY"), ("THA-DP-lh", "THA"), ("pTHA-rh", "THA"), ("NAc-shell-rh", "NAc"), ("pGP-lh", "GP"), ("aPUT-rh", "PUT"), ("CAU-body-lh", "CAU"), ("HIP-head-m1-rh", "HIP"), ("CAU-rh", "CAU")):
            self.assertEqual(atlases.network_of(name), structure, name)
        self.assertEqual(atlases.network_of("Network3", "yeo7"), "DorsAttn")
        self.assertEqual(atlases.network_of("Network_7", "yeo7"), "Default")
        self.assertIsNone(atlases.network_of("Network_9", "yeo7"))
        self.assertIsNone(atlases.network_of("Network3"))  # without a scheme only Schaefer and Tian names are read
        atlas = atlases.Atlas("buckner", np.ones((2, 2, 2), dtype=np.int32) * 5, np.eye(4), {5: "Network_5"}, networks="yeo7")
        self.assertEqual(atlas.network(5), "Limbic")


class LabelTableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-labels-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_templateflow_tsv_with_offset(self):
        path = self.write("a.tsv", "index\tname\txyz\n0\tLeft Frontal Pole \t63, 86, 49\n1\tRight Frontal Pole\t21, 86, 33\n")
        self.assertEqual(atlases.read_label_table(path, "tsv"), {0: "Left Frontal Pole", 1: "Right Frontal Pole"})
        self.assertEqual(atlases.read_label_table(path, "tsv", id_offset=1), {1: "Left Frontal Pole", 2: "Right Frontal Pole"})
        schaefer = self.write("s.tsv", "index\tname\tcolor\n1\t7Networks_LH_Vis_1\t#781180\n2\t7Networks_LH_Vis_2\t#781181\n")
        self.assertEqual(atlases.read_label_table(schaefer, "tsv")[2], "7Networks_LH_Vis_2")

    def test_lines_table(self):
        path = self.write("tian.txt", "aHIP-rh\npHIP-rh\n\nlAMY-rh\n")
        self.assertEqual(atlases.read_label_table(path, "lines"), {1: "aHIP-rh", 2: "pHIP-rh", 3: "lAMY-rh"})

    def test_lut_tables(self):
        simple = self.write("hcp.txt", "1 L_V1_ROI\n2 L_MST_ROI\n")
        self.assertEqual(atlases.read_label_table(simple, "lut"), {1: "L_V1_ROI", 2: "L_MST_ROI"})
        freesurfer = self.write("fs.txt", "# comment\n0 Unknown 0 0 0 0\n1 LC 255 0 0 0\n2 DR 0 255 0 0\n")
        self.assertEqual(atlases.read_label_table(freesurfer, "lut"), {0: "Unknown", 1: "LC", 2: "DR"})
        aal = self.write("aal.txt", "1 Precentral_L 2001\n2 Precentral_R 2002\n")
        self.assertEqual(atlases.read_label_table(aal, "lut", values=[2001, 2002]), {2001: "Precentral_L", 2002: "Precentral_R"})
        self.assertEqual(atlases.read_label_table(aal, "lut", values=[1, 2]), {1: "Precentral_L", 2: "Precentral_R"})
        self.assertEqual(atlases.read_label_table(aal, "lut", id_column="last"), {2001: "Precentral_L", 2002: "Precentral_R"})
        name_first = self.write("nf.txt", "Precentral_L 2001\nPrecentral_R 2002\n")
        self.assertEqual(atlases.read_label_table(name_first, "lut"), {2001: "Precentral_L", 2002: "Precentral_R"})
        with self.assertRaises(ValueError):
            atlases.read_label_table(simple, "xml")


class AtlasLoadTest(_Base):
    def make_entry(self, data, affine, labels_text, **fields):
        image = os.path.join(self.tmp, "atlas.nii.gz")
        nifti.write_nifti(image, data, affine, dtype=np.uint8)
        body = read_bytes(image)
        table = labels_text.encode()
        url = self.serve("/atlas.nii.gz", body)
        table_url = self.serve("/labels.tsv", table)
        return entry_for(
            url,
            body,
            id="synthetic",
            filename="atlas.nii.gz",
            networks="schaefer",
            label_table={"format": "tsv", "urls": [table_url], "filename": "labels.tsv", "size": len(table), "checksum": {"algo": "sha256", "value": sha256(table)}},
            **fields,
        )

    def test_load_reads_cache_only_and_labels_through_the_table(self):
        shape = (6, 6, 6)
        data = np.zeros(shape, dtype=np.uint8)
        data[:3], data[3:] = 1, 2
        entry = self.make_entry(data, las_affine(shape), "index\tname\n1\t7Networks_LH_Vis_1\n2\t7Networks_RH_Default_PCC_1\n")
        with self.assertRaises(fetch.NotCachedError):  # never downloads
            atlases.Atlas.load(entry, self.cache)
        self.assertEqual(self.server.log, [])
        fetch.fetch_set(entry, self.cache)
        atlas = atlases.Atlas.load(entry, self.cache)
        self.assertEqual(atlas.family, "fsl6")
        self.assertEqual(atlas.labels, {1: "7Networks_LH_Vis_1", 2: "7Networks_RH_Default_PCC_1"})
        self.assertEqual(atlas.shape, shape)
        self.assertEqual(atlas.badge, "green")
        # the label table is part of the cache state
        os.remove(fetch.label_table_path(entry, self.cache))
        with self.assertRaises(fetch.NotCachedError):
            atlases.Atlas.load(entry, self.cache)

    def test_family_is_recognized_from_the_grid_when_the_entry_has_none(self):
        spec = geometry.GRID_BY_ID["NLin6-2mm"]
        shape = spec.shape
        data = np.zeros(shape, dtype=np.uint8)
        data[40:50, 50:60, 40:50] = 3
        entry = self.make_entry(data, spec.affine, "index\tname\n3\tnucleus\n")
        entry["family"] = None
        entry["space"] = None
        fetch.fetch_set(entry, self.cache)
        atlas = atlases.Atlas.load(entry, self.cache)
        self.assertEqual((atlas.family, atlas.space), ("fsl6", "MNI152NLin6Asym"))

    def test_load_cached_skips_what_is_not_cached(self):
        catalog_path = os.path.join(self.tmp, "cat.json")
        shape = (4, 4, 4)
        data = np.ones(shape, dtype=np.uint8)
        entry = self.make_entry(data, las_affine(shape), "index\tname\n1\ta\n")
        missing = dict(entry, id="missing", filename="m.bin", urls=[dead_url("/m.bin")])
        with open(catalog_path, "w", encoding="utf-8") as f:
            json.dump({"format": fetch.CATALOG_FORMAT, "version": 1, "entries": [entry, missing]}, f)
        fetch.fetch_set(entry, self.cache)
        loaded = atlases.load_cached(self.cache, catalog=catalog_path)
        self.assertEqual([atlas.id for atlas in loaded], ["synthetic"])
        self.assertEqual([(resource.id, available) for resource, available in atlases.cache_status(self.cache, catalog_path)], [("synthetic", True), ("missing", False)])

    def test_a_table_that_lists_only_some_labels_is_completed_by_its_known_order(self):
        shape = (4, 4, 4)
        data = np.zeros(shape, dtype=np.uint8)
        data[0], data[1], data[2], data[3] = 1, 2, 3, 4  # values are index + 1
        table = "index\tname\n0\tLeft Alpha\n1\tRight Alpha\n4\tLeft Gamma\n"  # Beta (2 and 3) and Gamma's right side are missing
        entry = self.make_entry(data, las_affine(shape), table, id_offset=0)
        entry["label_table"]["id_offset"] = 1
        entry["label_table"]["complete"] = {"sides": ["Left", "Right"], "names": ["Alpha", "Beta", "Gamma"]}
        fetch.fetch_set(entry, self.cache)
        atlas = atlases.Atlas.load(entry, self.cache)
        self.assertEqual(atlas.labels, {1: "Left Alpha", 2: "Right Alpha", 5: "Left Gamma", 3: "Left Beta", 4: "Right Beta", 6: "Right Gamma"})
        self.assertEqual(atlas.label_name(3), "Left Beta")

    def test_probability_maps_are_not_label_volumes(self):
        with self.assertRaises(ValueError):
            atlases.Atlas("prob", np.full((3, 3, 3), 0.5), np.eye(4))

    def test_hemispheres_that_share_numbers_are_told_apart_by_x(self):
        shape = (8, 4, 4)
        data = np.zeros(shape, dtype=np.int32)
        data[:, :, :] = 1  # one area number on both sides
        affine = np.array([[2.0, 0, 0, -8.0], [0, 2.0, 0, 0], [0, 0, 2.0, 0], [0, 0, 0, 1.0]])  # voxel centres x = -8 ... 6
        atlas = atlases.Atlas("hcp", data, affine, {1: "L_V1_ROI"}, family="2009")
        atlas._split_hemispheres({"offset": 1000, "left_prefix": "L_", "right_prefix": "R_"})
        self.assertEqual(atlas.label_name(1), "L_V1_ROI")
        self.assertEqual(atlas.label_name(1001), "R_V1_ROI")
        self.assertEqual(int((atlas.data == 1).sum()), 5 * 16)  # x = -8, -6, -4, -2, 0 (the midline counts as left)
        self.assertEqual(atlas.label_name(atlas.values_at([[4.0, 0.0, 0.0]])[0]), "R_V1_ROI")
        self.assertEqual(atlas.label_name(atlas.values_at([[-4.0, 0.0, 0.0]])[0]), "L_V1_ROI")


class LabelPointTest(unittest.TestCase):
    def test_points_go_through_the_affine(self):
        atlas = halves_atlas()
        # voxel (i, j, k) of the LAS grid has its centre at x = 18 - 2 i, y = 2 j, z = 2 k
        for xyz, expected in (((18.0, 0.0, 0.0), 1), ((10.0, 4.0, 6.0), 1), ((8.0, 4.0, 6.0), 2), ((0.0, 18.0, 18.0), 2)):
            rows = atlases.label_point(xyz, atlas)
            self.assertEqual(rows, [("halves", atlas.labels[expected], "Vis" if expected == 1 else "Default")], xyz)
        # array indices would put x = 18 at the end of the array: the affine puts it in the first voxels
        self.assertEqual(atlas.values_at([[18.0, 0.0, 0.0]])[0], atlas.data[0, 0, 0])
        self.assertEqual(atlas.values_at([[0.0, 0.0, 0.0]])[0], atlas.data[9, 0, 0])

    def test_nearest_voxel_and_outside(self):
        atlas = halves_atlas()
        self.assertEqual(atlases.label_point((10.9, 0.4, 0.4), atlas)[0][1], atlas.labels[1])  # nearest centre is x = 10
        self.assertEqual(atlases.label_point((9.1, 0.0, 0.0), atlas)[0][1], atlas.labels[1])  # x = 10 is nearer than x = 8
        self.assertEqual(atlases.label_point((8.9, 0.0, 0.0), atlas)[0][1], atlas.labels[2])
        self.assertEqual(atlases.label_point((50.0, 0.0, 0.0), atlas), [("halves", None, None)])
        self.assertEqual(atlases.label_point((-3.0, 0.0, 0.0), atlas), [("halves", None, None)])  # beyond the first voxel (it spans -1 to 1)

    def test_two_atlases_and_many_points(self):
        first = halves_atlas()
        second = atlases.Atlas("second", np.ones((3, 3, 3), dtype=np.int32), np.array([[1.0, 0, 0, 7.0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]), {1: "block"}, family="fsl6")
        rows = atlases.label_point((8.0, 1.0, 1.0), [first, second])
        self.assertEqual([row[0] for row in rows], ["halves", "second"])
        self.assertEqual(rows[1][1:], ("block", None))
        results = atlases.label_points([(18.0, 0.0, 0.0), (0.0, 0.0, 0.0)], first)
        self.assertEqual([r[0][1] for r in results], [first.labels[1], first.labels[2]])

    def test_points_with_a_space_follow_the_family_rule(self):
        atlas = halves_atlas()
        refused = atlases.label_point((10.0, 4.0, 6.0), atlas, space="MNI152NLin2009cAsym")
        self.assertEqual(list(refused), [])
        self.assertEqual(refused.notices[0]["code"], "family_mismatch")
        allowed = atlases.label_point((10.0, 4.0, 6.0), atlas, space="MNI152NLin2009cAsym", allow_cross_family=True)
        self.assertEqual(len(allowed), 1)
        self.assertEqual(allowed.allowed_cross_family[0]["atlas"], "halves")
        self.assertEqual(len(atlases.label_point((10.0, 4.0, 6.0), atlas, space="MNI152NLin6Asym")), 1)


def block_mask(x_edges, y_edges, z_edges, spacing=1.0):
    """A mask on an RAS grid with voxel edges on multiples of ``spacing``: returns (occupancy, affine) of a full block."""
    lows = [e[0] for e in (x_edges, y_edges, z_edges)]
    shape = tuple(round((e[1] - e[0]) / spacing) for e in (x_edges, y_edges, z_edges))
    affine = np.diag([spacing, spacing, spacing, 1.0])
    affine[:3, 3] = [low + spacing / 2 for low in lows]
    return np.ones(shape), affine


class LabelMaskTest(unittest.TestCase):
    def test_percentages_on_a_synthetic_atlas(self):
        atlas = halves_atlas()
        # a 4 x 4 x 4 mm block at x 8..12, y 1..5, z 1..5 on a 1 mm grid: 3 mm of it (x 9..12) in label 1, 1 mm in label 2
        occupancy, affine = block_mask((8, 12), (1, 5), (1, 5))
        result = atlases.label_mask(occupancy, affine, atlas, space="MNI152NLin6Asym")
        self.assertEqual([row.label for row in result], [atlas.labels[1], atlas.labels[2]])
        (first, second) = result
        self.assertAlmostEqual(first.pct_of_mask, 75.0, places=6)
        self.assertAlmostEqual(second.pct_of_mask, 25.0, places=6)
        region = 5 * 10 * 10 * 8.0  # voxels of a label times 8 mm^3
        self.assertAlmostEqual(first.pct_of_region, 100 * 48.0 / region, places=6)
        self.assertAlmostEqual(second.pct_of_region, 100 * 16.0 / region, places=6)
        self.assertAlmostEqual(result.unlabelled["halves"], 0.0, places=6)
        self.assertEqual(result.details[0]["label_id"], 1)
        self.assertEqual(result.details[0]["network"], "Vis")
        self.assertAlmostEqual(result.details[0]["volume_mm3"], 48.0, places=6)
        self.assertEqual(result.notices, [])
        self.assertEqual(result.mask_family, "fsl6")

    def test_occupancy_above_one_is_bounded(self):
        atlas = halves_atlas()
        occupancy, affine = block_mask((10, 12), (2, 4), (2, 4))
        reference = atlases.label_mask(occupancy, affine, atlas, space="MNI152NLin6Asym")
        scaled = atlases.label_mask((occupancy * 255).astype(np.uint8), affine, atlas, space="MNI152NLin6Asym")
        self.assertAlmostEqual(scaled[0].pct_of_region, reference[0].pct_of_region, places=6)
        self.assertLessEqual(scaled[0].pct_of_region, 100.0)
        self.assertAlmostEqual(scaled[0].pct_of_mask, 100.0)

    def test_row_is_a_four_tuple(self):
        occupancy, affine = block_mask((10, 12), (2, 4), (2, 4))
        atlas, label, pct_mask, pct_region = atlases.label_mask(occupancy, affine, halves_atlas(), space="MNI152NLin6Asym")[0]
        self.assertEqual((atlas, label), ("halves", "7Networks_LH_Vis_1"))
        self.assertAlmostEqual(pct_mask, 100.0)
        self.assertAlmostEqual(pct_region, 100 * 8.0 / 4000.0)

    def test_mask_in_another_orientation_and_resolution(self):
        atlas = halves_atlas()
        # a block at x 8..12 of which only the large-x half (x 10..12, voxels 2 and 3 of the RAS grid) is set: all of it is in label 1
        occupancy, affine = block_mask((8, 12), (1, 5), (1, 5))
        occupancy[:2] = 0.0
        result = atlases.label_mask(occupancy, affine, atlas, space="MNI152NLin6Asym")
        self.assertEqual([row.label for row in result], [atlas.labels[1]])
        # the same mask stored LAS (file order flipped): reading it by array index would swap the sides of the block
        flipped = occupancy[::-1].copy()
        flipped_affine = affine.copy()
        flipped_affine[0, 0] = -1.0
        flipped_affine[0, 3] = affine[0, 3] + 3.0  # voxel (0, 0, 0) is now the one with the largest x
        result = atlases.label_mask(flipped, flipped_affine, atlas, space="MNI152NLin6Asym")
        self.assertEqual([row.label for row in result], [atlas.labels[1]])
        self.assertAlmostEqual(result[0].pct_of_mask, 100.0, places=6)
        # the wrong reading (array order taken as RAS) would put the set voxels at x 8..10: half in each label
        wrong = atlases.label_mask(flipped, affine, atlas, space="MNI152NLin6Asym")
        self.assertEqual(len(wrong), 2)
        # a 2 mm mask grid at x 8..12 (voxel edges on even numbers): half of each of two voxels lies in label 1
        coarse, coarse_affine = block_mask((8, 12), (0, 4), (0, 4), spacing=2.0)
        coarse_result = atlases.label_mask(coarse, coarse_affine, atlas, space="MNI152NLin6Asym")
        self.assertAlmostEqual(coarse_result[0].pct_of_mask, 75.0, places=6)

    def test_a_single_small_voxel_survives_a_coarser_atlas(self):
        atlas = halves_atlas()
        occupancy = np.zeros((20, 20, 20))
        occupancy[15, 10, 10] = 1.0
        affine = np.diag([0.5, 0.5, 0.5, 1.0])
        affine[:3, 3] = [-0.25, -0.25, -0.25]  # a 0.5 mm grid: voxel 15 spans x 7.0 to 7.5 (label 2)
        result = atlases.label_mask(occupancy, affine, atlas, space="MNI152NLin6Asym")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].label, atlas.labels[2])
        self.assertAlmostEqual(result[0].pct_of_mask, 100.0, places=6)
        self.assertAlmostEqual(result[0].pct_of_region, 100 * 0.125 / 4000.0, places=6)

    def test_oblique_mask_inside_one_label(self):
        atlas = halves_atlas()
        angle = np.deg2rad(30)
        rotation = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
        affine = np.eye(4)
        affine[:3, :3] = rotation  # 1 mm voxels turned about z
        affine[:3, 3] = [14.0, 4.0, 6.0]
        occupancy = np.ones((4, 4, 4))
        result = atlases.label_mask(occupancy, affine, atlas, space="MNI152NLin6Asym")
        self.assertEqual([row.label for row in result], [atlas.labels[1]])
        self.assertAlmostEqual(result[0].pct_of_mask, 100.0, places=4)

    def test_fractional_occupancy_counts_by_its_fraction(self):
        atlas = halves_atlas()
        occupancy, affine = block_mask((10, 12), (2, 4), (2, 4))
        result = atlases.label_mask(occupancy * 0.5, affine, atlas, space="MNI152NLin6Asym")
        self.assertAlmostEqual(result[0].pct_of_mask, 100.0, places=6)  # still all of the mask
        self.assertAlmostEqual(result[0].pct_of_region, 100 * 4.0 / 4000.0, places=6)

    def test_outside_the_atlas_is_unlabelled(self):
        atlas = halves_atlas()
        occupancy, affine = block_mask((16, 24), (2, 4), (2, 4))  # x 16..19 inside, 19..24 outside the atlas
        result = atlases.label_mask(occupancy, affine, atlas, space="MNI152NLin6Asym")
        self.assertAlmostEqual(result[0].pct_of_mask, 100 * 3.0 / 8.0, places=6)
        self.assertAlmostEqual(result.unlabelled["halves"], 100 * 5.0 / 8.0, places=6)

    def test_top_and_min_pct_and_empty_mask(self):
        atlas = halves_atlas()
        occupancy, affine = block_mask((8, 12), (1, 5), (1, 5))
        self.assertEqual(len(atlases.label_mask(occupancy, affine, atlas, space="MNI152NLin6Asym", top=1)), 1)
        self.assertEqual(len(atlases.label_mask(occupancy, affine, atlas, space="MNI152NLin6Asym", min_pct=50)), 1)
        empty = atlases.label_mask(np.zeros((3, 3, 3)), np.eye(4), atlas, space="MNI152NLin6Asym")
        self.assertEqual((list(empty), empty.notices[0]["code"]), ([], "empty_mask"))
        with self.assertRaises(ValueError):
            atlases.label_mask(np.ones((3, 3)), np.eye(4), atlas)

    def test_two_atlases_label_one_mask(self):
        first = halves_atlas()
        second = atlases.Atlas("second", np.where(np.arange(10)[:, None, None] < 5, 7, 8) * np.ones((10, 10, 10), dtype=np.int32), las_affine((10, 10, 10)), {7: "big x", 8: "small x"}, family="fsl6")
        occupancy, affine = block_mask((10, 12), (2, 4), (2, 4))
        result = atlases.label_mask(occupancy, affine, [first, second], space="MNI152NLin6Asym")
        self.assertEqual([(row.atlas, row.label) for row in result], [("halves", first.labels[1]), ("second", "big x")])


class CrossFamilyTest(unittest.TestCase):
    def setUp(self):
        self.atlas = halves_atlas()  # family fsl6
        self.occupancy, self.affine = block_mask((8, 12), (1, 5), (1, 5))

    def test_refused_by_default(self):
        result = atlases.label_mask(self.occupancy, self.affine, self.atlas, space="MNI152NLin2009cAsym")
        self.assertEqual(list(result), [])
        self.assertEqual(result.refused, ["halves"])
        self.assertEqual(result.notices[0]["code"], "family_mismatch")
        self.assertIn("fsl6", result.notices[0]["message"])
        self.assertIn("2009", result.notices[0]["message"])
        self.assertEqual(result.allowed_cross_family, [])
        self.assertEqual(result.mask_family, "2009")

    def test_allowed_explicitly_and_recorded(self):
        result = atlases.label_mask(self.occupancy, self.affine, self.atlas, allow_cross_family=True, space="MNI152NLin2009cAsym")
        self.assertEqual(len(result), 2)
        self.assertEqual(result.notices, [])
        self.assertEqual(result.allowed_cross_family, [{"atlas": "halves", "atlas_family": "fsl6", "mask_family": "2009", "mask_space": "MNI152NLin2009cAsym", "reason": "family_mismatch"}])

    def test_unknown_space_is_refused_too(self):
        result = atlases.label_mask(self.occupancy, self.affine, self.atlas)  # a 1 mm block grid is no standard grid
        self.assertEqual(list(result), [])
        self.assertEqual(result.notices[0]["code"], "family_unknown")
        self.assertEqual(len(atlases.label_mask(self.occupancy, self.affine, self.atlas, allow_cross_family=True)), 2)
        unknown_atlas = halves_atlas(family="unknown")
        self.assertEqual(list(atlases.label_mask(self.occupancy, self.affine, unknown_atlas, space="MNI152NLin6Asym")), [])

    def test_family_is_recognized_from_the_grid(self):
        # a mask on the exact NLin6 2 mm grid is of the fsl6 family and an atlas of that family labels it
        grid = geometry.GRID_BY_ID["NLin6-2mm"]
        occupancy = np.zeros(grid.shape)
        occupancy[44:48, 50:54, 40:44] = 1
        atlas = atlases.Atlas("nlin6", np.ones(grid.shape, dtype=np.int32), grid.affine, {1: "everything"}, family="fsl6")
        result = atlases.label_mask(occupancy, grid.affine, atlas)
        self.assertEqual((result.mask_family, len(result), result.notices), ("fsl6", 1, []))
        # the same mask against a 2009 atlas is refused
        other = atlases.Atlas("tf2009", np.ones(grid.shape, dtype=np.int32), grid.affine, {1: "everything"}, family="2009")
        refused = atlases.label_mask(occupancy, grid.affine, other)
        self.assertEqual((list(refused), refused.notices[0]["code"]), ([], "family_mismatch"))
        # a mask on the 2009c grid is of the 2009 family
        grid09 = geometry.GRID_BY_ID["TF2009-2mm"]
        occupancy09 = np.zeros(grid09.shape)
        occupancy09[40:44, 50:54, 40:44] = 1
        self.assertEqual(atlases.label_mask(occupancy09, grid09.affine, other).mask_family, "2009")

    def test_mixed_list_labels_only_the_compatible_atlas(self):
        compatible = halves_atlas()
        other = atlases.Atlas("other", np.ones((10, 10, 10), dtype=np.int32), las_affine((10, 10, 10)), {1: "x"}, family="2009")
        result = atlases.label_mask(self.occupancy, self.affine, [other, compatible], space="MNI152NLin6Asym")
        self.assertEqual({row.atlas for row in result}, {"halves"})
        self.assertEqual(result.refused, ["other"])


class LabelClusterTest(unittest.TestCase):
    def setUp(self):
        spec = geometry.GRID_BY_ID["NLin6-2mm"]
        self.grid = spec.grid
        data = np.zeros(spec.shape, dtype=np.int32)
        data[:45] = 1  # LAS grid: small i is large x
        data[45:] = 2
        self.atlas = atlases.Atlas("nlin6-halves", data, spec.affine, {1: "right", 2: "left"}, family="fsl6", space="MNI152NLin6Asym")

    def test_flat_vector_masked_vector_and_peak(self):
        volume = np.zeros(self.grid.shape)
        volume[10:14, 50:54, 40:44] = 1  # i < 45: large x, label 1
        volume[50:52, 50:52, 40:42] = 1  # label 2
        flat = volume.reshape(-1)
        result = atlases.label_cluster(flat, self.grid, self.atlas, top=1)
        self.assertEqual([(row.atlas, row.label) for row in result], [("nlin6-halves", "right")])
        self.assertAlmostEqual(result[0].pct_of_mask, 100 * 64 / 72, places=6)
        self.assertEqual(result.mask_family, "fsl6")
        both = atlases.label_cluster(volume, self.grid, self.atlas, top=None)
        self.assertEqual([row.label for row in both], ["right", "left"])
        indices = geometry.mask_indices(volume)
        masked = geometry.to_masked_vector(volume, indices)
        again = atlases.label_cluster(masked, self.grid, self.atlas, indices=indices, top=None)
        self.assertEqual([tuple(row) for row in again], [tuple(row) for row in both])
        peak_xyz = self.grid.voxel_to_world([[11, 51, 41]])[0]
        withpeak = atlases.label_cluster(flat, self.grid, self.atlas, peak=peak_xyz)
        self.assertEqual(withpeak.peak, [("nlin6-halves", "right", None)])
        with self.assertRaises(ValueError):
            atlases.label_cluster(np.ones(10), self.grid, self.atlas)

    def test_cluster_in_another_family_is_refused(self):
        other = atlases.Atlas("tf2009", np.ones(self.grid.shape, dtype=np.int32), self.grid.affine, {1: "x"}, family="2009")
        volume = np.zeros(self.grid.shape)
        volume[10:12, 50:52, 40:42] = 1
        result = atlases.label_cluster(volume, self.grid, [self.atlas, other])
        self.assertEqual(result.refused, ["tf2009"])
        self.assertEqual({row.atlas for row in result}, {"nlin6-halves"})
        self.assertEqual([row[0] for row in atlases.label_cluster(volume, self.grid, [self.atlas, other], peak=(60.0, 0.0, 0.0)).peak], ["nlin6-halves"])


if __name__ == "__main__":
    unittest.main()

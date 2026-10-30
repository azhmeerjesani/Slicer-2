"""Tests of the connectome fetcher (fetchconnectome.py) against a fake Dataverse.

The fake server is a ``http.server`` on 127.0.0.1 with the dataset JSON API (live terms text, version, files with
MD5), ``/api/access/datafile/<id>`` answering 303 to a signed URL, and a Range-capable file server that answers 403
once a signed URL has served its budget of bytes (and cuts the connection at that point), so that the re-signing,
the reconnection and the resume at a member offset are all exercised. Nothing leaves the machine, and no real data is
downloaded.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingFetchConnectomeTest.py -v
"""

import copy
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np
from scipy import io as sio

from LesionNetworkMappingLib import catalog, connectome, fetchconnectome, jobs, manager, nifti, runs

GSP_RUN = "GSP1000/sub-{s}/func/sub-{s}_bld00{r}_rest_skip4_stc_mc_bp_0.0001_0.08_resid_FS1mm_MNI1mm_MNI2mm_sm7_finalmask.nii.gz"
TERMS_TEXT = "<p>GSP Open Access Data Use Terms (2014-Apr-22)</p><p>1. I will not attempt to identify any participant.<br>2. I will acknowledge the project in publications. See <a href=\"https://example.org/terms\">the terms</a>.</p>"
TERMS_SHA = catalog.terms_sha256(TERMS_TEXT)
ACKNOWLEDGEMENT = "Data were provided by the Brain Genomics Superstruct Project (test acknowledgement)."
PARTICIPANTS_TSV = b"participant_id\tage\nsub-0001\t20\nsub-0002\t22\nsub-0003\t24\nsub-0004\t26\nsub-0005\t28\n"
DOI = "10.7910/DVN/FAKE000"


def _ellipsoid(shape, scale=1.3):
    ijk = np.indices(shape).reshape(3, -1).T
    center = (np.array(shape) - 1) / 2.0
    return ((((ijk - center) / (np.array(shape) / scale)) ** 2).sum(axis=1) <= 1.0).reshape(shape)


def _tar(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _md5(data):
    return hashlib.md5(data).hexdigest()  # noqa: S324


class FakeDataverse:
    """A fake Dataverse server: dataset JSON, 303 redirects to signed URLs and a Range-capable file server."""

    def __init__(self):
        self.files = {}
        self.version = (3, 0)
        self.terms = TERMS_TEXT
        self.token_budget = None  # bytes a signed URL serves before it answers 403 (None: unlimited)
        self.ignore_range = False
        self.delay_s = 0.0  # pause after every 64 kB piece (to see a download in progress)
        self.listing_md5 = {}  # file id -> md5 listed instead of the real one
        self.requests = []
        self.access = []
        self.tokens = {}
        self.forbidden = 0
        self._lock = threading.Lock()
        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"

    def close(self):
        self.http.shutdown()
        self.http.server_close()

    def reset(self):
        with self._lock:
            self.version = (3, 0)
            self.terms = TERMS_TEXT
            self.token_budget = None
            self.ignore_range = False
            self.delay_s = 0.0
            self.listing_md5 = {}
            self.requests, self.access, self.tokens = [], [], {}
            self.forbidden = 0

    def add(self, file_id, name, data, folder="GSP1000", tabular=False, listed_size=None):
        self.files[file_id] = {"name": name, "data": data, "folder": folder, "tabular": tabular, "listed_size": listed_size}

    def dataset(self):
        files = []
        for file_id, item in self.files.items():
            md5 = self.listing_md5.get(file_id) or _md5(item["data"])
            files.append({"label": item["name"], "directoryLabel": item["folder"], "dataFile": {
                "id": file_id, "filename": item["name"], "filesize": item["listed_size"] or len(item["data"]), "md5": md5, "checksum": {"type": "MD5", "value": md5}, "tabularData": item["tabular"]}})
        return {"status": "OK", "data": {"latestVersion": {"versionNumber": self.version[0], "versionMinorNumber": self.version[1], "termsOfUse": self.terms, "files": files}}}

    def requests_for(self, file_id):
        return [r for r in self.requests if r["file"] == file_id]

    def _handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # one request per connection: a cut body is an incomplete read

            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                path = parts.path
                if path.startswith("/api/datasets/"):
                    body = json.dumps(outer.dataset()).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path.startswith("/api/access/datafile/"):
                    file_id = int(path.rsplit("/", 1)[1])
                    with outer._lock:
                        outer.access.append({"file": file_id, "query": parts.query})
                        token = f"tok{len(outer.tokens) + 1}"
                        outer.tokens[token] = {"file": file_id, "served": 0}
                    self.send_response(303)
                    self.send_header("Location", f"/files/{token}/{file_id}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif path.startswith("/files/"):
                    self._file(path.split("/"))
                else:
                    self.send_error(404)

            def _file(self, parts):
                token, file_id = parts[2], int(parts[3])
                item = outer.files[file_id]
                data = item["data"]
                entry = outer.tokens.get(token)
                range_header = self.headers.get("Range")
                start = 0
                if range_header and not outer.ignore_range:
                    match = re.match(r"bytes=(\d+)-", range_header)
                    start = int(match.group(1)) if match else 0
                with outer._lock:
                    outer.requests.append({"file": file_id, "token": token, "range": range_header, "start": start})
                    budget = outer.token_budget
                    exhausted = entry is None or (budget is not None and entry["served"] >= budget)
                    if exhausted:
                        outer.forbidden += 1
                if exhausted:
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if start >= len(data):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(data)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = data[start:]
                if range_header and not outer.ignore_range:
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                allowed = len(body) if budget is None else min(len(body), max(0, budget - entry["served"]))
                try:
                    for offset in range(0, allowed, 65536):
                        piece = body[offset : min(offset + 65536, allowed)]
                        self.wfile.write(piece)
                        with outer._lock:
                            entry["served"] += len(piece)
                        if outer.delay_s:
                            time.sleep(outer.delay_s)
                except OSError:
                    pass

        return Handler


def _fast_retries():
    return mock.patch.object(fetchconnectome, "RETRY_DELAYS_S", (0.0,))


class FetchConnectomeBase(unittest.TestCase):
    """Builds three GSP-like archives (5 subjects, the real GSP run grid, short runs) and serves them."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="lnm-fetch-test-")
        cls._no_proxy = os.environ.get("NO_PROXY"), os.environ.get("no_proxy")
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"  # the requests session honours the environment's proxies
        rng = np.random.default_rng(5)
        grid = connectome.GSP_RUN_GRID
        support = _ellipsoid(grid.shape)
        cls.runs_dir = os.path.join(cls.root, "runs")
        os.makedirs(cls.runs_dir)
        layout = [["0001", "0002"], ["0003", "0004"], ["0005"]]
        cls.archives = []
        for subjects in layout:
            members = [("._junk", b"AppleDouble"), ("GSP1000/README", b"text")]
            for subject in subjects:
                data = np.zeros((*support.shape, 3), dtype=np.float32)
                data[support] = (100.0 + rng.standard_normal((int(support.sum()), 3))).astype(np.float32)
                path = os.path.join(cls.runs_dir, f"{subject}.nii.gz")
                nifti.write_nifti(path, data, grid.affine, time_step=3.0)
                with open(path, "rb") as f:
                    members.append((GSP_RUN.format(s=subject, r=1), f.read()))
                members.append((f"GSP1000/sub-{subject}/anat/T1.nii.gz", b"not read"))
            cls.archives.append(_tar(members))
        cls.server = FakeDataverse()
        names = [f"GSP1000_v2_{i:02d}.tar" for i in range(3)]
        for index, (name, data) in enumerate(zip(names, cls.archives, strict=True)):
            cls.server.add(1000 + index, name, data)
        cls.server.add(1100, "participants.tab", PARTICIPANTS_TSV, tabular=True, listed_size=len(PARTICIPANTS_TSV) + 40)
        cls.server.add(1101, "README", b"GSP1000 readme\n")
        cls.entry = cls._entry(names)

    @classmethod
    def _entry(cls, names):
        data = copy.deepcopy(catalog.load_catalog().get("gsp1000-ilxiks"))
        api = f"https://dataverse.invalid/api/datasets/:persistentId/?persistentId=doi:{DOI}"
        data["id"] = "fake-gsp"
        data["title"] = "GSP1000 Preprocessed Connectome (fake, v3.0)"
        data["acknowledgement"] = ACKNOWLEDGEMENT
        data["source"].update({"doi": DOI, "url": f"https://doi.org/{DOI}", "api": api, "version": "3.0"})
        data["terms"].update({"api": api, "url": f"https://doi.org/{DOI}"})
        files = []
        for index, name in enumerate(names):
            files.append({"name": name, "folder": "GSP1000", "id": 1000 + index, "size": len(cls.archives[index]), "checksum": {"algo": "md5", "value": _md5(cls.archives[index])}, "role": "archive"})
        files.append({"name": "README", "folder": "GSP1000", "id": 1101, "size": 15, "checksum": {"algo": "md5", "value": _md5(b"GSP1000 readme\n")}, "role": "docs"})
        files.append({"name": "participants.tab", "folder": "GSP1000", "id": 1100, "size": len(PARTICIPANTS_TSV) + 40, "checksum": {"algo": "md5", "value": _md5(PARTICIPANTS_TSV)}, "role": "participants"})
        data["files"] = files
        data["size"] = sum(f["size"] for f in files)
        data["disk"].update({"subjects": 5, "voxels": None, "timepoints_per_run": None, "stored_bytes": None})
        data["population"]["n"] = 5
        problems = catalog.validate_entry(data)
        assert not problems, problems
        return data

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        shutil.rmtree(cls.root, ignore_errors=True)
        for key, value in zip(("NO_PROXY", "no_proxy"), cls._no_proxy, strict=True):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        self.server.reset()
        self.dest = tempfile.mkdtemp(prefix="lnm-fetch-dest-")
        self.events = []
        patcher = _fast_retries()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.dest, True)

    def emit(self, event, **fields):
        self.events.append({"event": event, **fields})

    def fetch(self, entry=None, **kwargs):
        kwargs.setdefault("terms_sha", TERMS_SHA)
        kwargs.setdefault("streams", 2)
        kwargs.setdefault("server", self.server.url)
        kwargs.setdefault("emit", self.emit)
        return fetchconnectome.fetch_connectome(entry or self.entry, kwargs.pop("dest", self.dest), **kwargs)

    def bundle(self):
        return fetchconnectome.bundle_path(self.entry, self.dest)

    def reference(self, name="reference.lnmc"):
        """The same archives converted directly (no network), for comparison."""
        out = os.path.join(self.root, name)
        shutil.rmtree(out, ignore_errors=True)
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", connectome.ConversionWarning)
            for index, data in enumerate(self.archives):
                list(connectome.from_tar_stream(io.BytesIO(data), out, finalize=index == len(self.archives) - 1, name="reference"))
        return out

    def assertSameSubjects(self, first, second):
        a, b = connectome.ConnectomeBundle.open(first), connectome.ConnectomeBundle.open(second)
        self.assertEqual(a.subject_ids, b.subject_ids)
        self.assertEqual(a.mask_sha256, b.mask_sha256)
        for subject_id in a.subject_ids:
            sa, sb = a.load_subject(subject_id), b.load_subject(subject_id)
            self.assertTrue(np.array_equal(sa.data, sb.data), subject_id)
            self.assertTrue(np.array_equal(sa.norms, sb.norms), subject_id)

    def warnings(self, code):
        return [e for e in self.events if e["event"] == "warning" and e.get("code") == code]


class FetchRunTest(FetchConnectomeBase):
    def test_fetch_equals_the_direct_conversion_and_stores_no_archive(self):
        summary = self.fetch()
        reference = self.reference()
        self.assertSameSubjects(summary["path"], reference)
        self.assertEqual(summary["subjects"], 5)
        self.assertEqual(summary["archives"], 3)
        stored = [name for _, _, names in os.walk(self.dest) for name in names if name.endswith((".tar", ".part"))]
        self.assertEqual(stored, [])  # no archive is stored by default
        self.assertFalse(os.path.exists(os.path.join(fetchconnectome.work_dir(self.entry, self.dest), "archives")))
        self.assertFalse(os.path.exists(os.path.join(summary["path"], connectome.STAGING_DIR_NAME)))
        # Every archive was read from its first byte: the whole-archive MD5 was verified on the fly
        archives = [e for e in self.events if e["event"] == "result" and e.get("kind") == "archive"]
        self.assertEqual([e["verification"] for e in archives], ["md5", "md5", "md5"])
        self.assertEqual(summary["unverified_resumed"], 0)
        self.assertFalse(summary["checksum_override"])

    def test_connectome_json_provenance_without_local_paths(self):
        summary = self.fetch()
        bundle = connectome.ConnectomeBundle.open(summary["path"])
        self.assertEqual(bundle.metadata["format"], "lnmc")
        self.assertEqual(bundle.metadata["version"], 1)
        provenance = bundle.provenance
        self.assertEqual(provenance["source"]["doi"], DOI)
        self.assertEqual(provenance["source"]["version"], "3.0")
        names = {f["name"]: f for f in provenance["source"]["files"]}
        self.assertEqual(names["GSP1000_v2_00.tar"]["md5"], _md5(self.archives[0]))
        self.assertEqual(names["GSP1000_v2_00.tar"]["verification"], "md5")
        self.assertEqual(provenance["licence"], "GSP-DUT-2014")
        self.assertEqual(provenance["termsSha256"], TERMS_SHA)
        self.assertEqual(provenance["acknowledgement"], ACKNOWLEDGEMENT)
        self.assertEqual(provenance["population"]["n"], 5)
        self.assertEqual(provenance["population"]["ageRange"], [18, 36])
        self.assertIs(provenance["preprocessing"]["gsr"], True)
        self.assertEqual(provenance["trS"], 3.0)
        self.assertEqual(provenance["kind"], "timeseries")
        self.assertEqual(self.entry["space"], "MNI152NLin6Asym")
        self.assertEqual(bundle.metadata.get("space"), "MNI152NLin6Asym")  # a fetched bundle records the catalog's space
        self.assertEqual(manager.bundle_summary(summary["path"])["space"], "MNI152NLin6Asym")  # the manager's Space column
        self.assertEqual(provenance["participants"], "participants.tsv")
        self.assertNotIn("checksumOverride", provenance["source"])
        with open(os.path.join(summary["path"], "participants.tsv"), "rb") as f:
            self.assertEqual(f.read(), PARTICIPANTS_TSV)  # format=original: the file as it was uploaded, not the ingested .tab
        with open(os.path.join(summary["path"], "terms-accepted.txt"), encoding="utf-8") as f:
            self.assertEqual(catalog.terms_sha256(f.read()), TERMS_SHA)
        self.assertTrue(os.path.isfile(os.path.join(summary["path"], "docs", "README")))
        with open(os.path.join(summary["path"], "connectome.json"), encoding="utf-8") as f:
            self.assertNotIn(self.dest, f.read())  # a file that may be shared holds no local path
        tabular = [a for a in self.server.access if a["file"] == 1100]
        self.assertEqual([a["query"] for a in tabular], ["format=original"])
        self.assertTrue(all(a["query"] == "" for a in self.server.access if a["file"] != 1100))

    def test_the_run_is_registered(self):
        summary = self.fetch()
        records = runs.list_runs(self.dest, kind="fetch")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "done")
        self.assertEqual(records[0]["id"], summary["run_id"])
        self.assertEqual(records[0]["params"]["termsSha256"], TERMS_SHA)
        self.assertFalse(runs.read_summary(self.dest, records[0])["checksum_override"])
        self.assertNotIn(self.dest, json.dumps(records[0]["params"]))

    def test_streams_one_and_four_give_the_same_bundle(self):
        reference = self.reference()
        for streams in (1, 4):
            dest = os.path.join(self.dest, f"s{streams}")
            summary = self.fetch(dest=dest, streams=streams)
            self.assertSameSubjects(summary["path"], reference)
        with self.assertRaises(jobs.JobError) as ctx:
            self.fetch(dest=os.path.join(self.dest, "s5"), streams=5)
        self.assertEqual(ctx.exception.code, "usage")

    def test_subset_of_archives(self):
        summary = self.fetch(subset="archives:00-01")
        bundle = connectome.ConnectomeBundle.open(summary["path"])
        self.assertEqual(bundle.subject_ids, ["sub-0001", "sub-0002", "sub-0003", "sub-0004"])
        self.assertEqual(bundle.provenance["source"]["subset"], "archives:00-01")
        self.assertIsNone(bundle.provenance["population"].get("ageRange"))  # the subjects of a part are not the entry's population
        self.assertEqual(bundle.provenance["population"]["n"], 4)
        self.assertFalse([r for r in self.server.requests if r["file"] == 1002])  # the third archive was not requested

    def test_keep_archives_stores_each_archive(self):
        summary = self.fetch(keep_archives=True)
        for index, data in enumerate(self.archives):
            path = os.path.join(summary["work_dir"], "archives", f"GSP1000_v2_{index:02d}.tar")
            with open(path, "rb") as f:
                self.assertEqual(f.read(), data)
        self.assertEqual([f for f in os.listdir(os.path.join(summary["work_dir"], "archives")) if f.endswith(".part")], [])

    def test_an_existing_bundle_is_not_overwritten(self):
        self.fetch()
        with self.assertRaises(jobs.JobError) as ctx:
            self.fetch(dest=self.dest)
        self.assertEqual(ctx.exception.code, "exists")


class FetchTermsTest(FetchConnectomeBase):
    def test_a_different_terms_sha_refuses_before_anything_is_downloaded(self):
        with self.assertRaises(jobs.JobError) as ctx:
            self.fetch(terms_sha="0" * 64)
        self.assertEqual(ctx.exception.code, "terms_mismatch")
        self.assertEqual(ctx.exception.exit_code, jobs.EXIT_USAGE)
        self.assertEqual(ctx.exception.fields["live_sha256"], TERMS_SHA)
        self.assertEqual(self.server.access, [])
        self.assertEqual(self.server.requests, [])
        self.assertFalse(os.path.exists(fetchconnectome.bundle_path(self.entry, self.dest)))
        self.assertFalse(os.path.exists(fetchconnectome.work_dir(self.entry, self.dest)))

    def test_terms_that_changed_since_they_were_accepted_refuse(self):
        self.server.terms = TERMS_TEXT + "<p>3. New clause.</p>"
        with self.assertRaises(jobs.JobError) as ctx:
            self.fetch()  # the sha of the text that was accepted earlier
        self.assertEqual(ctx.exception.code, "terms_mismatch")
        self.assertEqual(self.server.access, [])
        self.assertEqual(self.fetch(terms_sha=catalog.terms_sha256(self.server.terms))["subjects"], 5)  # accepting the new text works

    def test_missing_or_malformed_terms_refuse(self):
        self.server.terms = None
        with self.assertRaises(jobs.JobError) as ctx:
            self.fetch()
        self.assertEqual(ctx.exception.code, "terms_missing")
        self.assertEqual(self.server.access, [])
        with self.assertRaises(jobs.JobError) as ctx:
            self.fetch(terms_sha="not a sha")
        self.assertEqual(ctx.exception.code, "usage")

    def test_live_terms_and_their_display_text(self):
        text, sha = fetchconnectome.live_terms(self.entry, server=self.server.url)
        self.assertEqual((text, sha), (TERMS_TEXT, TERMS_SHA))
        shown = fetchconnectome.terms_display_text(text)
        self.assertIn("GSP Open Access Data Use Terms", shown)
        self.assertIn("(https://example.org/terms)", shown)
        self.assertNotIn("<p>", shown)
        self.assertEqual(fetchconnectome.terms_display_text("plain text"), "plain text")

    def test_entries_the_app_may_not_fetch_are_refused(self):
        shipped = catalog.load_catalog()
        for entry_id, fragment in (("leaddbs-ppmi-fgroup2017", "states no terms"), ("ukb-migp-grouppca", "Not supported"), ("synthetic-demo", "")):
            with self.assertRaises(jobs.JobError) as ctx:
                fetchconnectome.fetch_connectome(shipped.get(entry_id), self.dest, TERMS_SHA, emit=self.emit)
            self.assertEqual(ctx.exception.code, "not_fetchable", entry_id)
            self.assertIn(fragment, ctx.exception.message)
        self.assertEqual(self.server.access, [])
        self.assertEqual(os.listdir(self.dest), [])

    def test_catalog_differences_are_warned_about(self):
        self.server.version = (3, 1)
        self.server.listing_md5 = {1100: "0" * 32}
        # The participants checksum differs from the catalog's: the live checksum is the one used, and it does not match the file
        with self.assertRaises(jobs.JobError) as ctx:
            self.fetch()
        self.assertEqual(ctx.exception.code, "checksum")
        warned = " ".join(e["message"] for e in self.warnings("catalog_differs"))
        self.assertIn("version 3.1", warned)
        self.assertIn("participants.tab has a different checksum", warned)

        self.server.listing_md5 = {}
        self.events.clear()
        self.assertEqual(self.fetch(dest=os.path.join(self.dest, "again"))["subjects"], 5)
        self.assertIn("version 3.1", " ".join(e["message"] for e in self.warnings("catalog_differs")))
        provenance = connectome.ConnectomeBundle.open(fetchconnectome.bundle_path(self.entry, os.path.join(self.dest, "again"))).provenance
        self.assertEqual(provenance["source"]["version"], "3.1")  # the version that was downloaded

    def test_check_reports_the_live_dataset(self):
        live = fetchconnectome.read_live(self.entry, server=self.server.url)
        self.assertEqual(fetchconnectome.compare_catalog(self.entry, live), [])
        self.server.version = (4, 0)
        live = fetchconnectome.read_live(self.entry, server=self.server.url)
        self.assertEqual(len(fetchconnectome.compare_catalog(self.entry, live)), 1)


class FetchStreamingTest(FetchConnectomeBase):
    def _budget(self):
        return max(len(data) for data in self.archives) // 3

    def test_signed_urls_are_signed_again_after_403_and_the_md5_stays_valid(self):
        self.server.token_budget = self._budget()
        summary = self.fetch()
        self.assertSameSubjects(summary["path"], self.reference())
        self.assertGreater(self.server.forbidden, 0)  # the file server answered 403 once a signed URL was used up
        self.assertGreater(summary["resigns"], 0)
        self.assertGreaterEqual(summary["reconnects"], summary["resigns"])
        # more signed URLs than files: the archives were signed again
        archive_signs = [a for a in self.server.access if a["file"] in (1000, 1001, 1002)]
        self.assertGreater(len(archive_signs), 3)
        archives = [e for e in self.events if e["event"] == "result" and e.get("kind") == "archive"]
        self.assertEqual([e["verification"] for e in archives], ["md5", "md5", "md5"])  # reconnections kept the hash contiguous
        # Every request after the first of an archive continued where the last one stopped
        for file_id in (1000, 1001, 1002):
            starts = [r["start"] for r in self.server.requests_for(file_id) if r["start"] or True]
            self.assertEqual(starts[0], 0)
            self.assertEqual(starts, sorted(starts))

    def test_urls_older_than_3500_s_are_signed_again(self):
        now = [0.0]

        class Session:
            def __init__(self):
                self.calls = 0

            def get(self, url, **kwargs):
                self.calls += 1
                response = mock.Mock(status_code=303, headers={"Location": f"/signed/{self.calls}"})
                return response

        session = Session()
        resolver = fetchconnectome.SignedUrlResolver(session, "https://dataverse.invalid", clock=lambda: now[0])
        file = {"id": 7, "name": "x.tar"}
        first = resolver.url(file)
        now[0] = 3499.0
        self.assertEqual(resolver.url(file), first)
        now[0] = 3501.0
        second = resolver.url(file)
        self.assertNotEqual(second, first)
        self.assertEqual(resolver.signs, 2)
        resolver.invalidate(file, "https://elsewhere")  # another URL: nothing is dropped
        self.assertEqual(resolver.url(file), second)
        resolver.invalidate(file, second)
        self.assertNotEqual(resolver.url(file), second)
        self.assertEqual(fetchconnectome.RESIGN_AFTER_S, 3500.0)

    def test_a_missing_archive_is_reported_as_missing_not_as_refused(self):
        for status, refused in ((404, False), (410, False), (401, True), (403, True)):
            session = mock.Mock()
            session.get.return_value = mock.Mock(status_code=status, headers={})
            resolver = fetchconnectome.SignedUrlResolver(session, "https://dataverse.invalid")
            with self.assertRaises(jobs.JobError) as ctx:
                resolver.url({"id": 5001, "name": "x.tar"})
            self.assertEqual(ctx.exception.code, "http")
            self.assertEqual(ctx.exception.fields["status"], status)
            message = str(ctx.exception)
            self.assertEqual("does not allow" in message, refused, message)
            if not refused:
                self.assertIn("no longer at the provider", message)
                self.assertIn("check the catalog for updates", message)

    def test_a_server_that_always_answers_403_gives_up(self):
        self.server.token_budget = 0
        with self.assertRaises(jobs.JobError) as ctx:
            self.fetch()
        self.assertEqual(ctx.exception.code, "http")
        self.assertEqual(ctx.exception.fields["status"], 403)

    def test_resume_mid_archive_continues_at_the_member_offset(self):
        bundle = self.bundle()

        def cancel():  # stop once the first subject of the second archive is in the bundle
            return os.path.isfile(os.path.join(bundle, "subjects", "sub-0003.f16.npy"))

        with self.assertRaises(jobs.Cancelled):
            self.fetch(cancel=cancel)
        state = fetchconnectome.read_state(self.entry, self.dest)
        first, second = state["archives"]["GSP1000_v2_00.tar"], state["archives"]["GSP1000_v2_01.tar"]
        self.assertEqual(first["status"], "done")
        self.assertEqual(second["status"], "partial")
        self.assertGreater(second["memberOffset"], 0)
        self.assertLess(second["memberOffset"], len(self.archives[1]))
        self.assertGreater(second["bytes"], 0)
        self.assertFalse(state["complete"])
        waiting = fetchconnectome.unfinished_fetch(self.entry, self.dest)
        self.assertGreater(waiting["fraction"], 0)
        self.assertLess(waiting["fraction"], 1)
        self.assertEqual(waiting["archives"], 3)

        requests_before = len(self.server.requests)
        summary = self.fetch(resume=True)
        resumed = [r for r in self.server.requests[requests_before:] if r["file"] == 1001]
        self.assertEqual(resumed[0]["range"], f"bytes={second['memberOffset']}-")  # the Range restarts at the member offset
        self.assertFalse([r for r in self.server.requests[requests_before:] if r["file"] == 1000])  # the finished archive is not read again
        self.assertSameSubjects(summary["path"], self.reference())
        verification = {e["name"]: e["verification"] for e in self.events if e["event"] == "result" and e.get("kind") == "archive"}
        self.assertEqual(verification["GSP1000_v2_01.tar"], fetchconnectome.NOT_VERIFIED_RESUMED)
        self.assertEqual(verification["GSP1000_v2_02.tar"], "md5")
        self.assertEqual(summary["unverified_resumed"], 1)
        self.assertEqual(len(self.warnings("not_verified")), 1)
        provenance = connectome.ConnectomeBundle.open(summary["path"]).provenance
        self.assertEqual({f["name"]: f["verification"] for f in provenance["source"]["files"]}["GSP1000_v2_01.tar"], "not verified (resumed); members verified by gzip CRC")
        self.assertTrue(fetchconnectome.read_state(self.entry, self.dest)["complete"])
        self.assertIsNone(fetchconnectome.unfinished_fetch(self.entry, self.dest))

    def test_a_started_fetch_needs_resume(self):
        bundle = self.bundle()
        with self.assertRaises(jobs.Cancelled):
            self.fetch(cancel=lambda: os.path.isfile(os.path.join(bundle, "subjects", "sub-0001.f16.npy")))
        with self.assertRaises(jobs.JobError) as ctx:
            self.fetch()
        self.assertEqual(ctx.exception.code, "exists")
        self.assertEqual(self.fetch(resume=True)["subjects"], 5)

    def test_a_server_that_ignores_range_cannot_resume_mid_archive(self):
        bundle = self.bundle()
        with self.assertRaises(jobs.Cancelled):
            self.fetch(cancel=lambda: os.path.isfile(os.path.join(bundle, "subjects", "sub-0003.f16.npy")))
        self.server.ignore_range = True
        with self.assertRaises(jobs.JobError) as ctx:
            self.fetch(resume=True)
        self.assertEqual(ctx.exception.code, "range")
        self.server.ignore_range = False
        self.assertEqual(self.fetch(resume=True)["subjects"], 5)  # the state survived the failed attempt

    def test_keep_archives_resume_reads_the_interrupted_archive_again(self):
        bundle = self.bundle()
        with self.assertRaises(jobs.Cancelled):
            self.fetch(keep_archives=True, cancel=lambda: os.path.isfile(os.path.join(bundle, "subjects", "sub-0003.f16.npy")))
        before = len(self.server.requests)
        summary = self.fetch(keep_archives=True, resume=True)
        again = [r for r in self.server.requests[before:] if r["file"] == 1001]
        self.assertEqual(again[0]["start"], 0)  # the stored archive must be complete
        with open(os.path.join(summary["work_dir"], "archives", "GSP1000_v2_01.tar"), "rb") as f:
            self.assertEqual(f.read(), self.archives[1])
        verification = {e["name"]: e["verification"] for e in self.events if e["event"] == "result" and e.get("kind") == "archive"}
        self.assertEqual(verification["GSP1000_v2_01.tar"], "md5")  # read from its first byte, so the MD5 could be verified
        self.assertSameSubjects(summary["path"], self.reference())

    def test_a_cancel_while_the_converter_waits_for_the_network_is_a_cancel_not_a_network_error(self):
        # The network-bound state of a real download: the converter waits in the queue when the cancel arrives (Pause)
        self.server.delay_s = 0.03
        for index, wait in enumerate((0.2, 0.45, 0.7)):
            with self.subTest(wait=wait):
                self.server.reset()
                self.server.delay_s = 0.03
                stop = threading.Event()
                timer = threading.Timer(wait, stop.set)
                timer.start()
                try:
                    with self.assertRaises(jobs.Cancelled):
                        self.fetch(cancel=stop.is_set, streams=1, dest=os.path.join(self.dest, f"out{index}"))
                finally:
                    timer.cancel()
                self.assertFalse([e for e in self.events if e["event"] == "error"])

    def test_progress_events_carry_bytes_and_totals(self):
        self.fetch()
        progress = [e for e in self.events if e["event"] == "progress"]
        self.assertTrue(progress)
        total = sum(len(data) for data in self.archives)
        self.assertTrue(all(e["total"] == total for e in progress))
        self.assertEqual(progress[-1]["done"], total)
        self.assertEqual(progress[-1]["archive_count"], 3)


class FetchIntegrityTest(FetchConnectomeBase):
    def test_a_checksum_mismatch_is_read_twice_then_stops_with_an_override_option(self):
        self.server.listing_md5 = {1001: "0" * 32}
        with self.assertRaises(jobs.JobError) as ctx:
            self.fetch()
        self.assertEqual(ctx.exception.code, "checksum")
        self.assertEqual(ctx.exception.fields["override"], "--accept-checksum-mismatch")
        self.assertEqual(ctx.exception.fields["file"], "GSP1000_v2_01.tar")
        self.assertEqual(len(self.warnings("checksum_retry")), 1)
        from_start = [r for r in self.server.requests_for(1001) if r["start"] == 0]
        self.assertEqual(len(from_start), 2)  # the archive was read once more to compute its MD5 again
        state = fetchconnectome.read_state(self.entry, self.dest)
        self.assertEqual(state["archives"]["GSP1000_v2_01.tar"]["status"], "mismatch")
        self.assertFalse(state["complete"])

        # Without the flag, a resumed run checks again and stops again; with it the data are kept and the override is recorded
        with self.assertRaises(jobs.JobError):
            self.fetch(resume=True)
        summary = self.fetch(resume=True, accept_checksum_mismatch=True)
        self.assertTrue(summary["checksum_override"])
        self.assertSameSubjects(summary["path"], self.reference())
        provenance = connectome.ConnectomeBundle.open(summary["path"]).provenance
        self.assertIs(provenance["source"]["checksumOverride"], True)
        verification = {f["name"]: f["verification"] for f in provenance["source"]["files"]}
        self.assertEqual(verification["GSP1000_v2_01.tar"], fetchconnectome.VERIFICATION_OVERRIDE)
        self.assertEqual(verification["GSP1000_v2_00.tar"], "md5")
        self.assertTrue(runs.list_runs(self.dest, kind="fetch", status="done")[-1])
        self.assertTrue(runs.read_summary(self.dest, runs.list_runs(self.dest, kind="fetch", status="done")[-1])["checksum_override"])

    def test_the_override_flag_accepts_a_mismatch_at_once(self):
        self.server.listing_md5 = {1002: "f" * 32}
        summary = self.fetch(accept_checksum_mismatch=True)
        self.assertTrue(summary["checksum_override"])
        self.assertEqual(len(self.warnings("checksum_override")), 1)
        self.assertEqual(connectome.ConnectomeBundle.open(summary["path"]).provenance["source"]["checksumOverride"], True)

    def test_a_truncated_archive_stops_and_is_resumable(self):
        # The provider cuts the connection for good after some bytes (a signed URL whose budget never renews)
        original = fetchconnectome.MAX_FAILURES
        self.server.token_budget = len(self.archives[0]) // 2
        with mock.patch.object(fetchconnectome, "MAX_FAILURES", 0), mock.patch.object(fetchconnectome, "MAX_RESIGN_FAILURES", 1):
            with self.assertRaises(jobs.JobError):
                self.fetch()
        self.assertEqual(fetchconnectome.MAX_FAILURES, original)
        self.server.token_budget = None
        self.assertEqual(self.fetch(resume=True)["subjects"], 5)


class FetchPlatformTest(FetchConnectomeBase):
    def test_caffeinate_runs_on_macos_for_the_whole_fetch(self):
        popen = mock.MagicMock()
        process = popen.return_value
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(jobs.subprocess, "Popen", popen):
            self.fetch(subset="archives:00-00")
        args = popen.call_args[0][0]
        self.assertEqual(args, ["caffeinate", "-i", "-w", str(os.getpid())])
        process.terminate.assert_called()  # released when the job ends

    def test_windows_calls_set_thread_execution_state(self):
        kernel = mock.MagicMock()
        kernel.SetThreadExecutionState.return_value = 1
        with mock.patch.object(jobs, "_IS_WINDOWS", True), mock.patch.object(jobs, "_kernel32", return_value=kernel):
            self.fetch(subset="archives:00-00")
        calls = [c[0][0] for c in kernel.SetThreadExecutionState.call_args_list]
        self.assertEqual(calls, [0x80000001, 0x80000000])  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED, then released

    def test_linux_does_nothing(self):
        popen = mock.MagicMock()
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(jobs.subprocess, "Popen", popen):
            self.fetch(subset="archives:00-00")
        popen.assert_not_called()

    def test_web_build_destination_rules(self):
        self.assertTrue(fetchconnectome.is_web_build({"SLICER_STUDIO_WEB": "1"}, exists=lambda p: False))
        self.assertTrue(fetchconnectome.is_web_build({}, exists=lambda p: p == "/.dockerenv"))
        self.assertFalse(fetchconnectome.is_web_build({}, exists=lambda p: False))
        self.assertEqual(fetchconnectome.default_destination(web=True), "/data/SlicerStudio")
        self.assertEqual(fetchconnectome.default_destination("/home/u/SlicerStudio", web=False), os.path.join("/home/u/SlicerStudio", "Connectomes"))
        self.assertEqual(fetchconnectome.destination_notice("/data/SlicerStudio/Connectomes", web=True), "")
        self.assertEqual(fetchconnectome.destination_notice("/data", web=True), "")
        self.assertIn("outside /data", fetchconnectome.destination_notice("/root/connectomes", web=True))
        self.assertIn("outside /data", fetchconnectome.destination_notice("/database", web=True))
        self.assertEqual(fetchconnectome.destination_notice("/root/connectomes", web=False), "")


class FetchLeadDbsTest(unittest.TestCase):
    """A Lead-DBS entry: metadata files go to the dataset folder, the runs are streamed from two archives."""

    def test_streamed_archives_equal_from_leaddbs(self):
        root = tempfile.mkdtemp(prefix="lnm-fetch-leaddbs-")
        saved = os.environ.get("NO_PROXY"), os.environ.get("no_proxy")
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        server = FakeDataverse()
        try:
            rng = np.random.default_rng(3)
            dataset = os.path.join(root, "GSPtest")
            os.makedirs(os.path.join(dataset, "vol"))
            shape = (5, 6, 4)
            mask = np.zeros(shape, dtype=bool)
            mask[1:4, 1:5, 1:3] = True
            outidx = np.sort(np.ravel_multi_index(np.nonzero(mask), shape, order="F")) + 1
            runs_by_subject = {"subA": ["subA_run1.mat"], "subB": ["subB_run1.mat", "subB_run2.mat"], "subC": ["subC_run1.mat"]}
            sub_ids = np.empty((3, 1), dtype=object)
            for index, (subject, names) in enumerate(runs_by_subject.items()):
                sub_ids[index, 0] = np.array([subject, *names], dtype=object)
                for name in names:
                    sio.savemat(os.path.join(dataset, "vol", name), {"gmtc": (50 + rng.standard_normal((10, len(outidx)))).astype(np.float32)})
            spm_mat = np.array([[-2.0, 0, 0, 92], [0, 2, 0, -128], [0, 0, 2, -74], [0, 0, 0, 1]])
            vol = {"outidx": outidx.astype(np.float64)[:, np.newaxis], "space": {"dim": np.array([shape], np.float64), "mat": spm_mat, "fname": "space.nii"}, "subIDs": sub_ids}
            sio.savemat(os.path.join(dataset, "dataset_volsurf.mat"), {"vol": vol})
            info = {"name": "GSP 1000 v2 (test)", "tag": "GSPtest", "type": "fMRI_timecourses", "citation": "Cohen 2023 https://doi.org/10.7910/DVN/KKTJQC",
                    "subsets": [{"name": "Full Set", "subs": [1, 2, 3]}, {"name": "First Subject", "subs": 1}]}
            with open(os.path.join(dataset, "dataset_info.json"), "w", encoding="utf-8") as f:
                json.dump(info, f)

            def member(name):
                with open(os.path.join(dataset, "vol", name), "rb") as f:
                    return (f"GSP1000-LeadDBS/vol/{name}", f.read())

            archives = [_tar([member("subA_run1.mat"), member("subB_run1.mat")]), _tar([member("subB_run2.mat"), member("subC_run1.mat")])]
            files = {}
            for name in ("dataset_info.json", "dataset_volsurf.mat"):
                with open(os.path.join(dataset, name), "rb") as f:
                    files[name] = f.read()
            entry = copy.deepcopy(catalog.load_catalog().get("gsp1000-kktjqc"))
            api = f"https://dataverse.invalid/api/datasets/:persistentId/?persistentId=doi:{DOI}"
            entry.update({"id": "fake-leaddbs", "title": "GSP1000 Preprocessed Connectome for Lead-DBS (fake)"})
            entry["source"].update({"api": api, "doi": DOI})
            entry["terms"].update({"api": api})
            catalog_files = []
            for index, data in enumerate(archives):
                server.add(2000 + index, f"GSP1000_leadDBS_{index:02d}.tar", data, folder="GSP1000-LeadDBS/vol")
                catalog_files.append({"name": f"GSP1000_leadDBS_{index:02d}.tar", "folder": "GSP1000-LeadDBS/vol", "id": 2000 + index, "size": len(data), "checksum": {"algo": "md5", "value": _md5(data)}, "role": "archive"})
            for index, (name, data) in enumerate(files.items()):
                server.add(2100 + index, name, data, folder="")
                catalog_files.append({"name": name, "folder": "", "id": 2100 + index, "size": len(data), "checksum": {"algo": "md5", "value": _md5(data)}, "role": "metadata"})
            entry["files"] = catalog_files
            entry["size"] = sum(f["size"] for f in catalog_files)
            entry["disk"] = {"subjects": 3, "voxels": None, "timepoints_per_run": None, "runs_per_subject": [1, 2], "stored_bytes": None}
            entry["population"]["n"] = 3
            entry["subsets"] = {"Full Set": 3, "First Subject": 1}
            self.assertEqual(catalog.validate_entry(entry), [])

            dest = os.path.join(root, "dest")
            with _fast_retries():
                summary = fetchconnectome.fetch_connectome(entry, dest, TERMS_SHA, streams=2, server=server.url, emit=lambda *a, **k: None)
            reference = connectome.from_leaddbs(dataset, os.path.join(root, "reference.lnmc"))
            a, b = connectome.ConnectomeBundle.open(summary["path"]), connectome.ConnectomeBundle.open(reference)
            self.assertEqual(sorted(a.subject_ids), sorted(b.subject_ids))
            for subject_id in a.subject_ids:
                self.assertTrue(np.array_equal(a.load_subject(subject_id).data, b.load_subject(subject_id).data), subject_id)
            self.assertEqual(a.subsets, b.subsets)
            self.assertEqual(a.provenance["source"]["doi"], DOI)
            self.assertEqual(a.metadata["mask"]["policy"], "volsurf")
        finally:
            server.close()
            shutil.rmtree(root, ignore_errors=True)
            for key, value in zip(("NO_PROXY", "no_proxy"), saved, strict=True):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class FetchCommandLineTest(FetchConnectomeBase):
    def _run(self, *args):
        catalog_path = os.path.join(self.dest, "catalog.json")
        if not os.path.exists(catalog_path):
            data = copy.deepcopy(catalog.load_catalog().data)
            data["entries"] = [self.entry]
            with open(catalog_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        env = {**os.environ, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost", "PYTHONPATH": MODULE_DIR}
        proc = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.fetchconnectome", *args, "--catalog", catalog_path, "--server", self.server.url], cwd=MODULE_DIR, env=env, capture_output=True, text=True, timeout=300, check=False)
        events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
        return proc, events

    def test_terms_command_shows_the_live_text_and_its_sha(self):
        proc, events = self._run("terms", "--entry", "fake-gsp")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = next(e for e in events if e["event"] == "result")
        self.assertEqual(result["kind"], "terms")
        self.assertEqual(result["text"], TERMS_TEXT)
        self.assertEqual(result["sha256"], TERMS_SHA)
        self.assertIn("GSP Open Access Data Use Terms", result["display"])
        self.assertEqual(events[-1]["event"], "done")

    def test_the_command_refuses_a_wrong_terms_sha(self):
        proc, events = self._run("--entry", "fake-gsp", "--dest", os.path.join(self.dest, "out"), "--terms-sha", "1" * 64)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        error = events[-1]
        self.assertEqual((error["event"], error["code"]), ("error", "terms_mismatch"))
        self.assertEqual(self.server.access, [])

    def test_the_command_fetches_a_subset_and_reports_events(self):
        out = os.path.join(self.dest, "out")
        proc, events = self._run("--entry", "fake-gsp", "--dest", out, "--terms-sha", TERMS_SHA, "--subset", "archives:00-00", "--streams", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(events[0]["event"], "start")
        self.assertTrue(any(e["event"] == "progress" for e in events))
        done = events[-1]
        self.assertEqual(done["event"], "done")
        self.assertEqual(done["outputs"], [os.path.join(out, "fake-gsp.lnmc")])
        self.assertEqual(connectome.ConnectomeBundle.open(done["outputs"][0]).subject_ids, ["sub-0001", "sub-0002"])

    def test_the_command_stops_on_a_cancel_file_and_resumes(self):
        out = os.path.join(self.dest, "out")
        cancel = os.path.join(self.dest, "cancel")
        open(cancel, "w").close()
        proc, events = self._run("--entry", "fake-gsp", "--dest", out, "--terms-sha", TERMS_SHA, "--cancel-file", cancel)
        self.assertEqual(proc.returncode, jobs.EXIT_CANCELLED, proc.stderr)
        self.assertEqual(events[-1]["event"], "cancelled")
        os.remove(cancel)
        proc, events = self._run("--entry", "fake-gsp", "--dest", out, "--terms-sha", TERMS_SHA, "--resume")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(connectome.ConnectomeBundle.open(events[-1]["outputs"][0]).subject_ids), 5)

    def test_the_command_exits_with_cancelled_when_the_cancel_file_appears_mid_download(self):
        self.server.delay_s = 0.03
        catalog_path = os.path.join(self.dest, "catalog.json")
        data = copy.deepcopy(catalog.load_catalog().data)
        data["entries"] = [self.entry]
        with open(catalog_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        for index, wait in enumerate((0.05, 0.3)):
            out, cancel = os.path.join(self.dest, f"out{index}"), os.path.join(self.dest, f"cancel{index}")
            env = {**os.environ, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost", "PYTHONPATH": MODULE_DIR}
            args = [sys.executable, "-m", "LesionNetworkMappingLib.fetchconnectome", "--entry", "fake-gsp", "--dest", out, "--terms-sha", TERMS_SHA, "--catalog", catalog_path,
                    "--server", self.server.url, "--streams", "1", "--cancel-file", cancel]
            proc = subprocess.Popen(args, cwd=MODULE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            events = []
            for line in proc.stdout:
                if not line.startswith("{"):
                    continue
                events.append(json.loads(line))
                if events[-1]["event"] == "progress" and not os.path.exists(cancel):
                    time.sleep(wait)
                    open(cancel, "w").close()  # what Pause does
            proc.wait(timeout=120)
            self.assertEqual(proc.returncode, jobs.EXIT_CANCELLED, (wait, proc.stderr.read(), events[-1]))
            self.assertEqual(events[-1]["event"], "cancelled")

    def test_the_command_stops_when_its_parent_process_is_gone(self):
        """--parent-pid: the application passes its pid; when it crashes the job stops (resumable) instead of downloading on unseen.

        The application starts the job through the PythonSlicer launcher, which outlives a crashed application, so the job's
        own orphan watchdog never fires; only the parent watchdog stops it.
        """
        self.server.delay_s = 0.03
        catalog_path = os.path.join(self.dest, "catalog.json")
        data = copy.deepcopy(catalog.load_catalog().data)
        data["entries"] = [self.entry]
        with open(catalog_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        out = os.path.join(self.dest, "out")
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        self.addCleanup(parent.wait)
        self.addCleanup(parent.kill)
        env = {**os.environ, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost", "PYTHONPATH": MODULE_DIR}
        args = [sys.executable, "-m", "LesionNetworkMappingLib.fetchconnectome", "--entry", "fake-gsp", "--dest", out, "--terms-sha", TERMS_SHA, "--catalog", catalog_path,
                "--server", self.server.url, "--streams", "1", "--parent-pid", str(parent.pid)]
        proc = subprocess.Popen(args, cwd=MODULE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        events = []
        for line in proc.stdout:
            if not line.startswith("{"):
                continue
            events.append(json.loads(line))
            if events[-1]["event"] == "progress" and parent.poll() is None:
                parent.kill()  # the application crashed
                parent.wait()
        proc.wait(timeout=120)
        self.assertEqual(proc.returncode, jobs.EXIT_CANCELLED, (proc.stderr.read(), events[-1:]))
        self.assertEqual(events[-1]["event"], "cancelled")
        self.assertIsNone(fetchconnectome.running_fetch(self.entry, out))  # the lock is released: Resume can start
        self.assertFalse(os.path.exists(fetchconnectome.lock_path(self.entry, out)))
        self.assertIsNotNone(fetchconnectome.unfinished_fetch(self.entry, out))
        self.assertIsNone(fetchconnectome.finished_fetch(self.entry, out))
        self.server.delay_s = 0
        proc, events = self._run("--entry", "fake-gsp", "--dest", out, "--terms-sha", TERMS_SHA, "--resume", "--parent-pid", str(os.getpid()))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(fetchconnectome.finished_fetch(self.entry, out), fetchconnectome.bundle_path(self.entry, out))
        self.assertEqual(len(connectome.ConnectomeBundle.open(events[-1]["outputs"][0]).subject_ids), 5)


if __name__ == "__main__":
    unittest.main()

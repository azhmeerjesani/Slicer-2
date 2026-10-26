"""Tests of the tutorial cohorts (tutorials.py, catalog/tutorials.json; L-53, addendum 4.4).

No data is downloaded from the real sources: a local http.server in a thread serves small fixture files that mimic
each layout (a Dataverse dataset with pinned md5 sums, an OpenNeuro S3 bucket with a paginated listing, a folder of
masks and a score table). The shipped catalog is checked for completeness, licences and sizes only.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingTutorialsTest.py -v
"""

import copy
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import fetch, geometry, jobs, nifti, study, tutorials

_SAVED_ENV = {}


def setUpModule():
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


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def md5(data):
    return hashlib.md5(data).hexdigest()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves ``server.files`` (path -> bytes) and the S3 listing (``server.buckets``: prefix of the bucket path -> keys)."""

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
        server.log.append(self.path)
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/" and "list-type" in query:
            prefix = query.get("prefix", [""])[0]
            keys = sorted(key for key in server.files if key.startswith("/" + prefix))
            start = int(query["continuation-token"][0]) if "continuation-token" in query else 0
            page = keys[start : start + server.page]
            more = start + server.page < len(keys)
            xml = ['<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">', f"<Prefix>{prefix}</Prefix><KeyCount>{len(page)}</KeyCount>"]
            xml.append(f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>")
            if more:
                xml.append(f"<NextContinuationToken>{start + server.page}</NextContinuationToken>")
            for key in page:
                body = server.files[key]
                etag = md5(body) if key not in server.multipart else md5(body) + "-2"
                xml.append(f"<Contents><Key>{key[1:]}</Key><LastModified>2023-01-01T00:00:00.000Z</LastModified><ETag>&quot;{etag}&quot;</ETag><Size>{len(body)}</Size></Contents>")
            xml.append("</ListBucketResult>")
            return self._send(200, "".join(xml).encode())
        body = server.files.get(urllib.parse.unquote(parsed.path))
        if body is None:
            return self._send(404)
        return self._send(200, body)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.files = {}
        self.multipart = set()
        self.log = []
        self.page = 3

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


def blob(path, grid, center, radius=2, dtype=np.uint8):
    """Write a NIfTI mask with a ball at voxel ``center`` on a grid; returns the voxel count."""
    data = np.zeros(grid.shape, dtype=np.uint8)
    ijk = np.indices(grid.shape)
    inside = sum((ijk[axis] - center[axis]) ** 2 for axis in range(3)) <= radius**2
    data[inside] = 1
    nifti.write_nifti(path, data, grid.affine, dtype=dtype)
    return int(inside.sum())


NATIVE_GRID = geometry.Grid((24, 24, 24), np.array([[1.5, 0, 0, -18.0], [0, 1.5, 0, -18.0], [0, 0, 1.5, -18.0], [0, 0, 0, 1.0]]))
MNI_GRID = geometry.GRID_BY_ID["NLin6-2mm"].grid
COLIN_GRID = geometry.GRID_BY_ID["SPM-1mm"].grid

JOUTSA_NAMES = ["0318", "0590", "r248", "0999", "r255"]
S3_SUBJECTS = {"sub-M2001": 93.3, "sub-M2002": 79.2, "sub-M2003": 54.8, "sub-M2005": None, "sub-M2007": 30.5, "sub-M2009": 66.1}
TABLE = (
    "participant_id\tsex\tage_at_stroke\trace\twab_days\twab_aq\twab_type\tnotes\n"
    "sub-M2001\tF\t23\tb\t8798\t93.3\tNone\t\"quoted, text\"\n"
    "sub-M2002\tM\t27\tw\t802\t79.2\tConduction\tn/a\n"
    "sub-M2003\tF\t31\tw\t4334\t54.8\tBroca\tn/a\n"
    "sub-M2005\tF\t40\tw\t100\tn/a\tn/a\tn/a\n"
    "sub-M2007\tM\t52\tb\t900\t30.5\tGlobal\tn/a\n"
    "sub-M2009\tM\t61\tb\t450\t66.1\tAnomic\tn/a\n"
    "sub-M2099\tF\t70\tw\t1000\t88.0\tNone\tn/a\n"
)


class TutorialsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-tutorials-")
        self.cache = os.path.join(self.tmp, "cache")
        self.server = _Server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.dest = os.path.join(self.tmp, "studies", "tutorial")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # fixtures

    def local(self, name):
        return os.path.join(self.tmp, name)

    def dataverse_catalog(self, names=JOUTSA_NAMES, md5_wrong=None):
        files = []
        for index, name in enumerate(names):
            path = self.local(f"{name}_lesion_MNI.nii")
            blob(path, MNI_GRID, (20 + index * 5, 50, 40), radius=3 + index % 2)
            data = read_bytes(path)
            file_id = 7000 + index
            self.server.files[f"/api/access/datafile/{file_id}"] = data
            files.append([file_id, f"{name}_lesion_MNI.nii", len(data), md5_wrong if md5_wrong and index == 1 else md5(data)])
        files.append([7999, "README.txt", 10, "0" * 32])  # not a mask: ignored
        entry = {
            "id": "joutsa_like",
            "title": "Joutsa-like",
            "summary": "A fixture with MNI masks.",
            "licence": "CC0-1.0",
            "badge": "green",
            "terms_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "citation": "Fixture A. Smoker lesions. doi:10.0000/fixture",
            "homepage": "https://doi.org/10.0000/fixture",
            "native": False,
            "space": "MNI152NLin6Asym",
            "size_bytes": sum(item[2] for item in files),
            "n_subjects": len(names),
            "source": {"type": "dataverse", "server": self.server.base, "doi": "doi:10.0000/fixture", "id_regex": r"^(?P<sid>[A-Za-z]*[0-9]+)_lesion_MNI\.nii$", "files": files},
            "study": {"name": "Tutorial: Joutsa-like", "group": "case", "columns": {}},
            "steps": ["Open the overlap map.", "Map the lesions."],
            "volume_covariate": None,
        }
        return {"entries": [entry]}

    def s3_catalog(self, scans=True, missing_scan=None):
        prefix = "ds00001/derivatives/lesion_masks/"
        for index, (sid, _score) in enumerate(S3_SUBJECTS.items()):
            session = 1000 + index
            base = f"{sid}_ses-{session}_acq-spc3_run-3_T2w"
            mask = self.local(f"{base}_desc-lesion_mask.nii.gz")
            blob(mask, NATIVE_GRID, (6 + index, 10, 10), radius=2 + index % 3)
            rel = f"{sid}/ses-{session}/anat/{base}"
            self.server.files[f"/{prefix}{rel}_desc-lesion_mask.nii.gz"] = read_bytes(mask)
            self.server.files[f"/{prefix}{rel}_desc-lesion_mask.json"] = b'{"Type": "Lesion"}'  # not a mask file
            if scans and rel != missing_scan:
                scan = self.local(f"{base}.nii.gz")
                nifti.write_nifti(scan, np.full(NATIVE_GRID.shape, 100 + index, dtype=np.int16), NATIVE_GRID.affine, dtype=np.int16)
                self.server.files[f"/ds00001/{rel}.nii.gz"] = read_bytes(scan)
        self.server.files["/ds00001/participants.tsv"] = TABLE.encode()
        columns = {
            "wab_aq": {"role": "score", "type": "numeric", "units": "score", "description": "Aphasia quotient"},
            "wab_days": {"role": "covariate", "type": "numeric", "units": "days"},
            "age_at_stroke": {"role": "covariate", "type": "numeric", "units": "years"},
            "sex": {"role": "covariate", "type": "categorical", "levels": {"F": "female", "M": "male"}},
            "race": {"role": "metadata", "type": "categorical", "levels": {"b": "Black", "w": "White"}},
            "wab_type": {"role": "metadata", "type": "categorical", "levels": {"Conduction": None, "Broca": None, "Global": None, "Anomic": None, "none": "Not aphasic"}},
        }
        entry = {
            "id": "arc_like",
            "title": "ARC-like",
            "summary": "A fixture with native masks and a table.",
            "licence": "CC0-1.0",
            "badge": "green",
            "terms_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "citation": "Fixture B. Aphasia cohort. doi:10.0000/arc",
            "homepage": "https://example.org/arc",
            "native": True,
            "space": "native",
            "size_bytes": 1000,
            "n_subjects": len(S3_SUBJECTS),
            "scan_bytes_each": 5000,
            "source": {
                "type": "s3",
                "base": self.server.base,
                "dataset": "ds00001",
                "mask_prefix": prefix,
                "mask_regex": r"^(?P<rel>sub-(?P<sid>[A-Za-z0-9]+)/ses-[A-Za-z0-9]+/anat/[^/]+_desc-lesion_mask\.nii\.gz)$",
                "table": "ds00001/participants.tsv",
                "scan": {"root": "ds00001/", "replace": [[r"_desc-lesion_mask\.nii\.gz$", ".nii.gz"]], "modality": "anat"},
                "expected": {"masks": len(S3_SUBJECTS), "bytes": 0},
            },
            "study": {"name": "Tutorial: ARC-like", "group": "case", "columns": columns, "value_map": {"wab_type": {"None": "none"}}},
            "steps": ["Normalize first.", "Run sLNM with the volume covariate."],
            "volume_covariate": "Aphasia severity is largely a volume effect: keep volume_ml.",
        }
        return {"entries": [entry]}

    def folder_catalog(self):
        folder = self.local("lesymap")
        os.makedirs(os.path.join(folder, "extdata", "lesions"))
        for index in range(4):
            blob(os.path.join(folder, "extdata", "lesions", f"lesion_{index + 1:03d}.nii.gz"), COLIN_GRID, (60 + index * 8, 100, 80), radius=4)
        blob(os.path.join(folder, "extdata", "colin_template.nii.gz"), COLIN_GRID, (90, 100, 90), radius=30)  # a template: left out
        with open(os.path.join(folder, "extdata", "behavior.csv"), "w", encoding="utf-8") as f:
            f.write("id,score,age\n1,10.5,40\n2,22.0,50\n3,8.25,61\n4,31.0,33\n")
        entry = {
            "id": "lesymap_like",
            "title": "LESYMAP-like",
            "summary": "A fixture folder.",
            "licence": "Apache-2.0",
            "badge": "green",
            "terms_url": "https://www.apache.org/licenses/LICENSE-2.0",
            "citation": "Fixture C. Simulated lesions.",
            "homepage": "https://example.org/lesymap",
            "native": False,
            "space": "MNIColin27",
            "source": {
                "type": "folder",
                "link": "https://example.org/lesymap",
                "grid": "SPM-1mm",
                "grid_space": "MNIColin27",
                "exclude_regex": "(template|colin_)",
                "table_regex": r"(behaviou?r|scores?)[^/]*\.(csv|tsv)$",
                "score_columns": ["score"],
            },
            "study": {"name": "Tutorial: LESYMAP-like", "group": "case", "columns": {}},
            "steps": ["Run VLSM."],
            "volume_covariate": "Keep volume.",
        }
        return {"entries": [entry]}, folder

    def build(self, catalog, tutorial_id, **kwargs):
        return tutorials.build_tutorial(tutorial_id, self.dest, cache=self.cache, catalog=catalog, **kwargs)

    def assertNoPartialBuild(self):
        parent = os.path.dirname(self.dest)
        left = [name for name in (os.listdir(parent) if os.path.isdir(parent) else []) if ".building-" in name]
        self.assertEqual(left, [])


class ShippedCatalogTest(unittest.TestCase):
    def test_every_recipe_is_complete_and_allowed(self):
        found = tutorials.entries()
        self.assertEqual([e["id"] for e in found], ["joutsa2022_addiction", "soop_nihss", "arc_aphasia", "lesymap_groundtruth"])
        for entry in found:
            self.assertEqual(tutorials.validate_entry(entry), [], entry["id"])
            self.assertIn(entry["licence"], ("CC0-1.0", "Apache-2.0"))
            self.assertEqual(entry["badge"], "green")
            self.assertTrue(entry["citation"] and entry["terms_url"] and entry["steps"] and entry["summary"])

    def test_no_unlicensed_source(self):
        with open(tutorials.CATALOG_FILE, encoding="utf-8") as f:
            text = f.read().lower()
        for word in ("heuvel", "gsp1000", "nonstated"):
            self.assertNotIn(word, text)
        for entry in tutorials.entries():
            self.assertNotIn("CC-BY-NC", entry["licence"])

    def test_joutsa_recipe(self):
        entry = tutorials.get("joutsa2022_addiction")
        files = entry["source"]["files"]
        self.assertEqual(len(files), 129)
        self.assertEqual(sum(item[2] for item in files), entry["size_bytes"])
        self.assertEqual(entry["source"]["doi"], "doi:10.7910/DVN/8BHHRS")
        self.assertFalse(entry["native"])
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{32}", item[3]) for item in files))
        self.assertEqual(len({item[0] for item in files}), 129)
        pattern = re.compile(entry["source"]["id_regex"])
        self.assertTrue(all(pattern.match(item[1]) for item in files))

    def test_native_recipes_say_normalize_first(self):
        for tutorial_id in ("soop_nihss", "arc_aphasia"):
            entry = tutorials.get(tutorial_id)
            self.assertTrue(entry["native"])
            self.assertEqual(entry["space"], "native")
            self.assertTrue(any("Normalize" in step for step in entry["steps"]))
            self.assertEqual(entry["source"]["type"], "s3")
            self.assertIn("ds0048", entry["source"]["dataset"])
        self.assertEqual(tutorials.get("arc_aphasia")["source"]["expected"], {"masks": 228, "bytes": 5688846})
        self.assertEqual(tutorials.get("soop_nihss")["source"]["dataset"], "ds004889")

    def test_volume_covariate_guidance_in_the_slnm_tutorial(self):
        arc = tutorials.get("arc_aphasia")
        text = arc["volume_covariate"].lower()
        self.assertIn("volume effect", text)
        self.assertIn("volume_ml", text)
        self.assertIn("covariate", text)
        self.assertEqual(arc["study"]["columns"]["wab_aq"]["role"], "score")
        self.assertEqual(arc["study"]["columns"]["wab_days"]["role"], "covariate")
        self.assertTrue(any("volume" in step.lower() for step in arc["steps"]))
        self.assertTrue(tutorials.get("soop_nihss")["volume_covariate"])

    def test_other_licences_are_refused(self):
        for licence in ("CC-BY-NC-4.0", "unknown", "CC-BY-4.0", ""):
            entry = copy.deepcopy(tutorials.get("arc_aphasia"))
            entry["licence"] = licence
            problems = tutorials.validate_entry(entry)
            self.assertTrue(any("licence" in problem for problem in problems), (licence, problems))
        entry = copy.deepcopy(tutorials.get("arc_aphasia"))
        entry["source"]["files"] = None
        entry["source"]["type"] = "ftp"
        self.assertTrue(any("source type" in problem for problem in tutorials.validate_entry(entry)))

    def test_estimate_and_sizes(self):
        arc = tutorials.get("arc_aphasia")
        full = tutorials.estimate(arc)
        self.assertEqual(full["subjects"], 228)
        self.assertEqual(full["bytes"], arc["size_bytes"])
        self.assertEqual(full["scan_bytes"], 0)
        part = tutorials.estimate(arc, limit=50, with_scans=True)
        self.assertEqual(part["subjects"], 50)
        self.assertEqual(part["scan_bytes"], 50 * arc["scan_bytes_each"])
        self.assertEqual(tutorials.estimate(tutorials.get("lesymap_groundtruth"))["bytes"], None)
        self.assertEqual(tutorials.human_size(116484549), "116 MB")
        self.assertEqual(tutorials.human_size(5697300), "5.7 MB")
        self.assertEqual(tutorials.human_size(None), "unknown size")

    def test_unknown_id_and_bad_catalog(self):
        with self.assertRaises(tutorials.TutorialError) as caught:
            tutorials.get("nope")
        self.assertEqual(caught.exception.code, "unknown_id")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"format": "other", "version": 1, "entries": []}, f)
            with self.assertRaises(tutorials.TutorialError):
                tutorials.load_catalog(path)


class DataverseBuildTest(TutorialsTestBase):
    def test_build_mni_cohort(self):
        catalog = self.dataverse_catalog()
        events = []
        report = self.build(catalog, "joutsa_like", progress=lambda stage, done, total, **extra: events.append((stage, done, total)))
        self.assertEqual(report["path"], self.dest)
        self.assertEqual(report["subjects"], 5)
        self.assertEqual(report["watermark"], "TUTORIAL DATA")
        self.assertEqual(report["next"], "map and analyse")
        self.assertEqual({stage for stage, _done, _total in events}, {"plan", "download", "build"})
        self.assertNoPartialBuild()
        built = study.Study.open(self.dest)
        self.assertEqual(built.subject_ids, ["sub-0318", "sub-0590", "sub-0999", "sub-r248", "sub-r255"])
        self.assertEqual(built.metadata["tutorial"], True)
        self.assertEqual(built.metadata["space"], "MNI152NLin6Asym")
        info = built.metadata["tutorial_info"]
        self.assertEqual((info["id"], info["licence"], info["native"], info["normalize_first"], info["scans"]), ("joutsa_like", "CC0-1.0", False, False, False))
        self.assertIn("Smoker lesions", info["citation"])
        for row in built.subjects:
            self.assertEqual((row["group"], row["space"], row["status"], row["include"]), ("case", "MNI152NLin6Asym", "traced", "1"))
            self.assertEqual(row["lesion"], f"lesions/{row['participant_id']}_space-MNI152NLin6Asym_desc-lesion_mask.nii.gz")
            path = built.abspath(row["lesion"])
            self.assertTrue(os.path.isfile(path))
            data, _affine, _header = nifti.read_nifti(path)
            self.assertEqual(data.shape, MNI_GRID.shape)
            self.assertAlmostEqual(float(row["volume_ml"]), round(int(np.count_nonzero(data)) * 8 / 1000.0, 4), places=3)
            sidecar = built.read_lesion_sidecar(row["participant_id"])
            self.assertEqual(sidecar["Type"], "Lesion")
            self.assertEqual(sidecar["SlicerStudio"]["tutorial"], "joutsa_like")
        # the volume covariate is a covariate by registry; there are no scores in this cohort
        self.assertIn("volume_ml", built.covariate_columns())
        self.assertEqual(built.score_columns(), [])

    def test_watermark_and_no_local_paths(self):
        self.build(self.dataverse_catalog(), "joutsa_like")
        self.assertEqual(tutorials.watermark_for_study(self.dest), "TUTORIAL DATA")
        self.assertEqual(tutorials.watermark_for_study(study.Study.open(self.dest)), "TUTORIAL DATA")
        self.assertTrue(tutorials.is_tutorial(os.path.join(self.dest, "study.json")))
        self.assertEqual(tutorials.tutorial_info(self.dest)["id"], "joutsa_like")
        self.assertIsNone(tutorials.watermark_for_study({"name": "mine"}))
        try:
            from LesionNetworkMappingLib import figures
        except ImportError:
            figures = None
        if figures is not None:
            self.assertEqual(figures.watermark_for(self.dest), "TUTORIAL DATA")
        # nothing that is shared names this computer: no absolute path in any text file of the study
        for folder, subfolders, files in os.walk(self.dest):
            subfolders[:] = [name for name in subfolders if name != ".studio"]
            for name in files:
                if name.endswith((".json", ".tsv", ".md")):
                    with open(os.path.join(folder, name), encoding="utf-8") as f:
                        text = f.read()
                    self.assertNotIn(self.tmp, text, name)
                    self.assertNotIn(self.dest, text, name)
        readme = read_text(os.path.join(self.dest, "README.md"))
        self.assertIn("TUTORIAL DATA", readme)
        self.assertIn("CC0-1.0", readme)
        self.assertIn("Smoker lesions", readme)
        self.assertIn("Map the lesions.", readme)

    def test_limit_and_cache_reuse(self):
        catalog = self.dataverse_catalog()
        report = self.build(catalog, "joutsa_like", limit=2)
        self.assertEqual(report["subjects"], 2)
        downloads = [path for path in self.server.log if "/api/access/datafile/" in path]
        self.assertEqual(len(downloads), 2)
        shutil.rmtree(self.dest)
        self.server.log.clear()
        report = self.build(catalog, "joutsa_like", limit=2)  # the files are cached and verified: no request
        self.assertEqual(report["subjects"], 2)
        self.assertEqual([path for path in self.server.log if "/api/access/datafile/" in path], [])
        self.assertGreater(tutorials.cache_bytes("joutsa_like", self.cache), 0)
        self.assertTrue(tutorials.clear_cache("joutsa_like", self.cache))
        self.assertEqual(tutorials.cache_bytes("joutsa_like", self.cache), 0)

    def test_dest_not_empty_and_failures_leave_nothing(self):
        catalog = self.dataverse_catalog()
        os.makedirs(self.dest)
        with open(os.path.join(self.dest, "mine.txt"), "w") as f:
            f.write("keep me")
        with self.assertRaises(tutorials.TutorialError) as caught:
            self.build(catalog, "joutsa_like")
        self.assertEqual(caught.exception.code, "dest_exists")
        self.assertEqual(os.listdir(self.dest), ["mine.txt"])
        self.assertNoPartialBuild()
        os.remove(os.path.join(self.dest, "mine.txt"))
        report = self.build(catalog, "joutsa_like")  # an empty existing folder is fine
        self.assertEqual(report["subjects"], 5)

    def test_checksum_mismatch_stops_the_build(self):
        catalog = self.dataverse_catalog(md5_wrong="1" * 32)
        with self.assertRaises(fetch.ChecksumError):
            self.build(catalog, "joutsa_like")
        self.assertFalse(os.path.exists(self.dest))
        self.assertNoPartialBuild()

    def test_cancel_before_building(self):
        catalog = self.dataverse_catalog()
        cancel = jobs.CancelToken()
        cancel.cancel("test")
        with self.assertRaises(jobs.Cancelled):
            self.build(catalog, "joutsa_like", cancel=cancel)
        self.assertFalse(os.path.exists(self.dest))
        self.assertNoPartialBuild()

    def test_empty_and_unreadable_masks(self):
        catalog = self.dataverse_catalog(names=["0001", "0002", "0003"])
        files = catalog["entries"][0]["source"]["files"]
        empty = self.local("empty.nii")
        nifti.write_nifti(empty, np.zeros(MNI_GRID.shape, dtype=np.uint8), MNI_GRID.affine, dtype=np.uint8)
        data = read_bytes(empty)
        self.server.files["/api/access/datafile/7001"] = data
        files[1][2], files[1][3] = len(data), md5(data)
        garbage = b"not a nifti file at all" * 40
        self.server.files["/api/access/datafile/7002"] = garbage
        files[2][2], files[2][3] = len(garbage), md5(garbage)
        report = self.build(catalog, "joutsa_like")
        self.assertEqual(report["subjects"], 1 + 1)  # the empty mask is kept but excluded
        self.assertEqual([item["participant_id"] for item in report["skipped"]], ["sub-0003"])
        built = study.Study.open(self.dest)
        self.assertEqual(built.value("sub-0002", "include"), "0")
        self.assertEqual(built.value("sub-0001", "include"), "1")
        self.assertTrue(any("empty" in message for message in report["warnings"]))

    def test_listing_a_dataverse_dataset_without_pinned_files(self):
        catalog = self.dataverse_catalog(names=["0001", "0002"])
        entry = catalog["entries"][0]
        files = entry["source"].pop("files")
        listing = {"status": "OK", "data": {"latestVersion": {"files": [{"label": name, "dataFile": {"id": file_id, "filesize": size, "md5": digest}} for file_id, name, size, digest in files]}}}
        self.server.files["/api/datasets/:persistentId/"] = json.dumps(listing).encode()
        self.assertEqual(tutorials.list_dataverse(self.server.base, "doi:10.0000/fixture"), [list(item) for item in files])
        self.assertEqual(len(tutorials.dataverse_candidates(entry)), 2)


class S3BuildTest(TutorialsTestBase):
    def test_native_cohort_with_table_and_roles(self):
        catalog = self.s3_catalog(scans=False)
        report = self.build(catalog, "arc_like")
        self.assertEqual(report["subjects"], 6)
        self.assertTrue(report["native"])
        self.assertEqual(report["next"], "normalize first (A-03)")
        self.assertEqual(report["scans"], 0)
        self.assertEqual(report["without_mask"], 1)  # sub-M2099 has a table row and no mask
        self.assertEqual(report["score_columns"], ["wab_aq"])
        self.assertIn("volume_ml", report["covariate_columns"])
        self.assertNoPartialBuild()
        # the masks were listed in pages of three
        self.assertGreaterEqual(len([path for path in self.server.log if "list-type" in path]), 2)
        built = study.Study.open(self.dest)
        self.assertEqual(built.metadata["tutorial"], True)
        info = built.metadata["tutorial_info"]
        self.assertEqual((info["native"], info["normalize_first"], info["scans"]), (True, True, False))
        self.assertIsNone(built.metadata.get("space"))
        self.assertEqual(built.value("sub-M2001", "wab_aq", float), 93.3)
        self.assertEqual(built.value("sub-M2001", "wab_type"), "none")  # "None" is not a missing value
        self.assertEqual(built.value("sub-M2001", "notes"), "quoted, text")
        self.assertIsNone(built.value("sub-M2005", "wab_aq", float))
        self.assertEqual(built.value("sub-M2002", "sex"), "M")
        for row in built.subjects:
            self.assertEqual((row["space"], row["status"], row["group"]), ("native", "traced", "case"))
            self.assertTrue(row["lesion"].startswith("lesions/") and row["lesion"].endswith("_space-native_desc-lesion_mask.nii.gz"))
            self.assertEqual(row["scan"], "n/a")
            self.assertGreater(float(row["volume_ml"]), 0)
            sidecar = built.read_lesion_sidecar(row["participant_id"])
            self.assertEqual(sidecar["Type"], "Lesion")
            self.assertNotIn("SpatialReference", sidecar)
        # the roles are confirmed in participants.json: pickers offer the score and the covariates
        self.assertEqual(list(built.score_columns()), [("wab_aq", True)])
        self.assertEqual(set(built.covariate_columns()), {"volume_ml", "wab_days", "age_at_stroke", "sex"})
        spec = built.column_spec("wab_aq")
        self.assertEqual((spec.role, spec.type, spec.units), ("score", "numeric", "score"))
        self.assertEqual(built.column_spec("race").role, "metadata")
        with open(os.path.join(self.dest, "participants.json"), encoding="utf-8") as f:
            dictionary = json.load(f)
        self.assertEqual(dictionary["wab_aq"]["SlicerStudio"]["role"], "score")
        self.assertTrue(dictionary["wab_aq"]["SlicerStudio"]["confirmed"])
        self.assertEqual(dictionary["wab_days"]["SlicerStudio"]["role"], "covariate")
        readme = read_text(os.path.join(self.dest, "README.md"))
        self.assertIn("normalize first (A-03)", readme)
        self.assertIn("volume effect", readme)
        self.assertEqual(tutorials.tutorial_info(self.dest)["guidance"], "Aphasia severity is largely a volume effect: keep volume_ml.")

    def test_limit_prefers_subjects_with_a_score(self):
        catalog = self.s3_catalog(scans=False)
        report = self.build(catalog, "arc_like", limit=4)
        self.assertEqual(report["subjects"], 4)
        built = study.Study.open(self.dest)
        self.assertNotIn("sub-M2005", built.subject_ids)  # the only subject without a score is dropped first
        self.assertEqual(built.subject_ids, ["sub-M2001", "sub-M2002", "sub-M2003", "sub-M2007"])

    def test_scans_are_an_option(self):
        catalog = self.s3_catalog(scans=True, missing_scan="sub-M2003/ses-1002/anat/sub-M2003_ses-1002_acq-spc3_run-3_T2w")
        report = self.build(catalog, "arc_like", with_scans=True)
        self.assertEqual(report["scans"], 5)
        self.assertTrue(any("sub-M2003" in message and "scan" in message for message in report["warnings"]), report["warnings"])
        built = study.Study.open(self.dest)
        self.assertTrue(built.metadata["tutorial_info"]["scans"])
        row = built.subject("sub-M2001")
        self.assertEqual(row["scan"], "sub-M2001/ses-1000/anat/sub-M2001_ses-1000_acq-spc3_run-3_T2w.nii.gz")
        self.assertTrue(os.path.isfile(built.abspath(row["scan"])))
        self.assertEqual(built.value("sub-M2003", "scan"), None)
        sidecar = built.read_lesion_sidecar("sub-M2001")
        self.assertEqual(sidecar["SpatialReference"], "../sub-M2001/ses-1000/anat/sub-M2001_ses-1000_acq-spc3_run-3_T2w.nii.gz")
        self.assertNotIn(self.tmp, json.dumps(sidecar))
        # without --scans no scan is requested
        shutil.rmtree(self.dest)
        self.server.log.clear()
        self.build(catalog, "arc_like")
        self.assertEqual([path for path in self.server.log if "/T2w.nii.gz" in path or ("_T2w.nii.gz" in path and "desc-lesion" not in path)], [])

    def test_offline_rebuild_from_cache_and_a_dropped_listing(self):
        catalog = self.s3_catalog(scans=False)
        self.build(catalog, "arc_like")
        shutil.rmtree(self.dest)
        report = self.build(catalog, "arc_like", offline=True)
        self.assertEqual(report["subjects"], 6)
        self.assertTrue(any("listing" in message for message in report["warnings"]), report["warnings"])
        # a tutorial that was never downloaded cannot be built offline
        other = copy.deepcopy(catalog)
        other["entries"][0]["id"] = "arc_other"
        shutil.rmtree(self.dest)
        with self.assertRaises(fetch.OfflineError):
            self.build(other, "arc_other", offline=True)

    def test_multipart_etag_is_not_a_checksum(self):
        catalog = self.s3_catalog(scans=False)
        for key in list(self.server.files):
            if key.endswith("_desc-lesion_mask.nii.gz"):
                self.server.multipart.add(key)
        entry = catalog["entries"][0]
        candidates, _warnings = tutorials.s3_candidates(entry, self.cache, None, None)
        self.assertEqual(len(candidates), 6)
        self.assertTrue(all(item["resource"]["checksum"] == fetch.RECORD_ON_FIRST_DOWNLOAD for item in candidates))
        self.assertEqual(self.build(catalog, "arc_like")["subjects"], 6)

    def test_changed_source_is_reported(self):
        catalog = self.s3_catalog(scans=False)
        catalog["entries"][0]["source"]["expected"]["masks"] = 9
        report = self.build(catalog, "arc_like")
        self.assertTrue(any("expects 9" in message for message in report["warnings"]))


    def test_a_second_mask_of_a_subject_is_left_out(self):
        catalog = self.s3_catalog(scans=False)
        prefix = "ds00001/derivatives/lesion_masks/"
        extra = self.local("extra.nii.gz")
        blob(extra, NATIVE_GRID, (3, 3, 3), radius=1)
        self.server.files[f"/{prefix}sub-M2001/ses-2000/anat/sub-M2001_ses-2000_T2w_desc-lesion_mask.nii.gz"] = read_bytes(extra)
        self.server.files[f"/{prefix}sub-m2002/ses-9999/anat/sub-m2002_ses-9999_T2w_desc-lesion_mask.nii.gz"] = read_bytes(extra)  # same id in lower case
        report = self.build(catalog, "arc_like")
        self.assertEqual(report["subjects"], 6)
        self.assertEqual(sum("already has a mask" in message for message in report["warnings"]), 2)
        built = study.Study.open(self.dest)
        self.assertEqual(built.subject_ids, sorted(S3_SUBJECTS, key=tutorials.natural_key))

    def test_table_columns_the_study_owns_are_not_copied(self):
        catalog = self.s3_catalog(scans=False)
        lines = TABLE.splitlines()
        lines[0] += "\tlesion\tvolume_ml\tspace\tinclude"
        lines[1:] = [line + "\tfoo.nii\t999\tMNI\t0" for line in lines[1:]]
        self.server.files["/ds00001/participants.tsv"] = ("\n".join(lines) + "\n").encode()
        report = self.build(catalog, "arc_like")
        self.assertTrue(any("used by the study itself" in message for message in report["warnings"]))
        built = study.Study.open(self.dest)
        for sid in built.subject_ids:
            self.assertNotEqual(built.value(sid, "volume_ml"), "999")
            self.assertEqual(built.value(sid, "space"), "native")
            self.assertTrue(os.path.isfile(built.abspath(built.value(sid, "lesion"))))
            self.assertNotEqual(built.value(sid, "include"), "0")

    def test_a_killed_build_leaves_nothing_after_the_next_one(self):
        catalog = self.s3_catalog(scans=False)
        parent = os.path.dirname(self.dest)
        # a stale folder of a process that no longer runs (pid 2**22 + 1 is above the usual pid limit) and one of a live process
        stale = os.path.join(parent, f"old.building-{2**22 + 1}")
        live = os.path.join(parent, f"other.building-{os.getppid()}")
        for path in (stale, live):
            os.makedirs(path)
        self.build(catalog, "arc_like")
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.isdir(live))


class FolderBuildTest(TutorialsTestBase):
    def test_folder_cohort_with_a_known_grid(self):
        catalog, folder = self.folder_catalog()
        with self.assertRaises(tutorials.TutorialError) as caught:
            self.build(catalog, "lesymap_like")
        self.assertEqual(caught.exception.code, "source_dir")
        report = self.build(catalog, "lesymap_like", source_dir=folder)
        self.assertEqual(report["subjects"], 4)  # the template is not a lesion
        built = study.Study.open(self.dest)
        self.assertEqual(built.subject_ids, ["sub-001", "sub-002", "sub-003", "sub-004"])
        self.assertEqual(built.metadata["space"], "MNIColin27")
        self.assertEqual(built.grid_answers, {"SPM-1mm": "MNIColin27"})
        self.assertEqual(built.value("sub-002", "space"), "MNIColin27")
        self.assertEqual(built.value("sub-003", "score", float), 8.25)
        self.assertEqual(built.score_columns(), [("score", True)])
        self.assertTrue(built.metadata["tutorial"])
        self.assertTrue(os.path.isfile(built.abspath(built.value("sub-001", "lesion"))))
        # the user's folder is never modified
        self.assertEqual(sorted(os.listdir(os.path.join(folder, "extdata"))), ["behavior.csv", "colin_template.nii.gz", "lesions"])
        self.assertEqual(self.server.log, [])  # nothing is downloaded for a folder source


class CommandLineTest(TutorialsTestBase):
    def run_cli(self, *args):
        process = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.tutorials", *args], cwd=MODULE_DIR, capture_output=True, text=True, timeout=300, check=False)
        events = []
        for line in process.stdout.splitlines():
            if line.startswith("{"):
                events.append(json.loads(line))
        return process.returncode, events

    def test_list(self):
        code, events = self.run_cli("list", "--cache", self.cache)
        self.assertEqual(code, 0, events)
        results = [event for event in events if event["event"] == "result"]
        self.assertEqual([event["id"] for event in results], ["joutsa2022_addiction", "soop_nihss", "arc_aphasia", "lesymap_groundtruth"])
        self.assertEqual(events[-1]["event"], "done")
        self.assertTrue(all(event["badge"] == "green" for event in results))

    def test_build_events(self):
        catalog = self.dataverse_catalog()
        path = self.local("catalog.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"format": tutorials.TUTORIALS_FORMAT, "version": tutorials.TUTORIALS_VERSION, **catalog}, f)
        code, events = self.run_cli("build", "--catalog", path, "--id", "joutsa_like", "--dest", self.dest, "--cache", self.cache, "--limit", "3")
        self.assertEqual(code, 0, events)
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds[0], "start")
        self.assertEqual(kinds[-1], "done")
        self.assertEqual([event["name"] for event in events if event["event"] == "stage"], ["plan", "download", "build"])
        self.assertTrue(any(event["event"] == "progress" and event.get("stage") == "download" for event in events))
        result = [event for event in events if event["event"] == "result"][-1]
        self.assertEqual((result["kind"], result["subjects"], result["native"]), ("study", 3, False))
        self.assertEqual(events[-1]["outputs"], [self.dest])
        self.assertTrue(study.Study.is_study(self.dest))

    def test_errors_are_events(self):
        code, events = self.run_cli("build", "--id", "no_such_tutorial", "--dest", self.dest, "--cache", self.cache)
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["event"], "error")
        self.assertEqual(events[-1]["code"], "unknown_id")
        self.assertFalse(os.path.exists(self.dest))


if __name__ == "__main__":
    unittest.main()

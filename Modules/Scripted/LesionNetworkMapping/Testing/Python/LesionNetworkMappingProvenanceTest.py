"""Tests of connectome provenance (format "lnmc" v1.1 optional keys), named subject subsets,
the engine's --subject-list, the pooling guard of export.py and the engine's orphan watchdog.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingProvenanceTest.py -v
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np
from scipy import io as sio

from LesionNetworkMappingLib import connectome, engine, export, jobs, nifti

GSP_DOI = "10.7910/DVN/KKTJQC"


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))


def _sphere_lesion(path, center_world, radius=3.0):
    affine = np.diag([-1.0, 1.0, 1.0, 1.0])
    affine[:3, 3] = [25.0, -25.0, -25.0]
    shape = (51, 51, 51)
    ijk = np.indices(shape).reshape(3, -1).T
    world = ijk @ affine[:3, :3].T + affine[:3, 3]
    lesion = (np.linalg.norm(world - np.asarray(center_world), axis=1) <= radius).reshape(shape)
    nifti.write_nifti(path, lesion.astype(np.uint8), affine)
    return path


class _TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-provenance-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


class LeadDbsSubsetsTest(_TempDirTestCase):
    def _write_dataset(self, info):
        """A 3-subject Lead-DBS fMRI connectome folder with the given dataset_info.json."""
        rng = np.random.default_rng(3)
        shape = (5, 6, 4)
        mask = np.zeros(shape, dtype=bool)
        mask[1:4, 1:5, 1:3] = True
        outidx = np.sort(np.ravel_multi_index(np.nonzero(mask), shape, order="F")) + 1
        dataset = self.path("GSPtest")
        os.makedirs(os.path.join(dataset, "vol"))
        sub_ids = np.empty((3, 1), dtype=object)
        for index, subject in enumerate(("subA", "subB", "subC")):
            sub_ids[index, 0] = np.array([subject, f"{subject}_run1.mat"], dtype=object)
            sio.savemat(os.path.join(dataset, "vol", f"{subject}_run1.mat"), {"gmtc": (50 + rng.standard_normal((10, len(outidx)))).astype(np.float32)})
        spm_mat = np.array([[-2.0, 0, 0, 92], [0, 2, 0, -128], [0, 0, 2, -74], [0, 0, 0, 1]])
        vol = {"outidx": outidx.astype(np.float64)[:, np.newaxis], "space": {"dim": np.array([shape], np.float64), "mat": spm_mat, "fname": "space.nii"}, "subIDs": sub_ids}
        sio.savemat(os.path.join(dataset, "dataset_volsurf.mat"), {"vol": vol})
        with open(os.path.join(dataset, "dataset_info.json"), "w", encoding="utf-8") as f:
            json.dump(info, f)
        return dataset

    def test_subsets_source_and_scalar_subs(self):
        # As in the GSP1000 v2 (Cohen 2023) dataset_info.json: no formatversion, a citation object and a scalar "subs"
        info = {
            "name": "GSP 1000 v2 (Cohen 2023)",
            "tag": "GSP1000v2",
            "type": "fMRI_timecourses",
            "subsets": [
                {"name": "Full Set", "subs": [1, 2, 3]},
                {"name": "First 2 Subjects", "subs": [1, 2]},
                {"name": "First Subject", "subs": 1},
                {"name": "Broken", "subs": [0, 7]},  # out of range: ignored
            ],
            "citation": {"name": "GSP 1000 v2 (Cohen 2023)", "short": "Cohen et al. 2023", "long": f"Cohen, A., Al-Fatly, B., Horn, A., 2023. GSP1000 Preprocessed Connectome for Lead DBS. https://doi.org/{GSP_DOI}"},
        }
        dataset = self._write_dataset(info)
        bundle = connectome.ConnectomeBundle.open(connectome.from_leaddbs(dataset, self.path("lead.lnmc")))
        self.assertEqual(bundle.subsets, {"Full Set": ["subA", "subB", "subC"], "First 2 Subjects": ["subA", "subB"], "First Subject": ["subA"]})
        source = bundle.provenance["source"]
        self.assertEqual((source["name"], source["tag"], source["doi"]), ("GSP 1000 v2 (Cohen 2023)", "GSP1000v2", GSP_DOI))
        self.assertEqual(source["citation"], info["citation"])
        self.assertIn(GSP_DOI, bundle.citation)  # the dataset's citation text when none is given
        self.assertEqual(bundle.subject_list("First Subject"), ["subA"])
        self.assertEqual(bundle.metadata["subjectsSha256"], export.subjects_sha256(["subA", "subB", "subC"]))
        self.assertEqual(bundle.metadata["mask"]["sha256"], bundle.mask_sha256)

        # Subsets that refer to subjects that were not converted are left out; a single subset object works
        info["subsets"] = {"name": "First Subject", "subs": 1}
        dataset_info = os.path.join(dataset, "dataset_info.json")
        with open(dataset_info, "w", encoding="utf-8") as f:
            json.dump(info, f)
        one = connectome.ConnectomeBundle.open(connectome.from_leaddbs(dataset, self.path("one.lnmc"), max_subjects=1))
        self.assertEqual(one.subsets, {"First Subject": ["subA"]})
        info["subsets"] = [{"name": "Full Set", "subs": [1, 2, 3]}, {"name": "First 2 Subjects", "subs": [1, 2]}]
        with open(dataset_info, "w", encoding="utf-8") as f:
            json.dump(info, f)
        two = connectome.ConnectomeBundle.open(connectome.from_leaddbs(dataset, self.path("two.lnmc"), max_subjects=2))
        self.assertEqual(list(two.subsets), ["First 2 Subjects"])
        # Provenance given by the caller is merged (its keys win)
        tagged = connectome.from_leaddbs(dataset, self.path("tagged.lnmc"), provenance={"licence": "GSP-DUT-2014", "source": {"version": "4.0"}, "preprocessing": {"gsr": True}})
        tagged = connectome.ConnectomeBundle.open(tagged)
        self.assertEqual((tagged.provenance["source"]["version"], tagged.provenance["source"]["doi"]), ("4.0", GSP_DOI))
        self.assertEqual((tagged.provenance["licence"], tagged.provenance["preprocessing"]["gsr"]), ("GSP-DUT-2014", True))


class BundleProvenanceTest(_TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.meta = connectome.make_synthetic(self.path("synthetic.lnmc"), n_subjects=7, name="Synthetic Provenance")
        self.bundle_path = self.meta["path"]

    def test_bundles_without_new_keys_open_unchanged(self):
        manifest = os.path.join(self.bundle_path, connectome.MANIFEST_NAME)
        with open(manifest, encoding="utf-8") as f:
            metadata = json.load(f)
        for key in connectome.PROVENANCE_KEYS:
            metadata.pop(key, None)  # a bundle written before provenance v1.1
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
        with open(manifest, "rb") as f:
            before = f.read()
        bundle = connectome.ConnectomeBundle.open(self.bundle_path)
        self.assertEqual(bundle.metadata, metadata)
        self.assertEqual(bundle.subsets, {})
        provenance = bundle.provenance
        self.assertEqual((provenance["kind"], provenance["licence"], provenance["preprocessing"]["gsr"], provenance["subsets"]), ("timeseries", "unknown", "unknown", {}))
        self.assertEqual(bundle.participants_rows(), [])
        self.assertEqual(bundle.mask_sha256, connectome.mask_content_sha256(bundle.mask))
        info = export.bundle_info(bundle)
        self.assertEqual((info["doi"], info["dataset_version"], info["licence"], info["gsr"], info["subsets"]), (None, None, None, None, []))
        # The engine runs on it without touching connectome.json
        lesion = _sphere_lesion(self.path("sub-01_lesion.nii.gz"), self.meta["blobs"]["A"]["center_world"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = engine.main(["--bundle", self.bundle_path, "--lesion", lesion, "--out", self.path("out"), "--subjects", "3"])
        self.assertEqual(code, 0, output.getvalue())
        with open(manifest, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_set_metadata_round_trip(self):
        ids = connectome.ConnectomeBundle.open(self.bundle_path).subject_ids
        keys = {
            "source": {"doi": "10.7910/DVN/ILXIKS", "version": "3.0", "url": "https://doi.org/10.7910/DVN/ILXIKS", "files": [{"name": "GSP1000_v2_00.tar", "md5": "0" * 32, "size": 1}]},
            "licence": "GSP-DUT-2014",
            "termsSha256": "a" * 64,
            "acknowledgement": "Data were provided by test.",
            "preprocessing": {"gsr": False, "nuisance": "WM+CSF", "bandpass": [0.0001, 0.08], "smoothingMm": 7, "pipelineUrl": "https://example.org/pipeline"},
            "population": {"n": 7, "ageRange": [18, 36], "sexRatio": "1:1", "scanners": 5},
            "trS": 3.0,
            "subsets": {"First 5 Subjects": ids[:5], "user:odd": ids[::2]},
            "mask": {"source": "GSP finalmask"},
            "kind": "timeseries",
        }
        metadata = connectome.set_metadata(self.bundle_path, **keys)
        bundle = connectome.ConnectomeBundle.open(self.bundle_path)
        self.assertEqual(bundle.metadata, metadata)
        for key, value in keys.items():
            if key != "mask":
                self.assertEqual(bundle.metadata[key], value)
        self.assertEqual(bundle.metadata["mask"], {"source": "GSP finalmask", "sha256": bundle.mask_sha256})
        self.assertEqual(bundle.provenance["preprocessing"]["gsr"], False)
        self.assertEqual(bundle.subject_list("user:odd"), ids[::2])
        # The keys survive another update, and None removes a key
        connectome.set_metadata(self.bundle_path, description="edited", trS=None)
        again = connectome.ConnectomeBundle.open(self.bundle_path)
        self.assertNotIn("trS", again.metadata)
        self.assertEqual((again.description, again.metadata["subsets"], again.metadata["source"]), ("edited", keys["subsets"], keys["source"]))
        # Required, unknown and invalid keys are refused and leave the bundle unchanged
        manifest = os.path.join(self.bundle_path, connectome.MANIFEST_NAME)
        with open(manifest, "rb") as f:
            before = f.read()
        for bad in (
            {"subjects": []},
            {"nVoxels": 1},
            {"name": "Other"},
            {"licenses": "CC0-1.0"},
            {"preprocessing": {"gsr": "yes"}},
            {"subsets": {"Bad": ["sub-001", "sub-999"]}},
            {"subsets": {"Empty": []}},
            {"participants": "../participants.tsv"},
            {"trS": -1},
            {"mask": {"sha256": "b" * 64}},
            {"subjectsSha256": "c" * 64},
        ):
            with self.subTest(keys=bad), self.assertRaises(ValueError):
                connectome.set_metadata(self.bundle_path, **bad)
        with open(manifest, "rb") as f:
            self.assertEqual(f.read(), before)
        # A subset listing unknown ids written by another tool is reported when used, but the bundle opens
        with open(manifest, encoding="utf-8") as f:
            metadata = json.load(f)
        metadata["subsets"] = {"Bad": ["sub-001", "sub-999"]}
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(metadata, f)
        broken = connectome.ConnectomeBundle.open(self.bundle_path)
        with self.assertRaisesRegex(ValueError, "sub-999"):
            broken.subsets

    def test_subject_lists(self):
        bundle = connectome.ConnectomeBundle.open(self.bundle_path)
        ids = bundle.subject_ids
        connectome.set_metadata(self.bundle_path, subsets={"First 5 Subjects": ids[:5]})
        bundle = connectome.ConnectomeBundle.open(self.bundle_path)
        resolved = bundle.resolve_subject_list("First 5 Subjects")
        self.assertEqual((resolved.ids, resolved.name, resolved.source, resolved.sha256), (ids[:5], "First 5 Subjects", "subset", export.subjects_sha256(ids[:5])))
        # A file: header, comments, separators, duplicates and any order; ids come back in bundle order
        list_file = self.path("subjects.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            f.write(f"participant_id\n# chosen subjects\n{ids[4]}\n{ids[1]}, {ids[2]}\n\n{ids[1]}\n")
        resolved = bundle.resolve_subject_list(list_file)
        self.assertEqual((resolved.ids, resolved.name, resolved.source, resolved.path), ([ids[1], ids[2], ids[4]], None, "file", os.path.abspath(list_file)))
        # A participants.tsv: only its first column holds the ids
        tsv = self.path("participants.tsv")
        with open(tsv, "w", encoding="utf-8") as f:
            f.write(f"participant_id\tage\tsex\n{ids[0]}\t25\tF\n{ids[3]}\t31\tM\n")
        self.assertEqual(bundle.subject_list(tsv), [ids[0], ids[3]])
        # Comma-separated text and lists
        self.assertEqual(bundle.subject_list(f"{ids[6]},{ids[0]}"), [ids[0], ids[6]])
        self.assertEqual(bundle.subject_list([ids[3]]), [ids[3]])
        # Unknown ids are listed in the error
        with self.assertRaisesRegex(ValueError, "2 subject id.*sub-998, sub-999"):
            bundle.subject_list([ids[0], "sub-998", "sub-999"])
        with open(list_file, "w", encoding="utf-8") as f:
            f.write("sub-001\nsub-404\n")
        with self.assertRaisesRegex(ValueError, "sub-404"):
            bundle.subject_list(list_file)
        with self.assertRaisesRegex(ValueError, "not a subset name.*First 5 Subjects"):
            bundle.subject_list("First 6 Subjects")
        with self.assertRaisesRegex(ValueError, "empty"):
            bundle.subject_list([])
        # Surrounding spaces of a subset name are ignored
        self.assertEqual(bundle.resolve_subject_list(" First 5 Subjects ").name, "First 5 Subjects")
        # A CSV table whose header has several columns: only the first column holds ids
        csv = self.path("participants.csv")
        with open(csv, "w", encoding="utf-8") as f:
            f.write(f"participant_id,age\n{ids[2]},25\n{ids[5]},30\n")
        self.assertEqual(bundle.subject_list(csv), [ids[2], ids[5]])

    def test_writer_provenance_and_participants(self):
        # from_nifti_timeseries copies participants.tsv and records it; the provenance is merged at finalize
        rng = np.random.default_rng(1)
        mask = np.zeros((4, 5, 3), dtype=bool)
        mask[1:3, 1:4, 1:2] = True
        affine = np.diag([2.0, 2.0, 2.0, 1.0])
        nifti.write_nifti(self.path("mask.nii.gz"), mask.astype(np.uint8), affine)
        for subject in ("sub-01", "sub-02"):
            nifti.write_nifti(self.path(f"{subject}_bold.nii.gz"), rng.standard_normal((4, 5, 3, 12)).astype(np.float32), affine)
        with open(self.path("participants.tsv"), "w", encoding="utf-8") as f:
            f.write("participant_id\tage\nsub-01\t20\nsub-02\t30\n")
        with open(self.path("participants.json"), "w", encoding="utf-8") as f:
            json.dump({"age": {"Units": "years"}}, f)
        path = connectome.from_nifti_timeseries(
            self.path("sub-*_bold.nii.gz"),
            self.path("mask.nii.gz"),
            self.path("nifti.lnmc"),
            runs_grouping=r"(?P<subject>sub-\d+)",
            participants=self.path("participants.tsv"),
            provenance={"preprocessing": {"gsr": True}, "trS": 2.0, "subsets": {"user:first": ["sub-01"]}},
        )
        bundle = connectome.ConnectomeBundle.open(path)
        self.assertEqual(bundle.participants_rows(), [{"participant_id": "sub-01", "age": "20"}, {"participant_id": "sub-02", "age": "30"}])
        self.assertTrue(os.path.isfile(os.path.join(path, "participants.json")))
        self.assertEqual((bundle.metadata["participants"], bundle.metadata["trS"], bundle.subsets), ("participants.tsv", 2.0, {"user:first": ["sub-01"]}))
        self.assertEqual(bundle.metadata["mask"]["sha256"], connectome.mask_content_sha256(mask))
        # An interrupted conversion resumed without the provenance argument keeps its provenance
        writer = connectome.ConnectomeWriter(self.path("resumed.lnmc"), mask, affine, provenance={"licence": "CC-BY-4.0", "subsets": {"one": ["sub-01"]}})
        writer.add_subject("sub-01", [rng.standard_normal((int(mask.sum()), 5))])
        writer = connectome.ConnectomeWriter(self.path("resumed.lnmc"), mask, affine, resume=True)
        writer.add_subject("sub-02", [rng.standard_normal((int(mask.sum()), 5))])
        resumed = connectome.ConnectomeBundle.open(writer.finalize())
        self.assertEqual((resumed.provenance["licence"], resumed.subsets), ("CC-BY-4.0", {"one": ["sub-01"]}))
        self.assertNotIn("pendingProvenance", resumed.metadata)
        # Bad provenance fails before anything is converted
        with self.assertRaisesRegex(ValueError, "Unknown connectome.json keys"):
            connectome.ConnectomeWriter(self.path("bad.lnmc"), mask, affine, provenance={"gsr": True})
        # A subset of unknown subjects fails at finalize
        writer = connectome.ConnectomeWriter(self.path("bad2.lnmc"), mask, affine, provenance={"subsets": {"x": ["nobody"]}})
        writer.add_subject("sub-01", [rng.standard_normal((int(mask.sum()), 5))])
        with self.assertRaisesRegex(ValueError, "nobody"):
            writer.finalize()

    def test_compute_degree_signed_fz(self):
        bundle = connectome.ConnectomeBundle.open(self.bundle_path)
        abs_fz = connectome.compute_degree(bundle, n_seeds=40, seed=2, measure="abs_fz", store=False)
        fz = connectome.compute_degree(bundle, n_seeds=40, seed=2, measure="fz", store=False)
        self.assertTrue(np.all(np.abs(fz) <= abs_fz + 1e-6))
        self.assertLess(fz.min(), 0.0)  # blob B is anti-correlated with A and A2
        with self.assertRaises(ValueError):
            connectome.compute_degree(bundle, measure="abs_r")
        # The signed map is stored apart: it never replaces the hubness map used by the QA
        connectome.compute_degree(bundle, n_seeds=40, seed=2, measure="fz")
        self.assertIsNone(bundle.degree)
        np.testing.assert_array_equal(np.load(os.path.join(self.bundle_path, "derived", "degree_fz.npy")), fz)
        connectome.compute_degree(bundle, n_seeds=40, seed=2, measure="abs_fz")
        np.testing.assert_array_equal(bundle.degree, abs_fz)

    def test_bundle_info_fields(self):
        ids = connectome.ConnectomeBundle.open(self.bundle_path).subject_ids
        connectome.set_metadata(
            self.bundle_path,
            source={"doi": "10.7910/DVN/ILXIKS", "version": "3.0"},
            licence="GSP-DUT-2014",
            termsSha256="d" * 64,
            preprocessing={"gsr": True, "smoothingMm": 6},
            population={"n": 1000, "ageRange": [18, 36]},
            subsets={"First 5 Subjects": ids[:5], "user:last": ids[-2:]},
        )
        bundle = connectome.ConnectomeBundle.open(self.bundle_path)
        info = export.bundle_info(bundle)
        self.assertEqual(info["doi"], "10.7910/DVN/ILXIKS")
        self.assertEqual(info["dataset_version"], "3.0")
        self.assertEqual(info["licence"], "GSP-DUT-2014")
        self.assertEqual(info["terms_sha256"], "d" * 64)
        self.assertEqual(info["acknowledgement"], export.GSP_ACKNOWLEDGEMENT)  # the GSP wording when none is recorded
        self.assertIs(info["gsr"], True)
        self.assertEqual(info["preprocessing"], {"gsr": True, "smoothingMm": 6})
        self.assertEqual(info["population"], {"n": 1000, "ageRange": [18, 36]})
        self.assertEqual(info["mask_sha256"], bundle.mask_sha256)
        self.assertEqual(info["subsets"], ["First 5 Subjects", "user:last"])
        self.assertEqual(info["manifest_sha256"], export.file_sha256(os.path.join(self.bundle_path, "connectome.json")))
        # data_sha256 ignores optional metadata, the manifest sha256 does not
        connectome.set_metadata(self.bundle_path, description="changed")
        edited = export.bundle_info(connectome.ConnectomeBundle.open(self.bundle_path))
        self.assertEqual(edited["data_sha256"], info["data_sha256"])
        self.assertNotEqual(edited["manifest_sha256"], info["manifest_sha256"])
        self.assertNotIn("warnings", info)
        json.dumps(info, allow_nan=False)
        # Invalid optional keys written by another tool are reported, not fatal
        manifest = os.path.join(self.bundle_path, connectome.MANIFEST_NAME)
        with open(manifest, encoding="utf-8") as f:
            metadata = json.load(f)
        metadata["subsets"] = {"broken": ["nobody"]}
        metadata["mask"] = {"sha256": "0" * 64}
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(metadata, f)
        broken = export.bundle_info(connectome.ConnectomeBundle.open(self.bundle_path))
        self.assertEqual((broken["subsets"], broken["mask_sha256"], len(broken["warnings"])), (None, None, 2))


class SubjectListCommandLineTest(_TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.meta = connectome.make_synthetic(self.path("synthetic.lnmc"), n_subjects=7, name="Synthetic")
        self.ids = connectome.ConnectomeBundle.open(self.meta["path"]).subject_ids
        connectome.set_metadata(
            self.meta["path"],
            subsets={"First 5 Subjects": self.ids[:5]},
            source={"doi": "10.7910/DVN/ILXIKS", "version": "3.0"},
            licence="GSP-DUT-2014",
            preprocessing={"gsr": True},
        )
        self.lesion = _sphere_lesion(self.path("sub-01_space-MNI152NLin6Asym_desc-lesion_mask.nii.gz"), self.meta["blobs"]["A"]["center_world"])

    def _run(self, *args):
        result = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.engine", *args], check=False, capture_output=True, text=True, env=_env(), cwd=self.tmp, timeout=300)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return result, events

    def test_named_subset_run_is_labelled_and_hashed(self):
        run_dir = self.path("run")
        run_json = os.path.join(run_dir, "run.json")
        result, events = self._run("--bundle", self.meta["path"], "--lesion", self.lesion, "--out", run_dir, "--subject-list", "First 5 Subjects", "--run-json", run_json, "--threads", "1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        start, done = events[0], events[-1]
        self.assertEqual((start["event"], start["subjects"], start["pid"]), ("start", 5, start["pid"]))
        self.assertIsInstance(start["pid"], int)
        self.assertEqual((done["event"], done["n"]), ("done", 5))
        expected = [os.path.join(run_dir, f"sub-01_conn-Synthetic_desc-{desc}SubsetFirst5Subjects_funcmap.nii.gz") for desc in ("AvgR", "AvgRFz", "T")]
        self.assertEqual(done["outputs"], expected)
        with open(run_json, encoding="utf-8") as f:
            run = json.load(f)
        self.assertEqual(run["desc_suffix"], "SubsetFirst5Subjects")
        self.assertEqual(run["subjects"]["list_name"], "First 5 Subjects")
        self.assertEqual(run["subjects"]["list_sha256"], export.subjects_sha256(self.ids[:5]))
        self.assertEqual(run["subject_ids_sha256"], run["subjects"]["list_sha256"])
        self.assertEqual((run["bundle"]["doi"], run["bundle"]["gsr"], run["bundle"]["subsets"]), ("10.7910/DVN/ILXIKS", True, ["First 5 Subjects"]))

        # The export carries the provenance into sidecars, provenance.json and methods.md
        out = self.path("export")
        files = export.export_run(run_dir, out)
        self.assertEqual([os.path.basename(p) for p in files["sidecars"]], ["sub-01_conn-Synthetic_desc-SubsetFirst5Subjects_lnm.json"])
        self.assertEqual(sorted(os.path.basename(p) for p in files["maps"]), sorted(os.path.basename(p) for p in expected))  # the subset label is kept
        with open(files["sidecars"][0], encoding="utf-8") as f:
            sidecar = json.load(f)
        self.assertEqual(sidecar["terms"], "GSP-DUT-2014")
        connectome_info = sidecar["connectome"]
        self.assertEqual((connectome_info["doi"], connectome_info["dataset_version"], connectome_info["gsr"]), ("10.7910/DVN/ILXIKS", "3.0", True))
        self.assertEqual((connectome_info["subset"], connectome_info["subject_list_sha256"]), ("First 5 Subjects", export.subjects_sha256(self.ids[:5])))
        self.assertEqual(connectome_info["mask_sha256"], connectome.ConnectomeBundle.open(self.meta["path"]).mask_sha256)
        self.assertEqual(connectome_info["acknowledgement"], export.GSP_ACKNOWLEDGEMENT)
        self.assertEqual(sidecar["disclaimer"], export.DISCLAIMER)
        with open(files["provenance"], encoding="utf-8") as f:
            provenance = json.load(f)
        self.assertEqual(provenance["subject_list"]["name"], "First 5 Subjects")
        self.assertEqual((provenance["terms"], provenance["acknowledgement"]), ("GSP-DUT-2014", export.GSP_ACKNOWLEDGEMENT))
        for key in ("siddiqi2026", "zaleskycash2026", "vandenheuvel2026"):
            self.assertIn(export.CITATIONS[key], provenance["citations"])
        with open(files["methods"], encoding="utf-8") as f:
            methods = f.read()
        for text in ("doi:10.7910/DVN/ILXIKS", "dataset version 3.0", 'subset "First 5 Subjects"', "with global signal regression", "## Acknowledgements", export.GSP_ACKNOWLEDGEMENT, export.DISCLAIMER, "doi:10.64898/2026.02.24.707529", "doi:10.1038/s41593-026-02259-3"):
            self.assertIn(text, methods)
        with open(files["reproduce"], encoding="utf-8") as f:
            self.assertIn("'First 5 Subjects'", f.read())
        self.assertEqual(export.check_poolable([run, provenance]), [])

    def test_subject_list_file_and_usage_errors(self):
        list_file = self.path("list.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            f.write(f"{self.ids[6]}\n{self.ids[2]}\n{self.ids[3]}\n")
        run_dir = self.path("run")
        run_json = os.path.join(run_dir, "run.json")
        result, events = self._run("--bundle", self.meta["path"], "--lesion", self.lesion, "--out", run_dir, "--subject-list", os.path.basename(list_file), "--run-json", run_json, "--threads", "1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(events[-1]["n"], 3)
        self.assertTrue(events[-1]["outputs"][0].endswith("sub-01_conn-Synthetic_desc-AvgR_funcmap.nii.gz"))  # no subset name: no suffix
        with open(run_json, encoding="utf-8") as f:
            run = json.load(f)
        expected = [self.ids[2], self.ids[3], self.ids[6]]
        self.assertEqual(run["subjects"]["list_sha256"], export.subjects_sha256(expected))
        self.assertEqual((run["subjects"]["list_name"], run["subjects"]["list_source"], run["subjects"]["list_file"]), (None, "file", list_file))
        # reproduce.sh refers to the list file by its absolute path
        files = export.export_run(run_dir, self.path("export"))
        with open(files["reproduce"], encoding="utf-8") as f:
            self.assertIn(list_file, f.read())
        # Comma-separated ids
        result, events = self._run("--bundle", self.meta["path"], "--lesion", self.lesion, "--out", self.path("run2"), "--subject-list", f"{self.ids[0]},{self.ids[1]}", "--threads", "1")
        self.assertEqual((result.returncode, events[-1]["n"]), (0, 2))
        # Unknown ids are an error event that lists them
        result, events = self._run("--bundle", self.meta["path"], "--lesion", self.lesion, "--out", self.path("run3"), "--subject-list", "sub-001,sub-404")
        self.assertEqual((result.returncode, events[-1]["event"]), (1, "error"))
        self.assertIn("sub-404", events[-1]["message"])
        # --subject-list excludes --subjects and --random
        for extra in (["--subjects", "3"], ["--random"]):
            result, events = self._run("--bundle", self.meta["path"], "--lesion", self.lesion, "--out", self.path("run4"), "--subject-list", "First 5 Subjects", *extra)
            self.assertEqual((result.returncode, [event["event"] for event in events]), (2, ["error"]))
        # An explicit --desc-suffix wins over the subset name
        result, events = self._run("--bundle", self.meta["path"], "--lesion", self.lesion, "--out", self.path("run5"), "--subject-list", "First 5 Subjects", "--desc-suffix", "PreviewN5")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(events[-1]["outputs"][0].endswith("_desc-AvgRPreviewN5_funcmap.nii.gz"))


class PoolingGuardTest(_TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.meta = connectome.make_synthetic(self.path("synthetic.lnmc"), n_subjects=6, name="Synthetic")
        self.lesion = _sphere_lesion(self.path("sub-01_lesion.nii.gz"), self.meta["blobs"]["A"]["center_world"])

    def _run(self, name, *args):
        run_json = self.path(name, "run.json")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = engine.main(["--bundle", self.meta["path"], "--lesion", self.lesion, "--out", self.path(name), "--run-json", run_json, *args])
        self.assertEqual(code, 0, output.getvalue())
        with open(run_json, encoding="utf-8") as f:
            return json.load(f)

    def test_check_poolable_and_compare_runs(self):
        first4 = self._run("a", "--subjects", "4")
        again = self._run("b", "--subjects", "4")
        self.assertEqual(export.check_poolable([first4, again]), [])
        self.assertEqual(export.compare_runs(first4, again), [])
        self.assertEqual(export.compatibility_key(first4), export.compatibility_key(again))
        key = export.compatibility_key(first4)
        self.assertEqual(key[0], first4["bundle"]["manifest_sha256"])
        self.assertEqual(key[1:], (first4["subject_ids_sha256"], "T", 2, False, export.LIBRARY_VERSION))

        random4 = self._run("c", "--subjects", "4", "--random", "--seed", "3")
        messages = export.check_poolable([first4, random4])
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("different subject lists (sha "), messages)
        self.assertIn(first4["subject_ids_sha256"][:12], messages[0])

        resampled = self._run("d", "--subjects", "4", "--supersample", "3")
        self.assertEqual(export.check_poolable([first4, resampled]), ["different resampling settings (supersample 2, binarized lesions, supersample 3, binarized lesions)"])

        # Editing connectome.json changes the manifest: reported, with a note that the data are the same
        connectome.set_metadata(self.meta["path"], preprocessing={"gsr": False}, source={"version": "2.1"})
        edited = self._run("e", "--subjects", "4")
        messages = export.check_poolable([first4, edited])
        self.assertEqual(len(messages), 1)
        self.assertIn("different connectome manifest", messages[0])
        self.assertIn("only connectome.json metadata differ", messages[0])
        banners = export.compare_runs(first4, edited)
        self.assertIn("different GSR (not recorded vs no)", banners)
        self.assertIn("different dataset version (not recorded vs 2.1)", banners)
        self.assertIn("different N (4 vs 6)", export.compare_runs(first4, self._run("f")))
        # Old run records (no supersample/probabilistic keys) fall back to their argv
        old = {key: value for key, value in first4.items() if key not in ("supersample", "probabilistic")}
        self.assertEqual(export.compatibility_key(old), key)
        old["argv"] = [*old["argv"], "--probabilistic"]
        self.assertIn("probabilistic lesions", export.check_poolable([first4, old])[0])
        self.assertEqual(export.check_poolable([first4]), [])

    def _stopped_run(self, name, *args):
        """A run stopped after its first snapshot (2 subjects), and its partial export's provenance.json."""
        compute = engine.compute_network_maps

        def stop_after_snapshot(*a, **k):
            snapshot = k["snapshot"]

            def snapshot_then_stop(*sa, **sk):
                result = snapshot(*sa, **sk)
                jobs.request_cancel("test stop")
                return result

            return compute(*a, **{**k, "snapshot": snapshot_then_stop})

        engine.compute_network_maps = stop_after_snapshot
        try:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(jobs.Cancelled):
                engine.main(["--bundle", self.meta["path"], "--lesion", self.lesion, "--out", self.path(name), "--run-json", self.path(name, "run.json"), "--snapshot-every", "2", *args])
        finally:
            jobs.reset_cancel()
            engine.compute_network_maps = compute
        with open(self.path(name, "run.json"), encoding="utf-8") as f:
            run = json.load(f)
        with open(export.export_run(self.path(name), self.path(name + "_export"), partial=True)["provenance"], encoding="utf-8") as f:
            return run, json.load(f)

    def test_partial_and_stopped_runs_are_never_poolable(self):
        ids = connectome.ConnectomeBundle.open(self.meta["path"]).subject_ids
        run_a, provenance_a = self._stopped_run("a", "--subject-list", ",".join(ids[:5]))
        _, provenance_b = self._stopped_run("b", "--subject-list", ",".join(ids[1:6]))
        self.assertEqual((provenance_a["N"], provenance_b["N"]), (2, 2))
        # Two partial exports on different lists: their subjects are unknown, so they are refused
        messages = export.check_poolable([provenance_a, provenance_b])
        self.assertIn("run did not finish (run 1: partial, N=2; run 2: partial, N=2)", messages)
        self.assertIn("subject list not recorded (run 1, run 2: partial, stopped or older run)", messages)
        self.assertEqual(export.compare_runs(provenance_a, provenance_b)[:2], messages[:2])
        # A stopped run.json and a finished run on the same list
        finished = self._run("c", "--subject-list", ",".join(ids[:5]))
        self.assertEqual(export.check_poolable([run_a, finished]), ["run did not finish (run 1: status running, N=2)"])
        self.assertEqual(export.check_poolable([finished, provenance_a])[0], "run did not finish (run 2: partial, N=2)")
        # The partial export does not claim the requested list's hash as the identity of its subjects
        self.assertIsNone(provenance_a["subject_list"]["sha256"])
        self.assertEqual(provenance_a["subject_list"]["requested_sha256"], export.subjects_sha256(ids[:5]))
        with open(os.path.join(self.path("a_export"), "methods.md"), encoding="utf-8") as f:
            self.assertIn("requested list sha256", f.read())
        sidecar = next(name for name in os.listdir(self.path("a_export")) if name.endswith(".json") and name != "provenance.json")
        with open(os.path.join(self.path("a_export"), sidecar), encoding="utf-8") as f:
            self.assertIsNone(json.load(f)["connectome"]["subject_list_sha256"])


class EngineStopTest(_TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.meta = connectome.make_synthetic(self.path("synthetic.lnmc"), n_subjects=4, name="Synthetic")
        self.lesion = _sphere_lesion(self.path("sub-01_lesion.nii.gz"), self.meta["blobs"]["A"]["center_world"])

    def test_cancellation_request_stops_the_engine(self):
        run_json = self.path("out", "run.json")
        output = io.StringIO()
        jobs.request_cancel("test stop")
        try:
            with contextlib.redirect_stdout(output), self.assertRaises(jobs.Cancelled) as context:
                engine.main(["--bundle", self.meta["path"], "--lesion", self.lesion, "--out", self.path("out"), "--run-json", run_json])
        finally:
            jobs.reset_cancel()
        self.assertEqual(context.exception.reason, "test stop")
        with open(run_json, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["status"], "running")  # a stopped run never claims to be done
        self.assertNotIn('"done"', output.getvalue())

    @unittest.skipUnless(hasattr(os, "mkfifo") and sys.platform != "win32", "needs POSIX named pipes")
    def test_orphaned_engine_stops(self):
        """A terminated launcher (here an intermediate process killed with SIGKILL) never leaves an engine running."""
        fifo = self.path("blocking_lesion.nii.gz")  # gzip streams only seek forward, which a pipe allows
        os.mkfifo(fifo)
        with open(self.lesion, "rb") as f:
            data = f.read()
        opened = threading.Event()
        release = threading.Event()
        stop = threading.Event()

        def feed():
            # The engine opens the lesion file several times (data, header); the first read blocks until released
            while not stop.is_set():
                with contextlib.suppress(BrokenPipeError), open(fifo, "wb") as pipe:  # the reader may stop after the header
                    if not opened.is_set():
                        opened.set()
                        release.wait(30)
                    pipe.write(data)

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        launcher = [sys.executable, "-c", "import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))"]
        command = [sys.executable, "-m", "LesionNetworkMappingLib.engine", "--bundle", self.meta["path"], "--lesion", fifo, "--lesion", self.lesion, "--out", self.path("out"), "--threads", "1"]
        process = subprocess.Popen(launcher + command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env(), cwd=self.tmp)
        try:
            self.assertTrue(opened.wait(60), "the engine did not start reading the lesion")
            process.kill()  # the launcher is gone; the engine is re-parented
            process.wait(10)
            time.sleep(2.5)  # the orphan watchdog polls every second
        finally:
            release.set()
        try:
            stdout, _ = process.communicate(timeout=120)  # EOF once the engine has exited
        finally:
            stop.set()
            with contextlib.suppress(OSError):
                os.close(os.open(fifo, os.O_RDONLY | os.O_NONBLOCK))  # unblocks a feeder waiting for a reader
            feeder.join(10)
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual([event["event"] for event in events], ["cancelled"], stdout)
        self.assertIn("launcher process", events[0]["reason"])
        self.assertFalse(os.path.exists(self.path("out", "sub-01_lesion_conn-Synthetic_desc-T_funcmap.nii.gz")))


if __name__ == "__main__":
    unittest.main()

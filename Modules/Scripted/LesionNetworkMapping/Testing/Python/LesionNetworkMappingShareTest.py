"""Tests of the share-safe study export and the NeuroVault-ready folder (share.py).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingShareTest.py -v
"""

import gzip
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import export, nifti, share
from LesionNetworkMappingLib.study import Study

SPACE = "MNI152NLin6Asym"
AFFINE = np.array([[-2.0, 0, 0, 90], [0, 2.0, 0, -126], [0, 0, 2.0, -72], [0, 0, 0, 1]])
SHAPE = (12, 14, 10)
GSP_BUNDLE = {"name": "GSP1000", "licence": export.GSP_LICENCE, "doi": "10.7910/DVN/ILXIKS", "dataset_version": "3.0", "space": "MNI152NLin6Asym"}
PRIVATE_TEXT = "John Smith 2024-01-02"


def _write_with_extension(path, data, descrip=PRIVATE_TEXT, aux_file="patient.dcm"):
    """A NIfTI-1 file with descrip, aux_file and db_name set and one header extension (as a DICOM converter writes it)."""
    nifti.write_nifti(path, data, AFFINE, descrip=descrip)
    with gzip.open(path, "rb") as f:
        raw = bytearray(f.read())
    raw[228:252] = aux_file.encode("latin-1").ljust(24, b"\0")[:24]
    raw[14:32] = b"Smith^John".ljust(18, b"\0")
    payload = bytes(raw[352:])
    extension = b"PatientName=Smith^John".ljust(32 - 8, b"\0")
    block = struct.pack("<ii", len(extension) + 8, 2) + extension
    raw = raw[:348] + b"\x01\0\0\0" + block + payload
    raw[108:112] = struct.pack("<f", 352.0 + len(block))
    with gzip.open(path, "wb") as f:
        f.write(bytes(raw))
    return path


def _sphere(center, radius=2.5):
    ijk = np.indices(SHAPE).reshape(3, -1).T
    return (np.linalg.norm(ijk - np.asarray(center), axis=1) <= radius).reshape(SHAPE).astype(np.uint8)


class ShareTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm_share_")
        self.root = os.path.join(self.tmp, "study")
        study = Study.create(self.root, name="Aphasia lit", space=SPACE)
        os.makedirs(os.path.join(self.root, "lesions"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "scans"), exist_ok=True)
        lesions = {
            "sub-JohnSmith20240102": (4, 5, 5),
            "sub-02": (7, 7, 4),
            "sub-03": (5, 9, 6),
            "sub-04": (6, 6, 6),
            "sub-05": (8, 4, 5),
        }
        for sid, center in lesions.items():
            _write_with_extension(os.path.join(self.root, "lesions", f"{sid}_space-{SPACE}_desc-lesion_mask.nii.gz"), _sphere(center))
        nifti.write_nifti(os.path.join(self.root, "scans", "sub-02_T1w.nii.gz"), np.ones(SHAPE, np.int16), AFFINE)
        study.add_subject("sub-JohnSmith20240102", {"group": "case", "status": "traced", "lesion": f"lesions/sub-JohnSmith20240102_space-{SPACE}_desc-lesion_mask.nii.gz",
                                                     "space": SPACE, "source_type": "case_report", "trace_method": "figure", "citation": "Doe 2020",
                                                     "record_id": "R1", "age": 61, "sex": "F", "notes": "seen by Dr Who", "rater": "AJ",
                                                     "modified": "2024-01-02T10:00:00+00:00"})
        study.add_subject("sub-02", {"group": "case", "status": "traced", "lesion": f"lesions/sub-02_space-{SPACE}_desc-lesion_mask.nii.gz", "space": SPACE,
                                     "source_type": "own_scan", "scan": "scans/sub-02_T1w.nii.gz", "age": 70, "sex": "M", "notes": "n/a", "rater": "AJ"})
        study.add_subject("sub-03", {"group": "case", "status": "traced", "lesion": f"lesions/sub-03_space-{SPACE}_desc-lesion_mask.nii.gz", "space": "native",
                                     "source_type": "dataset", "age": 55})
        study.add_subject("sub-04", {"group": "case", "include": "0", "lesion": f"lesions/sub-04_space-{SPACE}_desc-lesion_mask.nii.gz", "space": SPACE,
                                     "source_type": "case_report", "age": 40})
        study.add_subject("sub-05", {"group": "control", "status": "traced", "lesion": f"lesions/sub-05_space-{SPACE}_desc-lesion_mask.nii.gz", "space": SPACE,
                                     "source_type": "dataset", "source_dataset": "ds004884", "age": 50, "sex": "F"})
        study.save()
        study.write_lesion_sidecar("sub-02", studio={"tracer": "Alice", "note": "/home/alice/scans/raw.nii"})
        # Source material that must never leave the study
        figure_folder = os.path.join(self.root, "sources", "R1")
        os.makedirs(figure_folder)
        for name in ("fig-1.png", "paper.pdf", "fig-1_fit.json"):
            with open(os.path.join(figure_folder, name), "wb") as f:
                f.write(b"copyrighted")
        with open(os.path.join(self.root, "sources", "records.tsv"), "w", encoding="utf-8") as f:
            f.write("record_id\tcitation\tdoi\tpdf_path\tscreening_note\nR1\tDoe 2020\t10.1/xyz\t/home/alice/papers/doe.pdf\tasked Dr X\n")
        os.makedirs(os.path.join(self.root, ".studio", "inbox"), exist_ok=True)
        with open(os.path.join(self.root, ".studio", "session.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        self.study = Study.open(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_gsp_maps(self):
        """A GLM run with a T map (n = 20, 3 design columns) and a p map; a single-lesion map run with N = 1000."""
        glm = os.path.join(self.root, "derivatives", "lnm", "glm-a")
        os.makedirs(glm)
        values = np.linspace(-3, 3, int(np.prod(SHAPE)), dtype=np.float32).reshape(SHAPE)
        nifti.write_nifti(os.path.join(glm, "group_conn-GSP1000_desc-caseTstat_map.nii.gz"), values, AFFINE, descrip="glm df=17 /home/alice")
        nifti.write_nifti(os.path.join(glm, "group_conn-GSP1000_desc-casePFwe_map.nii.gz"), np.ones(SHAPE, np.float32), AFFINE)
        with open(os.path.join(glm, "glm.json"), "w", encoding="utf-8") as f:
            json.dump({"n": 20, "contrast": {"columns": ["case", "age", "volume_ml"], "label": "case", "spec": "case > 0"},
                       "connectome": {"bundle": GSP_BUNDLE}}, f)
        run = os.path.join(self.root, "derivatives", "lnm", "conn-GSP1000")
        os.makedirs(run)
        nifti.write_nifti(os.path.join(run, "sub-02_conn-GSP1000_desc-T_funcmap.nii.gz"), values, AFFINE, intent_code=3, intent_params=(999.0, 0.0, 0.0))
        with open(os.path.join(run, "provenance.json"), "w", encoding="utf-8") as f:
            json.dump({"format": export.PROVENANCE_FORMAT, "N": 1000, "terms": export.GSP_LICENCE, "connectome": GSP_BUNDLE}, f)
        return glm, run

    def export(self, name="share", **options):
        out = os.path.join(self.tmp, name)
        defaults = {"licence": "CC0-1.0", "lesions": True, "own_patient": "share"}
        defaults.update(options)
        return share.export_dataset(self.study, out, defaults), out


class ExclusionTest(ShareTestBase):
    def test_exclusions_enforced(self):
        result, out = self.export(aliases=("fmriprep", "lacuna"))
        files = result["files"]
        self.assertTrue(files)
        for relpath in files:
            self.assertFalse(relpath.endswith((".png", ".pdf")), relpath)
            self.assertNotIn("T1w", relpath)
            self.assertNotIn(".studio", relpath)
            self.assertFalse(relpath.startswith("sources/") and relpath != "sources/records.tsv", relpath)
            self.assertNotIn("fit.json", relpath)
            self.assertNotIn("inbox", relpath)
        # The native-space lesion and the excluded row are left out
        self.assertEqual(result["lesions"], 3)
        self.assertIn(("sub-03", "native space (only template-space masks are shared)"), result["skipped"])
        with open(os.path.join(out, "participants.tsv"), encoding="utf-8") as f:
            self.assertEqual(len(f.read().strip().split("\n")), 1 + 4)  # include = 0 is not exported
        # Records: citation columns only
        with open(os.path.join(out, "sources", "records.tsv"), encoding="utf-8") as f:
            records = f.read()
        self.assertEqual(records.split("\n")[0].split("\t"), ["record_id", "doi", "citation"])
        self.assertNotIn("alice", records)
        self.assertNotIn("Dr X", records)
        # Aliases are listed in .bidsignore and the dataset validates
        with open(os.path.join(out, ".bidsignore"), encoding="utf-8") as f:
            ignore = f.read()
        self.assertIn("*_label-lesion_roi.nii.gz", ignore)
        self.assertIn("*_label-lesion_space-*_mask.nii.gz", ignore)
        self.assertEqual(result["validation"], {})
        self.assertTrue(any(path.endswith("_label-lesion_roi.nii.gz") for path in files))
        for item in share.verify_export(out):
            self.assertEqual(item["status"], "ok", item)

    def test_default_columns_leave_out_free_text_names_and_dates(self):
        columns = share.default_columns(self.study)
        for name in ("notes", "rater", "modified", "scan", "lesion"):
            self.assertNotIn(name, columns)
        for name in ("group", "age", "sex", "citation", "source_type"):
            self.assertIn(name, columns)
        offered = {choice["name"]: choice for choice in share.column_choices(self.study)}
        self.assertNotIn("scan", offered)
        self.assertNotIn("lesion", offered)
        self.assertEqual(offered["notes"]["sensitive"], "free text")
        self.assertEqual(offered["rater"]["sensitive"], "tracer name")
        self.assertEqual(offered["modified"]["sensitive"], "date")
        _result, out = self.export()
        with open(os.path.join(out, "participants.tsv"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("Dr Who", text)
        self.assertNotIn("AJ", text.split("\n", 1)[1])
        self.assertNotIn("2024-01-02", text)
        with open(os.path.join(out, "participants.json"), encoding="utf-8") as f:
            dictionary = json.load(f)
        self.assertEqual(set(dictionary), {"participant_id", *share.default_columns(self.study)})
        self.assertNotIn(share.schema.FORK_KEY, json.dumps(dictionary))
        # A sensitive column the user ticks is exported with a warning
        checklist = share.checklist(self.study, {"licence": "CC0-1.0", "columns": ["age", "notes"]})
        item = next(item for item in checklist if item["id"] == "columns")
        self.assertEqual(item["status"], "warn")
        self.assertIn("notes", item["detail"])

    def test_own_patient_masks_need_a_decision(self):
        checklist = share.checklist(self.study, {"licence": "CC0-1.0", "lesions": True})
        item = next(item for item in checklist if item["id"] == "own_patient")
        self.assertEqual(item["status"], "blocker")
        with self.assertRaises(share.ShareError):
            share.export_dataset(self.study, os.path.join(self.tmp, "x"), {"licence": "CC0-1.0", "lesions": True})
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x")))
        result, _out = self.export(own_patient="exclude")
        self.assertEqual(result["lesions"], 2)
        self.assertIn(("sub-02", "own patient: left out"), result["skipped"])
        with open(result["record"], encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(record["own_patient_decision"], "exclude")
        self.assertEqual(record["own_patient_ids"], ["sub-02"])
        # Without lesions there is nothing to decide
        checklist = share.checklist(self.study, {"licence": "CC0-1.0"})
        self.assertNotIn("own_patient", [item["id"] for item in checklist])

    def test_output_inside_private_folder_is_refused(self):
        with self.assertRaises(share.ShareError):
            share.export_dataset(self.study, os.path.join(self.root, ".studio", "share-out"), {"licence": "CC0-1.0"})
        with self.assertRaises(share.ShareError):
            share.export_dataset(self.study, self.root, {"licence": "CC0-1.0"})
        with self.assertRaises(share.ShareError):  # anywhere in the study folder
            share.export_dataset(self.study, os.path.join(self.root, "export"), {"licence": "CC0-1.0"})
        os.makedirs(os.path.join(self.tmp, "busy"))
        with open(os.path.join(self.tmp, "busy", "x.txt"), "w") as f:
            f.write("x")
        with self.assertRaises(share.ShareError):
            share.export_dataset(self.study, os.path.join(self.tmp, "busy"), {"licence": "CC0-1.0"})

    def test_zip_export(self):
        out = os.path.join(self.tmp, "share.zip")
        result = share.export_dataset(self.study, out, {"licence": "CC0-1.0", "lesions": True, "own_patient": "share", "zip": True})
        self.assertEqual(result["path"], out)
        with zipfile.ZipFile(out) as archive:
            names = archive.namelist()
            self.assertIn("dataset_description.json", names)
            self.assertFalse(any(".studio" in name or name.endswith(".png") for name in names))
            self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))
        self.assertFalse([name for name in os.listdir(self.tmp) if ".partial-" in name])


class OfficialValidatorTest(ShareTestBase):
    """The folder names in .bidsignore are bare (bids-validator 3.0.2 does not honour "group/"); a table without folders is ignored."""

    def ignore_lines(self, out):
        with open(os.path.join(out, ".bidsignore"), encoding="utf-8") as f:
            return f.read().split()

    def test_default_export_lists_the_participants_table_in_bidsignore(self):
        result = share.export_dataset(self.study, os.path.join(self.tmp, "default"), {"licence": "CC0-1.0"})
        out = result["path"]
        self.assertEqual(result["lesions"], 0)
        self.assertFalse(any(path.startswith("sub-") for path in result["files"]))
        self.assertTrue(os.path.exists(os.path.join(out, "participants.tsv")))
        self.assertEqual(sorted(self.ignore_lines(out)), ["participants.json", "participants.tsv", "sources"])  # sources/records.tsv is in the fixture
        self.assertEqual(result["validation"], {})
        with open(os.path.join(out, "README.md"), encoding="utf-8") as f:
            self.assertIn("listed in .bidsignore", f.read())
        # Without the .bidsignore entry the validator would report PARTICIPANT_ID_MISMATCH, and Studio's check says so too
        os.remove(os.path.join(out, ".bidsignore"))
        self.assertIn("participants.tsv", share.validate_dataset(out))

    def test_lesions_keep_the_participants_table_visible(self):
        result, out = self.export("lesions")
        self.assertGreater(result["lesions"], 0)
        self.assertEqual(self.ignore_lines(out), ["sources"])  # the table is checked against the sub-<id> folders
        self.assertEqual(result["validation"], {})

    def test_extra_folders_are_listed_without_a_trailing_slash(self):
        self.add_gsp_maps()
        result, out = self.export("maps", maps=True, neurovault=True, terms_acknowledged=True, licence=export.GSP_LICENCE)
        lines = self.ignore_lines(out)
        for folder in ("group", "neurovault", "sources"):
            self.assertIn(folder, lines)
        self.assertFalse([line for line in lines if line.endswith("/")], lines)
        self.assertEqual(result["validation"], {})
        self.assertEqual(share.validate_dataset(out), {})


class ScrubTest(ShareTestBase):
    def test_descrip_aux_file_and_extensions_scrubbed(self):
        source = os.path.join(self.root, "lesions", f"sub-02_space-{SPACE}_desc-lesion_mask.nii.gz")
        before = nifti.read_nifti_header(source)
        self.assertEqual(before["descrip"], PRIVATE_TEXT)
        self.assertEqual(before["aux_file"], "patient.dcm")
        self.assertGreater(before["vox_offset"], 352)
        result, out = self.export()
        copied = [path for path in result["files"] if path.endswith("_desc-lesion_mask.nii.gz")]
        self.assertEqual(len(copied), 3)
        for relpath in copied:
            path = os.path.join(out, relpath)
            header = nifti.read_nifti_header(path)
            self.assertEqual(header["descrip"], "")
            self.assertEqual(header["aux_file"], "")
            self.assertEqual(header["vox_offset"], 352)
            self.assertEqual(bytes(nifti.read_raw_header(path)["db_name"]).strip(b"\0"), b"")
            with gzip.open(path, "rb") as f:
                self.assertNotIn(b"Smith", f.read())
            with open(path, "rb") as f:
                self.assertNotIn(b"sub-", f.read(64))  # no file name in the gzip header
        # Voxels and geometry are unchanged
        original_data, original_affine, _header = nifti.read_nifti(source)
        sums = sorted(int(nifti.read_nifti(os.path.join(out, path))[0].sum()) for path in copied)
        self.assertIn(int(original_data.sum()), sums)
        for path in copied:
            data, affine, _header = nifti.read_nifti(os.path.join(out, path))
            self.assertEqual(data.dtype, original_data.dtype)
            self.assertEqual(data.shape, original_data.shape)
            np.testing.assert_allclose(affine, original_affine)

    def test_no_absolute_paths_in_json_or_tsv(self):
        result, out = self.export()
        self.assertEqual(share.find_absolute_paths(out), [])
        # The sidecar of sub-02 had a local path and a tracer name in its provenance: neither is exported
        sidecars = [path for path in result["files"] if path.endswith("_mask.json")]
        for relpath in sidecars:
            with open(os.path.join(out, relpath), encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("alice", text.lower())
            self.assertNotIn("Alice", text)
        bad = os.path.join(self.tmp, "bad")
        os.makedirs(bad)
        with open(os.path.join(bad, "x.json"), "w", encoding="utf-8") as f:
            json.dump({"Sources": ["C:\\Users\\a\\scan.nii"]}, f)
        with open(os.path.join(bad, "y.tsv"), "w", encoding="utf-8") as f:
            f.write("a\tb\n1\t/home/a/b.nii\n")
        with open(os.path.join(bad, "z.json"), "w", encoding="utf-8") as f:
            json.dump({"url": "https://doi.org/10.1/x", "rel": "../sub-01/anat/x.nii.gz"}, f)
        self.assertEqual(sorted(path for path, _text in share.find_absolute_paths(bad)), ["x.json", "y.tsv"])

    def test_local_path_in_a_column_blocks_the_preview(self):
        self.study.set("sub-05", source_dataset="/home/alice/datasets/ds1")
        self.study.save()
        items = share.checklist(self.study, {"licence": "CC0-1.0", "columns": ["source_dataset", "age"]})
        item = next(item for item in items if item["id"] == "paths")
        self.assertEqual(item["status"], "blocker")
        self.assertIn("source_dataset", item["detail"])
        self.assertNotIn("/home/alice", item["detail"])
        items = share.checklist(self.study, {"licence": "CC0-1.0", "columns": ["age"]})
        self.assertEqual(next(item for item in items if item["id"] == "paths")["status"], "ok")


class PseudonymTest(ShareTestBase):
    def test_pseudonym_mapping_not_exported(self):
        result, out = self.export()
        originals = ["sub-JohnSmith20240102", "sub-02", "sub-05"]
        for relpath in result["files"]:
            for sid in originals:
                self.assertNotIn(sid, relpath)
            with open(os.path.join(out, relpath), "rb") as f:
                content = f.read()
            if relpath.endswith(".nii.gz"):
                content = gzip.decompress(content)
            self.assertNotIn(b"JohnSmith", content, relpath)
        with open(os.path.join(out, "participants.tsv"), encoding="utf-8") as f:
            ids = [line.split("\t")[0] for line in f.read().strip().split("\n")[1:]]
        self.assertEqual(sorted(ids), ids)
        self.assertTrue(all(sid.startswith("sub-P") for sid in ids))
        # The mapping is in the study's private folder only
        self.assertTrue(result["record"].startswith(os.path.join(self.root, ".studio", "share") + os.sep))
        with open(result["record"], encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(sorted(record["pseudonyms"].values()), sorted(["sub-JohnSmith20240102", "sub-02", "sub-03", "sub-05"]))
        self.assertEqual(sorted(record["pseudonyms"]), sorted(ids))
        self.assertFalse(any("pseudonym" in path.lower() or "share" in path.split("/")[0] for path in result["files"]))
        # The same lesion file is under its pseudonym
        new = next(new for new, old in record["pseudonyms"].items() if old == "sub-02")
        self.assertIn(f"{new}/anat/{new}_space-{SPACE}_desc-lesion_mask.nii.gz", result["files"])

    def test_ids_kept_when_not_pseudonymised_are_flagged(self):
        checklist = share.checklist(self.study, {"licence": "CC0-1.0", "pseudonymise": False})
        item = next(item for item in checklist if item["id"] == "ids")
        self.assertEqual(item["status"], "warn")
        self.assertIn("sub-JohnSmith20240102", item["detail"])
        self.assertEqual(share.id_concerns("sub-JohnSmith20240102"), ["date", "name"])
        self.assertEqual(share.id_concerns("sub-02"), [])
        self.assertEqual(share.id_concerns("sub-ARC0001"), [])
        self.assertEqual(share.id_concerns("sub-case12"), [])
        result, _out = self.export(pseudonymise=False)
        self.assertIn("sub-02/anat/sub-02_space-MNI152NLin6Asym_desc-lesion_mask.nii.gz", result["files"])
        with open(result["record"], encoding="utf-8") as f:
            self.assertEqual(json.load(f)["pseudonyms"], {})

    def test_pseudonyms_are_random_and_unique(self):
        sids = [f"sub-{i:02d}" for i in range(30)]
        first = share.pseudonyms(sids)
        self.assertEqual(len(set(first.values())), 30)
        self.assertTrue(all(value.startswith("sub-P0") for value in first.values()))
        self.assertNotEqual([first[sid] for sid in sids], sorted(first.values()))  # 1 in 30! to fail by chance

    def test_pseudonyms_never_reuse_original_ids(self):
        sids = [f"sub-P{i:03d}" for i in range(1, 9)]
        for _attempt in range(10):
            mapping = share.pseudonyms(sids)
            self.assertEqual(len(set(mapping.values())), len(sids))
            self.assertFalse(set(mapping.values()) & set(sids))
        mapping = share.pseudonyms(["P001", "sub-02"])  # bare labels count too
        self.assertFalse({value[4:] for value in mapping.values()} & {"P001"})

    def test_study_with_sub_p_ids_exports_pseudonymised(self):
        root = os.path.join(self.tmp, "pids")
        study = Study.create(root, name="P ids", space=SPACE)
        os.makedirs(os.path.join(root, "lesions"), exist_ok=True)
        sids = [f"sub-P{i:03d}" for i in range(1, 9)]
        for i, sid in enumerate(sids):
            relpath = f"lesions/{sid}_space-{SPACE}_desc-lesion_mask.nii.gz"
            nifti.write_nifti(os.path.join(root, relpath), _sphere((4 + i % 5, 5 + i % 4, 5)), AFFINE)
            study.add_subject(sid, {"group": "case", "status": "traced", "lesion": relpath, "space": SPACE, "source_type": "case_report", "age": 40 + i})
        study.save()
        study = Study.open(root)
        for attempt in range(4):
            out = os.path.join(self.tmp, f"pids-share{attempt}")
            result = share.export_dataset(study, out, {"licence": "CC0-1.0", "lesions": True})
            self.assertEqual(result["validation"], {})
            self.assertEqual(result["lesions"], len(sids))
            with open(result["record"], encoding="utf-8") as f:
                mapping = json.load(f)["pseudonyms"]
            self.assertFalse(set(mapping) & set(sids))
            for relpath in result["files"]:
                for sid in sids:
                    self.assertNotIn(sid, relpath)
                if relpath.endswith((".json", ".tsv", ".md", ".cff")):
                    with open(os.path.join(out, relpath), encoding="utf-8") as f:
                        content = f.read()
                    for sid in sids:
                        self.assertNotIn(sid, content, relpath)


class LicenceTest(ShareTestBase):
    def test_licence_is_required(self):
        with self.assertRaises(share.ShareError) as context:
            share.export_dataset(self.study, os.path.join(self.tmp, "x"), {})
        self.assertIn("licence", [item["id"] for item in share.blockers(context.exception.checklist)])
        self.assertEqual(share.suggested_licence(False), "CC0-1.0")

    def test_no_cc0_default_with_gsp_maps(self):
        self.add_gsp_maps()
        result = share.plan(self.study, {"maps": True})
        self.assertTrue(result["gsp"])
        self.assertIsNone(result["suggested_licence"])
        self.assertEqual(len(result["maps"]), 1)  # the T map; p maps and per-lesion maps are not group exports
        blockers = {item["id"] for item in share.blockers(result["checklist"])}
        self.assertEqual(blockers, {"licence", "gsp_terms"})
        # Choosing a licence is not enough: the GSP terms note must have been read
        with self.assertRaises(share.ShareError):
            share.export_dataset(self.study, os.path.join(self.tmp, "x"), {"maps": True, "licence": "CC0-1.0"})
        out = os.path.join(self.tmp, "gsp")
        result = share.export_dataset(self.study, out, {"maps": True, "licence": export.GSP_LICENCE, "terms_acknowledged": True, "neurovault": True})
        with open(os.path.join(out, "dataset_description.json"), encoding="utf-8") as f:
            description = json.load(f)
        self.assertEqual(description["DatasetType"], "derivative")
        self.assertEqual(description["License"], export.GSP_LICENCE)
        # The connectome of the maps, then the source dataset of an exported row
        self.assertEqual(description["SourceDatasets"], [{"DOI": "doi:10.7910/DVN/ILXIKS", "Version": "3.0"}, {"URL": "https://openneuro.org/datasets/ds004884"}])
        self.assertEqual(description["GeneratedBy"][0]["Name"], "Slicer Studio")
        self.assertTrue(description["GeneratedBy"][0]["CodeURL"].startswith("https://"))
        self.assertEqual(description["HowToAcknowledge"], export.GSP_ACKNOWLEDGEMENT)
        with open(os.path.join(out, "LICENSE"), encoding="utf-8") as f:
            licence = f.read()
        self.assertIn("GSP", licence)
        self.assertIn("Superstruct", licence)
        sidecar = os.path.join(out, "group", "group_conn-GSP1000_desc-caseTstat_map.json")
        with open(sidecar, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["DataUseTerms"], export.GSP_LICENCE)
        self.assertEqual(data["DegreesOfFreedom"], 17)
        self.assertEqual(nifti.read_nifti_header(os.path.join(out, "group", "group_conn-GSP1000_desc-caseTstat_map.nii.gz"))["descrip"], "")
        self.assertEqual(share.find_absolute_paths(out), [])
        self.assertEqual(result["validation"], {})

    def test_cc0_suggested_for_own_masks(self):
        self.add_gsp_maps()
        result = share.plan(self.study, {"lesions": True, "own_patient": "share"})
        self.assertFalse(result["gsp"])  # no maps chosen: only the user's masks
        self.assertEqual(result["suggested_licence"], "CC0-1.0")

    def test_no_cc0_default_with_maps_of_unknown_terms(self):
        run = os.path.join(self.root, "derivatives", "lnm", "glm-b")
        os.makedirs(run)
        nifti.write_nifti(os.path.join(run, "group_desc-caseTstat_map.nii.gz"), np.ones(SHAPE, np.float32), AFFINE)
        result = share.plan(self.study, {"maps": True})
        self.assertFalse(result["gsp"])
        self.assertTrue(result["maps"] and all(entry["terms_unknown"] for entry in result["maps"]))
        self.assertIsNone(result["suggested_licence"])
        item = next(item for item in result["checklist"] if item["id"] == "licence")
        self.assertEqual(item["status"], "blocker")
        self.assertIn("not recorded", item["detail"])

    def test_other_licence_needs_text(self):
        checklist = share.checklist(self.study, {"licence": "other"})
        self.assertIn("licence", [item["id"] for item in share.blockers(checklist)])
        _result, out = self.export(licence="other", licence_text="Ask the authors.")
        with open(os.path.join(out, "LICENSE"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "Ask the authors.")


class SidecarTest(ShareTestBase):
    def test_sidecars_valid(self):
        result, out = self.export()
        sidecars = [path for path in result["files"] if path.endswith("_desc-lesion_mask.json")]
        self.assertEqual(len(sidecars), 3)
        for relpath in sidecars:
            with open(os.path.join(out, relpath), encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["Type"], "Lesion")
            self.assertEqual(data["Resolution"], "2 mm")
            self.assertEqual(data[share.schema.FORK_KEY], {"space": SPACE})
            self.assertNotIn("Sources", data)
            self.assertNotIn("SpatialReference", data)
            self.assertTrue(os.path.isfile(os.path.join(out, relpath[: -len(".json")] + ".nii.gz")))
        with open(os.path.join(out, "dataset_description.json"), encoding="utf-8") as f:
            description = json.load(f)
        self.assertEqual(description["License"], "CC0-1.0")
        self.assertEqual(description["SourceDatasets"], [{"URL": "https://openneuro.org/datasets/ds004884"}])
        self.assertEqual(description["BIDSVersion"], share.BIDS_VERSION)
        with open(os.path.join(out, "CITATION.cff"), encoding="utf-8") as f:
            citation = f.read()
        self.assertIn("cff-version: 1.2.0", citation)
        self.assertIn('license: "CC0-1.0"', citation)
        for name in ("README.md", "CHANGES", "LICENSE"):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)


class ValidatorTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(share.validate_name("sub-P001/anat/sub-P001_space-MNI152NLin6Asym_desc-lesion_mask.nii.gz"), [])
        self.assertEqual(share.validate_name("sub-P001/anat/sub-P001_space-MNI152NLin6Asym_desc-lesion_mask.json"), [])
        self.assertEqual(share.validate_name("sub-P001/anat/sub-P001_label-lesion_roi.nii.gz"), [])
        self.assertEqual(share.validate_name("participants.tsv"), [])
        self.assertTrue(share.validate_name("sub-P001/anat/sub-P001_label-lesion_space-MNI152NLin6Asym_mask.nii.gz"))  # lacuna order
        self.assertTrue(share.validate_name("sub-P001/anat/sub-P002_desc-lesion_mask.nii.gz"))  # another subject's file
        self.assertTrue(share.validate_name("sub-P001/sub-P001_desc-lesion_mask.nii.gz"))  # no datatype folder
        self.assertTrue(share.validate_name("sub-P001/anat/sub-P001_desc-les_ion_mask.nii.gz"))
        self.assertTrue(share.validate_name("sub-P001/anat/sub-P001_foo-bar_mask.nii.gz"))  # unknown entity
        self.assertTrue(share.validate_name("sub-P_01/anat/sub-P_01_mask.nii.gz"))
        self.assertTrue(share.validate_name("notes.txt"))

    def test_bidsignore(self):
        tmp = tempfile.mkdtemp(prefix="lnm_share_v_")
        try:
            os.makedirs(os.path.join(tmp, "group"))
            open(os.path.join(tmp, "group", "anything.nii.gz"), "wb").close()
            open(os.path.join(tmp, "stray.txt"), "wb").close()
            self.assertEqual(set(share.validate_dataset(tmp)), {"group/anything.nii.gz", "stray.txt"})
            # bids-validator 3.0.2 does not honour a trailing slash ("group/" gives NOT_INCLUDED): Studio's check does not either
            with open(os.path.join(tmp, ".bidsignore"), "w", encoding="utf-8") as f:
                f.write("group/\n")
            self.assertEqual(set(share.validate_dataset(tmp)), {"group/anything.nii.gz", "stray.txt"})
            with open(os.path.join(tmp, ".bidsignore"), "w", encoding="utf-8") as f:
                f.write("group\n")
            self.assertEqual(set(share.validate_dataset(tmp)), {"stray.txt"})
        finally:
            shutil.rmtree(tmp)

    def test_participants_without_subject_folders(self):
        tmp = tempfile.mkdtemp(prefix="lnm_share_v_")
        try:
            with open(os.path.join(tmp, "participants.tsv"), "w", encoding="utf-8") as f:
                f.write("participant_id\tgroup\nsub-P001\tcase\n")
            self.assertEqual(list(share.validate_dataset(tmp)), ["participants.tsv"])  # the official validator: PARTICIPANT_ID_MISMATCH
            os.makedirs(os.path.join(tmp, "sub-P002", "anat"))
            open(os.path.join(tmp, "sub-P002", "anat", "sub-P002_space-MNI152NLin2009cAsym_desc-lesion_mask.nii.gz"), "wb").close()
            self.assertEqual(share.validate_dataset(tmp), {})  # a folder for only some of the participants is accepted
            shutil.rmtree(os.path.join(tmp, "sub-P002"))
            with open(os.path.join(tmp, ".bidsignore"), "w", encoding="utf-8") as f:
                f.write("participants.tsv\n")
            self.assertEqual(share.validate_dataset(tmp), {})
        finally:
            shutil.rmtree(tmp)


class NeuroVaultTest(ShareTestBase):
    def test_neurovault_folder_df(self):
        _glm, run = self.add_gsp_maps()
        out = os.path.join(self.tmp, "nv")
        folder = share.neurovault_folder(self.study, out)
        self.assertTrue(folder["gsp"])
        self.assertEqual([image["file"] for image in folder["images"]], ["group_conn-GSP1000_desc-caseTstat_map.nii.gz"])
        image = folder["images"][0]
        self.assertEqual(image["map_type"], "T")
        self.assertEqual(image["statistic_parameters"], 17)  # n - p of the GLM
        self.assertEqual(image["number_of_subjects"], 20)
        self.assertFalse(image["is_thresholded"])
        self.assertEqual(image["data_use_terms"], export.GSP_LICENCE)
        self.assertEqual(image["acknowledgement"], export.GSP_ACKNOWLEDGEMENT)
        self.assertIn("Positive values", image["sign_coding"])
        with open(os.path.join(out, "images.csv"), encoding="utf-8") as f:
            lines = f.read().strip().split("\n")
        self.assertEqual(lines[0].split(",")[:3], ["file", "name", "map_type"])
        self.assertEqual(len(lines), 2)
        with open(os.path.join(out, "README.md"), encoding="utf-8") as f:
            self.assertIn("Nothing was uploaded", f.read())
        self.assertEqual(share.find_absolute_paths(out), [])
        # A single-lesion run folder: the per-lesion T map, df = connectome N - 1, renamed with the pseudonym
        out2 = os.path.join(self.tmp, "nv2")
        folder = share.neurovault_folder(run, out2, id_map={"sub-02": "sub-P007"})
        self.assertEqual([image["file"] for image in folder["images"]], ["sub-P007_conn-GSP1000_desc-T_funcmap.nii.gz"])
        self.assertEqual(folder["images"][0]["statistic_parameters"], 999)
        self.assertEqual(folder["images"][0]["number_of_subjects"], 1)
        self.assertNotIn("sub-02", json.dumps(folder["images"]))

    def test_never_uploads(self):
        with open(share.__file__, encoding="utf-8") as f:
            source = f.read()
        for module in ("urllib", "http.client", "requests", "socket", "ssl"):
            self.assertNotIn(f"import {module}", source)
            self.assertNotIn(f"from {module}", source)


class UserTemplateTest(unittest.TestCase):
    """A study drawn on a template the user added (Slicer Studio A-10): the export names the template (id, name, space,
    sha256) and includes its file only with ``include_template`` (off by default), byte for byte so its checksum matches.
    """

    def setUp(self):
        import hashlib

        self.tmp = tempfile.mkdtemp(prefix="lnm_share_tpl_")
        templates = os.path.join(self.tmp, "Templates")
        folder = os.path.join(templates, "Lab ICBM 1.5")
        os.makedirs(folder)
        self.template_image = os.path.join(folder, "lab_icbm_T1w.nii.gz")
        nifti.write_nifti(self.template_image, np.arange(int(np.prod(SHAPE)), dtype=np.int16).reshape(SHAPE), AFFINE, descrip="ICBM lab template")
        with open(self.template_image, "rb") as f:
            self.sha = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(folder, "template.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": "slicer-studio-template", "id": "lab-icbm-1-5", "name": "Lab ICBM 1.5 mm", "space": "MNI (other)",
                       "files": {"image": "lab_icbm_T1w.nii.gz"}, "sha256": {"image": self.sha}}, f)
        self.templates = templates
        self.root = os.path.join(self.tmp, "study")
        template = {"id": "MNI (other)", "source": "user:lab-icbm-1-5", "name": "Lab ICBM 1.5 mm", "space": "MNI (other)", "sha256": self.sha}
        study = Study.create(self.root, name="Template study", template=template, space=SPACE)
        nifti.write_nifti(os.path.join(self.root, "lesions", f"sub-01_space-{SPACE}_desc-lesion_mask.nii.gz"), _sphere((5, 6, 5)), AFFINE)
        study.add_subject("sub-01", {"group": "case", "status": "traced", "lesion": f"lesions/sub-01_space-{SPACE}_desc-lesion_mask.nii.gz",
                                     "space": SPACE, "source_type": "case_report"})
        study.save()
        self.study = Study.open(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def export(self, name, **options):
        return share.export_dataset(self.study, os.path.join(self.tmp, name), {"licence": "CC0-1.0", "lesions": True, **options})

    def test_template_identity_in_metadata_without_the_file(self):
        self.assertIn("include_template", share.DEFAULT_OPTIONS)
        self.assertFalse(share.DEFAULT_OPTIONS["include_template"])  # off by default
        result = self.export("share", template_dirs=[self.templates])
        out = result["path"]
        self.assertIsNone(result["template"])
        self.assertFalse(any(relpath.startswith("template/") for relpath in result["files"]), result["files"])
        with open(os.path.join(out, "dataset_description.json"), encoding="utf-8") as f:
            description = json.load(f)
        self.assertEqual(description["SlicerStudio"]["template"],
                         {"id": "user:lab-icbm-1-5", "name": "Lab ICBM 1.5 mm", "space": "MNI (other)", "sha256": self.sha})
        with open(os.path.join(out, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        for text in ("Lab ICBM 1.5 mm", "user:lab-icbm-1-5", self.sha, "MNI (other)"):
            self.assertIn(text, readme)
        self.assertEqual(share.find_absolute_paths(out), [])

    def test_include_template_file_copies_it_unchanged(self):
        import hashlib

        result = self.export("share", include_template=True, template_dirs=[self.templates])
        out = result["path"]
        self.assertEqual(result["template"], "template/tpl-LabIcbm15.nii.gz")
        self.assertIn(result["template"], result["files"])
        with open(os.path.join(out, *result["template"].split("/")), "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), self.sha)  # byte for byte: a recipient adds it by checksum
        with open(os.path.join(out, "dataset_description.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["SlicerStudio"]["template"]["file"], result["template"])
        with open(os.path.join(out, ".bidsignore"), encoding="utf-8") as f:
            self.assertIn("template", f.read().split())
        self.assertEqual(share.validate_dataset(out), {})
        statuses = {item["id"]: item["status"] for item in result["checklist"]}
        self.assertEqual(statuses["template"], "warn")  # the licence note
        self.assertTrue(all(item["status"] == "ok" for item in result["checklist"] if item["id"] in ("headers", "exclusions", "paths")))

    def test_include_template_needs_the_file(self):
        items = share.checklist(self.study, {"licence": "CC0-1.0", "include_template": True, "template_dirs": [os.path.join(self.tmp, "elsewhere")]})
        self.assertEqual({item["id"]: item["status"] for item in items}["template"], "blocker")
        with self.assertRaises(share.ShareError):
            self.export("share", include_template=True)
        # Another file under the same id is never taken (its checksum differs)
        with open(os.path.join(self.templates, "Lab ICBM 1.5", "template.json"), encoding="utf-8") as f:
            record = json.load(f)
        record["sha256"]["image"] = "0" * 64
        with open(os.path.join(self.templates, "Lab ICBM 1.5", "template.json"), "w", encoding="utf-8") as f:
            json.dump(record, f)
        self.assertIsNone(share.find_template_file(self.study, [self.templates]))

    def test_builtin_template_study_unchanged(self):
        root = os.path.join(self.tmp, "builtin")
        study = Study.create(root, name="Built-in", template={"id": SPACE, "res": 2, "source": "builtin"}, space=SPACE)
        self.assertIsNone(share.template_identity(study))
        result = share.export_dataset(study, os.path.join(self.tmp, "builtin-share"), {"licence": "CC0-1.0", "include_template": True})
        with open(os.path.join(result["path"], "dataset_description.json"), encoding="utf-8") as f:
            self.assertNotIn("SlicerStudio", json.load(f))
        self.assertIsNone(result["template"])


if __name__ == "__main__":
    unittest.main()

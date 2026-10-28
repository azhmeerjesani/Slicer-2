"""Unit tests of the plain-file study model (LesionNetworkMappingLib.study).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingStudyTest.py -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import nifti, study
from LesionNetworkMappingLib.study import Study

MNI2009C_2MM = np.array([[2.0, 0, 0, -96.5], [0, 2, 0, -132.5], [0, 0, 2, -78.5], [0, 0, 0, 1]])
NATIVE = np.array([[-0.9, 0, 0, 80.0], [0, 0.9, 0, -100.0], [0, 0, 1.2, -60.0], [0, 0, 0, 1]])


class StudyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-study-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def _image(self, relative, shape=(4, 5, 6), affine=NATIVE, value=0):
        path = self.path(*relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        nifti.write_nifti(path, np.full(shape, value, np.uint8), affine)
        return path

    def _tsv_lines(self, folder):
        with open(os.path.join(folder, study.PARTICIPANTS_TSV), encoding="utf-8") as f:
            return f.read().split("\n")

    def test_create_open_save_round_trip(self):
        folder = self.path("Aphasia")
        template = {"id": "MNI152NLin2009cAsym", "res": 2, "source": "sampledata:MNI152NLin2009cAsym", "sha256": "7b16"}
        created = Study.create(folder, template=template, software={"slicer": "5.13.0"})
        for name in (study.STUDY_JSON, study.PARTICIPANTS_TSV, "lesions", "derivatives", ".studio/thumbs", ".studio/journal"):
            self.assertTrue(os.path.exists(os.path.join(folder, name)), name)
        self.assertEqual(self._tsv_lines(folder), ["\t".join(study.STANDARD_COLUMNS), ""])
        with open(os.path.join(folder, study.STUDY_JSON), encoding="utf-8") as f:
            metadata = json.load(f)
        self.assertEqual((metadata["format"], metadata["version"], metadata["name"], metadata["space"]), ("slicer-studio-study", 1, "Aphasia", "MNI152NLin2009cAsym"))
        self.assertEqual(metadata["rules"], study.DEFAULT_RULES)
        self.assertEqual(metadata["template"], template)
        with self.assertRaises(FileExistsError):
            Study.create(folder)

        created.add_subject("01", group="case", moca=21, nihss=4.5)
        created.add_subject("sub-02", {"group": "control", "include": False, "notes": "motion\tartefact"})
        created.add_subject("p03", volume_ml=12.3456789, centroid_x=-30.25, gaps=[3, 7], trace_seconds=np.float32(41.0))
        created.metadata["custom"] = {"kept": True}  # unknown study.json keys survive
        created.save()
        self.assertEqual([name for name in os.listdir(folder) if name.endswith(".tmp")], [])

        opened = Study.open(os.path.join(folder, study.STUDY_JSON))
        self.assertEqual(opened.subject_ids, ["sub-01", "sub-02", "sub-p03"])
        self.assertEqual(opened.columns, [*study.STANDARD_COLUMNS, "moca", "nihss"])
        self.assertEqual(opened.subjects, created.subjects)
        self.assertEqual(opened.metadata, created.metadata)
        row = opened.subject("sub-p03")
        self.assertEqual((row["volume_ml"], row["centroid_x"], row["gaps"], row["trace_seconds"], row["moca"]), ("12.3457", "-30.25", "3,7", "41", "n/a"))
        self.assertEqual(opened.subject("sub-02")["notes"], "motion artefact")
        self.assertEqual((opened.value("sub-01", "nihss", float), opened.value("sub-01", "volume_ml", float)), (4.5, None))
        lines = self._tsv_lines(folder)
        self.assertTrue(lines[0].startswith("participant_id\tgroup\tinclude\tstatus\tqc\t"))
        self.assertEqual(lines[1].split("\t")[:5], ["sub-01", "case", "1", "untraced", "unreviewed"])
        self.assertEqual(opened.numeric_columns(), ["moca", "nihss"])
        self.assertTrue(Study.is_study(folder))
        self.assertFalse(Study.is_study(self.tmp))

    def test_unknown_columns_keep_their_order(self):
        folder = self.path("bids")
        os.makedirs(folder)
        with open(os.path.join(folder, study.STUDY_JSON), "w", encoding="utf-8") as f:
            json.dump({"format": "slicer-studio-study", "version": 1, "name": "bids"}, f)
        with open(os.path.join(folder, study.PARTICIPANTS_TSV), "w", encoding="utf-8") as f:
            f.write("age\tparticipant_id\tgroup\tsex\tscore_b\tscore_a\n61\tsub-01\tcase\tF\t\t3\n58\tsub-02\tcontrol\tM\t7\tn/a\n")
        opened = Study.open(folder)
        expected_columns = ["participant_id", "age", "group", "sex", "score_b", "score_a", *[c for c in study.STANDARD_COLUMNS if c not in ("participant_id", "group")]]
        self.assertEqual(opened.columns, expected_columns)
        self.assertEqual(opened.subject("sub-01")["score_b"], "n/a")  # empty cell
        self.assertEqual(opened.subject("sub-02")["status"], "untraced")  # missing standard column: default
        opened.set("sub-01", qc="pass", score_c=1.5)
        opened.save()
        again = Study.open(folder)
        self.assertEqual(again.columns, [*expected_columns, "score_c"])
        self.assertEqual(again.subjects, opened.subjects)
        self.assertEqual(self._tsv_lines(folder)[0].split("\t")[:6], ["participant_id", "age", "group", "sex", "score_b", "score_a"])
        self.assertEqual(again.subject("sub-02")["score_c"], "n/a")

    def test_include_written_by_pandas(self):
        # pandas writes an integer column with an empty cell as floats (1.0 / 0.0 / empty): every reader must see
        # include = 0 as excluded and 1.0 as included, and the next save writes the values as Studio does
        folder = self.path("pandas")
        created = Study.create(folder)
        for number in range(1, 5):
            created.add_subject(f"sub-0{number}", status="traced", lesion=f"lesions/sub-0{number}_lesion.nii.gz")
        created.save()
        lines = self._tsv_lines(folder)
        header = lines[0].split("\t")
        column = {name: index for index, name in enumerate(header)}
        cells = [line.split("\t") for line in lines[1:] if line]
        for row, include in zip(cells, ("1.0", "0.0", "1.0", ""), strict=True):
            row[column["include"]] = include
        cells[1][column["status"]] = "Traced"  # another case of an allowed value
        cells[3][column["status"]] = cells[3][column["qc"]] = ""  # a row appended without these columns
        with open(os.path.join(folder, study.PARTICIPANTS_TSV), "w", encoding="utf-8") as f:
            f.write("\n".join(["\t".join(header)] + ["\t".join(row) for row in cells]) + "\n")

        opened = Study.open(folder)
        rows, excluded = opened.analysis_rows()
        self.assertEqual(([row["participant_id"] for row in rows], excluded), (["sub-01", "sub-03", "sub-04"], {study.EXCLUDED_INCLUDE: ["sub-02"]}))
        self.assertEqual([sid for sid, _path in opened.lesion_paths(with_ids=True)], ["sub-01", "sub-03", "sub-04"])
        self.assertEqual([row["include"] for row in opened.subjects], ["1", "0", "1", "1"])
        self.assertEqual([(row["status"], row["qc"]) for row in opened.subjects][1:], [("traced", "unreviewed"), ("traced", "unreviewed"), ("untraced", "unreviewed")])
        self.assertEqual([study.is_included(row) for row in opened.subjects], [True, False, True, True])
        # The next save repairs the file
        opened.save()
        saved = [line.split("\t") for line in self._tsv_lines(folder)[1:] if line]
        self.assertEqual([(row[column["include"]], row[column["status"]], row[column["qc"]]) for row in saved],
                         [("1", "traced", "unreviewed"), ("0", "traced", "unreviewed"), ("1", "traced", "unreviewed"), ("1", "untraced", "unreviewed")])
        # A float written on disk while the study is open is merged as the same value
        saved[2][column["include"]] = "0.0"
        with open(os.path.join(folder, study.PARTICIPANTS_TSV), "w", encoding="utf-8") as f:
            f.write("\n".join(["\t".join(header)] + ["\t".join(row) for row in saved]) + "\n")
        opened.set("sub-04", notes="kept")
        opened.save()
        self.assertEqual([row["include"] for row in opened.subjects], ["1", "0", "0", "1"])
        self.assertEqual([row["include"] for row in Study.open(folder).subjects], ["1", "0", "0", "1"])
        self.assertEqual([study.include_value(v) for v in ("1", "0", "1.0", "0.0", " 1.00 ", "-0.0", "True", "no", "N", "2", "n/a", "", None)],
                         ["1", "0", "1", "0", "1", "0", "1", "0", "0", None, None, None, None])

    def test_validation(self):
        created = Study.create(self.path("v"))
        created.add_subject("sub-01")
        for column, value in (("group", "patient"), ("include", 2), ("status", "done"), ("qc", "ok")):
            with self.subTest(column=column), self.assertRaises(ValueError):
                created.set("sub-01", {"group": "case", column: value})
        self.assertEqual(created.subject("sub-01")["group"], "n/a")  # nothing changed by a rejected update
        with self.assertRaises(ValueError):
            created.add_subject("sub-01")
        with self.assertRaises(ValueError):
            created.add_subject("sub-03", group="patient")
        self.assertNotIn("sub-03", created)
        with self.assertRaises(ValueError):
            created.set("sub-01", participant_id="sub-09")
        with self.assertRaises(KeyError):
            created.set("sub-02", group="case")
        with self.assertRaises(ValueError):
            study.participant_label("__")
        self.assertEqual(study.participant_label("P 03-b"), "sub-P03b")
        folder = self.path("dup")
        Study.create(folder)
        with open(os.path.join(folder, study.PARTICIPANTS_TSV), "w", encoding="utf-8") as f:
            f.write("participant_id\nsub-01\nsub-01\n")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            Study.open(folder)
        # Values beyond the header would be lost on the next save: an error naming the line; empty extra fields are fine
        with open(os.path.join(folder, study.PARTICIPANTS_TSV), "w", encoding="utf-8") as f:
            f.write("participant_id\tgroup\nsub-01\tcase\t\t\n\nsub-02\tcontrol\t5\n")
        with self.assertRaisesRegex(ValueError, "line 4: 3 fields but only 2 columns"):
            Study.open(folder)
        with open(os.path.join(folder, study.PARTICIPANTS_TSV), "w", encoding="utf-8") as f:
            f.write("participant_id\tgroup\nsub-01\tcase\t\t\n")
        self.assertEqual(Study.open(folder).subject("sub-01")["group"], "case")
        with open(os.path.join(folder, study.STUDY_JSON), "w", encoding="utf-8") as f:
            json.dump({"format": "slicer-studio-study", "version": 2}, f)
        with self.assertRaisesRegex(ValueError, "newer"):
            Study.open(folder)

    def test_tsv_import_export(self):
        created = Study.create(self.path("s"))
        created.add_subject("sub-01", group="case")
        created.add_subject("sub-02")
        exported = created.export_tsv(self.path("export.tsv"))
        with open(exported, encoding="utf-8") as f:
            with open(os.path.join(created.path, study.PARTICIPANTS_TSV), encoding="utf-8") as g:
                self.assertEqual(f.read(), "\t".join(created.columns) + "\nsub-01\tcase\t1\tuntraced\tunreviewed" + "\tn/a" * 14 + "\nsub-02\tn/a\t1\tuntraced\tunreviewed" + "\tn/a" * 14 + "\n")
                self.assertEqual(g.read(), "\t".join(created.columns) + "\n")  # not saved yet
        with open(self.path("scores.tsv"), "w", encoding="utf-8") as f:
            f.write("participant_id\tnihss\tgroup\n02\t7\tcontrol\nsub-01\t3\tn/a\nsub-05\t12\tcase\n")
        report = created.import_tsv(self.path("scores.tsv"))
        self.assertEqual(report, {"added": ["sub-05"], "updated": ["sub-02", "sub-01"], "columns": ["nihss"]})
        self.assertEqual([(row["participant_id"], row["group"], row["nihss"]) for row in created.subjects], [("sub-01", "case", "3"), ("sub-02", "control", "7"), ("sub-05", "case", "12")])
        with open(self.path("bad.tsv"), "w", encoding="utf-8") as f:
            f.write("participant_id\tgroup\nsub-01\tcontrol\nsub-02\tpatient\n")
        with self.assertRaises(ValueError):
            created.import_tsv(self.path("bad.tsv"))
        self.assertEqual(created.subject("sub-01")["group"], "case")  # validated before anything changed
        created.save()
        self.assertEqual(Study.open(created.path).subjects, created.subjects)

    def test_add_files_pairs_scans_and_lesions(self):
        folder = self.path("study")
        created = Study.create(folder)
        # BIDS raw data with a lesion mask, a brain mask and a 4D image; non-BIDS files; header-only reading
        self._image("bids/sub-01/anat/sub-01_T1w.nii.gz", shape=(97, 115, 97), affine=MNI2009C_2MM)
        self._image("bids/sub-01/anat/sub-01_label-lesion_roi.nii.gz", shape=(97, 115, 97), affine=MNI2009C_2MM, value=1)
        self._image("bids/sub-02/ses-1/anat/sub-02_ses-1_T1w.nii.gz")
        self._image("bids/sub-02/ses-1/anat/sub-02_ses-1_desc-brain_mask.nii.gz")
        self._image("bids/sub-02/ses-1/anat/sub-02_ses-1_FLAIR.nii.gz")
        nifti.write_nifti(self.path("bids", "sub-02", "func_bold.nii.gz"), np.zeros((2, 2, 2, 3), np.float32), NATIVE)
        self._image("bids/.git/sub-09_T1w.nii.gz")
        self._image("other/patient03_T1.nii.gz")
        lesion = self._image("other/patient03_lesion.nii")
        with open(self.path("other", "notes.txt"), "w", encoding="utf-8") as f:
            f.write("not an image")
        with open(lesion, "r+b") as f:  # truncate the voxel data: headers are enough
            f.truncate(400)
        report = created.add_files([self.path("bids"), self.path("other")])
        self.assertEqual(report["added"], ["sub-01", "sub-02", "sub-patient03"])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["pairs"]["sub-01"], {"scan": self.path("bids/sub-01/anat/sub-01_T1w.nii.gz"), "lesion": self.path("bids/sub-01/anat/sub-01_label-lesion_roi.nii.gz")})
        self.assertEqual(report["pairs"]["sub-02"]["lesion"], None)
        self.assertEqual(report["pairs"]["sub-02"]["scan"], self.path("bids/sub-02/ses-1/anat/sub-02_ses-1_T1w.nii.gz"))
        skipped = {os.path.basename(item["path"]): item["reason"] for item in report["skipped"]}
        self.assertEqual(skipped["sub-02_ses-1_desc-brain_mask.nii.gz"], "brain mask, deface mask or anatomical segmentation")
        self.assertIn("another scan", skipped["sub-02_ses-1_FLAIR.nii.gz"])
        self.assertIn("not a 3D image", skipped["func_bold.nii.gz"])
        self.assertNotIn("sub-09_T1w.nii.gz", skipped)
        self.assertNotIn("sub-09", created)
        row = created.subject("sub-01")
        self.assertEqual((row["scan"], row["lesion"], row["space"], row["status"]), ("../bids/sub-01/anat/sub-01_T1w.nii.gz", "../bids/sub-01/anat/sub-01_label-lesion_roi.nii.gz", "MNI152NLin2009cAsym", "traced"))
        row = created.subject("sub-patient03")
        self.assertEqual((row["scan"], row["lesion"], row["space"], row["status"]), ("../other/patient03_T1.nii.gz", "../other/patient03_lesion.nii", "native", "traced"))
        self.assertEqual(created.subject("sub-02")["status"], "untraced")
        self.assertEqual(created.abspath(created.subject("sub-01")["lesion"]), self.path("bids/sub-01/anat/sub-01_label-lesion_roi.nii.gz"))
        self.assertEqual(Study.detect_space(self.path("bids/sub-01/anat/sub-01_T1w.nii.gz")), "MNI152NLin2009cAsym")

        # Adding a lesion later fills the empty cell only
        new_lesion = self._image("later/sub-02_lesion.nii.gz")
        report = created.add_files(new_lesion)
        self.assertEqual((report["added"], report["updated"]), ([], ["sub-02"]))
        self.assertEqual((created.subject("sub-02")["lesion"], created.subject("sub-02")["status"]), ("../later/sub-02_lesion.nii.gz", "traced"))
        report = created.add_files(self._image("later2/sub-02_lesion.nii.gz"))
        self.assertEqual(report["updated"], [])
        self.assertIn("already has a lesion", report["skipped"][0]["reason"])
        # Files whose sform and qform disagree are added with a warning (Slicer may show them elsewhere)
        disagreeing = self._image("later3/sub-08_T1w.nii")
        record = nifti.read_raw_header(disagreeing)
        srow = record["srow_x"].copy()
        srow[3] += 3.0
        record["srow_x"] = srow
        with open(disagreeing, "r+b") as f:
            f.write(record.tobytes())
        report = created.add_files(disagreeing)
        self.assertEqual(report["added"], ["sub-08"])
        self.assertEqual([item["path"] for item in report["warnings"]], [disagreeing])
        self.assertIn("sform and qform disagree by up to 3 mm", report["warnings"][0]["message"])
        # A custom id rule
        custom = Study.create(self.path("custom"), rules={"id": r"P(\d+)"})
        self._image("custom_data/scan_P7.nii.gz")
        self.assertEqual(custom.add_files(self.path("custom_data"))["added"], ["sub-7"])

    def test_add_files_non_bids_names(self):
        created = Study.create(self.path("study"))
        # Ids with separators must not collapse into one subject ("patient_01" and "patient_02" are two subjects)
        names = {
            "sub-01": ("sub_01_T1.nii.gz", "sub_01_lesion.nii.gz"),
            "sub-02": ("sub_02_T1.nii.gz", "sub_02_lesion.nii.gz"),
            "sub-patient01": ("patient_01_T1.nii.gz", "patient_01_lesion.nii.gz"),
            "sub-patient02": ("patient_02_T1w.nii.gz", "patient_02_lesion_v2.nii.gz"),
            "sub-Patient7": ("Patient 7 T1.nii", "Patient 7 lesion.nii"),
            "sub-case12": ("T1_case_12_2mm.nii.gz", "case_12_FLAIR_lesion.nii.gz"),
            "sub-smith": ("smith_T1.nii", "smith_lesion.nii"),
        }
        for scan, lesion in names.values():
            self._image(f"raw/{scan}")
            self._image(f"raw/{lesion}")
        report = created.add_files(self.path("raw"))
        self.assertEqual(report["skipped"], [])
        self.assertEqual(sorted(report["added"]), sorted(names))
        for sid, (scan, lesion) in names.items():
            self.assertEqual(report["pairs"][sid], {"scan": self.path("raw", scan), "lesion": self.path("raw", lesion)}, sid)

        # BIDS derivatives with a native and a template T1w: the scan in the lesion's space is chosen
        self._image("derivatives/sub-03/anat/sub-03_T1w.nii.gz")
        mni_scan = self._image("derivatives/sub-03/anat/sub-03_space-MNI152NLin2009cAsym_T1w.nii.gz", shape=(97, 115, 97), affine=MNI2009C_2MM)
        self._image("derivatives/sub-03/anat/sub-03_space-MNI152NLin2009cAsym_desc-lesion_mask.nii.gz", shape=(97, 115, 97), affine=MNI2009C_2MM)
        report = created.add_files(self.path("derivatives"))
        self.assertEqual(report["pairs"]["sub-03"]["scan"], mni_scan)
        self.assertEqual(created.subject("sub-03")["space"], "MNI152NLin2009cAsym")
        # Rows are copies: cells change only through set() (validated)
        row = created.subject("sub-03")
        row["group"] = "patient"
        created.subjects[0]["status"] = "bad"
        self.assertEqual((created.subject("sub-03")["group"], created.subjects[0]["status"]), ("n/a", "traced"))

    def test_lesion_rule_matches_whole_words(self):
        created = Study.create(self.path("study"))
        lesions = {
            "sub-031768": "031768_LesionSmooth_stx.nii.gz",  # ATLAS v1.2
            "sub-r001s001": "sub-r001s001_ses-1_space-MNIEx2009aSym_label-L_desc-T1lesion_mask.nii.gz",  # ATLAS R2.0
            "sub-patient01": "patient01-lesion.nii.gz",
            "sub-P01": "P01_lesion1.nii.gz",
            "sub-P02": "P02_ROI.nii.gz",
            "sub-P03": "P03_LesionMask.nii.gz",
            "sub-P04": "P04_lesions.nii.gz",
            "sub-BraTS2021": "BraTS2021_00000_seg.nii.gz",
        }
        # Names that contain a lesion word inside another word are scans: skull-stripped images, surnames
        scans = {"sub-05": "sub-05_T1w_masked.nii.gz", "sub-Segura01": "Segura_01_T1.nii.gz", "sub-SEGURA02": "SEGURA_02_T1.nii.gz", "sub-patient01": "patient01-T1.nii.gz"}
        for name in [*lesions.values(), *scans.values()]:
            self._image(f"data/{name}")
        self._image("data/sub-06/anat/sub-06_T1w.nii.gz")
        self._image("data/sub-06/anat/sub-06_mod-T1w_defacemask.nii.gz")  # BIDS raw data
        self._image("data/sub-06/anat/sub-06_wmparc.nii.gz")
        report = created.add_files(self.path("data"))
        for sid, name in lesions.items():
            self.assertEqual(report["pairs"][sid]["lesion"], self.path("data", name), sid)
        for sid, name in scans.items():
            self.assertEqual(report["pairs"][sid]["scan"], self.path("data", name), sid)
        self.assertEqual({sid: created.subject(sid)["status"] for sid in ("sub-05", "sub-06", "sub-Segura01", "sub-SEGURA02")}, dict.fromkeys(("sub-05", "sub-06", "sub-Segura01", "sub-SEGURA02"), "untraced"))
        self.assertEqual(report["pairs"]["sub-06"], {"scan": self.path("data/sub-06/anat/sub-06_T1w.nii.gz"), "lesion": None})
        skipped = {os.path.basename(item["path"]): item["reason"] for item in report["skipped"]}
        self.assertEqual(skipped, dict.fromkeys(("sub-06_mod-T1w_defacemask.nii.gz", "sub-06_wmparc.nii.gz"), "brain mask, deface mask or anatomical segmentation"))
        self.assertEqual(sorted(created.lesion_paths()), sorted(self.path("data", name) for name in lesions.values()))
        offered = [created.next_subject()]  # the Draw loop offers the scans for tracing
        while len(offered) < 10 and created.next_subject(offered[-1]) not in offered:
            offered.append(created.next_subject(offered[-1]))
        self.assertEqual(sorted(offered), ["sub-05", "sub-06", "sub-SEGURA02", "sub-Segura01"])

        # Hyphens separate lesion and modality words from the subject id, not names
        self.assertEqual(created.subject("sub-patient01")["scan"], "../data/patient01-T1.nii.gz")
        self._image("names/smith-jones_T1.nii.gz")
        self._image("names/smith-brown_T1.nii.gz")
        self.assertEqual(created.add_files(self.path("names"))["added"], ["sub-smithbrown", "sub-smithjones"])
        # Custom rules are whole words too, and may start with global flags
        custom = Study.create(self.path("custom"), rules={"lesion": "(?i)les"})
        self._image("custom_data/P07_les.nii.gz")
        self._image("custom_data/P07_lesion.nii.gz")
        self.assertEqual(custom.add_files(self.path("custom_data"))["pairs"]["sub-P07"], {"scan": self.path("custom_data", "P07_lesion.nii.gz"), "lesion": self.path("custom_data", "P07_les.nii.gz")})
        self.assertTrue(study.whole_word_rule("lesion").search("Lesion_mask"))
        self.assertFalse(study.whole_word_rule("(?i)mask").search("MASKED"))

    def test_lesion_paths_next_subject_and_names(self):
        created = Study.create(self.path("loop"), space="MNI152NLin2009cAsym")
        for sid, group, include, status in (("sub-01", "case", 1, "traced"), ("sub-02", "control", 1, "untraced"), ("sub-03", "case", 0, "untraced"), ("sub-04", "case", 1, "untraced")):
            created.add_subject(sid, group=group, include=include, status=status)
        lesion = self._image("loop/lesions/sub-01_space-MNI152NLin2009cAsym_desc-lesion_mask.nii.gz")
        created.set("sub-01", lesion=created.lesion_relpath("sub-01"))
        created.set("sub-03", lesion="lesions/sub-03_missing.nii.gz")
        created.set("sub-04", lesion=self.path("elsewhere", "sub-04.nii.gz"), space="native")
        self.assertEqual(created.subject("sub-01")["lesion"], "lesions/sub-01_space-MNI152NLin2009cAsym_desc-lesion_mask.nii.gz")
        self.assertEqual(created.lesion_relpath("sub-04"), "lesions/sub-04_space-native_desc-lesion_mask.nii.gz")
        self.assertEqual(created.lesion_paths(), [lesion, self.path("elsewhere", "sub-04.nii.gz")])
        self.assertEqual(created.lesion_paths(include_only=False, group="case", with_ids=True), [("sub-01", lesion), ("sub-03", self.path("loop", "lesions", "sub-03_missing.nii.gz")), ("sub-04", self.path("elsewhere", "sub-04.nii.gz"))])
        self.assertEqual(created.lesion_paths(group="control"), [])
        self.assertEqual(created.lesion_paths(include_only=False, existing_only=True), [lesion])
        # Done -> next: untraced and included subjects, wrapping around
        self.assertEqual(created.next_subject(), "sub-02")
        self.assertEqual(created.next_subject("sub-02"), "sub-04")
        self.assertEqual(created.next_subject("sub-04"), "sub-02")
        self.assertEqual(created.next_subject("sub-04", include_only=False), "sub-02")
        self.assertEqual(created.next_subject("sub-02", include_only=False), "sub-03")
        self.assertEqual(created.next_subject("sub-01", status=None), "sub-02")
        created.set("sub-02", status="traced")
        created.set("sub-04", status="inprogress")
        self.assertIsNone(created.next_subject("sub-02"))
        self.assertEqual(created.next_subject("sub-02", status="inprogress"), "sub-04")
        created.remove_subject("sub-03")
        self.assertEqual(created.subject_ids, ["sub-01", "sub-02", "sub-04"])
        self.assertEqual(len(created), 3)
        self.assertEqual(study.format_value(float("nan")), "n/a")
        self.assertEqual(study.format_value(1e-9), "1e-09")
        self.assertEqual(study.format_value([]), "none")


if __name__ == "__main__":
    unittest.main()

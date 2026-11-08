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
        # Tables from Excel: empty column names after the last column (trailing tabs) are ignored, values under them are not
        path = os.path.join(folder, "excel.tsv")
        with open(path, "wb") as f:
            f.write(b"participant_id\tgroup\t\t\r\nsub-01\tcase\t\t\r\nsub-02\tcontrol\t\t\r\n")
        self.assertEqual(study.read_tsv(path)[0], ["participant_id", "group"])
        with open(path, "wb") as f:
            f.write(b"participant_id\tgroup\t\r\nsub-01\tcase\tx\r\n")
        with self.assertRaisesRegex(ValueError, "line 2: 3 fields but only 2 columns"):
            study.read_tsv(path)
        with open(path, "wb") as f:
            f.write(b"participant_id\t\tgroup\r\nsub-01\t\tcase\r\n")
        with self.assertRaisesRegex(ValueError, "duplicate or empty column names"):
            study.read_tsv(path)
        # Windows-1252 and UTF-16 (Excel's Text and Unicode Text) are refused with what to do, not a raw codec error
        for encoding in ("cp1252", "utf-16"):
            with open(path, "wb") as f:
                f.write("participant_id\tnotes\r\nsub-01\tcafé Müller\r\n".encode(encoding))
            with self.assertRaises(study.TableError) as raised:
                study.read_tsv(path)
            self.assertIn("Save the table as UTF-8", str(raised.exception))
            self.assertNotIn("codec", str(raised.exception))
            self.assertNotIn(folder, str(raised.exception))
            self.assertTrue(raised.exception.user_problem)
            self.assertIsInstance(raised.exception, ValueError)
        with open(path, "wb") as f:
            f.write("participant_id\tnotes\r\nsub-01\tcafé\r\n".encode("utf-8-sig"))
        self.assertEqual(study.read_tsv(path)[1][0]["notes"], "café")
        with open(os.path.join(folder, study.STUDY_JSON), "w", encoding="utf-8") as f:
            json.dump({"format": "slicer-studio-study", "version": 2}, f)
        with self.assertRaisesRegex(ValueError, "newer"):
            Study.open(folder)

    def test_quoted_values_written_by_pandas(self):
        # BIDS escapes a tab in a value with double quotes; pandas, R and Python's csv module also quote line breaks and
        # quotes. Such a file must open, merge into an open study, and come back intact (it used to make the study unopenable).
        import csv

        def rewrite(folder, notes):
            path = os.path.join(folder, study.PARTICIPANTS_TSV)
            with open(path, encoding="utf-8", newline="") as f:
                cells = list(csv.reader(f, delimiter="\t"))
            if "mRS" not in cells[0]:
                cells[0].append("mRS")
            notes_column, mrs_column = cells[0].index("notes"), cells[0].index("mRS")
            for number, row in enumerate(cells[1:]):
                row += [""] * (len(cells[0]) - len(row))
                row[notes_column], row[mrs_column] = notes.get(row[0], row[notes_column]), str(number)
            with open(path, "w", encoding="utf-8", newline="") as f:
                csv.writer(f, dialect="excel-tab", lineterminator="\n").writerows(cells)
            with open(path, encoding="utf-8") as f:
                return f.read()

        notes = {"sub-01": "left MCA\nsecond line", "sub-02": "tab\there", "sub-03": 'size 2" lesion', "sub-04": '"large" infarct'}
        folder = self.path("quoted")
        created = Study.create(folder)
        for number in range(1, 5):
            created.add_subject(f"sub-0{number}")
        created.save()
        text = rewrite(folder, notes)
        self.assertIn('"tab\there"', text)
        self.assertIn('"left MCA\nsecond line"', text)
        opened = Study.open(folder)
        self.assertEqual(opened.subject_ids, ["sub-01", "sub-02", "sub-03", "sub-04"])
        # tabs and line breaks become one space, as Studio writes notes
        expected = ["left MCA second line", "tab here", 'size 2" lesion', '"large" infarct']
        self.assertEqual([opened.subject(sid)["notes"] for sid in opened.subject_ids], expected)
        self.assertEqual([opened.subject(sid)["mRS"] for sid in opened.subject_ids], ["0", "1", "2", "3"])

        # The same edit made while the study is open is merged by the next save, which writes
        # typographic quotes (official 3D Slicer misreads straight ones) and holds what it wrote
        created.set("sub-01", rater="AB")
        rewrite(folder, notes)
        created.save()
        self.assertEqual([created.subject(sid)["mRS"] for sid in created.subject_ids], ["0", "1", "2", "3"])
        self.assertEqual(created.subject("sub-01")["rater"], "AB")
        self.assertEqual(created.subject("sub-04")["notes"], "\u201clarge\u201d infarct")
        expected = ["left MCA second line", "tab here", "size 2\u201d lesion", "\u201clarge\u201d infarct"]

        # What Studio writes stays one row per line and reads back the same in Studio and in a CSV reader
        reopened = Study.open(folder)
        self.assertEqual(reopened.subjects, created.subjects)
        with open(os.path.join(folder, study.PARTICIPANTS_TSV), encoding="utf-8", newline="") as f:
            cells = list(csv.reader(f, delimiter="\t"))
        self.assertEqual(len(cells), 5)
        notes_column = cells[0].index("notes")
        self.assertEqual([row[notes_column] for row in cells[1:]], expected)
        self.assertEqual([reopened.subject(sid)["notes"] for sid in reopened.subject_ids], expected)

        # A file written before quoting, with a value that starts with a lone quote, still opens as it did
        with open(os.path.join(folder, study.PARTICIPANTS_TSV), "w", encoding="utf-8") as f:
            f.write('participant_id\tnotes\tgroup\nsub-01\t"unclosed note\tcase\nsub-02\tsize 2"\tcontrol\nsub-03\t"a" b\tn/a\n')
        old = Study.open(folder)
        self.assertEqual([(row["participant_id"], row["notes"], row["group"]) for row in old.subjects],
                         [("sub-01", '"unclosed note', "case"), ("sub-02", 'size 2"', "control"), ("sub-03", '"a" b', "n/a")])

    def test_quotes_typed_in_cells_read_alike_in_official_slicer(self):
        """Straight double quotes in cells are written as typographic quotes in participants.tsv and
        supplementary_lesions.tsv, so that official 3D Slicer (VTK's delimited text reader, which takes every
        straight quote as a string delimiter and knows no escaped quote) reads every cell as Studio does.
        """
        try:
            import mrml
        except ImportError:
            mrml = None
        from LesionNetworkMappingLib import export

        typed = {"sub-01": 'Müller "et al." 2020', "sub-02": 'a"quote', "sub-03": 'lesion 5" wide', "sub-04": '"Pure alexia" (Smith, "2019")'}
        shown = {"sub-01": "Müller \u201cet al.\u201d 2020", "sub-02": "a\u201dquote", "sub-03": "lesion 5\u201d wide",
                 "sub-04": "\u201cPure alexia\u201d (Smith, \u201c2019\u201d)"}
        self.assertEqual(study.typographic_quotes("plain text"), "plain text")
        folder = self.path("quotes")
        created = Study.create(folder)
        for sid, text in typed.items():
            created.add_subject(sid, citation=text, notes=text, rater="AB")
        self.assertEqual({sid: created.subject(sid)["citation"] for sid in typed}, shown)  # held as written
        created.save()
        tables = [os.path.join(folder, study.PARTICIPANTS_TSV), export.supplementary_lesions_tsv(created, self.path("supplementary_lesions.tsv"))]
        for path in tables:
            with open(path, encoding="utf-8") as f:
                self.assertNotIn('"', f.read(), path)
            columns, rows = study.read_tsv(path)
            self.assertEqual({row["participant_id"]: row["citation"] for row in rows}, shown, path)
            if mrml is None:
                continue
            # What official 3D Slicer's table reader makes of the file: the same columns and cells
            scene = mrml.vtkMRMLScene()
            storage = mrml.vtkMRMLTableStorageNode()
            table = mrml.vtkMRMLTableNode()
            scene.AddNode(storage)
            scene.AddNode(table)
            storage.SetFileName(path)
            self.assertTrue(storage.ReadData(table), path)
            vtable = table.GetTable()
            read = [vtable.GetColumn(index).GetName() for index in range(vtable.GetNumberOfColumns())]
            self.assertEqual(read, columns, path)
            for number, row in enumerate(rows):
                self.assertEqual([vtable.GetValue(number, index).ToString() for index in range(len(columns))],
                                 [row[column] for column in columns], path)
        self.assertEqual({row["participant_id"]: row["notes"] for row in Study.open(folder).subjects}, shown)

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
        # Other spellings of the fixed values keep their meaning: include 1.0 / 0.0 (pandas) or yes, status in another case
        with open(self.path("pandas.tsv"), "w", encoding="utf-8") as f:
            f.write("participant_id\tinclude\tstatus\nsub-01\t0.0\tTraced\nsub-02\tyes\tn/a\n")
        created.import_tsv(self.path("pandas.tsv"))
        self.assertEqual([(row["include"], row["status"]) for row in created.subjects[:2]], [("0", "traced"), ("1", "untraced")])
        created.set("sub-01", include=1, status="untraced")
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

    def test_ids_that_differ_only_in_case_are_refused(self):
        # Subject ids name the lesion files (lesions/<id>_space-..._desc-lesion_mask.nii.gz): on a case-insensitive
        # disk (the macOS default) sub-P01 and sub-p01 would be one file, and one subject's lesion would replace the other's
        created = Study.create(self.path("case"), space="MNI152NLin2009cAsym")
        created.add_subject("P01", nihss="4")
        with self.assertRaisesRegex(ValueError, "sub-p01 differs from sub-P01 only in letter case"):
            created.add_subject("p01", nihss="17")
        self.assertEqual(created.subject_ids, ["sub-P01"])
        self.assertEqual(created.case_twin("sub-p01"), "sub-P01")
        self.assertIsNone(created.case_twin("sub-P01"))
        self.assertTrue(created.id_taken("sub-p01"))
        self.assertFalse(created.id_taken("sub-p02"))
        # A table: twins inside it, or a twin of a subject of the study, are refused before anything changes
        with open(self.path("twins.tsv"), "w", encoding="utf-8") as f:
            f.write("participant_id\tnihss\nsub-A02\t3\nsub-a02\t5\n")
        with self.assertRaisesRegex(ValueError, "sub-a02 differs from sub-A02 only in letter case"):
            created.import_tsv(self.path("twins.tsv"))
        with open(self.path("twin_of_study.tsv"), "w", encoding="utf-8") as f:
            f.write("participant_id\tnihss\nsub-P01\t5\nsub-p01\t17\n")
        with self.assertRaisesRegex(ValueError, "sub-p01 differs from sub-P01 only in letter case"):
            created.import_tsv(self.path("twin_of_study.tsv"))
        self.assertEqual(created.subject_ids, ["sub-P01"])
        self.assertEqual(created.subject("sub-P01")["nihss"], "4")
        # Files: a file of a case twin of a subject is that subject's file (one name on macOS): no second row
        lesion = self._image("drop/sub-p01_lesion.nii.gz", affine=MNI2009C_2MM, value=1)
        report = created.add_files([lesion])
        self.assertEqual(report["added"], [])
        self.assertEqual(report["updated"], ["sub-P01"])
        self.assertEqual(report["skipped"], [])
        self.assertEqual(created.subject_ids, ["sub-P01"])
        self.assertEqual(created.subject("sub-P01")["lesion"], created.relpath(lesion))
        # Removing a subject frees its id in every case
        created.remove_subject("sub-P01")
        self.assertIsNone(created.case_twin("sub-p01"))
        created.add_subject("p01")
        self.assertEqual(created.subject_ids, ["sub-p01"])
        self.assertEqual(len({path.casefold() for path in (created.lesion_relpath(sid) for sid in created.subject_ids)}), len(created))

    def test_files_go_to_the_row_whose_id_differs_only_in_case(self):
        # Adopt in place: participants.tsv says "Patient01" or "PATIENT03", the files say "patient01": one participant
        created = Study.create(self.path("caseids"), space="MNI152NLin2009cAsym")
        with open(self.path("caseids.tsv"), "w", encoding="utf-8") as f:
            f.write("participant_id\tnihss\nPatient01\t4\npatient02\t5\nPATIENT03\t6\n")
        created.import_tsv(self.path("caseids.tsv"))
        self.assertEqual(created.subject_ids, ["sub-Patient01", "sub-patient02", "sub-PATIENT03"])
        for n in (1, 2, 3):
            self._image(f"caseids_in/patient0{n}_T1w.nii.gz", affine=MNI2009C_2MM)
        for n in (1, 3):
            self._image(f"caseids_in/patient0{n}_lesion.nii.gz", affine=MNI2009C_2MM, value=1)
        report = created.add_files(self.path("caseids_in"))
        self.assertEqual(report["skipped"], [])
        self.assertEqual(report["added"], [])
        self.assertEqual(sorted(report["updated"]), ["sub-PATIENT03", "sub-Patient01", "sub-patient02"])
        self.assertEqual(created.subject_ids, ["sub-Patient01", "sub-patient02", "sub-PATIENT03"])
        for sid, n, traced in (("sub-Patient01", 1, True), ("sub-patient02", 2, False), ("sub-PATIENT03", 3, True)):
            row = created.subject(sid)
            self.assertEqual(row["scan"], created.relpath(self.path(f"caseids_in/patient0{n}_T1w.nii.gz")))
            self.assertEqual(row["lesion"], created.relpath(self.path(f"caseids_in/patient0{n}_lesion.nii.gz")) if traced else "n/a")
            self.assertEqual(row["status"], "traced" if traced else "untraced")
        self.assertEqual(sorted(report["pairs"]), ["sub-PATIENT03", "sub-Patient01", "sub-patient02"])
        # New subjects: files spelling one id in different cases make one subject, not a skipped twin
        scan = self._image("caseids_new/Patient09_T1w.nii.gz", affine=MNI2009C_2MM)
        lesion = self._image("caseids_new/patient09_lesion.nii.gz", affine=MNI2009C_2MM, value=1)
        report = created.add_files(self.path("caseids_new"))
        self.assertEqual(report["skipped"], [])
        self.assertEqual(len(report["added"]), 1)
        row = created.subject(report["added"][0])
        self.assertEqual(report["added"][0].casefold(), "sub-patient09")
        self.assertEqual((row["scan"], row["lesion"], row["status"]), (created.relpath(scan), created.relpath(lesion), "traced"))

    def test_subject_folders_name_their_files(self):
        # patients/patient01/T1.nii.gz + lesion.nii.gz (BB-1): names made only of modality or lesion words take the
        # subject id from their folder below the chosen folder (it was "0 subjects, 4 files skipped: no subject id")
        for subject, names in (("patient01", ("T1", "lesion")), ("patient02", ("T1", "lesion")), ("P003", ("FLAIR", "mask"))):
            for name in names:
                self._image(f"sub-cohort/patients/{subject}/anat/{name}.nii.gz" if subject == "P003" else f"sub-cohort/patients/{subject}/{name}.nii.gz", value=int(name in ("lesion", "mask")))
        patients = self.path("sub-cohort", "patients")
        created = Study.create(self.path("study_folders"))
        report = created.add_files(patients)
        self.assertEqual(report["skipped"], [])
        self.assertEqual(sorted(report["added"]), ["sub-P003", "sub-patient01", "sub-patient02"])
        for sid, subject, folder, scan, lesion in (("sub-patient01", "patient01", "", "T1", "lesion"), ("sub-patient02", "patient02", "", "T1", "lesion"), ("sub-P003", "P003", "anat", "FLAIR", "mask")):
            row = created.subject(sid)
            self.assertEqual(report["pairs"][sid], {"scan": os.path.join(patients, subject, folder, f"{scan}.nii.gz") if folder else os.path.join(patients, subject, f"{scan}.nii.gz"),
                                                    "lesion": os.path.join(patients, subject, folder, f"{lesion}.nii.gz") if folder else os.path.join(patients, subject, f"{lesion}.nii.gz")})
            self.assertEqual((row["status"], row["source_id"]), ("traced", subject))
        # The same files given one by one (createStudy) with the folder they were chosen in: the same subjects; folders
        # above the chosen folder are never read ("sub-cohort" made one subject of all of them)
        files = sorted(os.path.join(folder, name) for folder, _, names in os.walk(patients) for name in names)
        in_place = Study.create(patients)
        report = in_place.add_files(files, root=patients)
        self.assertEqual((report["skipped"], sorted(report["added"])), ([], ["sub-P003", "sub-patient01", "sub-patient02"]))
        # The chosen folder itself names no subject: files without an id directly in it are skipped, as before
        self._image("flat/T1.nii.gz")
        report = Study.create(self.path("study_flat")).add_files(self.path("flat"))
        self.assertEqual((report["added"], [item["reason"] for item in report["skipped"]]), ([], ["no subject id"]))
        # A BIDS subject folder chosen itself still names its files
        self._image("bids_one/sub-09/anat/T1w.nii.gz")
        self.assertEqual(Study.create(self.path("study_one")).add_files(self.path("bids_one", "sub-09"))["added"], ["sub-09"])

    def test_modality_folder_chosen_takes_id_from_the_subject_folder_above(self):
        # Import > add the folder sub-01/anat (or sub-01/ses-01/anat) itself, with no id in the file names: the chosen
        # folder is a modality or session folder, so the subject folder above it names the files, as it did before the
        # chosen folder bounded the search (BB-1 recheck). A chosen folder that is not one ("patients") still bounds it.
        self._image("bids_in/sub-01/anat/T1w.nii.gz")
        self._image("bids_in/sub-01/anat/lesion.nii.gz", value=1)
        self._image("bids_in/sub-02/ses-01/anat/T1w.nii.gz")
        self._image("bids_in/sub-02/ses-01/anat/lesion.nii.gz", value=1)
        created = Study.create(self.path("study_in"))
        report = created.add_files(self.path("bids_in", "sub-01", "anat"))
        self.assertEqual((report["skipped"], report["added"]), ([], ["sub-01"]))
        self.assertEqual(created.subject("sub-01")["status"], "traced")
        report = created.add_files(self.path("bids_in", "sub-02", "ses-01", "anat"))
        self.assertEqual((report["skipped"], report["added"]), ([], ["sub-02"]))
        self.assertEqual(created.subject("sub-02")["status"], "traced")
        # createStudy on such a folder: the files one by one, the folder as root
        report = Study.create(self.path("study_in2")).add_files(
            [self.path("bids_in", "sub-01", "anat", f"{name}.nii.gz") for name in ("T1w", "lesion")], root=self.path("bids_in", "sub-01", "anat"))
        self.assertEqual((report["skipped"], report["added"]), ([], ["sub-01"]))
        # The chosen folder names no subject but is not a modality folder: folders above it are not read
        self._image("sub-outer/patients/T1.nii.gz")
        self._image("sub-outer/patients/lesion.nii.gz", value=1)
        report = Study.create(self.path("study_in3")).add_files(self.path("sub-outer", "patients"))
        self.assertEqual((report["added"], [item["reason"] for item in report["skipped"]]), ([], ["no subject id"] * 2))


if __name__ == "__main__":
    unittest.main()

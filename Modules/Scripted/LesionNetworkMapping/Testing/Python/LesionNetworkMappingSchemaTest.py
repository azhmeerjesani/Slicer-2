"""Unit tests of study schema v2 (LesionNetworkMappingLib.schema and the schema v2 parts of study).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingSchemaTest.py -v
"""

import json
import os
import shutil
import socket
import statistics
import sys
import tempfile
import time
import unittest
from unittest import mock

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import nifti, schema, study
from LesionNetworkMappingLib.study import Study

MNI6_2MM = np.array([[-2.0, 0, 0, 90], [0, 2, 0, -126], [0, 0, 2, -72], [0, 0, 0, 1]])
NATIVE = np.array([[-0.9, 0, 0, 80.0], [0, 0.9, 0, -100.0], [0, 0, 1.2, -60.0], [0, 0, 0, 1]])


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-schema-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def _image(self, relative, shape=(4, 5, 6), affine=NATIVE, value=0):
        path = self.path(*relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        nifti.write_nifti(path, np.full(shape, value, np.uint8), affine)
        return path

    def _read(self, *parts):
        with open(self.path(*parts), encoding="utf-8") as f:
            return f.read()

    def _cohort(self, name="cohort", n=6):
        created = Study.create(self.path(name), space="MNI152NLin2009cAsym")
        for index in range(n):
            created.add_subject(f"sub-{index:02d}", group="case" if index % 2 else "control", volume_ml=10 + index)
        return created

    # Registry and roles

    def test_registry_and_role_inference(self):
        self.assertEqual(set(schema.STANDARD_ROLES), set(study.STANDARD_COLUMNS))
        self.assertEqual(study.STANDARD_COLUMNS[:3], ("participant_id", "group", "include"))  # unchanged
        roles = {name: spec.role for name, spec in schema.STANDARD_ROLES.items()}
        self.assertEqual((roles["participant_id"], roles["group"], roles["scan"], roles["lesion"], roles["volume_ml"], roles["space"]), ("id", "group", "path", "path", "covariate", "metadata"))
        self.assertEqual({roles[name] for name in ("include", "status", "qc", "n_parts", "gaps", "outside_brain_pct", "rater", "trace_seconds", "modified")}, {"bookkeeping"})
        self.assertEqual(schema.STANDARD_ROLES["volume_ml"].units, "mL")
        self.assertEqual(list(schema.STANDARD_ROLES["group"].levels), ["case", "control", "n/a"])  # group stays case/control/n/a
        for name in ("source_type", "fig_convention", "dimensionality", "review_status", "norm_qc"):
            self.assertTrue(schema.OPTIONAL_COLUMNS[name].strict, name)
        self.assertEqual({spec.section for spec in schema.OPTIONAL_COLUMNS.values()}, set(schema.SECTIONS) - {"case"} | {"case"})
        self.assertEqual((schema.OPTIONAL_COLUMNS["dataset"].role, schema.OPTIONAL_COLUMNS["age"].role, schema.OPTIONAL_COLUMNS["fit_rms_mm"].role), ("block", "covariate", "bookkeeping"))
        for name in ("scan_native", "lesion_native", "norm_suggestion", "norm_fingerprint", "volume_native_ml", "lesion_mtime", "lesion_size"):
            self.assertIn(name, schema.OPTIONAL_COLUMNS)

        # Inference: numeric when every value is a finite number, categorical up to 20 levels, text otherwise
        self.assertEqual(schema.infer_spec("moca", ["21", "n/a", "3.5", ""]).type, "numeric")
        self.assertEqual(schema.infer_spec("moca", ["21", "inf"]).type, "categorical")
        self.assertEqual(schema.infer_spec("site", ["A", "B", "A"]).levels, {"A": None, "B": None})
        self.assertEqual(schema.infer_spec("comment", [f"text {i}" for i in range(21)]).type, "text")
        self.assertEqual(schema.infer_spec("empty", ["n/a"]).type, "text")
        self.assertEqual(schema.infer_spec("moca", ["1"]).role, schema.UNKNOWN_ROLE)

        # Registration of later columns; a conflicting re-registration raises
        spec = schema.ColumnSpec("l01_test_metric", "numeric", "bookkeeping", section="analysis", units="mm")
        self.assertIs(schema.register_column(spec), spec)
        schema.register_column(schema.ColumnSpec("l01_test_metric", "numeric", "bookkeeping", section="analysis", units="mm"))  # same spec: fine
        with self.assertRaises(ValueError):
            schema.register_column(spec.replace(role="covariate"))
        with self.assertRaises(ValueError):
            schema.register_column(schema.ColumnSpec("age", "numeric", "score"))
        with self.assertRaisesRegex(ValueError, "standard column"):
            schema.register_column(schema.registered("volume_ml"))
        self.assertEqual(hash(schema.registered("sex")), hash(schema.registered("sex").replace()))  # specs with dict fields are hashable
        with self.assertRaises(ValueError):
            schema.ColumnSpec("x", role="outcome")
        del schema.OPTIONAL_COLUMNS["l01_test_metric"]

    def test_normalize_values(self):
        self.assertEqual(schema.normalize_value("sex", "female"), ("F", None))
        self.assertEqual(schema.normalize_value("sex", "Male"), ("M", None))
        self.assertEqual(schema.normalize_value("flipped", "1.0"), ("1", None))
        self.assertEqual(schema.normalize_value("image_modality", "t1w"), ("T1", None))
        text, warning = schema.normalize_value("sex", "unknown")
        self.assertEqual(text, "unknown")
        self.assertIn("Unknown sex value", warning)
        self.assertIn("not a number", schema.normalize_value("age", "sixty")[1])
        self.assertEqual(schema.normalize_value("review_status", "Approved"), ("approved", None))
        with self.assertRaisesRegex(ValueError, "Invalid review_status"):
            schema.normalize_value("review_status", "done")
        self.assertEqual(schema.normalize_value("review_status", "n/a"), ("n/a", None))
        self.assertEqual(schema.normalize_value("trace_method", "manual+smartfill+auto:nnunet"), ("manual+smartfill+auto:nnunet", None))
        self.assertIn("'magic'", schema.normalize_value("trace_method", "manual+magic")[1])
        self.assertEqual(schema.normalize_value("moca", "anything"), ("anything", None))  # not registered

        created = self._cohort()
        row = created.set("sub-00", sex="female", age=61)
        self.assertEqual((row["sex"], row["age"]), ("F", "61"))
        created.set("sub-01", sex="x")
        self.assertEqual(created.subject("sub-01")["sex"], "x")
        self.assertEqual(created.validation_warnings[-1][:3], ("sub-01", "sex", "x"))
        with self.assertRaises(ValueError):
            created.set("sub-00", {"review_status": "done", "age": 70})
        self.assertEqual(created.subject("sub-00")["age"], "61")  # nothing changed by a rejected update
        with self.assertRaises(ValueError):
            created.set("sub-00", source_type="paper")
        # import_tsv never fails on a lenient value, but does on a strict one
        with open(self.path("clinical.tsv"), "w", encoding="utf-8") as f:
            f.write("participant_id\tsex\thandedness\tage\nsub-02\twoman\tambi\t?\n")
        created.import_tsv(self.path("clinical.tsv"))
        self.assertEqual({key: created.subject("sub-02")[key] for key in ("sex", "handedness", "age")}, {"sex": "F", "handedness": "ambi", "age": "?"})
        with open(self.path("bad.tsv"), "w", encoding="utf-8") as f:
            f.write("participant_id\tnorm_qc\nsub-02\tok\n")
        with self.assertRaises(ValueError):
            created.import_tsv(self.path("bad.tsv"))

    def test_score_columns_leak_fix(self):
        created = self._cohort()
        for index, sid in enumerate(created.subject_ids):
            created.set(sid, moca=20 + index, fit_rms_mm=0.5, n_slices=3, age=50 + index, extrude_mm=2, assist_pct=0)
        created.set("sub-00", site="A")
        # numeric_columns() keeps its old answer (compatibility); score_columns() does not leak
        self.assertEqual(created.numeric_columns(), ["moca", "fit_rms_mm", "n_slices", "age", "extrude_mm", "assist_pct"])
        self.assertEqual(created.score_columns(), [("moca", False)])
        self.assertEqual(created.covariate_columns(), ["volume_ml", "age"])
        self.assertEqual(created.group_columns(), ["group"])
        self.assertEqual(created.block_columns(), [])
        spec = created.set_column_role("moca", "score", description="Montreal Cognitive Assessment", units="points")
        self.assertEqual((spec.role, spec.type), ("score", "numeric"))
        self.assertEqual(created.score_columns(), [("moca", True)])
        created.set("sub-01", dataset="cohortB")
        self.assertEqual(created.block_columns(), ["dataset"])
        created.set_column_role("site", "group")
        self.assertEqual(created.group_columns(), ["group", "site"])
        self.assertEqual(created.column_spec("site").type, "categorical")
        created.set_column_role("moca", "covariate")
        self.assertEqual(created.score_columns(), [])
        with self.assertRaises(ValueError):
            created.set_column_role("include", "score")
        with self.assertRaises(ValueError):
            created.set_column_role("moca", "outcome")
        with self.assertRaises(KeyError):
            created.set_column_role("nihss", "score")
        created.set_column_role("moca", "score")
        created.save()
        again = Study.open(created.path)
        self.assertEqual(again.score_columns(), [("moca", True)])
        self.assertEqual(again.column_spec("moca").units, "points")

    def test_participants_json_round_trip(self):
        created = self._cohort()
        created.set("sub-00", moca=12, age=40, sex="M")
        created.save()
        dictionary = json.loads(self._read("cohort", "participants.json"))
        self.assertEqual(list(dictionary), created.columns)
        self.assertEqual(dictionary["group"]["SlicerStudio"], {"type": "categorical", "role": "group", "confirmed": True, "by": "registry"})
        self.assertEqual(dictionary["group"]["Levels"]["case"], "Lesion associated with the symptom")
        self.assertEqual((dictionary["volume_ml"]["Units"], dictionary["age"]["Units"]), ("mL", "years"))
        self.assertEqual(dictionary["moca"], {"SlicerStudio": {"type": "numeric", "role": "unknown", "confirmed": False}})
        # Foreign keys (a BIDS tool's, unknown columns, another tool's SlicerStudio keys) are kept
        dictionary["moca"] = {"Description": "MoCA total", "TermURL": "https://example.org/moca", "SlicerStudio": {"type": "numeric", "role": "score", "confirmed": True, "note": "kept"}}
        dictionary["age"]["LongName"] = "Age at imaging"
        dictionary["removed_column"] = {"Description": "no longer in the table"}
        dictionary["group"]["Description"] = "Lesion group"
        dictionary["age"]["Units"] = "y"
        with open(self.path("cohort", "participants.json"), "w", encoding="utf-8") as f:
            json.dump(dictionary, f)
        opened = Study.open(created.path)
        self.assertEqual(opened.score_columns(), [("moca", True)])
        self.assertEqual(opened.column_spec("moca").description, "MoCA total")
        self.assertEqual((opened.column_spec("age").units, opened.column_spec("age").role), ("y", "covariate"))  # BIDS edits apply
        opened.set("sub-01", nihss=4)
        opened.save()
        again = json.loads(self._read("cohort", "participants.json"))
        self.assertEqual(again["moca"], dictionary["moca"])
        self.assertEqual(again["age"]["LongName"], "Age at imaging")
        self.assertEqual(again["removed_column"], {"Description": "no longer in the table"})
        self.assertEqual(again["group"]["Description"], "Lesion group")  # never overwritten
        self.assertEqual(again["nihss"]["SlicerStudio"]["confirmed"], False)
        self.assertEqual(list(again)[-1], "removed_column")
        # read/write helpers: atomic, unknown keys kept
        path = self.path("dictionary.json")
        schema.write_dictionary(path, {"a": {"Levels": {"1": "one"}, "X": [1]}})
        self.assertEqual(schema.read_dictionary(path), {"a": {"Levels": {"1": "one"}, "X": [1]}})
        self.assertEqual(schema.read_dictionary(self.path("missing.json")), {})
        self.assertEqual([name for name in os.listdir(self.tmp) if name.endswith(".tmp")], [])
        # An unreadable participants.json is never overwritten
        with open(self.path("cohort", "participants.json"), "w", encoding="utf-8") as f:
            f.write("[broken")
        with self.assertLogs(level="WARNING"):
            broken = Study.open(created.path)
        broken.save()
        self.assertEqual(self._read("cohort", "participants.json"), "[broken")

    def test_old_studies_and_settings(self):
        folder = self.path("old")
        os.makedirs(folder)
        metadata = {"format": "slicer-studio-study", "version": 1, "name": "old", "space": "MNI152NLin6Asym", "template": {"id": "MNI152NLin6Asym", "res": 2}}
        with open(os.path.join(folder, "study.json"), "w", encoding="utf-8") as f:
            json.dump(metadata, f)
        tsv = "participant_id\tgroup\tinclude\tstatus\tqc\tmoca\nsub-01\tcase\t1\ttraced\tpass\t20\n"
        with open(os.path.join(folder, "participants.tsv"), "w", encoding="utf-8") as f:
            f.write(tsv)
        opened = Study.open(folder)
        self.assertEqual(opened.columns, ["participant_id", "group", "include", "status", "qc", "moca", *study.STANDARD_COLUMNS[5:]])
        self.assertEqual(opened.trace_policy, {"space": "MNI152NLin6Asym", "res": 2, "convention_default": "radiological", "extrude_2d": None})
        self.assertEqual((opened.review_required, opened.blind_tracing, opened.subsets, opened.grid_answers), (False, False, {}, {}))
        opened.save()
        again = Study.open(folder)
        self.assertEqual(again.columns, opened.columns)  # no optional column was added
        self.assertEqual(again.metadata, metadata)  # no study.json key was added
        # Settings are written when set; review_required defaults to True with a case report
        again.set("sub-01", source_type="case_report")
        self.assertTrue(again.review_required)
        again.review_required = False
        again.trace_policy = {"space": "MNI152NLin6Asym", "res": 1, "extrude_2d": {"mode": "fractional", "total_mm": 2, "semantics": "exact"}}
        again.subsets = {"pilot": ["sub-01"]}
        again.save()
        reopened = Study.open(folder)
        self.assertFalse(reopened.review_required)
        self.assertEqual(reopened.trace_policy["extrude_2d"]["total_mm"], 2)
        self.assertEqual(reopened.trace_policy["convention_default"], "radiological")
        self.assertEqual(reopened.subsets, {"pilot": ["sub-01"]})
        reopened.review_required = None
        self.assertTrue(reopened.review_required)

    def test_analysis_rows(self):
        created = self._cohort(n=8)
        values = {
            "sub-00": {"moca": 20, "review_status": "approved"},
            "sub-01": {"moca": 21, "review_status": "approved", "include": 0},
            "sub-02": {"moca": None, "review_status": "approved"},
            "sub-03": {"moca": 23, "review_status": "pending"},
            "sub-04": {"moca": 24, "review_status": "approved", "norm_method": "brainsfit-cfm:standard", "norm_qc": "unreviewed"},
            "sub-05": {"moca": 25, "review_status": "approved", "norm_method": "brainsfit-cfm:standard", "norm_qc": "pass"},
            "sub-06": {"moca": 26, "review_status": "approved", "source_type": "case_report"},
            "sub-07": {"moca": "n.a.", "review_status": "approved"},
        }
        for sid, cells in values.items():
            created.set(sid, cells)
        created.set_column_role("moca", "score")
        self.assertTrue(created.review_required)  # a case report
        rows, excluded = created.analysis_rows(["moca", "volume_ml"])
        self.assertEqual([row["participant_id"] for row in rows], ["sub-00", "sub-05", "sub-06"])
        self.assertEqual(excluded, {"include=0": ["sub-01"], "missing moca": ["sub-02"], "review_status != approved": ["sub-03"], "norm_qc != pass": ["sub-04"], "not a number: moca": ["sub-07"]})
        rows, excluded = created.analysis_rows("moca", require_review=False, require_norm_qc=False, include_only=False)
        self.assertEqual(len(rows), 6)
        self.assertEqual(excluded, {"missing moca": ["sub-02"], "not a number: moca": ["sub-07"]})
        created.review_required = False
        self.assertNotIn(study.EXCLUDED_REVIEW, created.analysis_rows()[1])
        rows[0]["moca"] = "changed"
        self.assertEqual(created.subject(rows[0]["participant_id"])["moca"], "20")  # copies

    # Merge-save

    def test_concurrent_saves_merge(self):
        created = self._cohort()
        created.save()
        first, second = Study.open(created.path), Study.open(created.path)
        first.set("sub-00", moca=11, notes="first")
        first.add_subject("sub-90", group="case")
        first.remove_subject("sub-05")
        second.set("sub-01", moca=22, qc="pass")
        second.add_subject("sub-91", group="control")
        first.save()
        time.sleep(0.01)
        second.save()
        merged = Study.open(created.path)
        self.assertEqual((merged.subject("sub-00")["moca"], merged.subject("sub-00")["notes"]), ("11", "first"))
        self.assertEqual((merged.subject("sub-01")["moca"], merged.subject("sub-01")["qc"]), ("22", "pass"))
        self.assertEqual(merged.subject_ids, ["sub-00", "sub-01", "sub-02", "sub-03", "sub-04", "sub-05", "sub-91", "sub-90"])  # removed on disk only: kept
        self.assertEqual(second.subjects, merged.subjects)  # the saving study holds the merged table
        log = self._read("cohort", ".studio", "merge.log").splitlines()
        self.assertEqual(len(log), 1)  # the row removed on disk only is kept and logged; no cell conflict
        self.assertIn("participants.tsv sub-05: row removed on disk was kept", log[0])
        self.assertFalse(os.path.exists(self.path("cohort", ".studio", "participants.lock")))
        # A removal in memory removes the row; study.json keys merge too
        second.remove_subject("sub-90")
        merged.metadata["custom"] = "from merged"
        merged.save()
        second.metadata["other"] = 1
        second.save()
        final = Study.open(created.path)
        self.assertNotIn("sub-90", final)
        self.assertEqual((final.metadata["custom"], final.metadata["other"]), ("from merged", 1))
        # save(merge=False) overwrites (the behaviour before schema v2)
        stale = Study.open(created.path)
        final.set("sub-02", moca=5)
        final.save()
        stale.save(merge=False)
        self.assertEqual(Study.open(created.path).subject("sub-02")["moca"], "n/a")

    def test_same_cell_memory_wins(self):
        created = self._cohort()
        created.save()
        first, second = Study.open(created.path), Study.open(created.path)
        first.set("sub-02", moca=1, qc="fail")
        second.set("sub-02", moca=2, qc="fail")
        first.save()
        time.sleep(0.01)
        second.save()
        self.assertEqual(Study.open(created.path).subject("sub-02")["moca"], "2")
        log = self._read("cohort", ".studio", "merge.log").splitlines()
        self.assertEqual(len(log), 1)  # qc was changed to the same value on both sides: no conflict
        self.assertIn("participants.tsv sub-02 moca: kept '2' over '1' on disk (was 'n/a')", log[0])

    def test_merge_detects_changes_by_content(self):
        # Coarse timestamps (FAT 2 s, HFS+ 1 s, older ext4): another writer's same-size save lands in
        # the same tick as this study's baseline, so size and mtime are unchanged
        created = self._cohort()
        for sid in created.subject_ids:
            created.set(sid, moca=0)
        created.save()
        tsv = self.path("cohort", "participants.tsv")
        first, second = Study.open(created.path), Study.open(created.path)
        before = os.stat(tsv)
        second.set("sub-01", moca=2)
        second.save()
        self.assertEqual(os.stat(tsv).st_size, before.st_size)
        os.utime(tsv, ns=(before.st_atime_ns, before.st_mtime_ns))
        first.set("sub-03", moca=4)
        first.save()
        merged = Study.open(created.path)
        self.assertEqual((merged.subject("sub-01")["moca"], merged.subject("sub-03")["moca"]), ("2", "4"))
        # A lockless writer (save(merge=False), a spreadsheet) replacing the table while this study
        # finishes its save is merged by the next save: the baseline is the text written, not the file
        writer = Study.open(created.path)
        real_write = study._write_text_atomic
        intruded = []

        def write(path, text):
            if path.endswith("study.json") and not intruded:
                intruded.append(path)
                other = Study.open(created.path)
                other.set("sub-04", moca=7)
                other.save(merge=False)
            real_write(path, text)

        writer.set("sub-02", moca=1)
        with mock.patch.object(study, "_write_text_atomic", write):
            writer.save()
        self.assertEqual(intruded, [os.path.join(created.path, "study.json")])
        writer.set("sub-05", moca=5)
        writer.save()
        final = Study.open(created.path)
        self.assertEqual([final.subject(sid)["moca"] for sid in ("sub-01", "sub-02", "sub-03", "sub-04", "sub-05")], ["2", "1", "4", "7", "5"])

    def test_stale_lock_is_stolen(self):
        created = self._cohort()
        lock_path = self.path("cohort", ".studio", "participants.lock")
        with open(lock_path, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(), "token": "old", "started": "2026-01-01T00:00:00Z"}, f)
        old = time.time() - study.LOCK_STALE_S - 5
        os.utime(lock_path, (old, old))
        created.set("sub-00", moca=3)
        with self.assertLogs(level="WARNING") as logs:
            created.save()
        self.assertIn("stole a stale participants.lock", logs.output[0])
        self.assertIn("stole a stale participants.lock", self._read("cohort", ".studio", "merge.log"))
        self.assertFalse(os.path.exists(lock_path))
        self.assertEqual(Study.open(created.path).subject("sub-00")["moca"], "3")
        # A live lock makes save wait, then fail with jobs.Locked
        from LesionNetworkMappingLib import jobs

        with open(lock_path, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(), "token": "live", "started": "now"}, f)
        with self.assertRaises(jobs.Locked):
            created.save(lock_timeout=0.2)
        os.remove(lock_path)

    def test_inbox(self):
        created = self._cohort()
        created.save()
        values = {"norm_method": "brainsfit-cfm:standard", "norm_qc": "unreviewed", "norm_suggestion": "looks-ok", "norm_fingerprint": "0123456789ab", "volume_ml": 12.5, "volume_native_ml": 13.25}
        audit = {"event": "normalize", "sid": "sub-01", "fingerprint": "0123456789ab", "by": "normalize", "at": "2026-10-19T00:00:00+00:00"}
        tsv_before = self._read("cohort", "participants.tsv")
        first = Study.post_result(created.path, "sub-01", values, source="normalize", audit=audit)
        second = Study.post_result(os.path.join(created.path, "study.json"), "02", {"status": "mapped"}, source="pipeline")
        rejected = Study.post_results(created.path, {"sub-03": {"qc": "pass"}, "sub-77": {"qc": "pass"}}, source="pipe line!")
        self.assertEqual(self._read("cohort", "participants.tsv"), tsv_before)  # jobs never touch the table
        self.assertLess(os.path.basename(first), os.path.basename(second))
        self.assertRegex(os.path.basename(first), rf"^\d{{8}}T\d{{12}}Z-{os.getpid()}-normalize\.json$")
        self.assertTrue(os.path.basename(rejected).endswith("-pipeline.json"))
        with open(first, encoding="utf-8") as f:
            content = json.load(f)
        self.assertEqual((content["format"], content["version"], content["source"], content["rows"]["sub-01"]["volume_ml"]), ("slicer-studio-inbox", 1, "normalize", "12.5"))
        with self.assertRaises(ValueError):
            Study.post_result(created.path, "sub-01", {"norm_qc": "maybe"}, source="normalize")
        with open(os.path.join(os.path.dirname(first), "garbage.json"), "w", encoding="utf-8") as f:
            f.write("{not json")

        session = Study.open(created.path)
        with self.assertLogs(level="WARNING") as logs:
            applied = session.apply_inbox()
        self.assertIn("moved to rejected/", logs.output[0])
        self.assertEqual([(os.path.basename(path), sids) for path, sids in applied], [(os.path.basename(first), ["sub-01"]), (os.path.basename(second), ["sub-02"]), (os.path.basename(rejected), ["sub-03"]), ("garbage.json", [])])
        self.assertEqual([(os.path.basename(path), sid) for path, sid, _ in session.inbox_errors], [(os.path.basename(rejected), "sub-77"), ("garbage.json", None)])
        reopened = Study.open(created.path)
        row = reopened.subject("sub-01")
        self.assertEqual((row["norm_qc"], row["norm_suggestion"], row["volume_ml"], row["volume_native_ml"]), ("unreviewed", "looks-ok", "12.5", "13.25"))
        self.assertEqual((reopened.subject("sub-02")["status"], reopened.subject("sub-03")["qc"]), ("mapped", "pass"))
        inbox = os.path.join(created.path, ".studio", "inbox")
        self.assertEqual(sorted(os.listdir(inbox)), ["rejected"])
        self.assertEqual(sorted(os.listdir(os.path.join(inbox, "rejected"))), sorted([os.path.basename(rejected), "garbage.json"]))
        records = [json.loads(line) for line in self._read("cohort", ".studio", "audit.jsonl").splitlines()]
        self.assertEqual(records[0], audit)
        self.assertEqual((records[1]["event"], records[1]["source"], records[1]["sids"], records[1]["columns"]), ("inbox", "pipeline", ["sub-02"], ["status"]))
        self.assertEqual(records[2]["sids"], ["sub-03"])
        self.assertEqual(len(records), 3)
        _, excluded = reopened.analysis_rows()
        self.assertEqual(excluded, {"norm_qc != pass": ["sub-01"]})
        # Without save, the files wait for the next save
        Study.post_result(created.path, "sub-04", {"qc": "fail"}, source="review")
        later = Study.open(created.path)
        self.assertEqual(len(later.apply_inbox(save=False)), 1)
        self.assertEqual(later.apply_inbox(save=False), [])  # pending files are not applied twice
        self.assertEqual(len(os.listdir(inbox)), 2)
        later.save()
        self.assertEqual(os.listdir(inbox), ["rejected"])
        self.assertEqual(Study.open(created.path).subject("sub-04")["qc"], "fail")
        self.assertEqual(Study.open(self.path("cohort")).apply_inbox(), [])
        # Two sessions applying the same file: only the one that removes it writes the audit record
        Study.post_result(created.path, "sub-05", {"qc": "pass"}, source="review", audit={"event": "review", "sid": "sub-05"})
        one, two = Study.open(created.path), Study.open(created.path)
        self.assertEqual((len(one.apply_inbox(save=False)), len(two.apply_inbox(save=False))), (1, 1))
        one.save()
        two.save()
        records = [json.loads(line) for line in self._read("cohort", ".studio", "audit.jsonl").splitlines()]
        self.assertEqual([record for record in records if record.get("event") == "review"], [{"event": "review", "sid": "sub-05"}])
        # Two keys of one participant are refused
        with self.assertRaisesRegex(ValueError, "same participant"):
            Study.post_results(created.path, {"01": {"qc": "pass"}, "sub-01": {"qc": "fail"}}, source="review")

    # Lesion sidecars and file recognisers

    def test_lesion_sidecar(self):
        created = Study.create(self.path("s"), space="MNI152NLin6Asym")
        created.add_subject("sub-01", scan="../raw/sub-01_T1w.nii.gz", space="native")
        self._image("s/lesions/sub-01_space-native_desc-lesion_mask.nii.gz")
        created.set("sub-01", lesion=created.lesion_relpath("sub-01"))
        path = created.lesion_sidecar_path("sub-01")
        self.assertEqual(path, self.path("s", "lesions", "sub-01_space-native_desc-lesion_mask.json"))
        self.assertIsNone(created.read_lesion_sidecar("sub-01"))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"Source": "../../raw/sub-01_T1w.nii.gz", "GeneratedBy": [{"Name": "Slicer Studio"}], "SlicerStudio": {"rater": "AB"}}, f)
        self.assertEqual(created.write_lesion_sidecar("sub-01", studio={"trace_method": "manual"}), path)
        data = created.read_lesion_sidecar("sub-01")
        self.assertEqual(list(data)[:5], ["Type", "Source", "Sources", "SpatialReference", "Resolution"])
        self.assertEqual((data["Type"], data["Sources"], data["SpatialReference"], data["Resolution"]), ("Lesion", ["../../raw/sub-01_T1w.nii.gz"], "../../raw/sub-01_T1w.nii.gz", "0.9x0.9x1.2 mm"))
        self.assertEqual((data["Source"], data["GeneratedBy"]), ("../../raw/sub-01_T1w.nii.gz", [{"Name": "Slicer Studio"}]))  # unknown keys kept
        self.assertEqual({key: data["SlicerStudio"][key] for key in ("rater", "trace_method", "participant_id", "space")}, {"rater": "AB", "trace_method": "manual", "participant_id": "sub-01", "space": "native"})
        created.write_lesion_sidecar("sub-01", studio={"review_status": "approved"}, resolution="1 mm")
        data = created.read_lesion_sidecar("sub-01")
        self.assertEqual((data["Resolution"], data["SlicerStudio"]["trace_method"], data["SlicerStudio"]["review_status"]), ("1 mm", "manual", "approved"))
        # A template-space lesion without a lesion cell: default path, no SpatialReference
        created.add_subject("sub-02")
        self._image("s/lesions/sub-02_space-MNI152NLin6Asym_desc-lesion_mask.nii.gz", shape=(91, 109, 91), affine=MNI6_2MM)
        created.set("sub-02", lesion=created.lesion_relpath("sub-02"))
        data = json.loads(self._read("s", "lesions", os.path.basename(created.write_lesion_sidecar("sub-02"))))
        self.assertEqual(data, {"Type": "Lesion", "Resolution": "2 mm", "SlicerStudio": data["SlicerStudio"]})
        # A new sidecar of a lesion with a scan gets Draw's singular Source (D-U3) next to Sources
        created.set("sub-02", scan="../raw/sub-02_T1w.nii.gz")
        os.remove(created.lesion_sidecar_path("sub-02"))
        data = json.loads(self._read("s", "lesions", os.path.basename(created.write_lesion_sidecar("sub-02"))))
        self.assertEqual((data["Source"], data["Sources"]), ("../../raw/sub-02_T1w.nii.gz", ["../../raw/sub-02_T1w.nii.gz"]))
        created.set("sub-02", lesion="lesions/sub-02_lesion.nrrd")
        self.assertEqual(created.lesion_sidecar_path("sub-02"), self.path("s", "lesions", "sub-02_lesion.json"))
        # Lesions outside the study (source datasets referenced by add_files) are not modified unless asked
        created.add_subject("sub-03", lesion=self._image("raw/sub-03_lesion.nii.gz"))
        with self.assertRaisesRegex(ValueError, "outside the study"):
            created.write_lesion_sidecar("sub-03")
        self.assertFalse(os.path.exists(self.path("raw", "sub-03_lesion.json")))
        self.assertEqual(created.write_lesion_sidecar("sub-03", allow_outside=True), self.path("raw", "sub-03_lesion.json"))
        # An existing sidecar that is not JSON is never overwritten
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        with self.assertRaises(ValueError):
            created.write_lesion_sidecar("sub-01")
        self.assertEqual(self._read("s", "lesions", os.path.basename(path)), "{broken")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[1, 2]")
        with self.assertRaisesRegex(ValueError, "not a JSON object"):
            created.write_lesion_sidecar("sub-01")
        self.assertEqual(self._read("s", "lesions", os.path.basename(path)), "[1, 2]")

    def test_recognisers_set_source_id(self):
        created = Study.create(self.path("study"))
        lesions = {
            "sub-01": "bids/sub-01/anat/sub-01_space-MNI152NLin2009cAsym_label-lesion_mask.nii.gz",  # BIDS
            "sub-02": "fmriprep/sub-02/anat/sub-02_label-lesion_roi.nii.gz",  # fMRIPrep
            "sub-0007": "dataverse/0007_lesion_MNI.nii",  # Dataverse 8BHHRS
            "sub-r001s001": "atlas/sub-r001s001/ses-1/anat/sub-r001s001_ses-1_space-MNI152NLin2009aSym_label-L_desc-T1lesion_mask.nii.gz",  # ATLAS R2.0
            "sub-patient03": "other/patient_03_lesion.nii.gz",
        }
        for relative in lesions.values():
            self._image(relative, shape=(91, 109, 91), affine=MNI6_2MM)
        self._image("atlas/sub-r001s001/ses-1/anat/sub-r001s001_ses-1_space-MNI152NLin2009aSym_T1w.nii.gz", shape=(91, 109, 91), affine=MNI6_2MM)
        report = created.add_files([self.path(folder) for folder in ("bids", "fmriprep", "dataverse", "atlas", "other")])
        self.assertEqual(report["skipped"], [])
        for sid, relative in lesions.items():
            self.assertEqual(report["pairs"][sid]["lesion"], self.path(*relative.split("/")), sid)
        self.assertEqual(report["pairs"]["sub-r001s001"]["scan"], self.path("atlas/sub-r001s001/ses-1/anat/sub-r001s001_ses-1_space-MNI152NLin2009aSym_T1w.nii.gz"))
        source_ids = {sid: created.subject(sid)["source_id"] for sid in lesions}
        self.assertEqual(source_ids, {"sub-01": "n/a", "sub-02": "n/a", "sub-0007": "0007_lesion_MNI", "sub-r001s001": "n/a", "sub-patient03": "patient_03_lesion"})
        self.assertEqual(created.subject("sub-0007")["space"], "MNI152NLin6Asym")

    def test_merge_save_time_1000_rows(self):
        created = Study.create(self.path("big"), space="MNI152NLin2009cAsym")
        for index in range(1000):
            created.add_subject(f"sub-{index:04d}", group="case", status="traced", volume_ml=index / 7, moca=index % 30, age=50, sex="F", lesion=f"lesions/sub-{index:04d}.nii.gz")
        created.save()
        times = []
        for repeat in range(3):
            first, second = Study.open(created.path), Study.open(created.path)
            first.set("sub-0001", moca=repeat)
            second.set("sub-0002", moca=repeat + 1)
            first.save()
            start = time.perf_counter()
            second.save()  # re-reads the changed table and merges it
            times.append(time.perf_counter() - start)
        self.assertEqual(Study.open(created.path).subject("sub-0001")["moca"], "2")
        self.assertLessEqual(statistics.median(times), 0.150, times)


if __name__ == "__main__":
    unittest.main()
